"""Loan Review integration: existing source inputs -> Fact -> calculation -> narrative -> QA.

No network, new metric IDs, inferred approvals or missing-to-zero. Artifacts can
pass technical validation while substantive acceptance remains explicitly blocked.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import json
import hashlib
import csv
import re
from pathlib import Path

try:
    from .loan_review_contract import (AVAILABLE, METRICS, Fact, Evidence, Narrative, Status,
        Mode, _dates, calculate_fact, calculate_identity, approved_formula_value, validate_report)
    from .loan_review_sources import parse_request, normalize_value, number, day, load_json
except ImportError:
    from loan_review_contract import (AVAILABLE, METRICS, Fact, Evidence, Narrative, Status,
        Mode, _dates, calculate_fact, calculate_identity, approved_formula_value, validate_report)
    from loan_review_sources import parse_request, normalize_value, number, day, load_json

try:
    from .debt_policy import DEBT_FIELDS, DEBT_SUM_INPUTS, VERSION as DEBT_POLICY_VERSION, supported_debt_source, reported_debt_basis
except ImportError:
    from debt_policy import DEBT_FIELDS, DEBT_SUM_INPUTS, VERSION as DEBT_POLICY_VERSION, supported_debt_source, reported_debt_basis

FORMULA_VERSION = 'acceptance-v1.0-addendum-2026-10-23'
CF01_DBT_VERSION = 'cf01-dbt-4dp-half-even-v1'



APPROVED_POLICY_VERSION = 'user-approved-2026-10-24'
STANDARD_EBITDA_BASIS = 'Standard EBITDA: Net Income + Interest + Taxes + D&A'
APPROVED_DEFINITIONS = {
    'DEC-01': 'Unrestricted cash and equivalents + liquid short-term investments + net receivables; no inventory, prepayments, restricted cash or duplicate investments',
    'DEC-02': 'Consolidated total equity including NCI; Total Debt uses as-reported borrowing carrying amounts, disclosed lease scope and disjoint current/noncurrent or short-term/long-term-aggregate lines; no duplicate current portions, trade payables or operating liabilities',
    'DEC-04': 'Opening/closing average; verified complete Months; allowance deducted once; no LTM annualization',
    'DEC-05': 'Gross expensed interest, without netting interest income; separately disclosed capitalized interest, no duplication',
    'DEC-06': STANDARD_EBITDA_BASIS,
    'DEC-07': 'Only sources available at explicit as-of; latest explicit SEC amendment for the same concept and interval; ambiguous versions remain conflicts; retain prior versions',
    'DEC-08': 'Explicit unavailable states; critical missing facts and incomplete reconciliations block final acceptance',
    'DEC-09': 'Millions in source currency; two decimal places; parentheses for negatives; retain original precision and scale',
    'DEC-10': 'Structured inputs and validated workbook adapters only; non-SEC provenance remains explicit',
    'DEC-11': 'Dashboard excluded from this delivery',
}


def apply_approved_policy(report, inputs, candidates):
    """Business approval is separate from verification of company-specific inputs.

    DEC-03 deliberately remains unresolved: CFADS/debt-service components unknown.
    Existing Fact/Evidence/decision fields and metric IDs are reused.
    """
    evidence, decisions = dict(report.evidence), dict(report.decisions)
    roles = tuple(p.role for p in report.periods)
    decisions.pop('DEC-03', None)  # CFADS/debt service components still unresolved.
    for decision, content in APPROVED_DEFINITIONS.items():
        policy_version = DEBT_POLICY_VERSION if decision == 'DEC-02' else APPROVED_POLICY_VERSION
        policy_date = date(2026, 11, 7) if decision == 'DEC-02' else date(2026, 10, 24)
        locator = 'references/total-debt-policy.md' if decision == 'DEC-02' else 'references/loan-review-acceptance-spec-v1.0.md#9.1-9.2'
        eid = 'approval:' + policy_version + ':' + decision
        evidence[eid] = Evidence(eid, report.entity, report.scope,
            'user-approved-policy', policy_version, policy_date, locator, 'definition', roles, content)
        decisions[decision] = eid
    updated = replace(report, scale=Decimal(1000000), evidence=evidence, decisions=decisions)
    converted = {}
    for fid, original in inputs.items():
        item = dict(original)
        spec = METRICS[item['metric']]
        if item['status'] == 'sourced' and item['value'] is not None and item['unit'] == 'money':
            item['value'] = str(number(item['value']) * report.scale / updated.scale)
        # A business policy is not evidence that a raw label meets that policy.
        if spec.pending and item['status'] == 'sourced':
            selected = [candidates[eid] for eid in item.get('evidence_ids', ())]
            compatible = (bool(selected) and all(supported_debt_source(c, 'total_debt') for c in selected)
                          if item['metric'] == 'total_debt' else bool(selected) and all(c.get('definition') == spec.definition for c in selected))
            if compatible and item['metric'] == 'total_debt':
                item['basis'] = item.get('basis') or reported_debt_basis(selected)
            if item['metric'] in ('ltm_ebitda', 'ebitda_basis'):
                compatible = compatible and item.get('basis') == STANDARD_EBITDA_BASIS
            if not compatible:
                item.update(status='not_calculable', value=None,
                    reason='Approved policy; company-specific source definition not yet verified; candidate retained')
        if item['metric'] in DEBT_FIELDS and item['status'] == 'sourced' and number(item['value']) < 0:
            item.update(status='not_calculable', value=None, reason='Negative borrowing amount; retained source requires clarification')
        converted[fid] = item
    return updated, converted


def adopt_inputs(report, inputs, candidates):
    """Adopt exact source inputs; keep pending source candidates in the audit registry."""
    facts, evidence = dict(report.facts), dict(report.evidence)
    roles_by_candidate = {}
    for item in inputs.values():
        for eid in set(item.get('candidate_ids', ())) | set(item.get('evidence_ids', ())):
            roles_by_candidate.setdefault(eid, set()).add(item['period_role'])
    for eid, roles in roles_by_candidate.items():
        c = candidates[eid]
        if c['entity'] != report.entity or c['scope'] != report.scope:
            raise ValueError('Source candidate entity/scope mismatch')
        evidence[eid] = Evidence(eid, c['entity'], c['scope'], c['source_id'], c['version'],
            day(c['document_date']), c['locator'], 'fact' if c['status'] == 'sourced' else 'review',
            tuple(sorted(roles)), json.dumps({k: c.get(k) for k in ('value', 'unit', 'scale', 'definition', 'basis', 'excerpt', 'source_url', 'reason')}, ensure_ascii=False, default=str))
    for fid, item in inputs.items():
        metric, role = item['metric'], item['period_role']
        spec = METRICS[metric]
        f = facts.get(fid)
        if f is None:
            f = Fact(fid, metric, role, Status.MISSING, None, item['unit'], spec.definition,
                day(item['start']) if item['start'] else None, day(item['end']) if item['end'] else None,
                report.entity, report.scope, currency=item.get('currency'), reason='No adopted input',
                period_kind=item['period_kind'])
        state = item['status']
        state = 'missing' if state == 'not_disclosed' else state
        ids = tuple(item.get('evidence_ids', ()))
        if state == 'conflict':
            ids = tuple(item.get('candidate_ids', ()))
        reason = item.get('reason') or 'Input unavailable'
        pending = tuple(d for d in spec.pending if d not in report.decisions)
        if pending and state == 'sourced':
            state, reason = 'not_calculable', 'Unresolved definitions: ' + ', '.join(pending) + '; source candidate retained in audit'
        elif pending:
            reason += '; Unresolved definitions: ' + ', '.join(pending)
        if state == 'conflict' and len(ids) < 2:
            state, reason = 'not_calculable', 'Unresolved source compatibility: ' + reason
        if state == 'sourced':
            if not ids:
                raise ValueError('Available source input requires evidence')
            originals = [candidates[eid] for eid in ids]
            normalized = {str(normalize_value(c['value'], c['scale'], report.scale if f.unit == 'money' else 1))
                          if f.unit != 'text' else c['value'] for c in originals}
            if len(normalized) != 1 or normalized != {str(item['value'])}:
                # Decimal strings with different trailing zeroes are equivalent.
                if f.unit == 'text' or len({number(v) for v in normalized} | {number(item['value'])}) != 1:
                    raise ValueError('Source selection/value normalization mismatch')
            c = originals[0]  # Value/definition equality was checked by source selection.
            value = item['value'] if f.unit == 'text' else number(item['value'])
            original = c['value'] if f.unit == 'text' else number(c['value'])
            f = replace(f, status=Status.SOURCED, value=value, evidence_ids=ids, reason='',
                        original_value=original, original_scale=None if f.unit == 'text' else number(c['scale']),
                        basis=item.get('basis', ''), definition=c['definition'] if not METRICS[metric].formula else spec.definition)
        else:
            f = replace(f, status=Status(state), value=None, evidence_ids=ids, reason=reason)
        facts[fid] = f
    report = replace(report, facts=facts, evidence=evidence)
    validate_report(report, expected_mode=report.mode)
    return report


def _supporting_slots(report):
    facts = dict(report.facts)
    for p in report.periods:
        for metric in ('revenue_growth', 'gross_margin', 'operating_margin', 'pretax_margin', 'operating_margin_change'):
            fid = f'{metric}:{p.role}'; spec = METRICS[metric]
            start, end = _dates(metric, p)
            facts.setdefault(fid, Fact(fid, metric, p.role, Status.NOT_CALCULABLE, None, spec.unit,
                spec.definition, start, end, report.entity, report.scope, reason='Calculation not yet possible', period_kind='duration'))
    return replace(report, facts=facts)


def calculate_report(report, identity_reviews=(), *, cf01_backend=None, formula_backends=None, identity_backends=None):
    """Explicit existing decisions are required; reviews never become approvals."""
    report = _supporting_slots(report)
    formula_backends = formula_backends or {}
    identity_backends = identity_backends or {}
    for review in identity_reviews:
        formula = review['formula_id']
        if formula in identity_backends:
            expected_version = formula.lower().replace('-', '') + '-dbt-exact-v1'
            choice = identity_backends[formula]
            if isinstance(choice, dict):
                if choice.get('mode') != 'shadow' or set(choice) != {'mode', 'backend', 'observations'}:
                    raise ValueError('Invalid dbt identity shadow configuration')
                # Validate the reviewed identity before any external calculation.
                baseline = calculate_identity(report, **review)
                shadow_review = {**review, 'formula_version': expected_version}
                executor = lambda r, target, inputs, fid: _calculate_identity_external(r, target, inputs, fid, choice['backend'])
                candidate = calculate_identity(report, **shadow_review, calculation_executor=executor)
                before, after = baseline.facts[review['fact_id']], candidate.facts[review['fact_id']]
                comparison = ('matched' if before.value == after.value else 'mismatch') if before.status in AVAILABLE and after.status in AVAILABLE else 'unavailable'
                trace = json.loads(candidate.evidence[after.evidence_ids[-1]].content) if after.status in AVAILABLE else None
                choice['observations'].append(dict(formula_id=formula, fact_id=review['fact_id'],
                    python_status=before.status.value, dbt_status=after.status.value,
                    python_value=str(before.value) if before.value is not None else None,
                    dbt_value=str(after.value) if after.value is not None else None,
                    input_ids=list(review['input_ids']), comparison=comparison, trace=trace,
                    definition_evidence_id=review['definition_evidence_id'], reason=after.reason,
                    formula_version=after.formula_version,
                    calculation_locator=candidate.evidence[after.evidence_ids[-1]].locator if after.status in AVAILABLE else None))
                report = baseline
            else:
                if review['formula_version'] != expected_version:
                    raise ValueError('dbt identity requires its explicit exact formula version')
                executor = lambda r, target, inputs, fid: _calculate_identity_external(r, target, inputs, fid, choice)
                report = calculate_identity(report, **review, calculation_executor=executor)
        else:
            report = calculate_identity(report, **review)
    # The displayed basis is the approved formula, not an invented company EBITDA.
    for period in report.periods:
        role = period.role
        target = report.facts.get('ltm_ebitda:' + role)
        basis = report.facts.get('ebitda_basis:' + role)
        if target is None or basis is None or target.start is None:
            continue
        components = [report.facts.get(m + ':' + role) or report.facts.get('input:' + m + ':' + role)
                      for m in METRICS['ltm_ebitda'].inputs]
        if not all(f and f.status in AVAILABLE and (f.start, f.end) == (target.start, target.end)
                   for f in components):
            continue
        if basis.status in (Status.CONFLICT, Status.NOT_APPLICABLE) or (basis.status in AVAILABLE and basis.value != STANDARD_EBITDA_BASIS):
            continue
        if 'DEC-06' not in report.decisions or 'DEC-05' not in report.decisions:
            continue
        approval = report.evidence[report.decisions['DEC-06']]
        eid = 'policy-basis:' + role
        evidence = replace(approval, id=eid, kind='fact', period_roles=(role,),
                           content='User-selected calculation basis; not a company-reported EBITDA amount: ' + STANDARD_EBITDA_BASIS)
        basis = replace(basis, status=Status.SOURCED, value=STANDARD_EBITDA_BASIS,
                        original_value=STANDARD_EBITDA_BASIS, original_scale=None,
                        evidence_ids=(eid,), reason='', basis=STANDARD_EBITDA_BASIS)
        report = replace(report, facts={**report.facts, basis.id: basis}, evidence={**report.evidence, eid: evidence})
    # Current borrowing totals precede Total Debt, which precedes leverage.
    order = {'short_term_debt': 0, 'total_debt': 1, 'total_leverage': 3, 'operating_margin_change': 4}
    keys = sorted(report.facts, key=lambda fid: order.get(report.facts[fid].metric, 2))
    roles = [p.role for p in report.periods]
    for fid in keys:
        f = report.facts[fid]; spec = METRICS[f.metric]
        common = ':pct:' in fid
        if f.status in AVAILABLE or (not common and not spec.formula and f.metric != "ltm_ebitda"):
            continue
        # Source conflicts are not erased by a second calculation attempt.
        if f.status in (Status.CONFLICT, Status.NOT_APPLICABLE):
            continue
        if f.metric == 'current_ratio' and cf01_backend is not None:
            report = _calculate_cf01_external(report, fid, cf01_backend)
            continue
        metrics = (f.metric, 'revenue') if common else spec.inputs
        if f.metric == 'total_debt':
            if f.status == Status.NOT_CALCULABLE and f.evidence_ids:
                continue  # Keep a rejected reported total and its diagnostic; do not bypass it.
            supplied = [i for i in report.facts.values() if i.period_role == f.period_role and i.metric in DEBT_FIELDS and i.metric != 'total_debt'
                        and (i.status in AVAILABLE or i.status == Status.CONFLICT)]
            if f.status == Status.MISSING and not supplied:
                continue
            complete = []
            for route in DEBT_SUM_INPUTS:
                pair = [report.facts.get('input:' + m + ':' + f.period_role) for m in route]
                if all(i and i.status in AVAILABLE for i in pair):
                    complete.append(approved_formula_value('TOTAL-DEBT-v1', tuple(i.value for i in pair)))
            if len(set(complete)) > 1:
                updated = replace(f, status=Status.NOT_CALCULABLE, value=None,
                    reason='Alternative complete debt routes disagree; source components retained, no automatic selection')
                report = replace(report, facts={**report.facts, fid: updated})
                continue
            for route in DEBT_SUM_INPUTS:
                pair = [report.facts.get('input:' + m + ':' + f.period_role) for m in route]
                if all(i and i.status in AVAILABLE for i in pair):
                    metrics = route
                    break
        ids = []
        for n, metric in enumerate(metrics):
            role = f.period_role
            if n == 1 and f.metric in ('revenue_growth', 'operating_margin_change'):
                if report.mode == Mode.ANNUAL:
                    pos = roles.index(role)
                    if pos == 0:
                        role = 'prior_period_not_in_report'
                    else:
                        role = roles[pos - 1]
                else:
                    role = 'prior_interim' if role == 'current_interim' else 'prior_period_not_in_report'
            canonical = f'{metric}:{role}'
            ids.append(canonical if canonical in report.facts else f'input:{metric}:{role}')
        if not common and spec.formula in formula_backends:
            choice = formula_backends[spec.formula]
            if isinstance(choice, dict):
                if choice.get('mode') != 'shadow' or set(choice) != {'mode', 'backend', 'observations'}:
                    raise ValueError('Invalid dbt shadow configuration')
                candidate = _calculate_formula_external(report, fid, tuple(ids), choice['backend'])
                report = calculate_fact(report, fid, input_ids=tuple(ids), formula_version=FORMULA_VERSION)
                current, trial = report.facts[fid], candidate.facts[fid]
                comparison = 'unavailable'
                if current.status in AVAILABLE and trial.status in AVAILABLE:
                    with localcontext() as context:
                        context.prec = 300
                        rounded = current.value.quantize(Decimal('0.0001'), rounding=ROUND_HALF_EVEN)
                    comparison = 'matched' if rounded == trial.value else 'mismatch'
                trace = json.loads(candidate.evidence[trial.evidence_ids[0]].content) if trial.evidence_ids else None
                choice['observations'].append(dict(formula_id=spec.formula, fact_id=fid,
                    python_status=current.status.value, dbt_status=trial.status.value,
                    python_value=str(current.value) if current.value is not None else None,
                    dbt_value=str(trial.value) if trial.value is not None else None,
                    input_ids=list(ids), comparison=comparison, trace=trace,
                    reason=trial.reason, formula_version=trial.formula_version,
                    calculation_locator=candidate.evidence[trial.evidence_ids[0]].locator if trial.evidence_ids else None))
            else:
                report = _calculate_formula_external(report, fid, tuple(ids), choice)
            continue
        report = calculate_fact(report, fid, input_ids=tuple(ids),
            formula_version=DEBT_POLICY_VERSION if f.metric in DEBT_FIELDS else APPROVED_POLICY_VERSION if f.metric == "ltm_ebitda" else FORMULA_VERSION)
    return report


MIGRATED_FORMULAS = frozenset({'CF-02', 'CF-03', 'CF-04', 'CF-09', 'CF-16'})
MIGRATED_IDENTITIES = frozenset({'DER-01', 'DER-02', 'DER-03', 'DER-04', 'DER-05', 'DER-06'})


def _calculate_identity_external(report, target, inputs, formula, backend):
    if formula not in MIGRATED_IDENTITIES:
        raise ValueError('Unsupported dbt identity')
    if backend == 'offline':
        return None, None
    values = [f.value for f in inputs]
    request = dict(fact_id=target.id, entity=report.entity, scope=report.scope,
                   formula_id=formula, input_ids=[f.id for f in inputs], input_values=[str(v) for v in values])
    response = backend.calculate_formula(request)
    if (response.get('status') != 'calculated' or response.get('fact_id') != target.id
            or response.get('formula_id') != formula):
        raise ValueError('dbt identity returned unavailable or inconsistent result')
    converted = response.get('input_values')
    if not isinstance(converted, list) or len(converted) != 2 or any(Decimal(v) != x for v, x in zip(converted, values)):
        raise ValueError('dbt identity input conversion was lossy')
    with localcontext() as context:
        context.prec = 300
        expected = values[0] + values[1] if formula == 'DER-05' else values[0] - values[1]
    value, raw = Decimal(response['value']), Decimal(response['raw_value'])
    if not value.is_finite() or not raw.is_finite() or value != expected or raw != expected:
        raise ValueError('dbt identity failed independent exact verification')
    job_id, sql_sha256 = response['job_id'], response['sql_sha256']
    if not isinstance(job_id, str) or not job_id or not re.fullmatch('[a-f0-9]{64}', sql_sha256):
        raise ValueError('dbt identity provenance missing')
    eid = 'calculation:' + formula.lower() + ':' + target.period_role
    version = formula.lower().replace('-', '') + '-dbt-exact-v1'
    evidence = Evidence(eid, report.entity, report.scope, 'dbt-bigquery-identity', version, date.today(),
        'bigquery-job:' + job_id, 'review', (target.period_role,), json.dumps(dict(formula_id=formula,
        raw_value=str(raw), value=str(value), input_ids=request['input_ids'], input_values=request['input_values'],
        sql_sha256=sql_sha256), ensure_ascii=False, sort_keys=True))
    return value, evidence


def _calculate_formula_external(report, fact_id, ids, backend):
    """Check eligibility and bind inputs before delegating one approved formula."""
    target = report.facts[fact_id]
    spec = METRICS[target.metric]
    formula = spec.formula
    if formula not in MIGRATED_FORMULAS:
        raise ValueError('Unsupported dbt financial formula')
    if backend == 'offline':
        reason = 'Offline mode: migrated ' + formula + ' requires the dbt calculation backend'
    elif set(spec.pending) - set(report.decisions):
        reason = 'Unresolved definitions: ' + ', '.join(sorted(set(spec.pending) - set(report.decisions)))
    else:
        validate_report(report, expected_mode=report.mode)
        inputs = [report.facts.get(i) for i in ids]
        if any(f is None or f.status not in AVAILABLE for f in inputs):
            reason = 'Required inputs unavailable'
        else:
            periods = {p.role: p for p in report.periods}
            if tuple(f.metric for f in inputs) != spec.inputs:
                raise ValueError('dbt formula input identity/order mismatch')
            roles = [p.role for p in report.periods]
            for n, item in enumerate(inputs):
                role = target.period_role
                if n == 1 and formula in ('CF-09', 'CF-16'):
                    if report.mode == Mode.ANNUAL:
                        pos = roles.index(role)
                        role = roles[pos-1] if pos else None
                    else:
                        role = 'prior_interim' if role == 'current_interim' else None
                if (role is None or item.period_role != role or item.unit != METRICS[item.metric].unit
                        or item.entity != report.entity or item.scope != report.scope
                        or (item.start, item.end) != _dates(item.metric, periods[role])):
                    raise ValueError('dbt formula input scope, unit or period mismatch')
            values = tuple(f.value for f in inputs)
            with localcontext() as context:
                context.prec = 300
                interest_denominator = values[1] + values[2] if formula == 'CF-04' else None
            if ((formula in ('CF-02', 'CF-03', 'CF-09') and values[1] == 0)
                    or (formula == 'CF-04' and interest_denominator == 0)):
                reason = 'Zero divisor or nonnumeric input'
            else:
                version = formula.lower().replace('-', '') + '-dbt-4dp-half-even-v1'
                request = dict(fact_id=fact_id, entity=report.entity, scope=report.scope,
                    input_ids=ids, input_values=[str(v) for v in values], period_role=target.period_role,
                    formula_id=formula, formula_version=version)
                response = backend.calculate_formula(request)
                if response.get('status') != 'calculated' or response.get('fact_id') != fact_id or response.get('formula_id') != formula:
                    raise ValueError('dbt formula returned unavailable or inconsistent result')
                converted = response.get('input_values')
                if not isinstance(converted, list) or len(converted) != len(values) or any(Decimal(v) != expected for v, expected in zip(converted, values)):
                    raise ValueError('dbt formula input conversion was lossy')
                with localcontext() as context:
                    context.prec = 300
                    expected = approved_formula_value(formula, values).quantize(Decimal('0.0001'), rounding=ROUND_HALF_EVEN)
                    raw = Decimal(response['raw_value'])
                    result = Decimal(response['value'])
                    if not raw.is_finite() or not result.is_finite() or raw.quantize(Decimal('0.0001'), rounding=ROUND_HALF_EVEN) != result or result != expected:
                        raise ValueError('dbt formula failed independent four-decimal verification')
                job_id, sql_sha256 = response['job_id'], response['sql_sha256']
                if not isinstance(job_id, str) or not job_id or not re.fullmatch('[a-f0-9]{64}', sql_sha256):
                    raise ValueError('dbt formula provenance missing')
                evidence_id = 'calculation:' + formula.lower() + ':' + target.period_role
                evidence = Evidence(evidence_id, report.entity, report.scope, 'dbt-bigquery-formula', version,
                    date.today(), 'bigquery-job:' + job_id, 'review', (target.period_role,),
                    json.dumps(dict(formula_id=formula, raw_value=str(raw), value=str(result),
                                    input_ids=ids, input_values=[str(v) for v in values], sql_sha256=sql_sha256,
                                    rounding='ROUND_HALF_EVEN', places=4), ensure_ascii=False, sort_keys=True))
                updated = replace(target, status=Status.CALCULATED, value=result, reason='', formula_id=formula,
                                  formula_version=version, input_ids=ids, evidence_ids=(evidence_id,))
                result_report = replace(report, facts={**report.facts, fact_id: updated},
                                        evidence={**report.evidence, evidence_id: evidence})
                validate_report(result_report, expected_mode=report.mode)
                return result_report
    updated = replace(target, status=Status.NOT_CALCULABLE, value=None, reason=reason)
    return replace(report, facts={**report.facts, fact_id: updated})


def _calculate_cf01_external(report, fact_id, backend):
    """Adopt only a verified external CF-01 result; never use verification as fallback."""
    target = report.facts[fact_id]
    if backend == 'offline':
        reason = 'Offline mode: migrated CF-01 requires the dbt calculation backend'
    else:
        ids = tuple((metric + ':' + target.period_role) if metric + ':' + target.period_role in report.facts
                    else ('input:' + metric + ':' + target.period_role)
                    for metric in ('current_assets', 'current_liabilities'))
        values = [report.facts.get(i) for i in ids]
        if any(f is None or f.status not in AVAILABLE for f in values):
            reason = 'Required inputs unavailable'
        elif values[1].value == 0:
            reason = 'Zero divisor or nonnumeric input'
        else:
            a, b = (f.value for f in values)
            request = dict(fact_id=fact_id, entity=report.entity, scope=report.scope,
                assets=str(a), liabilities=str(b), input_ids=ids,
                period_role=target.period_role, end=target.end.isoformat(),
                formula_id='CF-01', formula_version=CF01_DBT_VERSION)
            response = backend.calculate_cf01(request)
            if response.get('status') == 'unavailable':
                reason = response.get('reason') or 'dbt CF-01 calculation unavailable'
            else:
                if (response.get('status') != 'calculated' or response.get('fact_id') != fact_id
                    or Decimal(response['assets_cast']) != a or Decimal(response['liabilities_cast']) != b):
                    raise ValueError('dbt CF-01 returned inconsistent identity or lossy input conversion')
                with localcontext() as context:
                    context.prec = 300
                    expected = (a / b).quantize(Decimal('0.0001'), rounding=ROUND_HALF_EVEN)
                    raw = Decimal(response['raw_value'])
                    if not raw.is_finite() or raw.quantize(Decimal('0.0001'), rounding=ROUND_HALF_EVEN) != Decimal(response['value']):
                        raise ValueError('dbt CF-01 raw and rounded values disagree')
                result = Decimal(response['value'])
                if not result.is_finite() or result != expected:
                    raise ValueError('dbt CF-01 failed independent four-decimal verification')
                job_id, sql_sha256 = response['job_id'], response['sql_sha256']
                if not isinstance(job_id, str) or not job_id or not re.fullmatch('[a-f0-9]{64}', sql_sha256):
                    raise ValueError('dbt CF-01 calculation provenance missing')
                evidence_id = 'calculation:cf01:' + target.period_role
                evidence = Evidence(evidence_id, report.entity, report.scope, 'dbt-bigquery-cf01',
                    CF01_DBT_VERSION, date.today(), 'bigquery-job:' + job_id, 'review', (target.period_role,),
                    json.dumps(dict(raw_value=str(raw), value=str(result), assets=str(a), liabilities=str(b),
                                    input_ids=ids, sql_sha256=sql_sha256, rounding='ROUND_HALF_EVEN', places=4),
                               ensure_ascii=False, sort_keys=True))
                updated = replace(target, status=Status.CALCULATED, value=result, reason='',
                    formula_id='CF-01', formula_version=CF01_DBT_VERSION, input_ids=ids,
                    evidence_ids=(evidence_id,))
                updated_report = replace(report, facts={**report.facts, fact_id: updated},
                                         evidence={**report.evidence, evidence_id: evidence})
                validate_report(updated_report, expected_mode=report.mode)
                return updated_report
    updated = replace(target, status=Status.NOT_CALCULABLE, value=None, reason=reason)
    return replace(report, facts={**report.facts, fact_id: updated})


def _pair(report):
    return ('fy_t_minus_1', 'fy_t') if report.mode == Mode.ANNUAL else ('prior_interim', 'current_interim')


def build_narratives(report):
    """Only verifiable comparisons. No invented cause, credit judgment or filler."""
    tables = []
    income_metrics = {'revenue': 'revenue', 'gross_margin': 'gross_margin', 'operating_margin': 'operating_margin',
                      'interest_expense': 'interest_expense', 'pretax_margin': 'pretax_margin'}
    for table in report.tables:
        narratives = []
        for topic in table.spec.topics:
            prev, curr = _pair(report)
            metric = income_metrics.get(topic, topic)
            if table.spec.key != 'income':
                prev, curr = ('fy_t_minus_1', 'fy_t') if report.mode == Mode.ANNUAL else ('latest_fy_end', 'current_interim')
                metric = 'current_ratio' if topic == 'liquidity' else {'interest_coverage': 'interest_coverage', 'cash_flow_ratio': 'cash_flow_ratio'}.get(topic, topic)
            if table.spec.key == 'ratios' and topic == 'liquidity':
                # Both ratios share the paragraph, but retain independent directions
                # and missing states. No quick-assets proxy is introduced.
                comparisons, ids, missing = [], [], []
                periods = {p.role: p for p in report.periods}
                for liquidity_metric in ('current_ratio', 'quick_ratio'):
                    pair_ids = tuple(f'{liquidity_metric}:{role}' for role in (prev, curr))
                    pair = [report.facts.get(fid) for fid in pair_ids]
                    if any(f is None or f.status not in AVAILABLE for f in pair):
                        missing.append(METRICS[liquidity_metric].label + ' 缺少可比較數值')
                        continue
                    before, after = pair
                    direction = '持平' if after.value == before.value else '上升' if after.value > before.value else '下降'
                    comparisons.append(f'{METRICS[liquidity_metric].label} 由 {periods[prev].end} 的 {before.value * 100:,.2f}% '
                                       f'{direction}至 {periods[curr].end} 的 {after.value * 100:,.2f}%。')
                    ids.extend(pair_ids)
                narratives.append(Narrative(topic, 'supported' if comparisons else 'missing',
                    text=' '.join(comparisons), claim_type='comparison', fact_ids=tuple(ids),
                    period_roles=(prev, curr) if comparisons else (), reason='；'.join(missing + ['所選來源輸入未附原因分析，本段僅作數值比較。'])))
                continue
            ids = (f'{metric}:{prev}', f'{metric}:{curr}')
            items = [report.facts.get(fid) for fid in ids]
            if any(f is None or f.status not in AVAILABLE for f in items):
                reasons = list(dict.fromkeys(f.reason if f else 'Required source fact not present' for f in items if f is None or f.status not in AVAILABLE))
                narratives.append(Narrative(topic, 'missing', reason='; '.join(reasons)))
                continue
            a, b = items
            def fmt(f):
                if f.unit in ('percent', 'percentage_points'): return f'{f.value * 100:,.2f}%'
                if f.unit == 'multiple': return f'{f.value:,.2f}x'
                return f'{f.value:,.2f}'
            direction = '持平' if b.value == a.value else '上升' if b.value > a.value else '下降'
            periods = {p.role: p for p in report.periods}
            text = f'{METRICS[metric].label} 由 {periods[prev].label} 的 {fmt(a)} {direction}至 {periods[curr].label} 的 {fmt(b)}。'
            if a.unit == 'percent' and metric.endswith('margin'):
                text += f'變動 {(b.value - a.value) * 100:,.2f} 個百分點。'
            if metric == 'revenue':
                growth = report.facts.get(f'revenue_growth:{curr}')
                if growth is not None and growth.status in AVAILABLE:
                    text += f'依CF-09計算成長率為 {growth.value * 100:,.2f}%。'
                    ids += (growth.id,)
            if metric == 'operating_margin':
                change = report.facts.get(f'operating_margin_change:{curr}')
                if change is not None and change.status in AVAILABLE: ids += (change.id,)
            text += '所選來源輸入未附可核實的原因證據，本段僅作數值比較。'
            narratives.append(Narrative(topic, 'supported', text=text, claim_type='comparison', fact_ids=ids,
                                        period_roles=(prev, curr)))
        tables.append(replace(table, narratives=tuple(narratives)))
    result = replace(report, tables=tuple(tables))
    validate_report(result, expected_mode=report.mode)
    return result


def validate_arithmetic(report):
    """Independent recomputation at the final boundary, separate from layout checks."""
    validate_report(report, expected_mode=report.mode)
    checked = []
    for f in report.facts.values():
        if f.status != Status.CALCULATED: continue
        values = tuple(report.facts[i].value for i in f.input_ids)
        if f.formula_id.startswith('DER-'):
            rules = {'DER-01': ('gross_profit', ('revenue', 'cost_of_revenue'), -1),
                     'DER-02': ('cost_of_revenue', ('revenue', 'gross_profit'), -1),
                     'DER-03': ('operating_expense', ('gross_profit', 'operating_income'), -1),
                     'DER-04': ('operating_income', ('gross_profit', 'operating_expense'), -1),
                     'DER-05': ('pretax_profit', ('net_income', 'income_tax'), 1),
                     'DER-06': ('net_income', ('pretax_profit', 'income_tax'), -1)}
            if f.formula_id not in rules: raise ValueError('Unapproved identity formula')
            target, components, sign = rules[f.formula_id]
            items = [report.facts[i] for i in f.input_ids]
            if f.metric != target or tuple(i.metric for i in items) != components:
                raise ValueError('Identity formula components mismatch')
            if any((i.start, i.end, i.period_role, i.currency) != (f.start, f.end, f.period_role, f.currency) for i in items):
                raise ValueError('Identity input period/unit mismatch')
            if f.formula_version == f.formula_id.lower().replace('-', '') + '-dbt-exact-v1':
                with localcontext() as context:
                    context.prec = 300
                    expected = values[0] + sign * values[1]
            else:
                expected = values[0] + sign * values[1]
        elif (f.formula_id in MIGRATED_FORMULAS and
              f.formula_version == f.formula_id.lower().replace('-', '') + '-dbt-4dp-half-even-v1'):
            with localcontext() as context:
                context.prec = 300
                value = approved_formula_value(f.formula_id, values)
                expected = value.quantize(Decimal('0.0001'), rounding=ROUND_HALF_EVEN) if value is not None else None
        elif f.formula_id == 'CF-01' and f.formula_version == CF01_DBT_VERSION:
            with localcontext() as context:
                context.prec = 300
                expected = (values[0] / values[1]).quantize(Decimal('0.0001'), rounding=ROUND_HALF_EVEN) if values[1] != 0 else None
        else:
            if f.formula_id == 'CASH-CHANGE-v1' and not any(report.evidence[eid].kind == 'definition' for eid in f.evidence_ids):
                raise ValueError('Cash change requires an explicit reviewed same-basis complete bridge')
            expected = approved_formula_value(f.formula_id, values)
        if expected is None or f.value != expected:
            raise ValueError(f'Arithmetic verification failed: {f.id}')
        checked.append(f.id)
    return checked


def validate_rendered_report(report, markdown):
    """Check emitted heading hierarchy, exact table dimensions and bound numbers."""
    headings = tuple((len(m[1]), m[2]) for line in markdown.splitlines()
                     if (m := re.fullmatch(r'(#{1,6}) (.+)', line)))
    if headings != report.headings:
        raise ValueError('Rendered report headings differ from contract')
    groups, current = [], []
    for line in markdown.splitlines() + ['']:
        if line.startswith('|'): current.append(line)
        elif current:
            groups.append(current); current = []
    if len(groups) != len(report.tables): raise ValueError('Rendered table count mismatch')
    for table, lines in zip(report.tables, groups):
        if len(lines) != len(table.spec.rows) + 2: raise ValueError('Rendered row count mismatch')
        cells = [[c.strip() for c in re.split(r'(?<!\\)\|', line)[1:-1]] for line in lines]
        if any(len(row) != len(table.spec.columns) + 1 for row in cells):
            raise ValueError('Rendered column count mismatch')
        periods = {p.role: p for p in report.periods}
        for label, column in zip(cells[0][1:], table.spec.columns):
            period = periods[column.removeprefix('pct:')]
            expected = '% of revenue' if column.startswith('pct:') else (
                period.end.isoformat() if table.spec.key in ('ratios', 'leverage') and period.end else period.label)
            expected = expected.replace('|', '\\|').replace('\r', ' ').replace('\n', ' ')
            if label != expected: raise ValueError('Rendered period binding mismatch')
        for metric, row, ids in zip(table.spec.rows, cells[2:], table.cells):
            expected_label = METRICS[metric].label
            if metric in ('revenue', 'cost_of_revenue'):
                allowed = {'Revenues', 'Net Sales', 'Net sales', 'Operating Cost', 'Cost Of Goods Sold', 'Cost of revenues', 'Cost of sales', 'Cost of services'}
                source_labels = {report.facts[i].definition for i in ids if report.facts[i].status in AVAILABLE and report.facts[i].unit == 'money'}
                if len(source_labels) == 1 and next(iter(source_labels)) in allowed:
                    expected_label = next(iter(source_labels))
            if row[0].split('／', 1)[0] != expected_label:
                raise ValueError('Rendered metric label mismatch')
            for text, fid in zip(row[1:], ids):
                f = report.facts[fid]
                if f.status not in AVAILABLE:
                    if re.fullmatch(r'[+-]?[\d,.]+(?:%|x)?', text):
                        raise ValueError('Rendered unavailable fact as a number')
                    continue
                if f.unit == 'text':
                    if text != str(f.value).replace('|', '\\|').replace('\n', ' '):
                        raise ValueError('Rendered text/basis mismatch')
                else:
                    suffix = '%' if f.unit == 'percent' else 'x' if f.unit == 'multiple' else ''
                    number = f.value * 100 if f.unit == 'percent' else f.value
                    expected_text = f'{abs(number):,.2f}{suffix}'
                    if number < 0: expected_text = f'({expected_text})'
                    if text != expected_text:
                        raise ValueError(f'Rendered value/unit mismatch: {fid}')
    if any(token in '\n'.join(line for group in groups for line in group) for token in ('[value]', '[refer to MD&A]', '{{value}}')):
        raise ValueError('Unfilled template placeholder in rendered report')
    return True


def automatic_reconciliation_checks(report):
    """Report missing reconciliation evidence instead of silently omitting QA.

    Asset/equity composition and full cash/FX definitions cannot be inferred from
    the registered metrics alone. Explicit reviewed source bridges are required.
    """
    checks = []
    for p in report.periods:
        for kind, metrics, reason in (
            ('cash_bridge', ('beginning_cash', 'ending_cash', 'cfo', 'cfi', 'cff'),
             '完整現金／受限制現金口徑、FX及其他調節未核實；不得反推未知項'),
            ('balance_sheet_equation', ('total_liabilities', 'net_worth'),
             '資產與權益／非控制權益完整組成未核實；Net Worth不能自動代理所有權益')):
            ids = [f'{m}:{p.role}' if f'{m}:{p.role}' in report.facts else f'input:{m}:{p.role}' for m in metrics]
            missing = [fid for fid in ids if fid not in report.facts or report.facts[fid].status not in AVAILABLE]
            checks.append({'kind': kind, 'period_role': p.role, 'period': p.label, 'status': 'not_calculable',
                           'reason': reason, 'required_input_ids': ids, 'unavailable_input_ids': missing,
                           'calculated_value': None, 'reported_value': None, 'difference': None})
    return checks


def verify_reconciliation(report, check):
    """Recompute a reviewed bridge exclusively from canonical monetary facts.

    A caller's PASS, amounts, tolerance, and generic evidence are not authority.
    A structured review must bind the exact equation and tolerance. Rounding
    tolerance is capped independently by the original source precision.
    """
    result = {**check, 'verified': False}
    if check.get('status') not in ('matched', 'within_precision'):
        return result
    try:
        role, kind = check['period_role'], check['kind']
        schemas = {
            'cash_bridge': ('ending_cash', ('beginning_cash', 'cfo', 'cfi', 'cff', 'cash_fx', 'cash_other')),
            'balance_sheet_equation': ('total_assets', ('total_liabilities', 'net_worth')),
        }
        reported_metric, term_metrics = schemas[kind]
        terms = check['terms']
        if not isinstance(terms, list) or any(set(t) != {'fact_id', 'coefficient'} for t in terms):
            raise ValueError('Explicit canonical bridge terms required')
        if any(number(t['coefficient']) != 1 for t in terms):
            raise ValueError('Bridge components must be signed source facts, each included once')
        ids = [t['fact_id'] for t in terms] + [check['reported_fact_id']]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate bridge component')
        facts = [report.facts[fid] for fid in ids]
        if tuple(f.metric for f in facts[:-1]) != term_metrics or facts[-1].metric != reported_metric:
            raise ValueError('Bridge component identity/order mismatch')
        if any(f.status not in AVAILABLE or f.unit != 'money' or f.currency != report.currency
               or f.period_role != role for f in facts):
            raise ValueError('Unavailable or incompatible canonical bridge facts')
        period = next(p for p in report.periods if p.role == role)
        if any((f.start, f.end) != _dates(f.metric, period) for f in facts):
            raise ValueError('Bridge component period mismatch')

        def lineage(f):
            if f.status == Status.SOURCED:
                return set(f.evidence_ids)
            return set().union(*(lineage(report.facts[i]) for i in f.input_ids))

        def rounding_bound(f):
            if f.status == Status.SOURCED:
                # Original source decimal places, never display rounding.
                quantum = Decimal(1).scaleb(f.original_value.as_tuple().exponent)
                return quantum * f.original_scale / report.scale / 2
            return sum((rounding_bound(report.facts[i]) for i in f.input_ids), Decimal(0))

        eids = check['evidence_ids']
        if len(eids) != len(set(eids)):
            raise ValueError('Duplicate reconciliation evidence')
        cited = [report.evidence[eid] for eid in eids]
        expected_sources = set().union(*(lineage(f) for f in facts))
        if not expected_sources <= set(eids) or not any(e.kind == 'fact' for e in cited):
            raise ValueError('Reconciliation must cite the canonical source lineage')
        if any(role not in e.period_roles for e in cited):
            raise ValueError('Reconciliation evidence period mismatch')
        tolerance = number(check.get('tolerance', 0))
        maximum = sum((rounding_bound(f) for f in facts), Decimal(0))
        if tolerance < 0 or tolerance > maximum:
            raise ValueError('Tolerance exceeds original-source rounding precision')
        binding = dict(kind=kind, period_role=role, terms=terms,
                       reported_fact_id=check['reported_fact_id'], currency=report.currency,
                       scale=str(report.scale), tolerance=str(tolerance))
        approved = False
        for e in cited:
            if e.kind not in ('review', 'definition'):
                continue
            try:
                approved |= json.loads(e.content) == binding
            except (ValueError, TypeError):
                pass
        if not approved:
            raise ValueError('Structured review of complete same-basis bridge required')
        calculated = sum((f.value for f in facts[:-1]), Decimal(0))
        reported = facts[-1].value
        difference = calculated - reported
        if kind == 'cash_bridge':
            change = report.facts.get('cash_change:' + role)
            if change and change.status in AVAILABLE and change.value != reported - facts[0].value:
                raise ValueError('Canonical cash change disagrees with ending minus beginning cash')
        status = 'matched' if difference == 0 else 'within_precision' if abs(difference) <= tolerance else 'unexplained_difference'
        if any(number(check[key]) != value for key, value in (
                ('calculated_value', calculated), ('reported_value', reported), ('difference', difference))):
            raise ValueError('Supplied reconciliation amounts differ from canonical arithmetic')
        result.update(calculated_value=calculated, reported_value=reported, difference=difference,
                      tolerance=tolerance, status=status, verified=status in ('matched', 'within_precision'))
    except (KeyError, ValueError, TypeError, AttributeError, StopIteration) as exc:
        result.update(status='not_calculable', verification_error=str(exc))
    return result


def acceptance_result(report, checks=(), limitations=()):
    checked = validate_arithmetic(report)
    bound = {fid for table in report.tables for row in table.cells for fid in row}
    unavailable = [{'fact_id': f.id, 'metric': f.metric, 'period_role': f.period_role, 'start': f.start, 'end': f.end,
                    'status': f.status.value, 'reason': f.reason, 'evidence_ids': f.evidence_ids}
                   for f in report.facts.values() if f.id in bound and f.status not in AVAILABLE]
    absent_narratives = [{'table': t.spec.key, 'topic': n.topic, 'reason': n.reason}
                         for t in report.tables for n in t.narratives if n.status != 'supported']
    try:
        from .formal_commentary import commentary_gaps
    except ImportError:
        from formal_commentary import commentary_gaps
    missing_commentary = commentary_gaps(report)
    blockers = list(limitations)
    if unavailable: blockers.append('Required report cells are unavailable or definitions unresolved')
    if 'DEC-08' not in report.decisions: blockers.append('DEC-08 missing-data/delivery policy remains unapproved')
    if 'DEC-09' not in report.decisions: blockers.append('DEC-09 presentation policy remains unapproved')
    checks = [verify_reconciliation(report, check) for check in checks]
    verified_coverage = {(c['kind'], c['period_role']) for c in checks if c['verified']}
    expected_coverage = {(kind, p.role) for kind in ('balance_sheet_equation', 'cash_bridge') for p in report.periods}
    if verified_coverage != expected_coverage or any(c['status'] not in ('matched', 'within_precision') for c in checks):
        blockers.append('Reconciliation is incomplete or contains unexplained differences')
    return {'technical_validation_passed': True, 'calculation_fact_ids_verified': checked,
            'final_report_accepted': False, 'substantive_data_ready': not blockers,
            'rendered_validation_passed': False, 'source_limitations': list(limitations), 'acceptance_blockers': blockers,
            'status_counts': dict(Counter(report.facts[fid].status.value for fid in sorted(bound))),
            'missing_or_pending': unavailable,
            'missing_inputs': [{'fact_id': f.id, 'metric': f.metric, 'period_role': f.period_role, 'status': f.status.value, 'reason': f.reason, 'evidence_ids': f.evidence_ids}
                               for f in report.facts.values() if f.id.startswith('input:') and f.status not in AVAILABLE], 'incomplete_narratives': absent_narratives,
            'reconciliations': list(checks), 'commentary_complete': not missing_commentary,
            'incomplete_commentary': missing_commentary, 'commentary_required_for_acceptance': False}


def _adopt_identity_review_bundle(report, bundle, *, expected_cik, as_of, identity_modes):
    """Adopt explicit reviewer attestations bound to this request and its inputs."""
    try:
        from .review_outputs import lineage_evidence_ids
    except ImportError:
        from review_outputs import lineage_evidence_ids
    required = {'schema_version', 'entity', 'cik', 'scope', 'as_of', 'currency', 'scale', 'mode', 'reviews'}
    if not isinstance(bundle, dict) or set(bundle) != required or bundle['schema_version'] != 1:
        raise ValueError('Invalid identity review bundle schema')
    if (not expected_cik or bundle['cik'] != expected_cik or bundle['entity'] != report.entity
            or bundle['scope'] != report.scope or bundle['currency'] != report.currency
            or number(bundle['scale']) != report.scale or bundle['mode'] != report.mode.value
            or bundle['as_of'] != as_of):
        raise ValueError('Identity review borrower, CIK, as_of, mode, currency or scale mismatch')
    if not isinstance(bundle['reviews'], list): raise ValueError('Identity reviews must be a list')
    evidence = dict(report.evidence)
    reviews, seen = [], set()
    for item in bundle['reviews']:
        fields = {'fact_id', 'formula_id', 'input_ids', 'period_start', 'period_end', 'bindings', 'evidence', 'verification'}
        if not isinstance(item, dict) or set(item) != fields or item['formula_id'] not in identity_modes:
            raise ValueError('Identity review needs explicitly selected formula and exact schema')
        target = report.facts.get(item['fact_id'])
        if target is None or target.id in seen: raise ValueError('Unknown or duplicate identity target')
        seen.add(target.id)
        if (item['period_start'] != (target.start.isoformat() if target.start else None)
                or item['period_end'] != (target.end.isoformat() if target.end else None)):
            raise ValueError('Identity review period differs from target')
        ids, bindings = item['input_ids'], item['bindings']
        if not isinstance(ids, list) or len(ids) != 2 or not isinstance(bindings, list) or len(bindings) != 2:
            raise ValueError('Identity review requires two bound source inputs')
        for fid, binding in zip(ids, bindings):
            if not isinstance(binding, dict) or set(binding) != {'fact_id', 'value', 'evidence_ids'}:
                raise ValueError('Identity review input binding is invalid')
            fact = report.facts.get(fid)
            if (fact is None or fact.status not in AVAILABLE or binding['fact_id'] != fid
                    or number(binding['value']) != fact.value or not isinstance(binding['evidence_ids'], list)
                    or sorted(binding['evidence_ids']) != sorted(lineage_evidence_ids(report, fid))):
                raise ValueError('Identity review input or evidence changed since review')
        raw, verification = item['evidence'], item['verification']
        if not isinstance(raw, dict) or set(raw) != {'id', 'source_id', 'version', 'document_date', 'locator', 'content'}:
            raise ValueError('Identity definition evidence has invalid fields')
        if any(not isinstance(raw[k], str) or not raw[k].strip() for k in raw):
            raise ValueError('Identity definition evidence requires source, version, date, locator and content')
        if (not isinstance(verification, dict) or set(verification) != {'verified_by', 'verified_at', 'method'}
                or not isinstance(verification['verified_by'], str) or not verification['verified_by'].strip()
                or verification['method'] != 'checked_against_source'):
            raise ValueError('Identity review requires explicit source verification')
        doc_date, verified_at = day(raw['document_date']), day(verification['verified_at'])
        if doc_date > day(as_of) or verified_at < doc_date:
            raise ValueError('Identity definition is after as_of or review predates source')
        if raw['id'] in evidence: raise ValueError('Duplicate identity definition evidence ID')
        content = json.dumps(dict(excerpt=raw['content'], fact_id=target.id, formula_id=item['formula_id'],
            bindings=bindings, period_start=item['period_start'], period_end=item['period_end'],
            verification=verification), ensure_ascii=False, sort_keys=True)
        evidence[raw['id']] = Evidence(raw['id'], report.entity, report.scope, raw['source_id'], raw['version'],
            doc_date, raw['locator'], 'definition', (target.period_role,), content)
        mode = identity_modes[item['formula_id']]
        reviews.append(dict(fact_id=target.id, formula_id=item['formula_id'], input_ids=tuple(ids),
            formula_version=(FORMULA_VERSION if mode == 'shadow' else item['formula_id'].lower().replace('-', '') + '-dbt-exact-v1'),
            definition_evidence_id=raw['id']))
    report = replace(report, evidence=evidence)
    validate_report(report, expected_mode=report.mode)
    return report, tuple(reviews)


def complete_report(report, inputs, candidates, *, checks=(), limitations=(), identity_reviews=(), cf01_backend=None, formula_backends=None, identity_backends=None,
                    identity_review_bundle=None, expected_cik=None, as_of=None, identity_modes=None):
    report, inputs = apply_approved_policy(report, inputs, candidates)
    report = adopt_inputs(report, inputs, candidates)
    if identity_review_bundle is not None:
        report, reviewed = _adopt_identity_review_bundle(report, identity_review_bundle,
            expected_cik=expected_cik, as_of=as_of, identity_modes=identity_modes or {})
        identity_reviews = tuple(identity_reviews) + reviewed
    report = calculate_report(report, identity_reviews, cf01_backend=cf01_backend,
                              formula_backends=formula_backends, identity_backends=identity_backends)
    report = build_narratives(report)
    return report, acceptance_result(report, checks or automatic_reconciliation_checks(report), limitations)


def report_from_source_bundle(bundle, *, checks=(), limitations=(), cf01_backend=None, formula_backends=None,
                              identity_backends=None, identity_review_bundle=None, identity_modes=None):
    report, _, _ = parse_request(bundle['request'])
    return complete_report(report, bundle['inputs'], bundle['evidence'], checks=checks, limitations=limitations,
                           cf01_backend=cf01_backend, formula_backends=formula_backends,
                           identity_backends=identity_backends, identity_review_bundle=identity_review_bundle,
                           expected_cik=bundle.get('cik'), as_of=bundle.get('as_of'), identity_modes=identity_modes)


def write_findings(report, acceptance, directory, source_bundle=None):
    """Separate audit artifacts; never add a Data Quality Appendix to the report."""
    directory = Path(directory)
    source_bundle = source_bundle or {}
    inputs, candidates = source_bundle.get('inputs', {}), source_bundle.get('evidence', {})
    periods = {p.role: p for p in report.periods}
    rows = []
    for entry in acceptance['missing_or_pending'] + acceptance['missing_inputs']:
        f = report.facts[entry['fact_id']]
        spec = METRICS[f.metric]
        common = f.unit == 'percent' and spec.unit == 'money'
        required = (f.metric, 'revenue') if common else spec.inputs
        dependencies = []
        for metric in required:
            fid = f'{metric}:{f.period_role}'
            if fid not in report.facts: fid = f'input:{metric}:{f.period_role}'
            fact = report.facts.get(fid)
            dependencies.append(fid + ': ' + (fact.status.value if fact else 'missing'))
        item = inputs.get(f.id, {})
        candidate_ids = item.get('candidate_ids', ()) or f.evidence_ids
        selected = [candidates[eid] for eid in candidate_ids if eid in candidates]
        rows.append({'fact_id': f.id, '科目': spec.label, '期間角色': f.period_role,
            '期間': periods[f.period_role].label, '狀態': f.status.value, '原因': f.reason,
            '必要輸入': '; '.join(dependencies), '待確認定義': '; '.join(d for d in spec.pending if d not in report.decisions),
            '原始候選值_未採入正式報表': '; '.join(str(c.get('value')) + ' [' + str(c.get('unit')) + ', scale=' + str(c.get('scale')) + ']' for c in selected),
            '來源位置': '; '.join(c['locator'] for c in selected)})
    with (directory / 'missing_data.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        columns = list(rows[0]) if rows else ['fact_id', '科目', '期間', '狀態', '原因']
        writer = csv.DictWriter(handle, fieldnames=columns); writer.writeheader(); writer.writerows(rows)
    check_rows = []
    for c in acceptance['reconciliations']:
        check_rows.append({'檢查': c['kind'], '期間': c.get('period', c.get('period_role', '')),
            '狀態': c['status'], '單位': c.get('unit', ''), '幣別': c.get('currency'), 'scale': c.get('scale'),
            '計算值': c.get('calculated_value'), '原檔列示值': c.get('reported_value'), '差額_計算減原檔': c.get('difference'),
            '原因與限制': c.get('reason', ''), '來源位置': '; '.join(c.get('source_locators', ()))})
    with (directory / 'reconciliations.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        columns = list(check_rows[0]) if check_rows else ['檢查', '期間', '狀態', '原因與限制']
        writer = csv.DictWriter(handle, fieldnames=columns); writer.writeheader(); writer.writerows(check_rows)
    lines = ['# 缺少資料、待確認定義與勾稽結果', '',
        '完整項目見同目錄 missing_data.csv；逐項來源、單位與差額見 reconciliations.csv。', '',
        '## 仍未完成的驗收條件', '']
    lines += ['- ' + reason for reason in acceptance['acceptance_blockers']]
    lines += ['', '## 無法勾稽的數值差額', '', '| 檢查 | 期間 | 差額（計算－原檔） | 單位 |', '| --- | --- | ---: | --- |']
    failed = [c for c in acceptance['reconciliations'] if c['status'] == 'unexplained_difference']
    for c in failed:
        lines.append(f"| {c['kind']} | {c.get('period', '')} | {c['difference']:,} | {c.get('currency', '')}, scale={c.get('scale', '')} |")
    if not failed: lines.append('| 尚無可列示的數值差額 | 不代表勾稽已完成 | — | 缺必要資料的項目另列not_calculable |')
    lines += ['', '差額只是已列來源數字的算術比較，原因尚未確定；未知FX、現金定義與其他調節未補0或反推。', '']
    (directory / 'review_findings.md').write_text('\n'.join(lines), encoding='utf-8')


def write_report_artifacts(report, acceptance, directory, *, source_bundle=None):
    try:
        from .credit_memo_writer import render_loan_review
    except ImportError:
        from credit_memo_writer import render_loan_review
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    # Never trust a caller-supplied PASS flag: recompute from the actual report.
    acceptance = acceptance_result(report, acceptance.get('reconciliations', ()), acceptance.get('source_limitations', ()))
    acceptance['final_report_accepted'] = acceptance['substantive_data_ready']
    markdown = render_loan_review(report, acceptance)
    validate_rendered_report(report, markdown)
    acceptance['rendered_validation_passed'] = True
    try:
        from .formal_commentary import commentary_payload
    except ImportError:
        from formal_commentary import commentary_payload
    commentary_json = json.dumps(commentary_payload(report), ensure_ascii=False, indent=2)
    payload = json.dumps(asdict(report), ensure_ascii=False, indent=2, default=str)
    acceptance['validator_version'] = 'formal-review-commentary-v1'
    acceptance['artifact_sha256'] = {
        'report.json': hashlib.sha256(payload.encode('utf-8')).hexdigest(),
        'report.md': hashlib.sha256(markdown.encode('utf-8')).hexdigest(),
        'commentary.json': hashlib.sha256(commentary_json.encode('utf-8')).hexdigest(),
    }
    (directory / 'report.md').write_text(markdown, encoding='utf-8')
    (directory / 'report.json').write_text(payload, encoding='utf-8')
    (directory / 'commentary.json').write_text(commentary_json, encoding='utf-8')
    (directory / 'acceptance.json').write_text(json.dumps(acceptance, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    write_findings(report, acceptance, directory, source_bundle)
    if source_bundle is not None:
        (directory / 'source_inputs.json').write_text(json.dumps(source_bundle, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    return {'report_path': str((directory / 'report.md').resolve()),
            'commentary_path': str((directory / 'commentary.json').resolve()),
            'report_data_path': str((directory / 'report.json').resolve()),
            'acceptance_path': str((directory / 'acceptance.json').resolve()),
            'findings_path': str((directory / 'review_findings.md').resolve()),
            'final_report_accepted': acceptance['final_report_accepted'],
            'technical_validation_passed': acceptance['technical_validation_passed'],
            'rendered_validation_passed': True}


def validate_saved_artifacts(directory):
    """Reject stale validation receipts as well as numerically stale reports."""
    try:
        from .review_workflow import restore_report
    except ImportError:
        from review_workflow import restore_report
    directory = Path(directory)
    acceptance = load_json(directory / 'acceptance.json')
    for name in ('report.json', 'report.md', 'commentary.json'):
        expected = acceptance.get('artifact_sha256', {}).get(name)
        if expected != hashlib.sha256((directory / name).read_bytes()).hexdigest():
            raise ValueError('Stale artifact validation receipt: ' + name)
    report = restore_report(json.loads((directory / 'report.json').read_text(encoding='utf-8')))
    try:
        from .formal_commentary import commentary_payload
    except ImportError:
        from formal_commentary import commentary_payload
    if json.loads((directory/'commentary.json').read_text(encoding='utf-8')) != commentary_payload(report):
        raise ValueError('Saved commentary differs from canonical narratives')
    verified = acceptance_result(report, acceptance.get('reconciliations', ()), acceptance.get('source_limitations', ()))
    validate_rendered_report(report, (directory / 'report.md').read_text(encoding='utf-8'))
    if (acceptance.get('final_report_accepted') != verified['substantive_data_ready']
            or acceptance.get('rendered_validation_passed') is not True):
        raise ValueError('Saved acceptance differs from independently verified report')
    return report, acceptance

"""Present the canonical formal report without a second financial calculation path."""
from dataclasses import asdict
from decimal import Decimal
from html import escape
import json
from pathlib import Path

try:
    from .loan_review_contract import METRICS, AVAILABLE
except ImportError:
    from loan_review_contract import METRICS, AVAILABLE


def report_payload(report):
    return json.loads(json.dumps(asdict(report), default=str, ensure_ascii=False))


FORMULA_METADATA = {
    'CF-01': ('流動比率', 'current_assets / current_liabilities', '流動資產 ÷ 流動負債'),
    'CF-02': ('速動比率', 'quick_assets / current_liabilities', '速動資產 ÷ 流動負債'),
    'CF-03': ('現金流量比率', 'cfo / current_liabilities', '營業活動現金流 ÷ 流動負債'),
    'CF-04': ('利息保障倍數', '(pretax_profit + interest_expense) / (interest_expense + capitalized_interest)', '（稅前利益＋利息費用）÷（利息費用＋資本化利息）'),
    'CF-09': ('營收成長率', '(current_revenue - prior_revenue) / prior_revenue', '（本期營收－前期營收）÷ 前期營收'),
    'CF-16': ('營業利益率變動', 'current_operating_margin - prior_operating_margin', '本期營業利益率－前期營業利益率'),
    'DER-01': ('毛利', 'revenue - cost_of_revenue', '營業收入－營業成本'),
    'DER-02': ('營業成本', 'revenue - gross_profit', '營業收入－毛利'),
    'DER-03': ('營業費用', 'gross_profit - operating_income', '毛利－營業利益'),
    'DER-04': ('營業利益', 'gross_profit - operating_expense', '毛利－營業費用'),
    'DER-05': ('稅前利益', 'net_income + income_tax', '稅後淨利＋所得稅費用／利益'),
    'DER-06': ('稅後淨利', 'pretax_profit - income_tax', '稅前利益－所得稅費用／利益'),
}

IDENTITY_CANDIDATES = {
    'DER-01': ('gross_profit', ('revenue', 'cost_of_revenue')),
    'DER-02': ('cost_of_revenue', ('revenue', 'gross_profit')),
    'DER-03': ('operating_expense', ('gross_profit', 'operating_income')),
    'DER-04': ('operating_income', ('gross_profit', 'operating_expense')),
    'DER-05': ('pretax_profit', ('net_income', 'income_tax')),
    'DER-06': ('net_income', ('pretax_profit', 'income_tax')),
}


def lineage_evidence_ids(report, fid, seen=None):
    """Return all direct and recursively referenced evidence IDs for a Fact."""
    seen = set() if seen is None else seen
    if fid in seen: return set()
    seen.add(fid)
    fact = report.facts[fid]
    result = set(fact.evidence_ids)
    for child in fact.input_ids:
        result.update(lineage_evidence_ids(report, child, seen))
    return result


def identity_candidates(report, formulas):
    """List evidence needing human review; never infer or approve an identity."""
    rows = []
    for formula in sorted(formulas):
        target_metric, metrics = IDENTITY_CANDIDATES[formula]
        for period in report.periods:
            target = report.facts.get(target_metric + ':' + period.role)
            if target is None: continue
            ids = [metric + ':' + period.role if metric + ':' + period.role in report.facts
                   else 'input:' + metric + ':' + period.role for metric in metrics]
            inputs = [report.facts.get(fid) for fid in ids]
            ready = all(f is not None and f.status in AVAILABLE and
                        (f.start, f.end, f.unit, f.currency, f.entity, f.scope) ==
                        (target.start, target.end, target.unit, target.currency, target.entity, target.scope)
                        for f in inputs)
            state = ('already_available' if target.status in AVAILABLE else
                     'blocked_target_' + target.status.value if target.status.value in ('conflict', 'not_applicable') else
                     'review_required' if ready else 'missing_or_incompatible_inputs')
            rows.append(dict(formula_id=formula, fact_id=target.id, period_role=period.role,
                period_start=target.start.isoformat() if target.start else None,
                period_end=target.end.isoformat() if target.end else None, status=state,
                input_ids=ids, inputs=[dict(fact_id=fid, status=f.status.value if f else 'missing',
                    value=str(f.value) if f and f.value is not None else None,
                    unit=f.unit if f else None, currency=f.currency if f else None,
                    evidence_ids=sorted(lineage_evidence_ids(report, fid)) if f else [],
                    sources=[dict(id=eid, source_id=report.evidence[eid].source_id,
                                  version=report.evidence[eid].version, locator=report.evidence[eid].locator,
                                  document_date=report.evidence[eid].document_date.isoformat())
                             for eid in sorted(lineage_evidence_ids(report, fid))] if f else [])
                    for fid, f in zip(ids, inputs)],
                note='僅供人工審查；不代表恆等式、公司範圍或定義已獲核准。'))
    return rows


def calculation_metadata(report, run_id, requested_formulas=()):
    """Build business-readable lineage from Report; never recalculate a value."""
    result = []
    for fact in report.facts.values():
        formula = fact.formula_id or (METRICS[fact.metric].formula if fact.status not in AVAILABLE else None)
        if formula not in FORMULA_METADATA: continue
        version = formula.lower().replace('-', '') + ('-dbt-exact-v1' if formula.startswith('DER-') else '-dbt-4dp-half-even-v1')
        if fact.formula_version != version and not (fact.status not in AVAILABLE and formula in requested_formulas):
            continue
        evidence_ids = sorted(lineage_evidence_ids(report, fact.id))
        inputs = []
        for iid in fact.input_ids:
            item = report.facts[iid]
            inputs.append(dict(fact_id=iid, metric_id=item.metric, value=str(item.value) if item.value is not None else None,
                               unit=item.unit, currency=item.currency, scale=str(report.scale) if item.unit=='money' else '1',
                               period_role=item.period_role, start=item.start.isoformat() if item.start else None,
                               end=item.end.isoformat() if item.end else None,
                               evidence_ids=sorted(lineage_evidence_ids(report, iid))))
        name, expression, display = FORMULA_METADATA[formula]
        result.append(dict(run_id=run_id, entity=report.entity, scope=report.scope, fact_id=fact.id,
            metric_id=fact.metric, metric_name=name, formula_id=formula, formula_version=fact.formula_version or version,
            formula_expression=expression, formula_display=display, input_ids=list(fact.input_ids), inputs=inputs,
            source_evidence_ids=[eid for eid in evidence_ids if report.evidence[eid].kind=='fact'],
            definition_evidence_ids=[eid for eid in evidence_ids if report.evidence[eid].kind=='definition'],
            evidence_ids=evidence_ids, evidence=[dict(id=eid, kind=report.evidence[eid].kind,
                source_id=report.evidence[eid].source_id, version=report.evidence[eid].version,
                locator=report.evidence[eid].locator) for eid in evidence_ids],
            result_value=str(fact.value) if fact.value is not None else None, result_unit=fact.unit,
            calculation_status=fact.status.value, reason=fact.reason,
            period_role=fact.period_role, period_start=fact.start.isoformat() if fact.start else None,
            period_end=fact.end.isoformat() if fact.end else None))
    return result


def write_dashboard(report, acceptance, directory):
    """Static, self-contained HTML; every displayed cell is bound to a formal Fact."""
    e = lambda value: escape(str(value), quote=True)
    try:
        from .formal_commentary import narrative_display
    except ImportError:
        from formal_commentary import narrative_display
    parts = [f'<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>{e(report.entity)} Loan Review</title>',
        '<style>body{font:16px system-ui;margin:32px;color:#243746}table{border-collapse:collapse;width:100%;margin:20px 0}td,th{border:1px solid #cbd5e1;padding:10px;text-align:right}th:first-child,td:first-child{text-align:left}th{background:#e8eef4}details{margin:12px 0}.pending{color:#9a3412}small{color:#475569}</style>',
        f'<h1>{e(report.entity)} · Loan Review</h1>',
        f'<p>{e(report.mode.value)} · {e(report.currency)} · scale={e(report.scale)}</p>',
        '<h2>' + ('正式驗收通過' if acceptance['final_report_accepted'] else '待驗收覆核稿') + '</h2>',
        '<ul>' + ''.join(f'<li class="pending">{e(x)}</li>' for x in acceptance['acceptance_blockers']) + '</ul>']
    periods = {p.role: p.label for p in report.periods}
    for table in report.tables:
        parts += [f'<h2>{e(table.spec.heading)}</h2><table><thead><tr><th>科目</th>']
        parts += [f'<th>{e("營收占比 · " + periods[c[4:]] if c.startswith("pct:") else periods[c])}</th>' for c in table.spec.columns]
        parts.append('</tr></thead><tbody>')
        for metric, cells in zip(table.spec.rows, table.cells):
            parts.append(f'<tr><th>{e(METRICS[metric].label)}</th>')
            for fid in cells:
                f = report.facts[fid]
                if f.status in AVAILABLE and f.unit != 'text':
                    value = Decimal(f.value)
                    if f.unit in ('percent', 'percentage_points'): value *= 100
                    label = f'{abs(value):,.2f}'
                    if value < 0: label = '(' + label + ')'
                    label += '%' if f.unit == 'percent' else ' pp' if f.unit == 'percentage_points' else 'x' if f.unit == 'multiple' else ''
                else:
                    label = f.value if f.status in AVAILABLE else f.status.value
                parts.append(f'<td data-fact-id="{e(fid)}" data-status="{e(f.status.value)}" title="{e(f.reason or f.definition)}">{e(label)}</td>')
            parts.append('</tr>')
        parts.append('</tbody></table>')
        if table.spec.key == 'leverage':
            for role in table.spec.columns:
                debt = report.facts.get('total_debt:' + role)
                if debt and debt.status in AVAILABLE and debt.basis:
                    parts.append(f'<p><small>Total Debt 口徑 · {e(role)}：{e(debt.basis)}</small></p>')
        for narrative in table.narratives + table.notes:
            display=narrative_display(report,table.spec.key,narrative)
            parts.append(f'<p data-commentary-topic="{e(narrative.topic)}" data-reviewed="{str(display["reviewed"]).lower()}">{e(display["text"])}</p>')
            for source in display['sources']:
                parts.append(f'<blockquote data-evidence-id="{e(source["evidence_id"])}">{e(source["quote"])}<br>'
                    f'{e(source["source_id"])} · {e(source["document_date"])} · {e(source["locator"])}</blockquote>')
    parts.append('<h2>來源與計算依據</h2>')
    for f in report.facts.values():
        parts.append(f'<details><summary>{e(f.id)} · {e(f.status.value)}</summary><p>{e(f.definition)}</p><p>{e(f.reason)}</p><p>Formula: {e(f.formula_id or "—")} · Inputs: {e(", ".join(f.input_ids))}</p>')
        for eid in f.evidence_ids:
            evidence = report.evidence[eid]
            parts.append(f'<p>{e(eid)} · {e(evidence.locator)}<br>{e(evidence.content)}</p>')
        parts.append('</details>')
    parts.append('</html>')
    target = Path(directory) / 'dashboard.html'
    target.write_text('\n'.join(parts), encoding='utf-8')
    return str(target.resolve())

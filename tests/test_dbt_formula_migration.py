"""Formal formula migration through the Report interface; synthetic fixtures only."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from scripts.loan_review_contract import Evidence, Fact, METRICS, Status, ContractError
from scripts.loan_review_pipeline import calculate_report, validate_arithmetic, acceptance_result, write_report_artifacts
from scripts.review_cloud import ReviewCloud
from scripts.review_workflow import run_review, export_excel
from scripts.credit_memo_writer import render_loan_review
from scripts.review_outputs import calculation_metadata
from scripts.loan_review_sources import prepare_source_inputs
from test_loan_review_sources import entry, extract, request_fixture, rows_of
from test_loan_review_contract import complete_fixture, fixture, source_fact


class ShadowCliTests(unittest.TestCase):
    def test_shadow_all_selects_every_formula(self):
        from unittest.mock import patch
        from scripts import review_workflow as workflow
        with patch.object(workflow, 'run_review', return_value={'status': 'draft', 'final_report_accepted': False}) as run, \
             patch.object(workflow, 'load_json', return_value={}), \
             patch('builtins.print'):
            workflow.cli(['SYNTHETIC-ONLY', '--request', 'request.json', '--execution', 'cloud', '--dbt-shadow-all'])
        self.assertEqual(run.call_args.kwargs['formula_migrations'],
                         {f: 'shadow' for f in workflow.MIGRATED_FORMULAS | workflow.MIGRATED_IDENTITIES})


    def test_migration_preset_selects_receipts_and_marks_others_unavailable(self):
        from unittest.mock import patch
        from scripts import review_workflow as workflow
        with patch.object(workflow, 'run_review', return_value={'status': 'draft', 'final_report_accepted': False}) as run, \
             patch.object(workflow, 'load_json', return_value={}), patch('builtins.print'):
            workflow.cli(['SYNTHETIC-ONLY', '--request', 'request.json', '--execution', 'cloud',
                          '--dbt-migrate-all', '--shadow-receipt', 'CF-03=/tmp/shadow.json'])
        kwargs = run.call_args.kwargs
        self.assertTrue(kwargs['cf01_migration'])
        self.assertEqual(kwargs['formula_migrations']['CF-03'], 'dbt')
        self.assertEqual(kwargs['formula_migrations']['CF-02'], 'unavailable')
        self.assertEqual(kwargs['formula_migrations']['DER-05'], 'unavailable')
        self.assertEqual(kwargs['promotion_receipts'], {'CF-03': '/tmp/shadow.json'})

    def test_unverified_formula_does_not_call_python_or_cloud(self):
        report = calculate_report(ratio_fixture(), formula_backends={'CF-02': 'unverified'})
        fact = report.facts['quick_ratio:fy_t']
        self.assertIsNone(fact.value)
        self.assertIn('successful prior shadow', fact.reason)


class ShadowJobVerificationTests(unittest.TestCase):
    def test_valid_job_and_tampered_parameter(self):
        import hashlib
        from types import SimpleNamespace
        cloud = ReviewCloud(client=MagicMock())
        cloud._formula_sql = 'SELECT @a'
        digest = hashlib.sha256(cloud._formula_sql.encode()).hexdigest()
        params = dict(fact_id='fact', formula_id='CF-02', a='1', b='3', c=None)
        job = SimpleNamespace(project=cloud.project, state='DONE', error_result=None,
            job_type='query', query=cloud._formula_sql,
            query_parameters=[SimpleNamespace(name=k, value=v) for k, v in params.items()])
        cloud.client.get_job.return_value = job
        observation = dict(fact_id='fact', calculation_locator='bigquery-job:job',
            trace=dict(formula_id='CF-02', input_values=['1', '3'], sql_sha256=digest))
        cloud.verify_shadow_evidence(observation)
        job.query_parameters[2].value = '2'
        with self.assertRaisesRegex(ValueError, 'parameters'):
            cloud.verify_shadow_evidence(observation)
        observation['trace']['sql_sha256'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'SQL'):
            cloud.verify_shadow_evidence(observation)


def ratio_fixture():
    report = complete_fixture('annual')
    role = 'fy_t'
    period = report.periods[-1]
    for metric, amount in [('quick_assets', 1), ('current_liabilities', 3)]:
        fid = 'input:' + metric + ':' + role
        fact = Fact(fid, metric, role, Status.MISSING, None, 'money', METRICS[metric].definition,
                    None, period.end, report.entity, report.scope, currency=report.currency,
                    reason='missing', period_kind='instant')
        report = replace(report, facts={**report.facts, fid: fact})
        report = source_fact(report, fid, Decimal(amount))
    target = report.facts['quick_ratio:' + role]
    target = replace(target, status=Status.NOT_CALCULABLE, value=None, original_value=None,
                     original_scale=None, evidence_ids=(), reason='pending')
    return replace(report, facts={**report.facts, target.id: target})


def source_input(report, metric, role, amount):
    fid = 'input:' + metric + ':' + role
    period = next(p for p in report.periods if p.role == role)
    spec = METRICS[metric]
    start = None if spec.period_kind == 'instant' else period.start
    f = Fact(fid, metric, role, Status.MISSING, None, 'money', spec.definition, start, period.end,
             report.entity, report.scope, currency=report.currency, reason='missing',
             period_kind='instant' if start is None else 'duration')
    return source_fact(replace(report, facts={**report.facts, fid: f}), fid, Decimal(amount))


def missing_target(report, metric, role='fy_t'):
    target = report.facts[metric + ':' + role]
    target = replace(target, status=Status.NOT_CALCULABLE, value=None, original_value=None,
                     original_scale=None, evidence_ids=(), reason='pending')
    return replace(report, facts={**report.facts, target.id: target})


class KnownWarehouse:
    def __init__(self, expected): self.expected = expected
    def calculate_formula(self, request):
        raw = self.expected[request['fact_id']]
        return dict(status='calculated', fact_id=request['fact_id'], formula_id=request['formula_id'],
                    input_values=request['input_values'], raw_value=raw, value=raw,
                    job_id='synthetic', sql_sha256='a'*64)


class FormulaMigrationTests(unittest.TestCase):
    def test_cf02_adopts_verified_warehouse_result_with_lineage(self):
        class Warehouse:
            def calculate_formula(self, request):
                return dict(status='calculated', fact_id=request['fact_id'], formula_id='CF-02',
                            input_values=request['input_values'], raw_value='0.33333333333333333333333333333333333333',
                            value='0.3333', job_id='synthetic', sql_sha256='a'*64)
        report = calculate_report(ratio_fixture(), formula_backends={'CF-02': Warehouse()})
        fact = report.facts['quick_ratio:fy_t']
        self.assertEqual(fact.value, Decimal('0.3333'))
        self.assertEqual(fact.formula_id, 'CF-02')
        self.assertEqual(fact.formula_version, 'cf02-dbt-4dp-half-even-v1')
        self.assertEqual(tuple(report.facts[i].metric for i in fact.input_ids), ('quick_assets', 'current_liabilities'))
        self.assertIn(fact.id, validate_arithmetic(report))
        metadata = calculation_metadata(report, 'a'*32)
        item = next(m for m in metadata if m['fact_id'] == fact.id)
        self.assertEqual(item['formula_display'], '速動資產 ÷ 流動負債')
        self.assertEqual([v['value'] for v in item['inputs']], ['1', '3'])
        self.assertEqual(item['run_id'], 'a'*32)
        self.assertTrue(item['source_evidence_ids'])

    def test_offline_selected_formula_is_unavailable_without_fallback(self):
        report = calculate_report(ratio_fixture(), formula_backends={'CF-02': 'offline'})
        fact = report.facts['quick_ratio:fy_t']
        self.assertEqual(fact.status, Status.NOT_CALCULABLE)
        self.assertIsNone(fact.value)
        self.assertIn('Offline mode', fact.reason)

    def test_zero_denominator_does_not_call_warehouse(self):
        class Warehouse:
            def calculate_formula(self, request): raise AssertionError('must not call warehouse')
        report = ratio_fixture()
        report = source_fact(report, 'input:current_liabilities:fy_t', Decimal(0))
        result = calculate_report(report, formula_backends={'CF-02': Warehouse()})
        self.assertEqual(result.facts['quick_ratio:fy_t'].status, Status.NOT_CALCULABLE)
        self.assertIn('Zero divisor', result.facts['quick_ratio:fy_t'].reason)

    def test_unapproved_definitions_never_call_warehouse(self):
        class Warehouse:
            def calculate_formula(self, request): raise AssertionError('unapproved formula must not execute')
        result = calculate_report(fixture(), formula_backends={'CF-02': Warehouse(), 'CF-04': Warehouse()})
        self.assertIn('DEC-01', result.facts['quick_ratio:fy_t'].reason)
        self.assertIn('DEC-05', result.facts['interest_coverage:fy_t'].reason)

    def test_wrong_warehouse_result_fails_without_python_fallback(self):
        class Warehouse:
            def calculate_formula(self, request):
                return dict(status='calculated', fact_id=request['fact_id'], formula_id='CF-02',
                    input_values=request['input_values'], raw_value='0.4', value='0.4',
                    job_id='synthetic', sql_sha256='a'*64)
        with self.assertRaisesRegex(ValueError, 'verification'):
            calculate_report(ratio_fixture(), formula_backends={'CF-02': Warehouse()})

    def test_shadow_compares_dbt_but_preserves_python_report_value(self):
        class Warehouse:
            def calculate_formula(self, request):
                return dict(status='calculated', fact_id=request['fact_id'], formula_id='CF-02',
                    input_values=request['input_values'], raw_value='0.33333333333333333333333333333333333333',
                    value='0.3333', job_id='synthetic', sql_sha256='a'*64)
        observations = []
        report = calculate_report(ratio_fixture(), formula_backends={
            'CF-02': {'mode':'shadow', 'backend':Warehouse(), 'observations':observations}})
        self.assertEqual(report.facts['quick_ratio:fy_t'].value, Decimal('0.3333333333333333333333333333'))
        comparison = next(o for o in observations if o['fact_id'] == 'quick_ratio:fy_t')
        self.assertEqual(comparison['dbt_value'], '0.3333')
        self.assertEqual(comparison['comparison'], 'matched')
        self.assertNotEqual(report.facts['quick_ratio:fy_t'].formula_version, 'cf02-dbt-4dp-half-even-v1')

    def test_shadow_workflow_records_comparison_outside_formal_report(self):
        class Cloud:
            def ensure(self): pass
            def stage_bundle(self, run_id, entity, bundle): return bundle
            def record_run(self, run_id, entity, manifest): pass
            def publish(self, *args): raise AssertionError('draft cannot publish')
            def calculate_formula(self, request):
                return dict(status='calculated', fact_id=request['fact_id'], formula_id=request['formula_id'],
                    input_values=request['input_values'], raw_value='4', value='4', job_id='test', sql_sha256='a'*64)
            def verify_shadow_evidence(self, observation): return None
        financial = extract({
            'NetCashProvidedByUsedInOperatingActivities': {'units': {'USD': [entry(12000000)]}},
            'LiabilitiesCurrent': {'units': {'USD': [entry(3000000, start=None)]}},
        })
        snapshot = dict(schema_version=1, entity='SYNTHETIC-ONLY', cik='0000000001', financial_facts=financial)
        with tempfile.TemporaryDirectory() as tmp:
            manifest = run_review('SYNTHETIC-ONLY', request_fixture(), source='snapshot', snapshot=snapshot,
                execution='cloud', cloud=Cloud(), output_dir=tmp, excel=False, commentary_mode='off',
                formula_migrations={'CF-03':'shadow'})
            self.assertEqual(manifest['status'], 'draft', manifest.get('errors'))
            report = json.loads(Path(tmp, 'report.json').read_text())
            shadow = json.loads(Path(tmp, 'formula_shadow.json').read_text())
            fact = report['facts']['cash_flow_ratio:fy_t']
            self.assertEqual(fact['value'], '4')
            self.assertNotEqual(fact['formula_version'], 'cf03-dbt-4dp-half-even-v1')
            self.assertEqual(next(r for r in shadow['results'] if r['fact_id']=='cash_flow_ratio:fy_t')['comparison'], 'matched')
            with tempfile.TemporaryDirectory() as next_dir:
                with self.assertRaisesRegex(ValueError, 'shadow receipt'):
                    run_review('SYNTHETIC-ONLY', request_fixture(), source='snapshot', snapshot=snapshot,
                        execution='cloud', cloud=Cloud(), output_dir=next_dir, excel=False, commentary_mode='off',
                        formula_migrations={'CF-03':'dbt'})
                promoted = run_review('SYNTHETIC-ONLY', request_fixture(), source='snapshot', snapshot=snapshot,
                    execution='cloud', cloud=Cloud(), output_dir=next_dir, excel=False, commentary_mode='off',
                    formula_migrations={'CF-03':'dbt'}, promotion_receipts={'CF-03':manifest['manifest_path']})
                self.assertEqual(promoted['status'], 'draft', promoted.get('errors'))
                promoted_fact = json.loads(Path(next_dir, 'report.json').read_text())['facts']['cash_flow_ratio:fy_t']
                self.assertEqual(promoted_fact['formula_version'], 'cf03-dbt-4dp-half-even-v1')

    def test_cloud_unverified_formula_remains_unavailable_in_report(self):
        class Cloud:
            def ensure(self): pass
            def stage_bundle(self, run_id, entity, bundle): return bundle
            def record_run(self, run_id, entity, manifest): pass
            def publish(self, *args): raise AssertionError('draft cannot publish')
            def calculate_formula(self, request): raise AssertionError('unverified formula must not run')
        financial = extract({
            'NetCashProvidedByUsedInOperatingActivities': {'units': {'USD': [entry(12000000)]}},
            'LiabilitiesCurrent': {'units': {'USD': [entry(3000000, start=None)]}},
        })
        source = dict(schema_version=1, entity='SYNTHETIC-ONLY', cik='0000000001', financial_facts=financial)
        with tempfile.TemporaryDirectory() as tmp:
            result = run_review('SYNTHETIC-ONLY', request_fixture(), source='snapshot', snapshot=source,
                execution='cloud', cloud=Cloud(), output_dir=tmp, excel=False, commentary_mode='off',
                formula_migrations={'CF-03': 'unavailable'}, cf01_migration=False)
            self.assertEqual(result['status'], 'draft', result.get('errors'))
            fact = json.loads(Path(tmp, 'report.json').read_text())['facts']['cash_flow_ratio:fy_t']
            self.assertIsNone(fact['value'])
            self.assertIn('successful prior shadow', fact['reason'])

    def test_offline_migration_exposes_unavailable_business_metadata(self):
        financial = extract({
            'NetCashProvidedByUsedInOperatingActivities': {'units': {'USD': [entry(12000000)]}},
            'LiabilitiesCurrent': {'units': {'USD': [entry(3000000, start=None)]}},
        })
        snapshot = dict(schema_version=1, entity='SYNTHETIC-ONLY', cik='0000000001', financial_facts=financial)
        with tempfile.TemporaryDirectory() as tmp:
            manifest = run_review('SYNTHETIC-ONLY', request_fixture(), source='snapshot', snapshot=snapshot,
                output_dir=tmp, excel=False, commentary_mode='off', formula_migrations={'CF-03':'dbt'})
            metadata = json.loads(Path(manifest['outputs']['formula_metadata_path']).read_text())
            item = next(m for m in metadata['calculations'] if m['fact_id']=='cash_flow_ratio:fy_t')
            self.assertEqual(item['calculation_status'], 'not_calculable')
            self.assertEqual(item['formula_display'], '營業活動現金流 ÷ 流動負債')
            self.assertIsNone(item['result_value'])
            self.assertIn('Offline mode', item['reason'])

    def test_warehouse_rejects_lossy_input_before_returning_result(self):
        client = MagicMock()
        client.query.return_value.result.return_value = [dict(fact_id='fixture', formula_id='CF-02',
            a_cast='0', b_cast='1', c_cast=None, raw_value='0', value='0')]
        cloud = ReviewCloud(project='test', dataset='test', client=client)
        cloud._formula_sql = 'select @fact_id'
        request = dict(fact_id='fixture', formula_id='CF-02', input_values=['1E-39', '1'])
        with self.assertRaisesRegex(ValueError, 'lossy'):
            cloud.calculate_formula(request)

    def test_cf03_uses_ytd_cfo_and_end_of_period_liabilities(self):
        report = complete_fixture('annual')
        report = source_fact(report, 'cfo:fy_t', Decimal('12'))
        report = source_input(report, 'current_liabilities', 'fy_t', '3')
        report = missing_target(report, 'cash_flow_ratio')
        result = calculate_report(report, formula_backends={'CF-03': KnownWarehouse({'cash_flow_ratio:fy_t':'4'})})
        fact = result.facts['cash_flow_ratio:fy_t']
        self.assertEqual(fact.value, Decimal('4.0000'))
        self.assertEqual(result.facts[fact.input_ids[0]].start, result.periods[-1].start)
        self.assertIsNone(result.facts[fact.input_ids[1]].start)
        text = render_loan_review(result, {'final_report_accepted':False, 'reconciliations':[]})
        self.assertIn('營業活動現金流 ÷ 流動負債', text)
        self.assertIn('cf03-dbt-4dp-half-even-v1', text)

    def test_cf04_includes_capitalized_interest(self):
        report = complete_fixture('annual')
        report = source_fact(report, 'pretax_profit:fy_t', Decimal('100'))
        report = source_fact(report, 'interest_expense:fy_t', Decimal('20'))
        report = source_input(report, 'capitalized_interest', 'fy_t', '10')
        report = missing_target(report, 'interest_coverage')
        result = calculate_report(report, formula_backends={'CF-04': KnownWarehouse({'interest_coverage:fy_t':'4'})})
        self.assertEqual(result.facts['interest_coverage:fy_t'].value, Decimal('4.0000'))

    def test_cf03_excel_matches_rounded_formal_result(self):
        class Warehouse:
            def calculate_formula(self, request):
                return dict(status='calculated', fact_id=request['fact_id'], formula_id='CF-03',
                    input_values=request['input_values'], raw_value='0.33333333333333333333333333333333333333',
                    value='0.3333', job_id='synthetic', sql_sha256='a'*64)
        report = complete_fixture('annual')
        report = source_fact(report, 'cfo:fy_t', Decimal('1'))
        report = source_input(report, 'current_liabilities', 'fy_t', '3')
        report = missing_target(report, 'cash_flow_ratio')
        report = calculate_report(report, formula_backends={'CF-03': Warehouse()})
        with tempfile.TemporaryDirectory() as tmp:
            write_report_artifacts(report, acceptance_result(report), tmp)
            self.assertTrue(Path(export_excel(tmp)).is_file())

    def test_cf09_compares_exact_prior_annual_period(self):
        report = complete_fixture('annual')
        report = source_fact(report, 'revenue:fy_t', Decimal('120'))
        values = {'revenue_growth:fy_t_minus_2':'0', 'revenue_growth:fy_t_minus_1':'0', 'revenue_growth:fy_t':'0.2'}
        result = calculate_report(report, formula_backends={'CF-09': KnownWarehouse(values)})
        fact = result.facts['revenue_growth:fy_t']
        self.assertEqual(fact.value, Decimal('0.2000'))
        self.assertEqual(fact.input_ids, ('revenue:fy_t', 'revenue:fy_t_minus_1'))

    def test_cf09_quarterly_uses_comparable_ytd_not_previous_year_end(self):
        report = complete_fixture('quarterly')
        report = source_fact(report, 'revenue:current_interim', Decimal('120'))
        result = calculate_report(report, formula_backends={'CF-09': KnownWarehouse({'revenue_growth:current_interim':'0.2'})})
        fact = result.facts['revenue_growth:current_interim']
        self.assertEqual(fact.value, Decimal('0.2000'))
        self.assertEqual(fact.input_ids, ('revenue:current_interim', 'revenue:prior_interim'))
        self.assertEqual(result.facts[fact.input_ids[0]].start, date(2025,1,1))

    def test_cf16_uses_operating_margin_upstream(self):
        report = complete_fixture('annual')
        report = source_fact(report, 'operating_income:fy_t', Decimal('30'))
        for role in ('fy_t_minus_3', 'fy_t_minus_2', 'fy_t_minus_1'):
            report = source_fact(report, 'operating_income:' + role, Decimal('20'))
        values = {'operating_margin_change:fy_t_minus_2':'0', 'operating_margin_change:fy_t_minus_1':'0',
                  'operating_margin_change:fy_t':'0.1'}
        result = calculate_report(report, formula_backends={'CF-16': KnownWarehouse(values)})
        fact = result.facts['operating_margin_change:fy_t']
        self.assertEqual(fact.value, Decimal('0.1000'))
        self.assertEqual(fact.input_ids, ('operating_margin:fy_t', 'operating_margin:fy_t_minus_1'))

    def test_der05_requires_reviewed_identity_then_uses_warehouse(self):
        report = fixture()
        report = source_fact(report, 'net_income:fy_t', Decimal('80'))
        report = source_fact(report, 'income_tax:fy_t', Decimal('-20'))
        review = dict(fact_id='pretax_profit:fy_t', formula_id='DER-05',
            input_ids=('net_income:fy_t', 'income_tax:fy_t'), formula_version='der05-dbt-exact-v1',
            definition_evidence_id='bridge')
        backend = KnownWarehouse({'pretax_profit:fy_t':'60'})
        with self.assertRaisesRegex(ContractError, 'CALC-14'):
            calculate_report(report, identity_reviews=[review], identity_backends={'DER-05':backend})
        e = Evidence('bridge', report.entity, report.scope, 'synthetic-review', '1', date(2026, 1, 1),
            'fixture://review', 'definition', ('fy_t',), 'Reviewed same-scope tax identity')
        report = replace(report, evidence={**report.evidence, e.id:e})
        result = calculate_report(report, identity_reviews=[review], identity_backends={'DER-05':backend})
        fact = result.facts['pretax_profit:fy_t']
        self.assertEqual(fact.value, Decimal('60'))
        self.assertIn('bridge', fact.evidence_ids)
        self.assertEqual(fact.formula_version, 'der05-dbt-exact-v1')
        self.assertIn(fact.id, validate_arithmetic(result))
        text = render_loan_review(result, {'final_report_accepted':False, 'reconciliations':[]})
        self.assertIn('稅後淨利＋所得稅費用／利益', text)
        self.assertIn('fixture://review', text)
        with tempfile.TemporaryDirectory() as tmp:
            write_report_artifacts(result, acceptance_result(result), tmp)
            self.assertTrue(Path(export_excel(tmp)).is_file())

    def test_der05_shadow_keeps_original_result_and_records_trial(self):
        report = fixture()
        report = source_fact(report, 'net_income:fy_t', Decimal('80'))
        report = source_fact(report, 'income_tax:fy_t', Decimal('-20'))
        evidence = Evidence('bridge', report.entity, report.scope, 'synthetic-review', '1', date(2026,1,1),
                            'fixture://review', 'definition', ('fy_t',), 'Reviewed same-scope tax identity')
        report = replace(report, evidence={**report.evidence, evidence.id:evidence})
        review = dict(fact_id='pretax_profit:fy_t', formula_id='DER-05',
            input_ids=('net_income:fy_t', 'income_tax:fy_t'), formula_version='legacy-identity-v1',
            definition_evidence_id='bridge')
        observations = []
        result = calculate_report(report, identity_reviews=[review], identity_backends={
            'DER-05': {'mode':'shadow', 'backend':KnownWarehouse({'pretax_profit:fy_t':'60'}),
                       'observations':observations}})
        self.assertEqual(result.facts['pretax_profit:fy_t'].formula_version, 'legacy-identity-v1')
        self.assertEqual(result.facts['pretax_profit:fy_t'].value, Decimal(60))
        self.assertEqual(observations[0]['comparison'], 'matched')

    def test_all_reviewed_der_formulas_use_exact_input_order_and_lineage(self):
        examples = [
            ('DER-01', 'gross_profit', ('revenue', 'cost_of_revenue'), (500, 320), '180'),
            ('DER-02', 'cost_of_revenue', ('revenue', 'gross_profit'), (500, 180), '320'),
            ('DER-03', 'operating_expense', ('gross_profit', 'operating_income'), (180, 120), '60'),
            ('DER-04', 'operating_income', ('gross_profit', 'operating_expense'), (180, 60), '120'),
            ('DER-05', 'pretax_profit', ('net_income', 'income_tax'), (80, -20), '60'),
            ('DER-06', 'net_income', ('pretax_profit', 'income_tax'), (60, -20), '80'),
        ]
        for formula, target, metrics, values, expected in examples:
            with self.subTest(formula=formula):
                report = fixture()
                for metric, value in zip(metrics, values):
                    report = source_fact(report, metric+':fy_t', Decimal(value))
                evidence = Evidence('bridge', report.entity, report.scope, 'synthetic-review', '1', date(2026,1,1),
                    'fixture://review', 'definition', ('fy_t',), 'Reviewed same-scope identity')
                report = replace(report, evidence={**report.evidence, evidence.id:evidence})
                review = dict(fact_id=target+':fy_t', formula_id=formula,
                    input_ids=tuple(metric+':fy_t' for metric in metrics),
                    formula_version=formula.lower().replace('-', '')+'-dbt-exact-v1', definition_evidence_id='bridge')
                result = calculate_report(report, identity_reviews=[review],
                    identity_backends={formula:KnownWarehouse({review['fact_id']:expected})})
                fact = result.facts[review['fact_id']]
                self.assertEqual(fact.value, Decimal(expected))
                self.assertEqual(fact.input_ids, review['input_ids'])
                self.assertIn('bridge', fact.evidence_ids)

    def test_der_selection_without_review_lists_candidates_without_deriving(self):
        class Cloud:
            def ensure(self): pass
            def stage_bundle(self, run_id, entity, bundle): return bundle
            def record_run(self, run_id, entity, manifest): pass
            def publish(self, *args): raise AssertionError('draft cannot publish')
            def calculate_formula(self, request): raise AssertionError('unreviewed DER must not run')
        financial = extract({
            'NetIncomeLoss': {'units': {'USD': [entry(80000000)]}},
            'IncomeTaxExpenseBenefit': {'units': {'USD': [entry(-20000000)]}},
        })
        snapshot = dict(schema_version=1, entity='SYNTHETIC-ONLY', cik='0000000001', financial_facts=financial)
        with tempfile.TemporaryDirectory() as tmp:
            manifest = run_review('SYNTHETIC-ONLY', request_fixture(), source='snapshot', snapshot=snapshot,
                execution='cloud', cloud=Cloud(), output_dir=tmp, excel=False, commentary_mode='off',
                formula_migrations={'DER-05':'shadow'})
            self.assertEqual(manifest['status'], 'draft', manifest.get('errors'))
            report = json.loads(Path(tmp, 'report.json').read_text())
            self.assertIsNone(report['facts']['pretax_profit:fy_t']['value'])
            inventory = json.loads(Path(tmp, 'identity_candidates.json').read_text())
            row = next(r for r in inventory['candidates'] if r['formula_id']=='DER-05' and r['period_role']=='fy_t')
            self.assertEqual(row['status'], 'review_required')

    def test_explicit_reviewed_der_shadow_keeps_direct_workflow_authority(self):
        class Cloud:
            def ensure(self): pass
            def verify_shadow_evidence(self, observation): pass
            def stage_bundle(self, run_id, entity, bundle): return bundle
            def record_run(self, run_id, entity, manifest): pass
            def publish(self, *args): raise AssertionError('draft cannot publish')
            def calculate_formula(self, request):
                return dict(status='calculated', fact_id=request['fact_id'], formula_id=request['formula_id'],
                    input_values=request['input_values'], raw_value='60', value='60', job_id='test', sql_sha256='a'*64)
        financial = extract({
            'NetIncomeLoss': {'units': {'USD': [entry(80000000)]}},
            'IncomeTaxExpenseBenefit': {'units': {'USD': [entry(-20000000)]}},
        })
        snapshot = dict(schema_version=1, entity='SYNTHETIC-ONLY', cik='0000000001', financial_facts=financial)
        source_bundle = prepare_source_inputs(request_fixture(), rows_of(financial))
        bindings = [dict(fact_id=fid, value=source_bundle['inputs'][fid]['value'],
                         evidence_ids=source_bundle['inputs'][fid]['evidence_ids'])
                    for fid in ('net_income:fy_t', 'income_tax:fy_t')]
        reviews = dict(schema_version=1, entity='SYNTHETIC-ONLY', cik='0000000001', scope='entity-wide',
            as_of='2026-10-20', currency='USD', scale='1000000', mode='annual',
            reviews=[dict(fact_id='pretax_profit:fy_t', formula_id='DER-05',
                input_ids=['net_income:fy_t', 'income_tax:fy_t'],
                period_start='2025-01-01', period_end='2025-12-31',
                bindings=bindings,
                evidence=dict(id='review:tax', source_id='synthetic', version='v1', document_date='2026-02-01',
                    locator='fixture://tax', content='Reviewed same consolidated tax and income scope.'),
                verification=dict(verified_by='Synthetic reviewer', verified_at='2026-03-01', method='checked_against_source'))])
        with tempfile.TemporaryDirectory() as root:
            tmp = str(Path(root, 'shadow'))
            manifest = run_review('SYNTHETIC-ONLY', request_fixture(), source='snapshot', snapshot=snapshot,
                execution='cloud', cloud=Cloud(), output_dir=tmp, excel=False, commentary_mode='off',
                formula_migrations={'DER-05':'shadow'}, identity_review_bundle=reviews)
            self.assertEqual(manifest['status'], 'draft', manifest.get('errors'))
            facts = json.loads(Path(tmp, 'report.json').read_text())['facts']
            self.assertEqual(facts['pretax_profit:fy_t']['value'], '60')
            self.assertNotEqual(facts['pretax_profit:fy_t']['formula_version'], 'der05-dbt-exact-v1')
            shadow = json.loads(Path(tmp, 'formula_shadow.json').read_text())
            self.assertEqual(shadow['results'][0]['comparison'], 'matched')
            receipt_path = manifest['manifest_path']
            tmp = str(Path(root, 'promoted'))
            manifest = run_review('SYNTHETIC-ONLY', request_fixture(), source='snapshot', snapshot=snapshot,
                execution='cloud', cloud=Cloud(), output_dir=tmp, excel=False, commentary_mode='off',
                formula_migrations={'DER-05':'dbt'}, identity_review_bundle=reviews,
                promotion_receipts={'DER-05':receipt_path})
            self.assertEqual(manifest['status'], 'draft', manifest.get('errors'))
            facts = json.loads(Path(tmp, 'report.json').read_text())['facts']
            self.assertEqual(facts['pretax_profit:fy_t']['formula_version'], 'der05-dbt-exact-v1')
            metadata = json.loads(Path(tmp, 'formula_metadata.json').read_text())['calculations']
            item = next(x for x in metadata if x['fact_id']=='pretax_profit:fy_t')
            self.assertIn('review:tax', item['definition_evidence_ids'])
        # A review is invalid if one of the bound source values changes.
        reviews['reviews'][0]['bindings'][0]['value'] = '81'
        with tempfile.TemporaryDirectory() as tmp:
            manifest = run_review('SYNTHETIC-ONLY', request_fixture(), source='snapshot', snapshot=snapshot,
                execution='cloud', cloud=Cloud(), output_dir=tmp, excel=False, commentary_mode='off',
                formula_migrations={'DER-05':'shadow'}, identity_review_bundle=reviews)
            self.assertEqual(manifest['status'], 'failed')
            self.assertIn('changed since review', manifest['errors']['pipeline'])
            self.assertFalse(Path(tmp, 'report.json').exists())


if __name__ == '__main__':
    unittest.main()

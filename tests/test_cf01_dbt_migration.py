"""CF-01 migration at the formal report interface; no live cloud data."""
from decimal import Decimal
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts import loan_review_pipeline as pipeline
from scripts import nexstar_workbook_review as workbooks
from scripts.loan_review_contract import Fact, METRICS, Status
from scripts.loan_review_sources import prepare_source_inputs
from test_loan_review_pipeline import fixture_book
from test_review_integration import snapshot
from test_loan_review_sources import entry, extract, request_fixture, rows_of
from scripts.review_workflow import run_review, export_excel
from scripts.credit_memo_writer import render_loan_review
from scripts.review_cloud import ReviewCloud
from scripts.review_outputs import report_payload
from test_pr_review_regressions import accepted_fixture
from test_loan_review_contract import source_fact


class CF01MigrationTests(unittest.TestCase):
    def setUp(self):
        book = fixture_book('E-loan YYYY_Nexstar 2025.xls')
        self.base, self.inputs, self.evidence = workbooks.build_inputs(book, 'annual')

    def test_offline_migrated_cf01_remains_unavailable(self):
        report, _ = pipeline.complete_report(self.base, self.inputs, self.evidence,
                                             cf01_backend='offline')
        fact = report.facts['current_ratio:fy_t']
        self.assertEqual(fact.status, Status.NOT_CALCULABLE)
        self.assertIsNone(fact.value)
        self.assertIn('offline', fact.reason.lower())

    def test_cloud_result_is_recorded_with_version_and_source_lineage(self):
        class SyntheticWarehouse:
            def calculate_cf01(self, request):
                if request['fact_id'] != 'current_ratio:fy_t':
                    return {'status': 'unavailable', 'reason': 'synthetic fixture omitted'}
                return {'status': 'calculated', 'fact_id': request['fact_id'],
                        'assets_cast': request['assets'], 'liabilities_cast': request['liabilities'],
                        'value': '2.0717', 'raw_value': '2.0717', 'job_id': 'synthetic', 'sql_sha256': 'a'*64}
        report, acceptance = pipeline.complete_report(self.base, self.inputs, self.evidence,
                                                       cf01_backend=SyntheticWarehouse())
        fact = report.facts['current_ratio:fy_t']
        self.assertEqual(fact.value, Decimal('2.0717'))
        self.assertEqual(fact.formula_version, 'cf01-dbt-4dp-half-even-v1')
        self.assertEqual(tuple(report.facts[i].metric for i in fact.input_ids),
                         ('current_assets', 'current_liabilities'))
        self.assertTrue(all(report.facts[i].evidence_ids for i in fact.input_ids))
        self.assertIn('raw_value', report.evidence[fact.evidence_ids[0]].content)
        self.assertIn(fact.id, pipeline.validate_arithmetic(report))
        text = render_loan_review(report, acceptance)
        self.assertIn('流動資產 ÷ 流動負債', text)
        self.assertIn('cf01-dbt-4dp-half-even-v1', text)

    def test_cloud_lossy_cast_never_becomes_an_accepted_ratio(self):
        class LossyWarehouse:
            def calculate_cf01(self, request):
                return {'status': 'calculated', 'fact_id': request['fact_id'],
                        'assets_cast': '0', 'liabilities_cast': request['liabilities'], 'value': '0'}
        with self.assertRaisesRegex(ValueError, 'lossy'):
            pipeline.complete_report(self.base, self.inputs, self.evidence, cf01_backend=LossyWarehouse())

    def test_cloud_adapter_rejects_lossy_bignumeric_input(self):
        client = MagicMock()
        client.query.return_value.result.return_value = [dict(fact_id='synthetic', assets_cast='0',
                                                              liabilities_cast='1', value='0', raw_value='0')]
        cloud = ReviewCloud(project='test', dataset='test', client=client)
        cloud._cf01_sql = 'select @fact_id'  # isolate the external query boundary
        with self.assertRaisesRegex(ValueError, 'lossy'):
            cloud.calculate_cf01(dict(fact_id='synthetic', assets='1E-39', liabilities='1'))

    def test_cloud_disagreement_never_uses_python_as_fallback(self):
        class IncorrectWarehouse:
            def calculate_cf01(self, request):
                return {'status': 'calculated', 'fact_id': request['fact_id'],
                        'assets_cast': request['assets'], 'liabilities_cast': request['liabilities'],
                        'value': '999', 'raw_value': '999'}
        with self.assertRaisesRegex(ValueError, 'verification'):
            pipeline.complete_report(self.base, self.inputs, self.evidence, cf01_backend=IncorrectWarehouse())

    def test_local_workflow_explicitly_marks_migrated_ratio_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_review('SYNTHETIC-ONLY', request_fixture(), source='snapshot',
                                snapshot=snapshot(), output_dir=tmp, excel=False,
                                commentary_mode='off', cf01_migration=True)
            self.assertEqual(result['cf01_engine'], 'offline_unavailable')
            facts = json.loads(Path(tmp, 'report.json').read_text())['facts']
            self.assertEqual(facts['current_ratio:fy_t']['status'], 'not_calculable')
            self.assertIn('Offline mode', facts['current_ratio:fy_t']['reason'])

    def test_cloud_workflow_writes_report_but_draft_is_not_published(self):
        class SyntheticCloud:
            def __init__(self): self.published = False
            def ensure(self): pass
            def stage_bundle(self, run_id, entity, bundle): return bundle
            def record_run(self, run_id, entity, manifest): pass
            def publish(self, *args): self.published = True
            def calculate_cf01(self, request):
                return {'status': 'calculated', 'fact_id': request['fact_id'],
                        'assets_cast': request['assets'], 'liabilities_cast': request['liabilities'],
                        'value': '0.3333', 'raw_value': '0.33333333333333333333333333333333333333',
                        'job_id': 'synthetic', 'sql_sha256': 'a'*64}
        financial = extract({
            'AssetsCurrent': {'units': {'USD': [entry(1000000, start=None)]}},
            'LiabilitiesCurrent': {'units': {'USD': [entry(3000000, start=None)]}},
        })
        source = {'schema_version': 1, 'entity': 'SYNTHETIC-ONLY', 'cik': '0000000001',
                  'financial_facts': financial}
        cloud = SyntheticCloud()
        with tempfile.TemporaryDirectory() as tmp:
            result = run_review('SYNTHETIC-ONLY', request_fixture(), source='snapshot', snapshot=source,
                output_dir=tmp, execution='cloud', cloud=cloud, excel=False, commentary_mode='off',
                cf01_migration=True)
            self.assertEqual(result['status'], 'draft', result.get('errors'))
            self.assertEqual(result['cf01_engine'], 'bigquery_dbt')
            self.assertFalse(cloud.published)
            fact = json.loads(Path(tmp, 'report.json').read_text())['facts']['current_ratio:fy_t']
            self.assertEqual(fact['value'], '0.3333')

    def test_publish_rechecks_dbt_result_before_writing(self):
        report, acceptance = accepted_fixture()
        period = report.periods[-1]
        for metric, amount in [('current_assets', 120), ('current_liabilities', 80)]:
            fid = 'input:' + metric + ':' + period.role
            fact = Fact(fid, metric, period.role, Status.MISSING, None, 'money', METRICS[metric].definition,
                        None, period.end, report.entity, report.scope, currency=report.currency,
                        reason='not populated', period_kind='instant')
            report = replace(report, facts={**report.facts, fid: fact})
            report = source_fact(report, fid, Decimal(amount))
        target = report.facts['current_ratio:fy_t']
        target = replace(target, status=Status.CALCULATED, value=Decimal('1.5000'),
            original_value=None, original_scale=None, evidence_ids=(), reason='',
            formula_id='CF-01', formula_version='cf01-dbt-4dp-half-even-v1',
            input_ids=('input:current_assets:fy_t', 'input:current_liabilities:fy_t'))
        report = replace(report, facts={**report.facts, target.id: target})
        # The user approved publication only under this existing dataset's settings.
        cloud = ReviewCloud(project='focus-heuristic-438521-v4', dataset='sec_raw', client=MagicMock())
        bad = {'status':'calculated', 'fact_id':target.id, 'assets_cast':'120',
               'liabilities_cast':'80', 'value':'1.4'}
        flags = {**acceptance, 'final_report_accepted':True, 'technical_validation_passed':True,
                 'rendered_validation_passed':True}
        gated = ReviewCloud(project='test', dataset='test', client=MagicMock())
        with patch.object(gated, 'write') as write:
            with self.assertRaisesRegex(ValueError, 'governance confirmation'):
                gated.publish('a'*32, report.entity, report_payload(report), flags)
            write.assert_not_called()
        with patch.object(cloud, 'calculate_cf01', return_value=bad), patch.object(cloud, 'write') as write:
            with self.assertRaisesRegex(ValueError, 'recheck failed'):
                cloud.publish('a'*32, report.entity, report_payload(report), flags)
            write.assert_not_called()
        good = {**bad, 'value': '1.5000'}
        with patch.object(cloud, 'calculate_cf01', return_value=good), patch.object(cloud, 'write') as write, patch.object(cloud, 'dbt'):
            cloud.publish('a'*32, report.entity, report_payload(report), flags)
            write.assert_called_once()

    def test_excel_export_preserves_verified_four_decimal_ratio(self):
        class SyntheticWarehouse:
            def calculate_cf01(self, request):
                if request['fact_id'] != 'current_ratio:fy_t':
                    return {'status': 'unavailable', 'reason': 'synthetic fixture omitted'}
                return {'status': 'calculated', 'fact_id': request['fact_id'],
                        'assets_cast': request['assets'], 'liabilities_cast': request['liabilities'],
                        'value': '2.0717', 'raw_value': '2.0717', 'job_id': 'synthetic', 'sql_sha256': 'a'*64}
        report, acceptance = pipeline.complete_report(self.base, self.inputs, self.evidence,
                                                       cf01_backend=SyntheticWarehouse())
        with tempfile.TemporaryDirectory() as tmp:
            pipeline.write_report_artifacts(report, acceptance, tmp)
            target = export_excel(tmp)
            self.assertTrue(Path(target).is_file())
            self.assertTrue(json.loads(Path(tmp, 'excel_validation.json').read_text())['passed'])

    def test_excel_even_rounding_on_positive_and_negative_ties(self):
        for assets, expected in [(123445, '1.2344'), (123455, '1.2346'), (-123445, '-1.2344')]:
            class SyntheticWarehouse:
                def calculate_cf01(self, request):
                    return {'status': 'calculated', 'fact_id': request['fact_id'],
                            'assets_cast': request['assets'], 'liabilities_cast': request['liabilities'],
                            'value': expected, 'raw_value': expected, 'job_id': 'synthetic', 'sql_sha256': 'a'*64}
            financial = extract({
                'AssetsCurrent': {'units': {'USD': [entry(assets * 1000000, start=None)]}},
                'LiabilitiesCurrent': {'units': {'USD': [entry(100000 * 1000000, start=None)]}},
            })
            bundle = prepare_source_inputs(request_fixture(), rows_of(financial))
            report, acceptance = pipeline.report_from_source_bundle(bundle, cf01_backend=SyntheticWarehouse())
            with self.subTest(assets=assets), tempfile.TemporaryDirectory() as tmp:
                pipeline.write_report_artifacts(report, acceptance, tmp, source_bundle=bundle)
                target = export_excel(tmp)
                self.assertTrue(Path(target).is_file())


if __name__ == '__main__':
    unittest.main()

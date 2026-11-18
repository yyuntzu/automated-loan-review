"""Opt-in real dbt/BigQuery synthetic integration; submits SELECT jobs only."""
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts.loan_review_pipeline import report_from_source_bundle, write_report_artifacts
from scripts.loan_review_sources import prepare_source_inputs
from scripts.review_cloud import ReviewCloud
from scripts.review_workflow import export_excel
from test_loan_review_sources import entry, extract, request_fixture, rows_of


@unittest.skipUnless(os.environ.get('CF01_LIVE_TEST') == '1', 'Opt in to synthetic BigQuery SELECT jobs')
class LiveCF01Tests(unittest.TestCase):
    def test_source_to_dbt_bigquery_to_report_and_excel(self):
        tags = {
            'AssetsCurrent': {'units': {'USD': [entry(1000000, start=None)]}},
            'LiabilitiesCurrent': {'units': {'USD': [entry(3000000, start=None)]}},
        }
        bundle = prepare_source_inputs(request_fixture(), rows_of(extract(tags)))
        report, acceptance = report_from_source_bundle(bundle, cf01_backend=ReviewCloud())
        fact = report.facts['current_ratio:fy_t']
        self.assertEqual(fact.value, Decimal('0.3333'))
        self.assertEqual(fact.formula_version, 'cf01-dbt-4dp-half-even-v1')
        trace = report.evidence[fact.evidence_ids[0]]
        self.assertTrue(trace.locator.startswith('bigquery-job:'))
        self.assertEqual(json.loads(trace.content)['raw_value'], '0.33333333333333333333333333333333333333')
        self.assertFalse(acceptance['final_report_accepted'])
        with tempfile.TemporaryDirectory() as tmp:
            write_report_artifacts(report, acceptance, tmp, source_bundle=bundle)
            self.assertIn('流動資產 ÷ 流動負債', Path(tmp, 'report.md').read_text())
            export_excel(tmp)
            self.assertTrue(json.loads(Path(tmp, 'excel_validation.json').read_text())['passed'])


if __name__ == '__main__':
    unittest.main()

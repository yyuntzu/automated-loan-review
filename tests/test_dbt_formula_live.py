"""Opt-in synthetic BigQuery SELECTs for the five Phase 3 formulas."""
import os
import unittest
from decimal import Decimal
from dataclasses import replace
from pathlib import Path
import tempfile

from scripts.review_cloud import ReviewCloud
from scripts.loan_review_pipeline import calculate_report, acceptance_result, write_report_artifacts
from scripts.review_outputs import calculation_metadata
from scripts.review_workflow import export_excel
from scripts.loan_review_contract import Status
from test_dbt_formula_migration import ratio_fixture, source_input, missing_target
from test_loan_review_contract import complete_fixture, source_fact


@unittest.skipUnless(os.environ.get('FORMULA_LIVE_TEST') == '1', 'Opt in to synthetic BigQuery SELECT jobs')
class LiveFormulaTests(unittest.TestCase):
    def test_all_five_formulas_and_lossy_cast(self):
        cloud = ReviewCloud()
        for formula, inputs, expected in [
            ('CF-02', ['1', '3'], '0.3333'), ('CF-03', ['-6', '3'], '-2'),
            ('CF-04', ['100', '20', '10'], '4'), ('CF-09', ['-50', '-100'], '-0.5'),
            ('CF-16', ['0.3', '0.2'], '0.1'),
            ('DER-01', ['500', '320'], '180'), ('DER-02', ['500', '180'], '320'),
            ('DER-03', ['180', '120'], '60'), ('DER-04', ['180', '60'], '120'),
            ('DER-05', ['80', '-20'], '60'), ('DER-06', ['60', '-20'], '80')]:
            with self.subTest(formula=formula):
                result = cloud.calculate_formula(dict(fact_id='synthetic:' + formula,
                                                       formula_id=formula, input_values=inputs))
                self.assertEqual(Decimal(result['value']), Decimal(expected))
                self.assertEqual(result['status'], 'calculated')
                cloud.verify_shadow_evidence(dict(fact_id='synthetic:' + formula,
                    calculation_locator='bigquery-job:' + result['job_id'],
                    trace=dict(formula_id=formula, input_values=inputs, sql_sha256=result['sql_sha256'])))
        with self.assertRaisesRegex(ValueError, 'lossy'):
            cloud.calculate_formula(dict(fact_id='lossy', formula_id='CF-02', input_values=['1E-39', '1']))

    def test_five_formulas_flow_through_formal_report_and_excel(self):
        cloud = ReviewCloud()
        reports = []
        reports.append(('CF-02', 'quick_ratio:fy_t', Decimal('0.3333'), ratio_fixture()))
        report = complete_fixture('annual')
        report = source_fact(report, 'cfo:fy_t', Decimal('1'))
        report = source_input(report, 'current_liabilities', 'fy_t', '3')
        reports.append(('CF-03', 'cash_flow_ratio:fy_t', Decimal('0.3333'), missing_target(report, 'cash_flow_ratio')))
        report = complete_fixture('annual')
        report = source_fact(report, 'pretax_profit:fy_t', Decimal('-10'))
        report = source_fact(report, 'interest_expense:fy_t', Decimal('20'))
        report = source_input(report, 'capitalized_interest', 'fy_t', '10')
        reports.append(('CF-04', 'interest_coverage:fy_t', Decimal('0.3333'), missing_target(report, 'interest_coverage')))
        report = source_fact(complete_fixture('annual'), 'revenue:fy_t', Decimal('120'))
        reports.append(('CF-09', 'revenue_growth:fy_t', Decimal('0.2000'), report))
        report = complete_fixture('annual')
        for role in ('fy_t_minus_3', 'fy_t_minus_2', 'fy_t_minus_1'):
            report = source_fact(report, 'operating_income:'+role, Decimal('20'))
        report = source_fact(report, 'operating_income:fy_t', Decimal('30'))
        reports.append(('CF-16', 'operating_margin_change:fy_t', Decimal('0.1000'), report))
        for formula, fid, expected, source in reports:
            with self.subTest(formula=formula), tempfile.TemporaryDirectory() as tmp:
                # complete_fixture() previously computed common-size facts. Input
                # mutations above invalidate them; recalculate instead of using stale values.
                facts = dict(source.facts)
                for key, fact in facts.items():
                    if ':pct:' in key and fact.status == Status.CALCULATED:
                        facts[key] = replace(fact, status=Status.NOT_CALCULABLE, value=None,
                            formula_id=None, formula_version=None, input_ids=(), reason='recalculate fixture')
                tables = tuple(replace(table, narratives=tuple(
                    replace(n, status='missing', text='', fact_ids=(), period_roles=(), reason='synthetic fixture')
                    for n in table.narratives)) for table in source.tables)
                source = replace(source, facts=facts, tables=tables)
                report = calculate_report(source, formula_backends={formula:cloud})
                self.assertEqual(report.facts[fid].value, expected)
                self.assertTrue(any(m['fact_id']==fid and m['source_evidence_ids']
                                    for m in calculation_metadata(report, 'a'*32)))
                write_report_artifacts(report, acceptance_result(report), tmp)
                self.assertTrue(Path(export_excel(tmp)).is_file())


if __name__ == '__main__':
    unittest.main()

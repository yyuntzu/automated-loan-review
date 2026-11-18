"""Synthetic precision probe: guards and interpretation, not a live BQ test."""
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / 'experiments/cf01_shadow/probe.py'
spec = importlib.util.spec_from_file_location('cf01_shadow_probe', PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def fixture_rows():
    values = {
        'exact': ('120', '80', '1.5', '1.5'),
        'third': ('1', '3', '0.33333333333333333333333333333333333333', '0.333333333333333333'),
        'negative_numerator': ('-50', '100', '-0.5', '-0.5'),
        'negative_denominator': ('50', '-100', '-0.5', '-0.5'),
        'zero_numerator': ('0', '20', '0', '0'),
        'zero_denominator': ('1', '0', None, None),
        'missing_numerator': (None, '20', None, None),
        'too_many_fraction_digits': ('0', '1', '0', '0'),
    }
    return [dict(case_id=key, assets_text=inputs[0], liabilities_text=inputs[1],
                 assets_bq=values[key][0], liabilities_bq=values[key][1],
                 bq_ratio=values[key][2], bq_proposed_18=values[key][3])
            for key, inputs in probe.EXPECTED.items()]


class ShadowProbeTests(unittest.TestCase):
    def test_distinguishes_precision_mismatch_zero_missing_and_lossy_cast(self):
        result = probe.assess(fixture_rows())
        cases = {r['case_id']: r for r in result['results']}
        self.assertFalse(result['rounding_approved'])
        self.assertEqual(cases['third']['status'], 'compared')
        self.assertFalse(cases['third']['raw_values_equal'])
        self.assertEqual(cases['third']['python_proposed_18'], '0.333333333333333333')
        self.assertEqual(cases['zero_numerator']['python_decimal_28'], '0')
        self.assertEqual(cases['zero_denominator']['status'], 'zero_denominator')
        self.assertEqual(cases['missing_numerator']['status'], 'missing_input')
        self.assertEqual(cases['too_many_fraction_digits']['status'], 'blocked_input_precision')

    def test_rejects_missing_duplicate_or_changed_input(self):
        rows = fixture_rows()
        with self.assertRaises(ValueError): probe.assess(rows[:-1])
        with self.assertRaises(ValueError): probe.assess(rows[:-1] + [rows[0]])
        rows[0]['assets_text'] = '121'
        with self.assertRaisesRegex(ValueError, 'input mismatch'): probe.assess(rows)

    def test_reports_disagreement_instead_of_replacing_result(self):
        rows = fixture_rows()
        next(r for r in rows if r['case_id'] == 'exact')['bq_proposed_18'] = '1.4'
        case = next(r for r in probe.assess(rows)['results'] if r['case_id'] == 'exact')
        self.assertEqual(case['status'], 'proposed_rounding_mismatch')
        self.assertEqual(case['bigquery_proposed_18'], '1.4')


if __name__ == '__main__':
    unittest.main()

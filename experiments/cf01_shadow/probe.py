"""Isolated CF-01 precision probe; no borrower data, production tables or writes.

Run explicitly with --run-bigquery to submit the synthetic SELECT as a query job.
This experiment never feeds results into Report, acceptance or publication.
"""
import argparse
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.loan_review_contract import approved_formula_value

SQL = Path(__file__).parent / 'models' / 'cf01_probe.sql'
QUANTUM = Decimal('0.000000000000000001')
EXPECTED = {'exact': ('120', '80'), 'third': ('1', '3'),
            'negative_numerator': ('-50', '100'), 'negative_denominator': ('50', '-100'),
            'zero_numerator': ('0', '20'), 'zero_denominator': ('1', '0'),
            'missing_numerator': (None, '20'),
            'too_many_fraction_digits': ('0.000000000000000000000000000000000000001', '1')}


def decimal_or_none(value):
    return Decimal(value) if value is not None else None


def assess(rows):
    """Compare actual query results, rejecting changed or incomplete fixtures."""
    ids = [row.get('case_id') for row in rows]
    if len(ids) != len(EXPECTED) or set(ids) != set(EXPECTED):
        raise ValueError('Missing, unexpected or duplicate fixture result')
    report = []
    for row in rows:
        if (row['assets_text'], row['liabilities_text']) != EXPECTED[row['case_id']]:
            raise ValueError('Fixture input mismatch')
        a = decimal_or_none(row['assets_text'])
        b = decimal_or_none(row['liabilities_text'])
        a_bq = decimal_or_none(row['assets_bq'])
        b_bq = decimal_or_none(row['liabilities_bq'])
        # SAFE_CAST may round, so verify original input values before interpreting ratios.
        exact_inputs = (a == a_bq and b == b_bq)
        with localcontext() as ctx:
            ctx.prec = 28  # Explicit baseline for this probe, not a claim about every production context.
            ctx.rounding = ROUND_HALF_EVEN
            baseline = approved_formula_value('CF-01', (a, b))
        bq_value = decimal_or_none(row['bq_ratio'])
        bq_proposed = decimal_or_none(row['bq_proposed_18'])
        proposed = None
        if a is not None and b not in (None, Decimal(0)):
            try:
                with localcontext() as ctx:
                    ctx.prec = 100
                    proposed = (a / b).quantize(QUANTUM, rounding=ROUND_HALF_EVEN)
            except InvalidOperation:
                pass
        if not exact_inputs:
            status = 'blocked_input_precision'
        elif a is None or b is None:
            status = 'missing_input'
        elif b == 0:
            status = 'zero_denominator'
        elif bq_value is None or bq_proposed is None or proposed is None:
            status = 'blocked_numeric_range'
        elif bq_proposed != proposed:
            status = 'proposed_rounding_mismatch'
        else:
            status = 'compared'
        report.append(dict(case_id=row['case_id'], status=status,
            input_cast_exact=exact_inputs, python_decimal_28=str(baseline) if baseline is not None else None,
            bigquery_raw=str(bq_value) if bq_value is not None else None,
            python_proposed_18=str(proposed) if proposed is not None else None,
            bigquery_proposed_18=str(bq_proposed) if bq_proposed is not None else None,
            raw_values_equal=baseline == bq_value if baseline is not None and bq_value is not None else None))
    return {'schema_version': 1, 'experiment': 'synthetic-cf01-shadow',
            'authority': 'none', 'rounding_approved': False,
            'python_baseline': 'Decimal precision=28, ROUND_HALF_EVEN; probe only',
            'results': sorted(report, key=lambda r: r['case_id'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-bigquery', action='store_true', help='Submit the synthetic SELECT query')
    parser.add_argument('--project', required=True, help='Project billed for the query job')
    parser.add_argument('--location', default='US')
    opts = parser.parse_args()
    if not opts.run_bigquery:
        parser.error('Explicit --run-bigquery is required; this creates a query job')
    result = subprocess.run(['bq', '--project_id=' + opts.project, 'query',
        '--location=' + opts.location, '--nouse_legacy_sql', '--format=json'],
        input=SQL.read_text(), capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise SystemExit('BigQuery query failed: ' + result.stderr.strip())
    print(json.dumps(assess(json.loads(result.stdout)), indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()

-- Synthetic fixtures only. This query reads no source tables and creates no tables.
-- The proposed 18-place rounding is an experiment, not an approved financial rule.
with fixtures as (
  select 'exact' as case_id, '120' as assets_text, '80' as liabilities_text union all
  select 'third', '1', '3' union all
  select 'negative_numerator', '-50', '100' union all
  select 'negative_denominator', '50', '-100' union all
  select 'zero_numerator', '0', '20' union all
  select 'zero_denominator', '1', '0' union all
  select 'missing_numerator', cast(null as string), '20' union all
  select 'too_many_fraction_digits', '0.000000000000000000000000000000000000001', '1'
), converted as (
  select *, safe_cast(assets_text as bignumeric) as assets,
         safe_cast(liabilities_text as bignumeric) as liabilities
  from fixtures
), computed as (
  select *, safe_divide(assets, liabilities) as ratio from converted
)
select case_id, assets_text, liabilities_text,
       cast(assets as string) as assets_bq,
       cast(liabilities as string) as liabilities_bq,
       cast(ratio as string) as bq_ratio,
       cast(round(ratio, 18, 'ROUND_HALF_EVEN') as string) as bq_proposed_18
from computed
order by case_id

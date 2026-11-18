{{ config(materialized='ephemeral', tags=['formal_approved_formulas']) }}
-- Inputs are STRING query parameters bound by the caller; no source tables.
with inputs as (
  select @fact_id as fact_id, @formula_id as formula_id,
         safe_cast(@a as bignumeric) as a, safe_cast(@b as bignumeric) as b,
         safe_cast(@c as bignumeric) as c
), calculated as (
  select *, case
    when formula_id in ('CF-02', 'CF-03') then safe_divide(a, b)
    when formula_id = 'CF-04' then safe_divide(safe_add(a, b), safe_add(b, c))
    when formula_id = 'CF-09' then safe_divide(safe_subtract(a, b), b)
    when formula_id = 'CF-16' then safe_subtract(a, b)
    when formula_id in ('DER-01', 'DER-02', 'DER-03', 'DER-04', 'DER-06') then safe_subtract(a, b)
    when formula_id = 'DER-05' then safe_add(a, b)
    else null end as result
  from inputs
)
select fact_id, formula_id, cast(a as string) as a_cast, cast(b as string) as b_cast,
       cast(c as string) as c_cast, cast(result as string) as raw_value,
       cast(case when starts_with(formula_id, 'DER-') then result
                 else round(result, 4, 'ROUND_HALF_EVEN') end as string) as value
from calculated

{{ config(materialized='ephemeral', tags=['formal_cf01']) }}
-- The caller binds STRING parameters. Never substitute source values into SQL text.
with inputs as (
  select @fact_id as fact_id, safe_cast(@assets as bignumeric) as assets,
         safe_cast(@liabilities as bignumeric) as liabilities
), calculated as (
  select *, safe_divide(assets, liabilities) as ratio from inputs
)
select fact_id, cast(assets as string) as assets_cast,
       cast(liabilities as string) as liabilities_cast,
       cast(ratio as string) as raw_value,
       cast(round(ratio, 4, 'ROUND_HALF_EVEN') as string) as value
from calculated

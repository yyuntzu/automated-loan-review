{{ config(severity='error') }}
-- Each displayed record represents exactly one accepted calculated Fact.
with metadata as (
  select * from {{ ref('fct_loan_review_formula_metadata') }}
), duplicate_keys as (
  select run_id, entity, fact_id, 'duplicate' as reason
  from metadata group by run_id, entity, fact_id having count(*) != 1
), incomplete as (
  select run_id, entity, fact_id, 'missing_required_metadata' as reason
  from metadata
  where metric_id is null or metric_name is null or formula_id is null
     or formula_version is null or formula_expression is null or formula_display is null
     or inputs_json is null or evidence_json is null or result_value_exact is null
     or result_unit is null or calculation_status != 'calculated'
)
select * from duplicate_keys
union all
select * from incomplete

{{ config(materialized='view', tags=['formal_review_marts']) }}
-- One row per calculated Fact. Values/inputs are recorded by the verified Report,
-- never re-aggregated or recalculated by this view.
with metadata as (
  select * from {{ source('formal_review_raw', 'raw_review_results') }}
  where record_kind = 'formula_metadata'
  qualify row_number() over(partition by run_id, entity, record_id order by created_at desc) = 1
)
select m.run_id, m.entity, m.record_id as fact_id,
       json_value(m.payload, '$.metric_id') as metric_id,
       json_value(m.payload, '$.metric_name') as metric_name,
       json_value(m.payload, '$.formula_id') as formula_id,
       json_value(m.payload, '$.formula_version') as formula_version,
       json_value(m.payload, '$.formula_expression') as formula_expression,
       json_value(m.payload, '$.formula_display') as formula_display,
       json_query(m.payload, '$.inputs') as inputs_json,
       json_query(m.payload, '$.evidence') as evidence_json,
       json_value(m.payload, '$.result_value') as result_value_exact,
       json_value(m.payload, '$.result_unit') as result_unit,
       json_value(m.payload, '$.calculation_status') as calculation_status,
       json_value(m.payload, '$.period_role') as period_role,
       json_value(m.payload, '$.period_start') as period_start,
       json_value(m.payload, '$.period_end') as period_end,
       m.payload as metadata_payload, m.created_at
from metadata m
join {{ ref('fct_loan_review_acceptance') }} a using (run_id, entity)
where a.final_report_accepted

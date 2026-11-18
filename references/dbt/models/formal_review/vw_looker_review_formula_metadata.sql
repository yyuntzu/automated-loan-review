{{ config(materialized='view', tags=['formal_review_marts', 'looker_studio']) }}
-- Join only at run/entity grain, preserving one row for each calculated Fact.
select m.* from {{ ref('fct_loan_review_formula_metadata') }} m
join {{ ref('vw_looker_review_runs') }} r using (run_id, entity)

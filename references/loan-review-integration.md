# Formal Loan Review integration

## CF-01 dbt migration: staged opt-in

`--cf01-dbt` (or HTTP `cf01_migration: true`) selects the new CF-01 path for a
run. It is not yet the default: the rollout remains staged pending broader
historical coverage and verification.
Existing runs and legacy requests retain their original calculation version.

In cloud mode Python selects and verifies input Facts, then an ephemeral dbt
model is compiled and executed by a parameterized BigQuery SELECT. The model
does not create a table or append calculation requests to `raw_review_sources`.
Input conversions must round-trip exactly; Python independently checks the
BigQuery result at four decimal places using `ROUND_HALF_EVEN` before adopting
it under formula version `cf01-dbt-4dp-half-even-v1`. Zero denominators and
missing inputs remain unavailable; a mismatch fails the run. The existing
acceptance and output gates remain in force, and cloud publication rechecks
each migrated CF-01 result using the dbt model before writing results. The
user approved existing permissions and retention for `focus-heuristic-438521-v4.sec_raw`;
publication of migrated CF-01 facts is restricted to that project and dataset.
The result's raw BigQuery ratio, query job ID, SQL hash and input lineage are
retained as review evidence in the local report; publication would persist that
evidence in the existing `raw_review_results` only after the gate is opened.

In local mode with this option, CF-01 is unavailable with an explicit reason;
Python does not compute a substitute. Directly sourced values retain their
existing treatment. The run manifest records `cf01_engine`. The Markdown
report describes the input values, period and source locators for the migrated
ratio. Excel verifies the derived value and asks for a new Loan Review when a
CF-01 input is edited: its floating-point arithmetic cannot be treated as the
authority. Native Excel behavior has not been automatically validated.

The real BigQuery integration test uses only synthetic values and runs only
when `CF01_LIVE_TEST=1`. A check of accepted and published historical runs in
the configured dataset returned no calculated CF-01 records; that result
does not establish historical equivalence or coverage. A previous synthetic
probe observed loss of `1E-39` during BIGNUMERIC conversion, so an apparently
successful `SAFE_CAST` alone is never sufficient proof of exact conversion.

The default CLI, shell and versioned `/v2` HTTP entrypoints use `review_workflow.run_review()`.
Unversioned HTTP routes also retain the historical request shapes described below.
Financial results are calculated once by the existing formal contract. Dashboard,
Excel and BigQuery publication consume that same report; dbt does not recalculate
financial ratios or infer missing facts. The old `analyze_ticker()` function remains
a low-level compatibility API. Use `run_review()` for new Python integrations.

## Run locally

From the repository root, using Python with the project dependencies installed:

```bash
venv/bin/python scripts/agent_runner.py AMZN --request /path/to/request.json \
  --output-dir /tmp/amzn-review-new

# Fully offline, reproducible synthetic example (not borrower financial data):
venv/bin/python scripts/agent_runner.py SYNTHETIC-ONLY \
  --request examples/synthetic_review_request.json \
  --source-snapshot examples/synthetic_source_snapshot.json \
  --output-dir /tmp/synthetic-review-new

# Same workflow via shell; quoting and options are forwarded unchanged:
PYTHON="$PWD/venv/bin/python" bash scripts/run_analysis.sh AMZN \
  --request /path/to/request.json --output-dir /tmp/amzn-review-new
```

The output directory must be empty. If omitted, a unique
`$OUTPUT_DIR/review_<run_id>` directory is created (`OUTPUT_DIR=/tmp/sec_reports`
by default). One ticker/request per CLI run; HTTP supports a batch of reviews.
Explicit fiscal dates and calendar evidence remain mandatory. Ticker/FY labels
alone do not establish a valid reporting period. `--report-request` and
`--period-request` are aliases of `--request` on the default formal CLI.

Default outputs are `report.md`, `report.json`, `acceptance.json`,
`source_inputs.json`, `missing_data.csv`, `reconciliations.csv`,
`review_findings.md`, `dashboard.html`, `loan_review.xlsx`,
`excel_validation.json`, and `run_manifest.json`. `--no-html` / `--no-excel`
explicitly disable optional outputs. The manifest lists requested output
success/failure independently. An Excel error does not discard a valid Markdown
report or prevent cloud publication from being attempted.

Formal runs also write `delivery.json` and return it as `delivery` in the manifest.
This receipt groups the local report/dashboard/Excel paths and the optional
Looker Studio URL state. For run-specific cloud dashboard links, configure
`--looker-config CONFIG.json` or `LOOKER_STUDIO_CONFIG`; see
[Looker Studio automatic delivery](looker-visualization.md#automatic-delivery-configuration).

Exit codes: **0 accepted**, **2 valid draft awaiting acceptance**, **1 execution or
requested output failure**. Invalid CLI arguments also use argparse's exit 2;
read its stderr for an input error versus JSON stdout for a completed draft.
`final_report_accepted` describes substantive report acceptance; manifest status
also accounts for failures in requested outputs/cloud publication.

## Excel runtime

The exporter uses bundled Node and `@oai/artifact-tool`, with no repository-local
package installation. Defaults are under
`~/.cache/codex-runtimes/codex-primary-runtime/dependencies/`.
Override `REVIEW_NODE` and `REVIEW_NODE_MODULES` (or `REVIEW_RUNTIME_ROOT`) for
other environments. Without this runtime, use `--no-excel` or install/configure
the supported runtime. Export failures are reported, never silently skipped.

The workbook has `Review` and `Sources` sheets, including the evidence registry.
It is generic for entity, annual/quarterly mode, currency and report
scale. Numeric source values retain original amount/scale. Calculated facts use
Excel formulas linked through formal `input_ids`. Every available numeric result
is compared to the canonical report before export. Source edits, blanks and zero
are recalculated and checked, then restored. Workbook verification uses the
artifact-tool engine; native Microsoft Excel is not automatically run.

## Cloud mode

```bash
export GCP_PROJECT_ID=your-project
export BQ_DATASET_ID=your_dataset
export BQ_LOCATION=US
export SEC_USER_AGENT='Your Name your-email@example.com'

venv/bin/python scripts/agent_runner.py AMZN --request /path/to/request.json \
  --execution cloud --output-dir /tmp/amzn-cloud-review-new
```

Requires Google Application Default Credentials with dataset/table read/write
permissions and a working `dbt-bigquery` installation. Project/dataset/location
are used by both storage and dbt profiles. `DBT_PROJECT_DIR`, `DBT_PROFILES_DIR`
and `DBT_TARGET` remain configurable. By default, a Python dbt script beside the
active interpreter is invoked using that interpreter, handling relocated virtualenv
shebangs automatically. `DBT_EXECUTABLE` selects a different dbt executable;
`DBT_PYTHON` optionally invokes a Python dbt script with a specific interpreter.
For a relocated virtualenv with a stale dbt shebang:

```bash
export DBT_EXECUTABLE="$PWD/venv/bin/dbt"
export DBT_PYTHON="$PWD/venv/bin/python"
```

Cloud sequence:

1. Validate/extract sources into the formal source bundle (no financial calculations).
2. Append run-scoped observations/inputs/evidence to `raw_review_sources`.
3. `dbt run` and `dbt test --select stg_loan_review_run_sources`.
4. Read only this run and entity, check exact JSON round-trip against the source
   bundle, then adopt sources and calculate the formal report.
5. Generate local deliverables. Only after final acceptance and every requested
   deliverable passes, append canonical report/facts/evidence/acceptance to
   `raw_review_results` without rounding Decimal payload strings. Drafts retain
   local outputs and cloud source/run audit records, with `blocked_acceptance`;
   delivery failures use `blocked_delivery`. Neither is a successful publication.
6. Build/test `tag:formal_review_marts`; write the final run manifest to
   `raw_review_runs`. A failed mandatory cloud step fails the run.

Raw tables are append-only. Deterministic load job IDs prevent duplicate retries
of the same run payload. dbt views deduplicate run/key records and preserve prior
runs. No financial value is summed across source versions. New marts:

- `fct_loan_review_facts`: formal fact/status/value/formula; unavailable is null.
- `dim_loan_review_evidence`: source/definition/review evidence.
- `bridge_loan_review_evidence`: run-scoped fact/evidence relationships.
- `fct_loan_review_acceptance`: technical/rendered/substantive acceptance flags.
- `dim_loan_review_runs`: requested outputs and publication/execution status.

Join by `run_id` and filter acceptance/run status as appropriate. The acceptance
view exposes final acceptance only after the run manifest records both `accepted`
and `published`; failed warehouse validation cannot expose an accepted run. Exact values and original payloads are preserved alongside
BI-friendly numeric casts; BIGNUMERIC/Excel floating-point precision limits still
apply. Existing legacy marts are retained and are not the formal result source.

Reuse a previously staged **SEC/snapshot** formal source bundle with its exact
original request (new fiscal periods/as-of/supplements require a new source run):

```bash
venv/bin/python scripts/agent_runner.py AMZN --request /path/to/original-request.json \
  --source bigquery --source-run-id PREVIOUS_32_CHARACTER_RUN_ID \
  --execution cloud --output-dir /tmp/amzn-reused-review-new
```

Without `--source-run-id`, `--source bigquery` explicitly builds
`stg_loan_review_sources`, then uses the existing lossless raw SEC ledger reader.
That backend requires the legacy `raw_income_statement`, `raw_balance_sheet` and
`raw_cash_flow` tables to have been populated with complete lineage metadata.
Formal cloud mode stores its own run-scoped bundles; it does not rewrite legacy
raw financial tables. Local mode can read BQ sources but does not publish results.

## Workbook source

```bash
venv/bin/python scripts/agent_runner.py NXST --source workbook \
  --workbook /path/to/nexstar.xls --mode annual --review-date 2026-10-27 \
  --output-dir /tmp/nxst-workpaper-review-new
```

Quarterly requires `--mode quarterly --quarter 1|2|3`. The read-only `.xls`
adapter remains specific to the validated Nexstar Report1 layout; arbitrary
company workbooks are not inferred. Its source data are routed through the same
calculation, output and optional cloud stages. Workbook provenance restrictions
remain acceptance blockers; a review date does not establish SEC publication
date or independently verified CIK. Do not supply a separate request/supplement
to this adapter, as its calendar/identity come from the validated workbook.

## HTTP

Start the existing Flask service with `venv/bin/python scripts/agent_runner.py`.
`POST /v2/analyze/AMZN` uses an inline JSON envelope:

```json
{
  "review_request": {"schema_version": 1, "...": "full existing request contract"},
  "source": "sec",
  "execution": "local",
  "html": true,
  "excel": true,
  "supplements": []
}
```

For offline requests use `source: "snapshot"` and `source_snapshot` with the full
snapshot object. For BQ reuse supply `source_run_id`. HTTP accepts structured
JSON, not arbitrary workbook/server file paths. `POST /v2/run` accepts
`{"reviews": [{"ticker": "AMZN", "review_request": {"...": "full request"}}]}`.
HTTP 400 means malformed options/request, 500 execution/output failure, 200 a
completed review; inspect `status`/`final_report_accepted` to distinguish a draft.
Artifact paths in responses are server-local; use local files or your deployment's
artifact delivery layer. Existing `/memo/<ticker>` serves legacy files only.

The unversioned `/run` still accepts `{"tickers": ["AMZN"]}` and GET uses the
configured default tickers. `/analyze/AMZN` still accepts an empty body or `html`,
and GET can serve the generated HTML. These calls explicitly use the legacy
pipeline and existing warehouse marts, not formal source adoption. Their results
always have `final_report_accepted=false`; legacy Markdown includes a notice.
The historical KPI definitions and calendar-year labels are compatibility
estimates, not the approved formal metric definitions. Warehouse availability,
SEC ingestion, and any configured MD&A service are still required by that path.
There is no raw-SUM fallback when the compatibility marts are unavailable.
Unversioned routes also accept formal envelopes for existing formal clients;
`/v2` never silently falls back to legacy execution.

## Compatibility and verification

`--legacy --period-request request.json` explicitly selects the historical
BigQuery/Memo workflow. `--source-request` explicitly selects source-only staging.
The historical string-only MD&A API remains isolated to legacy mode. Formal mode
uses the evidence-bound commentary stage documented in `formal-commentary.md`. Formal narratives remain
evidence-bound; this integration does not invent a new LLM approval path.

Run `venv/bin/python -m unittest discover -s tests -v`.
For parser-only dbt verification (no cloud queries):

```bash
venv/bin/python venv/bin/dbt parse --project-dir references/dbt \
  --profiles-dir references/dbt --target-path /tmp/review-dbt-parse \
  --log-path /tmp/review-dbt-logs
```

The cloud boundary is tested offline with mocked clients and explicit run-ID,
round-trip, publication and failure assertions. Live SEC/GCP/dbt query execution
requires configured network access/credentials. No production cloud writes are
performed by the unit tests. DEC-03/CFADS/debt-service definitions and missing
company-specific evidence continue to block final acceptance when applicable.

## Review remediation

See [PR #1 remediation](pr1-review-remediation.md) for the eight findings, strict
reconciliation schema, regression tests, publication gates, and verification limits.
Run artifacts under `outputs/` and local `node_modules` links are ignored; create
new deliverables locally rather than committing validation receipts or raw filings.


## Evidence-bound commentary

Formal SEC runs retrieve MD&A for the requested fiscal endpoints and `as_of`, then
generate source-cited proposals. Snapshot runs remain offline. Use `--commentary`
with a documents/claims bundle, or explicitly select `--commentary-mode model`
to invoke the configured model on supplied documents. See
[formal commentary](formal-commentary.md) for inputs, review, examples, and limits.

Outputs now include `commentary.json` and `commentary_validation.json`; JSON,
Markdown, Dashboard, Excel, and the formal BigQuery commentary view share the
same narratives. Commentary is display-only and does not gate financial acceptance. Current MD&A
is sufficient; missing summaries or model failures remain explicit warnings while
numeric report delivery continues. Page-number enhancements are deferred.


## Per-formula migration and review evidence

Cloud runs can select `--dbt-formula CF-03=shadow` (repeat the option for
individual CF-02/03/04/09/16 or DER-01..06). Shadow keeps the existing report
calculation, saves comparisons in `formula_shadow.json`, and records its hash in
the manifest. Each cloud `--dbt-formula CF-03=dbt` requires
`--shadow-receipt CF-03=/path/to/prior/run_manifest.json`: the previous run must
have at least one matching result and no mismatches for that formula, entity and
scope. The job must still be accessible and its compiled SQL and bound inputs
must match the receipt. Expired, unavailable, altered or mismatched evidence
blocks promotion. A match does not prove all cases were exercised: review the
complete shadow results and tests before enabling a formula. New runs can omit
the flag to select the historical cloud engine; published runs are unchanged.
HTTP uses `formula_migrations` and `promotion_receipts` objects; each receipt
contains `manifest` and the exact `shadow_json` text, not a server file path.

Selected migrated calculations are unavailable locally; no Python fallback is
used. DER calculations additionally require an explicit `--identity-reviews`
JSON bundle with reviewer, source definition, period and input evidence bindings.
`identity_candidates.json` is an inventory for human review, not an approval.
The report and `formula_metadata.json` show formula, inputs, period, source,
result, status and version; approved cloud publication also exposes metadata
through formal/Looker views. Final ratios use four decimal places and
ROUND_HALF_EVEN; lossy input conversion blocks calculation.

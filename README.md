# automated-loan-review

SEC-based formal Annual and Quarterly Loan Review workflow, with evidence-linked commentary and shared Markdown, Excel and optional cloud outputs.

## Version

Independent source snapshot from `sec-credit-risk-analysis`, commit `f412f44b914296dbf9dd7756803458309fa9e265`. The initial commit contains the selected current source tree, without the previous repository history or local run artifacts. `master` is the primary branch.

## Time zone

The application uses US Central time, `America/Chicago`, including automatic daylight-saving transitions (CST/CDT). Formal run start/end timestamps and cloud ingestion timestamp strings include the Central UTC offset. BigQuery TIMESTAMP stores absolute instants and may display them in UTC. SEC fiscal dates and source publication dates retain their original dates.

The shell entry point exports `TZ=America/Chicago`. Python formal workflow timestamps use `ZoneInfo("America/Chicago")` directly, including when invoked without the shell wrapper. The runtime requires IANA time-zone data.

## Run

Use a Python environment containing the project dependencies and provide a verified fiscal request. See `references/loan-review-integration.md` for runtime and output requirements.

```bash
python scripts/agent_runner.py TICKER --request /path/to/request.json --output-dir /path/to/new-output-directory
```

The output directory must be empty. Exit status 0 means accepted, 2 means a completed draft with unresolved financial acceptance, and 1 means execution or requested output failure. Missing values are never silently filled.

## Local configuration

`.env.example` is a template, not an automatically loaded credential file. Configure your own SEC user agent and any optional provider/cloud credentials in the execution environment. Credentials, `.env`, virtual environments, logs and generated reports are excluded from version control. Existing local credentials and external Codex skills are not included in this package.

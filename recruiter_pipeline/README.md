# Recruiter hiring-volume pipeline

Sibling of `msp/cfo/insurance` pipelines. **No shared imports.**

Surfaces companies **hiring heavily** (3+ unique roles in a rolling 30-day
window) for staffing / recruiting agencies — the loudest "needs hiring help"
signal there is.

## How it works

Company-centric ingestion of ATS job boards (Greenhouse / Lever / Ashby): one
call returns a company's entire open-role list, so counting *unique* roles is
exact, not inferred. Each company is aggregated into a hiring-volume summary
(unique-role count, function mix, primary location); heavy hirers ship to
`data/recruiter-leads.json`.

```bash
cd recruiter_pipeline
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m recruiter_pipeline.daily_run --help
python -m pytest -q
```

Seed ATS boards live in `recruiter_pipeline/seed_boards.json` (grow this list;
board discovery is a later enhancement). Output is uploaded to
`RECRUITER_UPLOAD_URL` and read by the outreach recruiter pack.

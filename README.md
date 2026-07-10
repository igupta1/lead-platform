# lead-platform

Buying-signal lead pipelines. Each package scrapes signals about SMBs (jobs,
funding, headcount, breaches), scores them for a vertical, and uploads a JSON
inventory to Vercel Blob, which the website and outreach engines read over HTTP.

Three independent sibling packages — **no shared imports** (each owns its DB
schema, scoring weights, enrichment prompt, Apollo title list, and output shape):

| Package | Vertical | Daily cron |
|---|---|---|
| `pipeline/msp_pipeline/` | MSP / MSSP / cloud | `.github/workflows/daily-leads.yml` |
| `cfo_pipeline/` | Fractional CFO | `.github/workflows/cfo-leads.yml` |
| `insurance_pipeline/` | Insurance | `.github/workflows/insurance-leads.yml` |

## Data model

- **`<pkg>/data/leads.db`** — SQLite, the pipeline's incremental state. **Committed**
  (the cron reads last run's DB to dedupe/enrich, then commits the updated one).
- **`<pkg>/data/leads.json`** — the run output. **Not committed** — it's uploaded
  to Vercel Blob every run (the served source) and is `.gitignore`d here.

Each cron: checkout → run pipeline (`--upload` posts JSON to Blob) → commit the
updated `leads.db`. The upload URL is the deployed site's `api/upload-*`
endpoint, so moving this out of the website repo doesn't change anything.

## Run one locally

```bash
cd cfo_pipeline
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
python -m cfo_pipeline.daily_run --help
python -m pytest -q
```

See `docs/` for the build roadmap and per-milestone notes.

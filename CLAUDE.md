# CLAUDE.md

Buying-signal lead pipelines (Python). Supply side: scrape signals → score →
upload a JSON lead inventory to Vercel Blob. Consumed by the website and the
outreach engines over HTTP; nothing here imports them or vice-versa.

## Packages (siblings, NO shared imports)

- `pipeline/msp_pipeline/` — MSP / MSSP / cloud
- `cfo_pipeline/` — fractional CFO
- `insurance_pipeline/` — insurance

Each owns its DB schema, scoring weights, enrichment prompt, Apollo title list,
and output shape. Keep them independent — do not factor shared code across them
without explicit instruction.

## Data

- `<pkg>/data/leads.db` — SQLite incremental state, **committed** by the cron.
- `<pkg>/data/leads.json` — run output, **gitignored** (served from Vercel Blob).

## Forbidden without explicit instruction

- Committing `.env` or `**/data/leads.json`.
- Wholesale-destructive ops (`rm -rf` of dirs, `git reset --hard`, force push,
  dropping/truncating tables, mass file deletes). Single-file deletes are fine.

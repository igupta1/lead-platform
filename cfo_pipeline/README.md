# Fractional CFO lead magnet pipeline

Sibling of `pipeline/msp_pipeline/` and `insurance_pipeline/`. **No
shared imports** — own DB schema, own scoring weights, own
enrichment prompt, own Apollo title list, own JSON output shape.

## What this surfaces

US SMBs (≤~75 employees) currently in the buying window for a
fractional CFO. The buying-signal model:

- **Primary (gate):** the company is hiring finance leadership one
  rung below CFO — Controller, VP / Head / Director of Finance,
  Accounting Manager, Finance Manager. They need finance leadership
  but aren't big enough to absorb a full-time CFO yet.
- **Hard exclude:** the company has an open *full-time* CFO posting.
  That company is buying a CFO, not a fractional one. Drop entirely
  from output (and from the DB).
- **Secondary (urgency, not a gate):** recent Form D filing (last
  90 days) or freshly announced seed / Series A round. Cash to spend +
  reporting obligations to a new board = the fractional-CFO window.

## Setup

```bash
cd cfo_pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in real keys
```

## Run

```bash
source .venv/bin/activate
python -m cfo_pipeline.daily_run
```

Useful flags (mirror of `insurance_pipeline`):
- `--limit N` per-source candidate cap (smoke runs)
- `--dry-run` fetch sources only, no DB / LLM / JSON writes
- `--rescore-only` skip fetch+enrichment, just rescore + regen
- `--upload` POST the generated JSON to the Vercel endpoint
- `--reenrich` force re-enrichment on every lead

## Tests

```bash
source .venv/bin/activate
pytest
```

## Output

Writes `data/leads.json` with the shape `{generated_at, leads: [...]}`.
Uploaded to Vercel Blob key `cfo-leads-current.json` (separate from
both the MSP pipeline's `leads-current.json` and the insurance
pipeline's `insurance-leads-current.json`). Served by
`api/generate-cfo-leads.js` to the `/cfo` page.

## Disqualifier table

Single-niche DBs forgot one thing the multi-niche schema handled by
accident: a "this company is permanently out" set that persists across
runs. The CFO disqualifier (an open full-time CFO posting) is a
*sticky* fact — observed Monday should still block a Form D filing on
Friday — so we keep it in a dedicated `disqualified` table keyed by
`name_key`. The jobs source writes to it; every source's upsert
consults it.

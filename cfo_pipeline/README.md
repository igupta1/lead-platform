# Fractional CFO lead magnet pipeline

Sibling of `pipeline/msp_pipeline/` and `insurance_pipeline/`. **No
shared imports** — own DB schema, own scoring weights, own
enrichment prompt, own Apollo title list, own JSON output shape.

## What this surfaces

US SMBs (≤~75 employees) currently in the buying window for a
fractional CFO. The buying-signal model:

- **In-market (hottest, weight 80):** the company posted a
  *Fractional / Interim / Part-time CFO* role. They are literally
  shopping for the service being sold.
- **Primary (gate, weight 60):** the company is hiring finance
  leadership one rung below CFO — Controller, VP / Head / Director of
  Finance, Accounting Manager, Finance Manager. They need finance
  leadership but aren't big enough to absorb a full-time CFO yet.
- **Hard exclude:** the company has an open *full-time* CFO posting,
  OR its Form D lists a CFO among related persons. That company has
  (or is buying) a CFO, not a fractional one. Drop entirely from
  output (and from the DB); sticky via the disqualified table.
- **Secondary (urgency, not a gate, weight 25):** recent Form D
  filing (last 90 days) or freshly announced seed / Series A round.
  Cash to spend + reporting obligations to a new board = the
  fractional-CFO window. Funding-only leads are gated at output:
  resolved domain required, known offerings must be ≥ $500K, and at
  most 75 funding-only cards ship so hiring-signal leads dominate.

Form D XML is mined (not just fetched for the pooled-fund check):
offering amount, related persons (officer names → free DM data),
industry group, revenue range. Scores decay from the *event* date
(posting date / filing date), not the scrape date.

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
- `--rescore-only` skip fetch+enrichment, just rescore + rewrite
- `--upload` POST the generated JSON to the Vercel endpoint
- `--reenrich` force re-enrichment on every lead
- `--enrich-budget N` max Gemini web lookups per run (default 300 —
  inside the free-tier 500/day grounded-search quota; overflow leads
  stay in the DB and drain on later runs, hiring-signal leads first)
- `--apollo-top-n N` Apollo DM enrichment on the top-N leads (default 30)

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

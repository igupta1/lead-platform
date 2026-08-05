# CLAUDE.md

Buying-signal lead-magnet platform (Python). One package, `leadgen/`: shared
sources feed one deduped company store; a **niche is a query** over that store.
Produces the companies that go *inside* the gift — not the agencies you email.
Consumed by the website and the outreach engine over HTTP; nothing here imports
them or vice-versa.

## Shape (`leadgen/leadgen/`)

- `sources/` — fetched once per run, each returns `LeadCandidate`s: `jobs`
  (the seven job-post signal types), `fractional_boards` (fractional-CFO
  posts), `breaches` (HHS OCR + state AGs). The two EDGAR sources were
  DELETED (2026-08-04): funding is the bottom tier in every niche that used
  it, so its first lead ranked 474th (cfo) / 1032nd (accounting) and never
  reached a gift. ~960 lines for zero gifts. Do not re-add without a tier
  change that would actually surface them.
- `db.py` — the single company store: one row per company (fuzzy-deduped
  across every source), all signals attached, one score per niche.
- `enrichment.py` — LLM (Gemini/OpenAI) enrichment: domain, headcount,
  industry/niche, insight; purges junk and companies ≥100 employees. **No
  Apollo, no decision-maker lookup** (magnets need no contact). `insight` is
  NOT published — every outreach line is code-templated — but it is still
  computed: the niche classifier and the CFO-competitor gate both key off it.
  A looked-up domain must resolve in DNS or it is stored as None.
- `scoring.py` + `niches/` — niche config + the recency-weighted tiered scorer.
  Five niches: `accounting`, `cfo`, `mssp`, `msp`, `cloud`.
- `run.py` — the one orchestrator: fetch → upsert → enrich → score → project
  one inventory per niche → write local JSON. Enrichment runs the per-lead
  Gemini/OpenAI lookups **concurrently** (`--enrich-workers`, default 12; needs a
  paid Gemini tier / `GEMINI_MIN_INTERVAL_S=0`) — the network work is planned in
  parallel (`enrichment.plan_enrichment`, no DB) and the writes applied serially
  (`apply_enrichment`), so a full backlog refreshes each night. `--time-budget-s`
  is a backstop that stops early (reserving time to emit/commit); progress is
  committed per lead. Publishing to Vercel Blob happens in CI, not here.

## Signal contract (enforced in `models.py`)

Every stored `Signal` carries verbatim `evidence_text` + a `source_url` — no
evidence, not stored. Funding is **SEC Form D/C only** (no RSS/headline
proxies). No `exec_hired` / title-absence guessing.

## Data

- `leadgen/data/leads.db` — SQLite incremental state, **committed** by the cron
  (`.github/workflows/daily-leads.yml`, 08:00 UTC daily; commits even on partial
  failure so progress persists, and alerts via `NTFY_TOPIC` on failure/timeout).
- `leadgen/data/*-leads.json` + `taxonomy.json` — per-niche run output,
  **gitignored**, published nightly to a **public Vercel Blob** by the CI
  `vercel blob put` step (stable pathnames, short cache). The outreach engine
  reads them straight from that blob (no website in the path).

## Forbidden without explicit instruction

- Committing `.env` or `**/data/*-leads.json`.
- Re-adding Apollo, RSS funding, the EDGAR Form D/C sources, an
  insurance/trucking/recruiter niche, or an `exec_hired`/fresh-vs-aged/
  still-open concept (all deliberately removed).
- Publishing `insight`, or letting any model write a lead's copy line.
- Wholesale-destructive ops (`rm -rf` of dirs, `git reset --hard`, force push,
  dropping/truncating tables, mass file deletes). Single-file deletes are fine.

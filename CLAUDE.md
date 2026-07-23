# CLAUDE.md

Buying-signal lead-magnet platform (Python). One package, `leadgen/`: shared
sources feed one deduped company store; a **niche is a query** over that store.
Produces the companies that go *inside* the gift — not the agencies you email.
Consumed by the website and the outreach engine over HTTP; nothing here imports
them or vice-versa.

## Shape (`leadgen/leadgen/`)

- `sources/` — fetched once per run, each returns `LeadCandidate`s:
  `edgar_form_d` (Form D funding), `edgar_form_c` (Form C funding), `jobs`
  (the seven job-post signal types), `fractional_boards` (fractional-CFO
  posts), `breaches` (HHS OCR + state AGs).
- `db.py` — the single company store: one row per company (fuzzy-deduped
  across every source), all signals attached, one score per niche.
- `enrichment.py` — LLM (Gemini/OpenAI) enrichment: domain, headcount,
  industry/niche, insight; purges junk and companies ≥100 employees. **No
  Apollo, no decision-maker lookup** (magnets need no contact).
- `scoring.py` + `niches/` — niche config + the recency-weighted tiered scorer.
  Five niches: `accounting`, `cfo`, `mssp`, `msp`, `cloud`.
- `run.py` — the one orchestrator: fetch → upsert → enrich → score → project
  one inventory per niche → upload.

## Signal contract (enforced in `models.py`)

Every stored `Signal` carries verbatim `evidence_text` + a `source_url` — no
evidence, not stored. Funding is **SEC Form D/C only** (no RSS/headline
proxies). No `exec_hired` / title-absence guessing.

## Data

- `leadgen/data/leads.db` — SQLite incremental state, **committed** by the cron.
- `leadgen/data/*-leads.json` — per-niche run output, **gitignored** (served
  from Vercel Blob).

## Forbidden without explicit instruction

- Committing `.env` or `**/data/*-leads.json`.
- Re-adding Apollo, RSS funding, an insurance/trucking/recruiter niche, or an
  `exec_hired`/fresh-vs-aged/still-open concept (all deliberately removed).
- Wholesale-destructive ops (`rm -rf` of dirs, `git reset --hard`, force push,
  dropping/truncating tables, mass file deletes). Single-file deletes are fine.

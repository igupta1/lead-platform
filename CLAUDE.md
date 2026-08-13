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
  Apollo, no decision-maker lookup** (magnets need no contact). `insight` IS
  published, for exactly one consumer: the outreach engine's Gate B fit check.
  It is the only field describing what a company does, so unpublishing it makes
  Gate B judge empty strings and silently drop every vertical claim (measured:
  cfo went 11/23 niched to 0/23 over 2026-08-04..05). It is also used locally by
  the niche classifier and the CFO-competitor gate. It must never be rendered
  into copy — every outreach line stays code-templated.
  A looked-up domain must resolve in DNS or it is stored as None.
- `scoring.py` + `niches/` — niche config + the recency-weighted tiered scorer.
  Six niches: `bookkeeping`, `accounting`, `cfo`, `mssp`, `msp`, `cloud`.

  The three FINANCE niches are one ladder, split by the rung a company is
  hiring at, because each rung is a different sale to a different buyer:
  `bookkeeping` (junior: bookkeeper, AP/AR, payroll) · `accounting`
  (controller) · `cfo` (fractional CFO). Measured on live inventory, only 1.7%
  of companies carry both a junior and a controller-level signal, so the first
  two barely overlap. `accounting` and `cfo` DO share `job_finance_lead` — a
  fractional-controller-only pool is far too thin to gift from — but each leads
  with its own explicit-intent signal, so the gifts still differ.
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

## Monitoring (`monitoring.py`, nightly — alerts via `NTFY_TOPIC`)

Three checks, all on a run that exits 0: a source that fell to zero, a niche
that lost >50% of its leads, and the two that need no previous run — a latched
Gemini quota, and the **insight floor**.

The floor alerts when a published niche has <20% of leads carrying `insight`.
It is a catastrophe detector, NOT a quality bar: observed fill is 97-100%, so
only a dead/rotated key, a drained balance, a provider outage, or the field
being dropped from the record can reach 20%. Keep the threshold LOW — a tight
one pages on ordinary model variance, gets muted, and then the real outage is
missed. It runs on `--skip-fetch` too, since that is the publish path.

## Forbidden without explicit instruction

- Committing `.env` or `**/data/*-leads.json`.
- Re-adding Apollo, RSS funding, the EDGAR Form D/C sources, an
  insurance/trucking/recruiter niche, or an `exec_hired`/fresh-vs-aged/
  still-open concept (all deliberately removed).
- UNpublishing `insight` (it feeds outreach Gate B — see `enrichment.py` above),
  or letting any model write a lead's copy line.
- Wholesale-destructive ops (`rm -rf` of dirs, `git reset --hard`, force push,
  dropping/truncating tables, mass file deletes). Single-file deletes are fine.

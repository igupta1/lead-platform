# MSP Lead Magnet Pipeline — Roadmap

Milestone-by-milestone plan. Each milestone is one to a few commits with a
clear deliverable and exit criteria. Lock criteria for moving on: `pytest`,
`ruff check`, and `mypy msp_pipeline` all clean.

---

## M0 — Foundation scaffold ✅ DONE (commit `6882a62`)

Directory tree, venv (Python 3.13), deps, gitignore, root + `pipeline/CLAUDE.md`.

---

## M1 — Schema + `db.py` (in progress)

**Scope.** SQLite `leads` schema, Pydantic models that mirror it, and the CRUD
layer (`db.py`) including fuzzy upsert via rapidfuzz.

**Deliverables.**
- `msp_pipeline/models.py` — `Lead`, `Signal`, `LeadCandidate`, enums
  (`NicheName`, `SignalType`, `SourceName`)
- `msp_pipeline/db.py` — `init_db`, `upsert_lead`, `get_lead`, `iter_leads`,
  `append_signal`, `update_enrichment`, `update_scores`, `update_niche_copy`
- `tests/test_db.py` — round-trip, fuzzy upsert merge/insert, signal append,
  niche-ordered iteration

**Dependencies.** M0.

**Exit.** Tests + lint clean. Smoke `init_db` against a temp SQLite produces
the `leads` table with all indexes.

**Out of scope.** Source modules, scoring, LLM, JSON output.

---

## M2 — Source modules

**Scope.** Three source modules that fetch from free APIs/feeds, parse
responses, and return `LeadCandidate` objects with attached `Signal`s. No DB
writes from source modules — they return candidates; the caller upserts.

**Deliverables.**
- `msp_pipeline/sources/jobs.py` — JobSpy (LinkedIn/Indeed) + Adzuna +
  HN Algolia. Emits `job_posted_*` and `exec_hired`.
- `msp_pipeline/sources/funding.py` — SEC EDGAR Form D + TechCrunch RSS +
  PR Newswire RSS. Emits `funding_raised`.
- `msp_pipeline/sources/breaches.py` — HHS Wall of Shame + California AG +
  Maine AG. Emits `breach_disclosed`.
- `tests/fixtures/` — captured response samples per upstream API
  (per `pipeline/CLAUDE.md`: never live HTTP in tests).
- `tests/test_sources_jobs.py`, `test_sources_funding.py`,
  `test_sources_breaches.py`.

**Dependencies.** M1 (uses `LeadCandidate`, `Signal`, enums).

**Exit.** Each module, given a captured fixture, returns the expected
`LeadCandidate` list. Tests + lint clean.

**Out of scope.** Computed signals (`headcount_threshold_crossed`,
`headcount_growth_rapid`) — those need enriched headcount, deferred to M4.

---

## M3 — `llm.py` wrappers (lean)

**Scope.** Thin wrappers around the Anthropic + Gemini SDKs so all LLM calls
in the codebase route through one place (per `pipeline/CLAUDE.md`). Support
structured output (Pydantic schema → JSON), exponential backoff on rate
limits, and a logging hook so a bad response is debuggable.

**Deliverables.**
- `msp_pipeline/llm.py` —
  `call_claude(prompt, *, response_model=None, max_tokens=...)`,
  `call_gemini(prompt, *, response_model=None)`. Reads keys from `.env` via
  python-dotenv.

**Dependencies.** M0.

**Exit.** Hand-run smoke against real keys: round-trip a small structured
response from each provider. **No dedicated `tests/test_llm.py`** — the
wrappers get their first real test via mocks in `tests/test_enrichment.py`
(M4), once the API surface has been validated against a real consumer.

**Out of scope.** Prompt templates for enrichment/outreach — they live in
their own modules. Dedicated `tests/test_llm.py` is deferred to M4.

---

## M4 — `enrichment.py`

**Scope.** Given a `Lead`, fill in `industry`, `headcount`, `headcount_band`,
`country`, `state`, `city`, `founded_year`, `linkedin_url`, `description`. Two
passes: (a) deterministic compute (`headcount_band` from `headcount`,
country from postal/state, etc.), (b) LLM classification of `industry` from
name + recent signals. Also emits the *computed* signals
(`headcount_threshold_crossed`, `headcount_growth_rapid`).

**Deliverables.**
- `msp_pipeline/enrichment.py` — `enrich(conn, lead)` (mutates via
  `db.update_enrichment` + `db.append_signal`),
  `compute_band(headcount)`, `classify_industry(lead)` → calls
  `llm.call_claude`.
- `tests/test_enrichment.py` — deterministic helpers tested directly; LLM
  path tested with mocked `call_claude` (this is also the first real
  exercise of the M3 `llm.py` wrapper API).

**Dependencies.** M1 (db writes), M3 (llm wrappers).

**Exit.** A lead with raw signals goes through `enrich()` and ends with all
enrichment columns + computed signals populated. Tests + lint clean.

**Out of scope.** A real headcount data API — for now headcount comes from
the source layer or an LLM estimate.

---

## M5 — `scoring.py`

**Scope.** Per-niche rule-based scorer. Given an enriched `Lead`, compute
three scores in `[0, 100]`: `it_msp_score`, `mssp_score`, `cloud_score`.
Signal-weighted with recency decay. **No LLM here** — pure deterministic so
scores are explainable and reproducible.

**Deliverables.**
- `msp_pipeline/scoring.py` — `score(lead) -> dict[NicheName, float]`. Helpers
  per niche (`_score_it_msp`, `_score_mssp`, `_score_cloud`) so weights tune
  independently. A `WEIGHTS` table at the top documenting each
  `(signal_type, niche) → weight`.
- `tests/test_scoring.py` — table-driven cases: a lead with N signals →
  expected score; recency decay verified; missing signals → score still
  produced (no NaN/None propagation).

**Dependencies.** M1, M4.

**Exit.** Scores deterministic, test-covered, hand-checked against a few
representative fixtures. Tests + lint clean.

**Out of scope.** Insights and outreach copy — M6.

---

## M6 — `outreach.py`

**Scope.** For each niche where the lead's score crosses a threshold,
generate two pieces of LLM copy: a one-sentence `insight` (why this lead
matches the niche) and a 3–5 sentence `outreach` draft. Per-niche prompts,
structured output via Pydantic.

**Deliverables.**
- `msp_pipeline/outreach.py` — `generate(lead, niche, score) -> (insight, outreach)`.
  Per-niche prompt templates in the same file.
- `tests/test_outreach.py` — mocks `call_claude`; verifies prompt assembly
  per niche; verifies returned tuple shape.

**Dependencies.** M3, M5.

**Exit.** Tests green; hand-run against real keys for one fixture lead;
outputs read sensibly (manual inspection). Tests + lint clean.

**Out of scope.** Multi-language copy, A/B testing, embeddings/RAG.

---

## M7 — `daily_run.py` + JSON output (local)

**Scope.** Orchestrate the full pipeline end-to-end against a real DB:
fetch → upsert → enrich new/stale → re-score → regenerate copy where scores
moved past a delta. Final step: emit consumer-facing JSON to
`pipeline/data/leads.json`. Vercel upload deferred to M8.

**Deliverables.**
- `msp_pipeline/daily_run.py` — `main()` entry point. Per-source failure is
  logged but doesn't abort other sources. Writes `data/leads.json`.
- CLI: `python -m msp_pipeline.daily_run [--limit N] [--dry-run]`.
- `tests/test_daily_run.py` — end-to-end with all sources mocked; verifies
  output JSON shape.
- Output JSON schema documented in `pipeline/CLAUDE.md`.

**Dependencies.** M1–M6.

**Exit.** `python -m msp_pipeline.daily_run --dry-run` against a fresh DB
produces a non-empty `leads.json`. Tests + lint clean. JSON shape stable
enough for M8/M9 to consume.

**Out of scope.** Upload (M8), API endpoints (M8), React (M9).

---

## M8 — `api/upload-leads.js` + Vercel Blob integration

**Scope.** Vercel serverless function that accepts a JSON body (the leads
blob) and writes it to Vercel Blob via `BLOB_READ_WRITE_TOKEN`. Auth via a
shared bearer token in env. Pipeline `daily_run.py` gains `--upload` to POST
the local JSON.

**Deliverables.**
- `api/upload-leads.js` — POST handler, auth header check, writes to Blob
  with a stable key (e.g. `leads-current.json`).
- `msp_pipeline/daily_run.py` — `--upload` flag.
- Manual smoke against a Vercel preview URL.

**Dependencies.** M7.

**Exit.** `python -m msp_pipeline.daily_run --upload` against the preview
deployment writes a blob, verified via Vercel Blob CLI or `curl` of the
read URL. No regressions on existing `/` or `/gtm` routes.

**Out of scope.** React reading the blob (M9), rate limiting, blob versioning.

---

## M9 — `api/generate-leads.js` + React pages

**Scope.** The consumer side. `api/generate-leads.js` reads the blob and
serves filtered subsets per niche. Three React pages mirror the existing
`AITools.js` style (Tailwind, header/footer, card list).

**Deliverables.**
- `api/generate-leads.js` — GET handler with `?niche=` query param. Returns
  sorted, score-thresholded leads.
- `src/pages/ITMsps.js`, `src/pages/Mssps.js`, `src/pages/Cloud.js` — same
  component skeleton parameterized by niche.
- Routes added to `src/App.js`: `/it-msps`, `/mssps`, `/cloud`.
- Likely `src/components/LeadCard.js` (shared) to avoid duplication.

**Dependencies.** M8.

**Exit.** All three pages render real data from a recent run, filter/sort
per niche, match the existing site's visual language. No regressions on `/`
or `/gtm`.

**Out of scope.** Manual lead curation UI, lead enrichment UX, analytics.

---

## M10 — GitHub Actions cron

**Scope.** Schedule `daily_run.py --upload` to run nightly via GitHub Actions.
SQLite state persists across runs by committing the updated
`pipeline/data/leads.db` back to the repo at the end of each run. Manual
trigger via `workflow_dispatch` for testing.

**Deliverables.**
- `.github/workflows/daily-leads.yml` — cron schedule (e.g., 06:00 UTC daily)
  + `workflow_dispatch` trigger. Checks out the repo, sets up Python 3.13,
  installs `pipeline/` editable, runs `python -m msp_pipeline.daily_run --upload`,
  then commits `pipeline/data/leads.db` if changed and pushes back to the
  default branch.
- GitHub repo secrets configured (Settings → Secrets and variables → Actions):
  `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`,
  `BLOB_READ_WRITE_TOKEN`, `LEADS_UPLOAD_API_KEY`.
- `concurrency` block (one nightly run at a time, cancel in-flight on manual
  trigger).
- Workflow needs `contents: write` permission to push the DB commit back.

**Dependencies.** M8.

**Exit.** Manual `workflow_dispatch` run succeeds end-to-end: pipeline runs,
blob is updated, `pipeline/data/leads.db` is committed back to the repo,
no regressions on existing routes (`/`, `/gtm`). The next scheduled run picks
up the committed DB and shows expected incremental behavior (no full
re-scrape from scratch).

**Out of scope.** Multi-region runs, retries on partial source failures
(handled at app level in M7), monitoring/alerting on workflow failure.

---

## Cross-cutting hygiene (every milestone)

- `pytest` green, `ruff check`, `mypy msp_pipeline` clean before merging.
- One commit per milestone (or per logical sub-step), terse lowercase message.
- Update `pipeline/CLAUDE.md` whenever conventions change.
- No source-module code creates DB rows directly — they return candidates;
  `daily_run.py` (and only `daily_run.py`) calls `db.upsert_lead`.
- All LLM calls go through `msp_pipeline/llm.py` — no direct SDK use elsewhere.

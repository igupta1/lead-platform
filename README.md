# lead-platform

Buying-signal lead-magnet platform. One package, `leadgen/`, that finds SMBs
(<100 employees) showing public buying signals, dedupes them into one company
store, and emits one lead inventory per niche — the companies that go *inside*
the gift the outreach engine sends.

**A niche is a query, not a pipeline.** Shared sources are fetched once and
written into one deduped `leads.db`; each company is scored for every niche it
qualifies for, so a company that filed a Form D *and* is hiring a Controller is
one record that can surface in both the Accounting and CFO inventories.

## Niches & signals

| Niche | Signals |
|---|---|
| Accounting / Bookkeeping | Form D/C funding · junior finance hire · finance-lead hire |
| Fractional CFO | Fractional/interim CFO post · finance-lead hire · Form D funding |
| MSSP | Breach (HHS OCR + state AGs) · security roles |
| MSP | IT / helpdesk roles (incl. IT leadership) |
| Cloud / DevOps | DevOps / SRE roles · Form D funding |

## Sources (shared, fetched once)

- **SEC Form D / Form C** — funding (`sources/edgar_form_d.py`, `edgar_form_c.py`).
- **Job posts** — JobSpy + Adzuna + fractional boards (`sources/jobs.py`,
  `fractional_boards.py`), classified into seven role signal types.
- **Breaches** — HHS OCR Breach Portal + state AGs (`sources/breaches.py`).

Every stored signal carries verbatim evidence + a source URL, or it isn't
stored. Funding is SEC Form D/C only.

## Data model

- **`leadgen/data/leads.db`** — SQLite, the platform's incremental state.
  **Committed** (the cron reads it to dedupe/enrich, then commits it back).
- **`leadgen/data/<niche>-leads.json`** — per-niche run output. **Not
  committed** — uploaded to Vercel Blob each run and `.gitignore`d here.

The single cron (`.github/workflows/daily-leads.yml`) does:
checkout → run (`--upload` posts each niche inventory) → commit `leads.db`.

## Run locally

```bash
cd leadgen
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
cp .env.example .env      # fill in OPENAI_API_KEY, GEMINI_API_KEY, ...
python -m leadgen.run --since-days 45 --enrich-budget 50   # writes data/*-leads.json
python -m pytest -q
```

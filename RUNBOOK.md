# Runbook — running the four new niches

Four buying-signal niches ship as inventories here (the scraper) that the
`outreach-platform` packs consume:

| Niche | Producer | Local output | Keys needed to run |
|---|---|---|---|
| Trucking | `insurance_pipeline` | `data/trucking-leads.json` | insurance keys |
| Commercial P&C | `insurance_pipeline` | `data/pc-leads.json` | insurance keys |
| Bookkeeping | `cfo_pipeline.bookkeeping_run` | `cfo_pipeline/data/bookkeeping-leads.json` | **none** |
| Recruiters | `recruiter_pipeline` | `recruiter_pipeline/data/recruiter-leads.json` | **none** |

The outreach packs read these JSON files **directly** (no website API needed),
so you can preview everything locally without any upload setup.

---

## 1. Secrets — what to put where

`.env` files are gitignored and **never committed** — create them locally.

### `insurance_pipeline/.env` (only file with secrets you must set)
```bash
cp insurance_pipeline/.env.example insurance_pipeline/.env
```
Fill **only these three** (same values your existing CFO/insurance crons use —
find them in your GitHub Actions repo secrets or your other local `.env`):
```
OPENAI_API_KEY=...
GEMINI_API_KEY=...
APOLLO_API_KEY=...
```
Leave every `*_UPLOAD_URL` / `*_UPLOAD_API_KEY` **blank** for now (see §4).

### Bookkeeping & Recruiters
No keys. Bookkeeping scrapes public job boards (jobspy); recruiters read public
ATS boards. Nothing to create.

---

## 2. Run the pipelines (writes the local JSON inventories)

```bash
# trucking + P&C  (both fall out of one insurance run)
cd insurance_pipeline
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
python -m insurance_pipeline.daily_run
ls data/trucking-leads.json data/pc-leads.json
deactivate && cd ..

# bookkeeping
cd cfo_pipeline
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
python -m cfo_pipeline.bookkeeping_run
ls data/bookkeeping-leads.json
deactivate && cd ..

# recruiters — edit recruiter_pipeline/recruiter_pipeline/seed_boards.json first
cd recruiter_pipeline
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
python -m recruiter_pipeline.daily_run --dry-run --verbose   # sanity: heavy-hirer count
python -m recruiter_pipeline.daily_run
ls data/recruiter-leads.json
deactivate && cd ..
```

**Recruiter seed boards:** `seed_boards.json` ships with verified-live *large*
companies to prove the plumbing. Big firms have in-house recruiting, so for real
leads, swap in **SMB** board slugs (`boards.greenhouse.io/<slug>`,
`jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`). This dry-run is the one place
with real-world uncertainty — eyeball the output before trusting it.

---

## 3. Preview the outreach emails (sends nothing)

From the `outreach-platform` repo (adjust the `../lead-platform/...` paths):
```bash
cd system_b
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

python -m system_b.scripts.recruiter_walkthrough \
  --inventory ../../lead-platform/recruiter_pipeline/data/recruiter-leads.json

python -m system_b.scripts.bookkeeping_walkthrough \
  --inventory ../../lead-platform/cfo_pipeline/data/bookkeeping-leads.json

python -m system_b.scripts.insurance_agency_walkthrough \
  --trucking-inventory ../../lead-platform/insurance_pipeline/data/trucking-leads.json \
  --pc-inventory       ../../lead-platform/insurance_pipeline/data/pc-leads.json
```
Pass a real prospect list with `--csv` (columns: `firm_name,city,state,first_name`
plus `function` for recruiters, or `subniche`/`focus`/`website` for insurance
agencies — the packs auto-detect from a `website` when the specialty is blank).

---

## 4. Publishing (later — needs website work first)

To serve these inventories (not just read them locally), each needs a Vercel
Blob upload endpoint in the **website** repo, mirroring `api/upload-cfo-leads.js`:
`api/upload-trucking-leads.js`, `api/upload-pc-leads.js`,
`api/upload-bookkeeping-leads.js`, `api/upload-recruiter-leads.js`.

Once those exist, set the matching env vars (reusing the existing
`LEADS_UPLOAD_API_KEY` bearer secret) and run each pipeline with `--upload`:
```
TRUCKING_UPLOAD_URL=https://www.ishaangpta.com/api/upload-trucking-leads
PC_UPLOAD_URL=https://www.ishaangpta.com/api/upload-pc-leads
BOOKKEEPING_UPLOAD_URL=https://www.ishaangpta.com/api/upload-bookkeeping-leads
RECRUITER_UPLOAD_URL=https://www.ishaangpta.com/api/upload-recruiter-leads
# *_UPLOAD_API_KEY = the same LEADS_UPLOAD_API_KEY bearer secret
```

**Only then** add daily crons (copy `.github/workflows/cfo-leads.yml`, swap the
run command). Until the endpoints exist, a `--upload` cron would fail and the
gitignored JSON would be lost — so hold off on crons until §4 is done.

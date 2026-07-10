# Multi-niche expansion — build spec (decisions locked)

## Architecture decision (locked)
One **shared, well-tagged inventory per signal** in `lead-platform` (the scraper).
**All** niche/seniority/geo routing happens in `outreach-platform`'s per-buyer packs
(the System B pattern: research the buyer's site → classify their niche → fit-gate →
gift ~3 fitting companies). The scraper never decides who a lead is "for"; it emits
facts with good tags.

Why: outreach-platform already does site-based niche detection + per-buyer fit.
Rebuilding that as scraper niches would duplicate the matcher 4×. The scraper's only
new job is broad inventories that honor a consistent **tag contract**.

---

## Tag contract (every inventory carries these so outreach can route)
- `industry` — coarse company industry (exists; sometimes wrong, caught by the LLM fit gate)
- `location` — city + state (FMCSA has it; jobs must keep it)
- `function` — hiring signals: finance / security / IT / sales / …
- `role_tier` — hiring signals: junior / mid / senior  ← **new**
- `unique_role_count` — hiring-volume signal: distinct open roles per company in the window  ← **new**

---

## Scraper (lead-platform) changes, per inventory

### A. Finance-hire inventory — EXISTS (`cfo_pipeline`), widen + retag. Serves CFO **and** bookkeeping. No new file.
Widen the finance-title taxonomy downward and tag each hire with `role_tier`:

- **junior (bookkeeping-leaning):** Bookkeeper · Staff Accountant · Junior Accountant ·
  Accounting Clerk · Accounting Assistant · AP Clerk/Specialist · AR Clerk/Specialist ·
  Accounts Payable/Receivable Specialist · Payroll Specialist/Administrator · Billing Specialist
- **mid:** Accounting Manager · Assistant Controller · Senior Accountant · Finance Manager
- **senior (CFO-leaning):** Controller · VP Finance · Director/Head of Finance
- **disqualifier (unchanged):** open **full-time CFO** posting → drop for both buyers

Routing rule (in outreach, not scraper):
- A **lone junior finance hire at a small company = the hottest bookkeeping lead** (accounting
  pain, no commitment to an in-house department → outsource window).
- Several finance hires / a finance *leader* (Controller, VP Finance) → fractional-CFO lead.
- Full-time CFO → neither.

### B. Hiring-volume inventory — NEW (biggest real work). New Blob key + endpoint.
- **"Hiring heavily" = 3+ UNIQUE roles in a rolling 30-day window** (dedupe by normalized
  title — 3 posts of the same job ≠ heavy). *(30d is a starting point; widen to ~45d if volume is thin.)*
- Requires **per-company aggregation** of distinct open roles — the current per-signal schema
  doesn't tally this.
- **Stop discarding** job-heavy companies (existing pipelines filter them out; recruiters want them).
- **Sources — primary: ATS boards (Greenhouse / Lever / Ashby).** They're *company-centric* —
  one call = a company's full open-role list — so counting unique roles is accurate, not inferred.
  Seed the company universe from companies already seen across pipelines + ATS discovery; keep
  existing RSS/HN feeds as supplementary breadth.
- Tag per company: `unique_role_count`, the `function`s they're hiring, `industry`, `location`.

### C. Trucking new-carrier inventory — EXISTS (`insurance_pipeline/sources/fmcsa.py`). New Blob key + endpoint.
- Already emits new authorities with `city` + `state`. Publish as its **own** inventory
  (distinct from the P&C/growth inventory) so the insurance outreach pack can pull the right
  list per sub-niche. Minimal code change — a trucking-filtered output + a dedicated upload.

---

## Outreach (outreach-platform) changes — thin per-buyer packs
Engine (research → classify → fit → gift → card) reused. Each niche = a `niches/<buyer>/` pack.

- **niches/insurance_agency/** (router): research site → sub-niche.
  - trucking → trucking new-carrier inventory, filtered to the agent's **licensed states**
  - p&c → growth inventory *(built later)*
- **niches/bookkeeping/**: draws the shared finance-hire inventory; fit prefers a **lone `role_tier=junior`** hire.
  (CFO pack draws the same inventory, prefers mid/senior.)
- **niches/recruiter/**: draws hiring-volume inventory. Site niched → heavy hirers in that
  industry; generalist → heavy hirers in their **geography** (fallback). Reuses `gift/fit.py`.

### New shared engine capability: geography matching
Trucking + recruiter-fallback both match on **location**; today `fit.py` matches an industry
`niche_label` only. Add a small geo filter/scorer (lead.location vs buyer territory) — shared.

---

## Publishing architecture (decided)
Mirror the existing convention (each inventory = its own Vercel Blob key + `api/generate-*`
endpoint, read by outreach via `SCRAPER_BASE_URL`), because consumers read inventories by URL
and separate keys keep them decoupled.
- Finance-hire inventory → **existing** cfo key, now carrying `role_tier` (serves CFO + bookkeeping)
- Hiring-volume inventory → **new** key + endpoint (recruiters)
- Trucking new-carrier inventory → **new** key + endpoint
- P&C growth inventory → new key later

---

## Honest risks / watch-items
- **Industry tags are noisy** — recruiter niche-match leans hardest on them; LLM fit gate mitigates, costs a call per candidate.
- **Volume aggregation + ATS ingestion is the genuinely new build** — company-centric schema, company universe to maintain.
- **Licensed states** for insurance agents come from buyer input; scraper can't infer them.
- **P&C incumbency** — soft, incumbent-locked signal, deferred on purpose.
- **Benefits/PEO — dropped**: trigger is an event ("crossed 50"), we only get a noisy static headcount. No clean signal.

---

## Build order (locked)
1. **Trucking insurance** — FMCSA inventory exists → publish its own key + build geo-match + insurance pack (trucking branch). One niche end-to-end.
2. **Bookkeeping** — widen finance taxonomy + `role_tier` tag → bookkeeping pack routes on a lone junior hire. No new source, no new file.
3. **Recruiters** — hiring-volume inventory (ATS ingestion + per-company unique-role tally, 3-in-30d) + new key + recruiter pack with geo fallback.
4. **Commercial P&C** — second signal into the existing insurance pack.

---

## Implementation status
- **Step 1 (trucking inventory publish)** — scraper side: `insurance_pipeline.daily_run` now writes a
  trucking-filtered sub-inventory (`data/trucking-leads.json`) alongside the full insurance inventory and
  uploads it to `TRUCKING_UPLOAD_URL` (best-effort; skipped if env unset). Outreach-side geo-match + the
  insurance_agency pack are the remaining part of step 1, tracked in `outreach-platform`.

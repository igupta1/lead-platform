# Insurance pipeline v2 — decoupled

v1 (in git history at `df38dcd^`) bundled insurance with MSP/MSSP/Cloud
as a fourth niche on the shared pipeline. Result: the `/insurance`
dashboard was 96% MSP leads sorted by a 0 insurance score. This plan
throws that out and builds insurance as a sibling pipeline.

## v1-of-v2 ship: scope-reduction note (post-smoke)

What the first smoke run revealed:

1. **FMCSA deferred** — same as planned, MCMIS bulk needs its own
   iteration.
2. **FL SunBiz blocked by Cloudflare.** SunBiz's search-by-date UI
   sits behind Cloudflare's bot-challenge page ("Just a moment...").
   Plain `requests` scraping returns the challenge HTML, not the
   results table. Shipping SunBiz requires either Playwright /
   headless browser (heavyweight in CI) or a paid bypass service
   (Bright Data, ScraperAPI, OpenCorporates with a key). The
   `sos_fl.py` parser is kept on disk for when we wire one of those
   in. **Not in `_SOURCES` for v1.**
3. **SMB cap raised 250 → 500.** Insurance buyers span the lower
   middle market more broadly than IT-MSP buyers do. A 340-person
   fintech (Kodiak AI in the first smoke) is squarely a CFO-buying-
   D&O target, not enterprise. The MSP cap was too tight here.
4. **funding alone carries v1.** Until SunBiz unblocks or FMCSA
   lands, funding is the only live source. Source-diversity
   criterion fails by design this ship.

Path back to source-diversity in priority order:
- OpenCorporates paid key → SunBiz/CO/WA filings, no scraping
- FMCSA MCMIS monthly bulk → real commercial-auto signal
- Playwright in cron → SunBiz scrape, but slow runners

## Constraints (set by user)

1. **MSP/MSSP/Cloud pages unchanged.** Revert every v1 edit that
   touched the shared pipeline or the existing dashboards. The three
   niches stay on one pool of leads, one DB, one cron, one JSON.
2. **Insurance is fully isolated.** Its own directory, DB, sources,
   cron, JSON blob, and API endpoint. Two leads coincidentally
   surfacing in both dashboards is allowed, but the pipelines have
   zero shared code state and never read each other's data.
3. **Sources must be insurance-relevant.** No carrying over the
   "scrape IT job titles" template — those don't predict commercial
   insurance buying. Pick sources that map to a real commercial-lines
   buying trigger.

## Architecture

```
pipeline/                          ← MSP / MSSP / Cloud (unchanged, 3 niches)
  msp_pipeline/
  data/leads.db
  data/leads.json

insurance_pipeline/                ← NEW, parallel, isolated
  insurance_pipeline/
    models.py        # NicheName enum removed (one niche per pipeline now)
    db.py            # different schema: no it_msp_* / mssp_* / cloud_* columns
    daily_run.py
    enrichment.py    # finance/ops/owner DM prompt by default
    scoring.py       # signals weighted for commercial-insurance buying
    outreach.py
    apollo.py        # finance/ops/HR title list, no IT bias
    sources/
      fmcsa.py          # new
      sos_fl.py         # new
      ...
  data/leads.db
  data/leads.json
  tests/

api/
  generate-leads.js               ← unchanged, serves MSP blob
  generate-insurance-leads.js     ← NEW, serves insurance blob

src/pages/Insurance.js            ← edit: point at the new endpoint

.github/workflows/
  daily-leads.yml                 ← unchanged
  insurance-leads.yml             ← NEW, runs insurance_pipeline only

Vercel Blob:
  leads-current.json              ← MSP/MSSP/Cloud (existing)
  insurance-leads-current.json    ← NEW
```

Two pipelines, same shape, zero shared imports. `LeadCard.js` and
`LeadFilters.js` are reused but they're already niche-agnostic
React — they just render whatever JSON shape the API hands them.

## Decoupling (revert MSP-side v1 edits)

Files that need to revert to pre-v1 state:
- `pipeline/msp_pipeline/models.py` — drop `NicheName.INSURANCE`, drop
  `JOB_OPS_ROLE` / `JOB_BLUE_COLLAR` / `JOB_FLEET_ROLE` /
  `JOB_FINANCE_OPS` / `NEW_BUSINESS_FILED`, drop `SourceName.FILINGS`,
  drop `insurance_score` / `insurance_insight`.
- `pipeline/msp_pipeline/db.py` — drop the insurance columns + index +
  niche map entries. Keep the migrations-before-indexes fix (that was
  a real bug; orthogonal to insurance).
- `pipeline/msp_pipeline/scoring.py` — drop `NicheName.INSURANCE`
  weights and `_INSURANCE_VENDOR_NAME_RES`.
- `pipeline/msp_pipeline/outreach.py` — drop INSURANCE framing, revert
  `_SCORING_SIGNAL_TYPES` and `_SIGNAL_PAYLOAD_FIELDS`.
- `pipeline/msp_pipeline/daily_run.py` — drop INSURANCE niche entries,
  revert `_SCORING_SIGNAL_TYPES`, drop the import of `jobs_insurance` +
  `business_filings`.
- `pipeline/msp_pipeline/enrichment.py` — revert the DM prompt to the
  original IT-first text.
- `pipeline/msp_pipeline/apollo.py` — revert title list and
  `_score_person` to the original IT-only scoring.
- `pipeline/msp_pipeline/sources/jobs_insurance.py` — delete (moves to
  insurance_pipeline).
- `pipeline/msp_pipeline/sources/business_filings.py` — delete.
- `pipeline/tests/*` — revert the assertions extended to 4 niches.
- `api/upload-leads.js` — `REQUIRED_NICHES` back to `['it_msp', 'mssp', 'cloud']`.
- `api/generate-leads.js` — `VALID_NICHES` back to 3.

What stays in shared code:
- `src/pages/Insurance.js` + the route in `App.js` + the Header NavLink —
  they point at the new endpoint after this work.
- `src/components/LeadCard.js` — the `SIGNAL_KIND_META` entries and
  `new_business_filed` render branch I added are insurance-only signal
  types. Keeping them in the shared component is fine; MSP cards never
  carry those types post-decoupling.

## Source strategy — where insurance leads actually come from

The honest answer to "where do we find insurance prospects" is:
**different sources than MSP**. IT job postings are weak insurance
signals; carrier-of-record changes and risk-exposure changes are
strong signals. Ranked by signal quality × access cost:

### Tier 1 — build first

1. **FMCSA (Federal Motor Carrier Safety Administration) MCMIS census.**
   - Why: every commercial trucking entity in the US must register
     with FMCSA and carries commercial auto insurance (or self-bond,
     rare). When a new USDOT number is issued, that company *needs*
     commercial auto coverage by federal mandate.
   - Where: free monthly snapshot ZIP at
     `https://ai.fmcsa.dot.gov/SMS/Tools/Downloads.aspx`. ~3.5M motor
     carriers; ~10–15K newly-registered per month.
   - Signal: `new_motor_carrier_authority` with payload {usdot,
     legal_name, dba, address, fleet_size_power_units, drivers,
     carrier_op_status, issue_date}.
   - DM: usually the owner-operator (small carriers) or safety
     director (mid-size). FMCSA filings include a contact name.
   - Why this beats SoS filings: pre-filtered to one of the highest
     insurance-spend verticals; explicit insurance-mandate signal.

2. **FL SunBiz daily corporate filings (direct web scrape, not FTP).**
   - Why: Florida files ~5K new business entities per day. New entity
     = needs GL / WC / property starter coverage. FL is also the user's
     stated target geography and has by far the most accessible bulk
     data of any state.
   - **Primary path: scrape `search.sunbiz.org`'s daily filings UI.**
     The GitHub Actions runners are spotty on anonymous FTP and the
     SunBiz FTP has had multi-day outages. Web scraping is not
     noticeably harder than parsing cordata fixed-width and it's
     resilient to FTP being deprecated. Build one path well rather
     than two paths poorly.
   - Signal: `new_business_filed` with payload {state=FL, filing_type,
     filed_on, registered_agent, officers}.
   - DM: registered agent is frequently a service company (CT
     Corporation, Northwest Registered Agent, etc.) which is useless
     as a sales target. Apollo enrichment is **required** here, not
     optional — without it the DM panel surfaces the service company
     as the lead.
   - Beats v1's OpenCorporates path: no API key, no rate limit, no
     401.

### Tier 2 — second wave once Tier 1 is live

3. **OSHA inspection records.**
   - Why: an OSHA inspection (especially with citations) is a workers
     comp re-rating trigger.
   - **Initial weight: 20, not 35.** Workers comp is mandatory in
     49 states, so most inspected employers already have coverage —
     the trigger here is "carrier might re-rate or non-renew," not
     "needs coverage." Also skewed toward construction /
     manufacturing rather than commercial lines broadly. Softer
     signal than new-entity filings; weight reflects that.
   - Where: free API at `https://www.osha.gov/pls/imis/establishment.html`,
     plus monthly DOL bulk CSV downloads.
   - Signal: `osha_inspection_recorded` {establishment, sic, naics,
     citations, penalty}.

4. **CO + WA Secretary of State daily filings.**
   - Why: same as FL but expands geographic coverage. CO has a clean
     bulk-download CSV; WA has corp-search export pages.
   - Where: `https://www.coloradosos.gov/biz/` and
     `https://ccfs.sos.wa.gov`.
   - Signal: `new_business_filed`, same shape as FL.

5. **NYC building permits (DOB).**
   - Why: a new construction permit = WC + GL + builders-risk
     exposure for the contractor. Restricted to one geography but
     NYC's DOB OpenData is one of the cleanest public feeds in the US.
   - Where: NYC OpenData Socrata API.
   - Signal: `building_permit_issued` {job_type, owner_name,
     contractor_name, estimated_cost}.

### Tier 3 — defer / drop

- **OpenCorporates** — paid for any real volume. v1 demonstrated the
  free tier returns 401 on most queries.
- **Crunchbase / Dodge / Bizapedia** — paid SaaS.
- **State liquor license boards** — high signal for restaurant/bar
  GL+property but every state has a different application portal.
  Defer until a beachhead vertical is chosen.
- **County property recorder transactions** — 3,000+ counties, no
  national free feed.
- **DMV / fleet registrations** — DPPA blocks marketing use.

### What we are NOT bringing over from v1

- The `jobs_insurance` keyword classifier. Office-manager and CDL-driver
  postings are weak signals on their own — the source produced 4 real
  leads on the demo run, three of them ops postings at companies that
  weren't strongly insurance-shaped (Tin Can, IATSE Local 15). It can
  come back as a Tier 2/3 confirmation signal, but Tier 1 doesn't need
  it.
- The OpenCorporates-backed `business_filings` source. Drop in favor
  of direct FL SunBiz.

## Success criteria — defined before building

v1 failed in part because nobody pre-committed to what "good" looked
like, so a 96%-MSP-leads dashboard slipped through. v2 ships only if
all three of the following are true after the first non-dry-run.

- **Volume.** ≥30 distinct leads on `/insurance` with a non-null
  score, AND ≥10 of those carrying a complete DM panel (name + at
  least one of email / LinkedIn).
- **Source diversity.** Neither single source contributes >75% of the
  top 30. FMCSA-only or SoS-only both fail. Once both Tier 1 sources
  are live, each must contribute ≥25% of the top 30.
- **Smell test.** A stranger handed `/insurance` with no context
  should answer "what is this dashboard for?" with something like
  "selling insurance to small businesses" within 10 seconds of
  scrolling. If they say "IT services" or anything about a CIO, v1
  has shipped again under a new name.

Red-flag list — any of these in the top 30 means we've failed by the
same mode as v1:
- An IT-job-posting card
- An insurance carrier / broker / MGA at any rank
- Top 10 dominated by a single signal type
- A company whose primary signal is unrelated to a commercial-lines
  buying trigger

## LeadCard render shapes — sketched before building

Two new signal types render on `/insurance`. **No card layout changes
needed**; the existing DM-panel + signals-list layout handles both. A
FMCSA filing's USDOT / fleet / authority data lives inside the signal
pill payload, not as a top-line chip.

**`new_motor_carrier_authority`** (FMCSA):
- Pill: reuse the yellow / fleet color (already in `SIGNAL_KIND_META`
  from v1 — `bg-yellow-500/15`).
- Primary text: `Motor carrier · N power units` (or `Motor carrier`
  alone if size unknown).
- Extra: `authority issued <date>`.
- Link: `https://safer.fmcsa.dot.gov/CompanySnapshot.aspx?query=<usdot>`
  → "SAFER".
- Industry classifier should always emit `logistics_transport` for
  these — skip the LLM call for known FMCSA leads.
- DM panel: Apollo's org-search by legal name + small-biz title
  preference (Owner / Founder / President) handles owner-operators.
  Mid-size carriers (50+ trucks) fall back to FMCSA's contact name
  field when Apollo misses.

**`new_business_filed`** (FL SunBiz):
- Pill: lime `bg-lime-500/15` (already wired in v1).
- Primary text: `<filing_type> filed in <state>` + `filed <date>`.
- No link (SunBiz doesn't have stable public URLs per filing).
- DM panel: Apollo is **load-bearing** (see Tier 1 notes). Without
  it, the registered agent service co. shows up as the lead.

## Scoring shape for insurance

Single-niche pipeline → simple, no `NicheName` enum.
Signal weights (initial, tuned post-first-run):

```python
SIGNAL_WEIGHTS = {
    NEW_MOTOR_CARRIER_AUTHORITY: 50,  # explicit insurance mandate
    NEW_BUSINESS_FILED:          45,  # starter-coverage need
    BUILDING_PERMIT_ISSUED:      30,  # contractor exposure (Tier 2)
    FUNDING_RAISED:              25,  # D&O / benefits scale-up (reused if added)
    OSHA_INSPECTION_RECORDED:    20,  # WC re-rate trigger, NOT new-coverage (Tier 2)
}
```

30-day half-life decay carries over from MSP. SMB cap (headcount ≤ 250)
carries over.

**Vendor purge (not score-zero, not a contradiction with v1).**
Insurance brokers / carriers / MGAs / TPAs get **purged** from the
insurance DB pre-scoring — dropped entirely from the pool. v1
score-zeroed them so they stayed available to the other niches; v2
is single-niche, so a broker has no other home to be preserved for.
Same insight selected score-zero in v1's architecture and selects
purge in v2's. Different architectures, different right answers, same
goal: brokers don't appear on the insurance dashboard.

**Jobs-insurance keyword classifier — dropped, data-backed.** v1's
smoke run produced 4 leads from this source in the top-of-page slot.
Three were generic office-manager / ops-coordinator postings at
companies with no other insurance-relevant context (Tin Can, IATSE
Local 15, Springfield Rehab). The classifier is high-recall,
low-precision — useful as a confirmation signal (a FMCSA carrier
that ALSO posted a controller hire = higher intent) but not strong
enough to justify its own source slot in v1.

## Build order

1. **Decouple.** Revert v1 changes from `pipeline/msp_pipeline/` and
   shared files (list above). Confirm MSP/MSSP/Cloud pages render
   identically to pre-v1.
2. **Scaffold `insurance_pipeline/`.** Mirror MSP's directory layout
   but with a single-niche schema. Reuse `llm.py` *by copy*, not by
   import — keep zero cross-imports between the two packages.
3. **Build FMCSA source.** Monthly MCMIS pull, parse, diff against
   prior snapshot, emit `new_motor_carrier_authority` candidates.
4. **Build FL SunBiz source.** Daily FTP + cordata parser. Emit
   `new_business_filed` candidates.
5. **Wire scoring / enrichment / outreach.** Finance/ops/owner DM
   prompt by default; no IT bias.
6. **New serverless endpoint** `api/generate-insurance-leads.js`
   reading from `insurance-leads-current.json` blob.
7. **Point `src/pages/Insurance.js`** at the new endpoint.
8. **New cron** `.github/workflows/insurance-leads.yml`, runs at a
   different hour than the MSP cron to avoid concurrent commits.
9. **Smoke run** via `workflow_dispatch` with `--limit 20`.
10. **Tune** weights and the carrier-name disqualifier on observed
    data.

## Open questions

1. **Directory location** — `insurance_pipeline/` at the repo root, or
   nested under `pipeline/insurance_pipeline/`? Top-level is more
   honest (it's a sibling, not a subpackage); nested keeps all
   Python in `pipeline/`. **Lean: top-level.**
2. ~~**Apollo for insurance**~~ — **Resolved: keep Apollo on.** FL
   SunBiz filings list a registered agent service company (CT
   Corporation, Northwest, etc.) instead of a human for the majority
   of LLC filings. Without Apollo, the DM panel surfaces that service
   co. as the lead — the visible defect on the dashboard. Accept the
   doubled credit burn; the failure mode without it is worse than the
   cost.
3. **Coincidental cross-dashboard appearance** — if "TransGlobal
   Insurance Agency" gets a FMCSA filing AND a breach, it'd appear
   on both dashboards (zero on MSP via no filter; zero on insurance
   via vendor regex). The vendor regex on insurance already handles
   this on the insurance side. Acceptable? **Lean: yes, dedup is
   not worth a cross-DB query.**
4. ~~**FL SunBiz FTP reliability**~~ — **Resolved: web-scrape the
   daily filings UI as primary, drop FTP entirely.** GitHub Actions
   runners are flaky on anonymous FTP and SunBiz's FTP has had
   multi-day outages historically. Build one path well. (See Tier 1
   #2 for the corresponding source-description update.)
5. **MCMIS file size** — the monthly census ZIP is ~250MB. Don't
   commit it to git. Either fetch on every run and discard, or store
   in Vercel Blob. **Lean: fetch + discard.**

---

**Where to view this plan:** `pipeline/INSURANCE_PLAN.md` (this file).
Once `insurance_pipeline/` lands, this plan moves there.

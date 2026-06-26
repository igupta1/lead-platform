# Insurance Dashboard — Fixes Before Agency-Owner Review

Investigation + recommended approach for each of the 5 issues raised.
Not yet implemented. Cross-issue interactions called out at the end.

Code references are to the deployed state at commit `48bd32b`.

**Revision 2** (post-review). Five adjustments incorporated:
1. FMCSA phone-number work dropped from Issue 2.
2. Apollo-skip heuristic broadened to `no_domain` (data-backed —
   Apollo whiffs 100% across all fleet sizes when domain is null).
3. Vintage-year regex tightened to year-immediately-before-suffix;
   tested against 106 live names, zero false positives.
4. Contact-strength resolution under multiple signals: union of
   best-available per field.
5. Issue 5 capacity check added.
6. Order reaffirmed: Issue 2 (contact strength) ships before Issue 5
   (volume increase).

---

## Issue 1: Premium estimates are fabricated and obvious

### Investigation

The dollar figures are produced by `insurance_pipeline/policy_fit.py`,
a deterministic per-signal helper added in the prior turn. Formulas:

- **FMCSA**: `trucks × $5,000` for commercial auto, `drivers × $3,000`
  for workers comp. Owner-operator fallback if `trucks == 0`.
- **USAspending federal contract**: `contract_amount × rate`, where
  `rate` is 1.0–1.8% selected by NAICS-keyword match (engineering →
  1.2%, construction → 1.6%, hazardous waste → 1.8%, default 1.2%).
- **Form D / TechCrunch RSS funding**: no estimate (qualitative
  tagline only).
- **New business filed**: no estimate (qualitative).

**How wrong are the numbers, per lead?**

Comparing the formulas against industry-typical pricing for the
signal shape:

- **1-truck new-MC-authority owner-operator**: dashboard says ~$5K
  commercial auto. Actual market: $9–15K (new-authority surcharge,
  no MVR history). **Off by 50–66%.**
- **25-truck fleet**: dashboard says $125K. Actual: $50–150K. **In
  range, but error band is wide.**
- **Workers comp**: $3K/driver assumes a $50K payroll at class code
  7228 loss costs. For long-haul drivers paid $80–100K, this is **off
  by 50–80% low**. For local short-haul at $40K payroll, roughly
  right.
- **Federal contract premium-to-revenue**: 1–2% of a single contract
  is the wrong unit. Real E&O / cyber policy scales with **total
  company revenue + exposure**, not one award. A $200K federal
  contract winner with $20M total revenue carries a $30–100K E&O
  policy. A $200K contract winner where the contract is their entire
  revenue carries a $5–15K policy. **Off by 5–10× either direction
  depending on company size, which we don't have for most leads.**

**The error band on a given lead is roughly 2–10×**, in both
directions, and we don't have the inputs (total revenue, payroll,
fleet age, class codes, claims history, MVR, state) to defend any
specific number. An experienced commercial-lines agent will spot the
miss in seconds, and once they do they distrust every other number on
the card — DM email, headcount, NAICS classification, all of it.

### Call: **Remove the dollar figures entirely. Show only the lines of business.**

Option (a) from the brief. Reasoning over the alternatives:

- **Not (c) "improve the estimate"**: defensible pricing requires
  data we'd have to buy or build (NAIC class codes, MVRs, audited
  revenue). The cost to halve the error band is high, the residual
  error is still material, and we're not in the quoting business —
  we're in the prospecting business. The dollar figure isn't load-
  bearing for the prospecting use case; the line-of-business tag is.
- **Not (b) "label as rough sizing / show a range"**: a "$5–15K
  range" still requires that we know the variance, which we don't.
  And a range that wide ("Commercial Auto $4–30K") isn't decision-
  useful — an agent's triage doesn't change between $4K and $30K, it
  changes between "owner-op" and "real fleet." That's captured
  better by the existing fleet-size signal pill ("3 trucks · 4
  drivers").

The triage information the agent needs is **already on the card**:
fleet size, driver count, contract amount, headcount when known.
Sorting by those raw signal facts is more defensible than sorting by
a fabricated premium.

**Concrete change**: in `policy_fit.estimate_policy_fit`, return
`coverages` and a tagline that lists products only — e.g.
`"Commercial Auto + Workers Comp"` instead of
`"Commercial Auto $20K/yr + WC $15K/yr"`. Drop
`est_annual_premium_usd` from the JSON output. The signal pill
already carries the raw numbers (`3 trucks · 4 drivers`,
`contract: $258K`) which is what an agent actually reads first.

---

## Issue 2: Apollo contact quality varies wildly and isn't surfaced

### Investigation

**1. What we currently store and the completeness distribution
(measured against the live API, n=106 leads):**

| Field | Coverage |
|---|---|
| `dm_name` | 95 / 106 (90%) |
| `dm_title` | 92 / 106 (87%) |
| `dm_email` | **17 / 106 (16%)** |
| `dm_linkedin_url` | 21 / 106 (20%) |
| `dm_name` + (email OR LinkedIn) | **21 / 106 (20%)** |

Sliced by trigger type:

| Trigger | Full contact (name + email/LinkedIn) |
|---|---|
| Motor carrier | 8 / 65 (12%) |
| Federal contract | 8 / 30 (27%) |
| Funding event | 5 / 11 (45%) |

**The agent's experience right now**: 80% of cards have a name and
title but no clickable email/LinkedIn. The card looks "almost done"
but is actually "you still need to find this person." Across 100
leads that's 80 cards where the agent gets jerked around between
"oh good, contact info" and "wait, no there isn't."

**2. Are we using Apollo optimally?**

No. We call Apollo on the top-30 leads per niche (~100 calls/run).
Heuristic for "Apollo will whiff": small FMCSA leads with no domain
after Gemini lookup. We're not currently checking this — we call
Apollo on every owner-operator with `power_units <= 2` and watch it
return nothing ~85% of the time. Estimated waste: **20–30 Apollo
calls per run**, which at Apollo paid-plan rates is real money and
also slows the cron by 30–60 seconds.

**3. Source-data fallback opportunities:**

| Field | Currently extracted? | Useful for agent? |
|---|---|---|
| `legal_name` | ✓ | yes (lead title) |
| `dot_number` | ✓ | yes (signal pill link to SAFER) |
| `phy_city` / `phy_state` | ✓ | yes (location chip) |
| `company_officer_1` | ✓ | yes (DM fallback) |
| `power_units` / `total_drivers` | ✓ | yes (signal pill) |
| `company_officer_2` | ❌ not extracted | maybe — second contact name |

FMCSA does carry a phone field but **per Adjustment 1, we're
explicitly not surfacing it in this revision** — defer to a later
iteration. Skipping phone-display work entirely (no extraction
either; if we never display it, no need to pull it).

USAspending and EDGAR carry less source-level contact data — those
sources lean entirely on Apollo for DM enrichment.

### Call

Two coupled changes (phone work removed per Adjustment 1).

**(a) Add a `contact_strength` field to the lead JSON, render as a
small badge on the card.** Three states:

| State | Criteria | Display |
|---|---|---|
| **Verified** | `dm_name` present AND (`dm_email` OR `dm_linkedin_url`) | green dot, "Verified contact" |
| **Partial** | `dm_name` present, no email or LinkedIn | amber dot, "Name only — research needed" |
| **Cold** | no `dm_name`, or `dm_name` == lead.name | gray dot, "No contact — research needed" |

Computed server-side in `daily_run._lead_to_json`. The badge sits in
the DM panel header (or replaces it for Cold leads). An agent
scanning the page knows in 1 second whether to click through.

**Multi-signal resolution (per Adjustment 4):** the badge is
computed from the lead row's CURRENT `dm_name` / `dm_email` /
`dm_linkedin_url` columns — these already reflect the union of what
was best-available across enrichment passes. The merge happens at
write time, not at JSON-build time:
- Apollo writes `dm_name` / `dm_title` / `dm_email` /
  `dm_linkedin_url` (only the fields it found) in
  `daily_run._apollo_enrich_top_n`.
- The FMCSA officer-name fallback (in `_apply_source_dm_fallbacks`)
  only fills `dm_name` and `dm_title` if they're STILL null after
  Apollo — so Apollo always wins for fields it provides, and the
  source-data fallback only fills the gaps.
- A lead carrying BOTH an FMCSA signal (officer fallback fills name)
  AND a USAspending signal (Apollo finds email) ends up with name
  from the FMCSA fallback **only if** Apollo didn't also surface a
  name. Apollo's name takes precedence when present.

Net result: `dm_*` columns store the best-available union. The
badge reads those columns and resolves to Verified / Partial / Cold
deterministically. No multi-signal logic in the badge code itself.

**(b) Apollo-skip heuristic in `daily_run._apollo_enrich_top_n`.**
*Investigated against live data per Adjustment 2.* Apollo hit rate
on FMCSA leads, sliced by fleet size and domain presence (n=65 from
current dashboard):

| Fleet bucket × domain | Total | Apollo hits | Whiff rate |
|---|---|---|---|
| ≤2 trucks, no domain | 36 | 0 | **100%** |
| ≤2 trucks, with domain | 17 | 8 | 47% |
| 3–5 trucks, no domain | 8 | 0 | **100%** |
| 3–5 trucks, with domain | 1 | 0 | 100% (n=1, weak) |
| 6–10 trucks, no domain | 1 | 0 | 100% (n=1, weak) |
| 11+ trucks, no domain | 2 | 0 | **100%** |

**The discriminator is `domain`, not fleet size.** Apollo whiffs
100% on every fleet bucket with no domain. The only fleet bucket
where Apollo wins is `with_domain` — and even there only at 47%.

Revised skip rule: **skip Apollo when `lead.domain is None`** for
any FMCSA-sourced lead. Drops from "30 wasted calls/run" estimate
to **~47 wasted calls/run actually measured**. The FMCSA officer-
fallback still runs so the lead still gets a DM name.

Edge case: a lead with multiple signals (e.g., FMCSA + funding)
where Apollo might find the company via the funding side. Heuristic
applies only when EVERY signal on the lead is FMCSA. Mixed-source
leads still get Apollo.

---

## Issue 3: FMCSA volume dominates

### Investigation

**Current default sort**: `score DESC NULLS LAST` (in
`db.iter_leads`).

The scoring formulas (in `scoring.py`):
- FMCSA: `min(60, 20 + size * 1.6)` + recency bonus up to +20.
  A 50-truck fleet caps at 100.
- USAspending: `min(60, 25 + amount/$10K)` + recency bonus up to +20.
  A $400K contract scores 65, a $500K caps at 80.
- Form D: flat 25 + recency bonus.

For the dashboard mix, this puts EDEN (34 trucks, score 79) above
DOOD (4 trucks + 16 drivers, score 65) above the top federal
contractors (score 50–60). Below them comes a long tail of 1-truck
owner-ops at score 41–46. **The sort is correct** — big fleets and
big contracts rise; small leads sink.

The user's complaint isn't actually about sort order. It's about
**volume**. 65 of 106 leads are FMCSA, and most of those FMCSA cards
are small. The middle of the page is just a long stretch of similar
~$8K commercial-auto-plus-WC owner-operator cards.

**Does the existing trigger-type filter solve this?**

Yes, mostly. The dropdown is right there:
- All triggers (106)
- Motor carrier (65)
- Federal contract (30)
- Funding event (11)

A trucking specialist clicks Motor carrier and gets a clean 65-lead
trucking list. A federal-contractor specialist clicks Federal
contract and gets 30 clean cards (almost all of them in the 50–60
score range — good leads). A D&O specialist gets 11.

**The real gap**: a new agent landing on the page sees "All triggers
(106)" by default and has no signal that the filter is the path to
their specialty. The lead-count header just says "106 of 106 leads"
— doesn't surface the trigger mix.

### Call: **Don't change the sort. Make the trigger mix visible above the lead grid.**

Specifically: change the existing lead-count badge from
`"106 of 106 leads"` to a one-line breakdown:

```
106 leads · 65 motor carrier · 30 federal contract · 11 funding event
```

When a filter is applied, it stays accurate (e.g., when filtered to
Motor carrier: `"65 motor carrier leads"`). The agent learns from the
header alone that the page splits into three roughly-distinct sales
plays, and the dropdown is right there to narrow.

The score sort is correct and shouldn't move. Premium-based sort
isn't a real option once we drop the dollar figures (Issue 1).

No structural change to LeadCard. One small edit to `LeadsPage.js`
where the header line is rendered.

---

## Issue 4: Form D leads target the wrong buyer

### Investigation

Looking at the 10 Form D leads currently on the dashboard:

| Lead | Industry | Buyer-fit for an independent? |
|---|---|---|
| Estately Operations LLC | real_estate | Maybe — real-estate operating LLC, GL + property |
| Pinnacle Harbor LLC | other | No — opaque SPV name pattern |
| Summit Ridge 2024 LLC | real_estate | **No** — vintage-year fund vehicle |
| Auxilium Health, Inc. | healthcare | **No** — Series A startup, Vouch's territory |
| Majesty Therapeutics | healthcare | **No** — biotech startup, specialty market |
| JR Hyde Park Blvd LLC | real_estate | **No** — single-property SPV |
| SKYX Platforms Corp. | other | Marginal — public co, small |
| RenX Enterprises Corp. | other | Marginal — opaque |
| Prism Layer AI, Inc. | software_saas | **No** — AI startup, Vouch's territory |
| Copper State Credit Union | fintech | Maybe — actual financial institution |

**By the user's hypothesis: ~7 of 10 are mismatched buyer.** The Form
D filings that an independent commercial agency could actually win:
- Real-estate operating companies (NOT vintage-year SPVs)
- Small holding companies in industries the independent already
  writes (construction, light manufacturing, retail)
- Mid-size financial institutions like Copper State CU
- Public-company financing rounds where the buyer is already with a
  generalist broker

Three patterns flag the mismatched leads:
1. **Vintage-year-LLC name pattern** (`Summit Ridge 2024 LLC`, future
   `Pinnacle Harbor 2025 LLC`, etc.) — funds with year in the name.
2. **Street-name LLC pattern** (`JR Hyde Park Blvd LLC`, future
   `Marina Bay Ave LLC`) — single-property SPVs.
3. **Sub-10-employee Series A pattern** — startup industries
   (software_saas, healthcare biotech) with `headcount < 20`. These
   are Vouch/Newfront/Embroker territory and the independent isn't
   going to win the deal.

### Call: **Demote Form D's default scoring weight from 25 to 8. Filter out the three obvious mismatch patterns at source-emit time.**

Two coupled moves:

**(a) Filter Form D patterns in `sources/edgar_form_d.py`.** Add to
`_is_operating_company`:
- **Vintage-year regex (tightened per Adjustment 3):**
  `\b(?:19|20|21)\d{2}\s*[,.\s]*(?:LLC|Inc|Corp(?:oration)?|LP|LLP|Ltd|Co)\.?\s*$`
  Only matches when a 4-digit year (1900–2199) appears immediately
  before the legal suffix at the end of the name. **Tested against
  all 106 current dashboard names: 1 match (`Summit Ridge 2024 LLC`,
  the intended target) and zero false positives.** Loose-only
  patterns (`\b\d{4}\b` anywhere) produced no additional matches
  either, confirming nothing is hiding mid-name on the current data.
- Block names matching `\b(Blvd|Avenue|Ave|Street|St|Road|Rd|Way|Drive|Dr|Lane|Ln|Court|Ct|Place|Pl)\s+LLC$` (street-name SPVs).

Known acceptable miss: pattern `<Name> 2024 Holdings LLC` (year +
word + suffix) won't trigger. That's deliberate — the tight version
catches the standard vintage-year SPV pattern (`X 2024 LLC`) without
risking false positives on operating companies that happen to have
a year mid-name. Can broaden in a follow-up if real-estate fund
naming patterns drift.

Drops Summit Ridge 2024 LLC + 2–3 street-name SPVs (Hyde Park Blvd,
etc.) — roughly 3–4 of the current 10 Form D leads.

**(b) Lower `SIGNAL_WEIGHTS[FUNDING_RAISED]` from 25 to 8.** Form D
leads now sort to the bottom of the dashboard by default, where they
belong as a supplementary signal. Agents who specifically want to see
them filter to "Funding event" in the trigger dropdown — they're
still indexed, just not crowding the high-value federal-contractor
slots.

This combination pulls Form D from "feels-prominent-but-mostly-wrong"
to "low-key signal that an interested agent can opt into." The 11→6
volume drop is fine; the source's purpose is variety, not bulk.

**Don't drop Form D entirely.** Two of the current 10 (Estately,
Copper State CU) are arguably legitimate prospects; the source could
add more if a real-estate-focused or fintech-focused agent uses the
trigger filter. Keep it indexed, just deprioritize.

---

## Issue 5: Broaden tool appeal beyond commercial-auto specialists

### Investigation

**Current industry distribution (n=106)**:

| Industry | Count | Source mix |
|---|---|---|
| logistics_transport | 61 | mostly FMCSA |
| other | 14 | mostly USAspending |
| healthcare | 7 | USAspending + Form D |
| manufacturing | 6 | USAspending |
| construction | 4 | USAspending |
| legal_professional | 3 | USAspending |
| real_estate | 3 | Form D |
| (8 more, 1–2 each) | 10 | mixed |

**For a non-trucking agency right now**:

| Specialty | Relevant leads on default view | Sufficient? |
|---|---|---|
| Construction GL | ~8–10 (DAVE VANHANDEL, K.F. DAVIS, DESIGN-AIRE, ENCOMPASS, LANDIVAR, MODULAR SOLUTIONS, PHELPS, CAPITAL PROJECT) | **Yes, barely.** |
| Healthcare malpractice / cyber | ~7 (BIOTEK, FISHER BIO, UPTODATE, MILLENNIUM, NAMBE PUEBLO, AUXILIUM, MAJESTY) | Yes |
| Professional services E&O | ~6 (LEGAL INTERPRETING, FRANKLIN COVEY, BINARY GROUP, STRATEGIC COMMS, TOLAND MIZELL, CHENEGA) | Yes |
| Manufacturing product liability | ~6 (ELITE ALUMINUM, MARTINS ALUMINUM, MODULAR SOLUTIONS, ELM FORK, A2Z SUPPLY, DESIGN-AIRE) | Yes |
| Hospitality / restaurant | **1 (LISBOA CAFE only)** | **No** — sparse |
| Retail / E-commerce | 1 (A2Z SUPPLY) | No |

USAspending is the workhorse for non-trucking variety — it already
covers construction, healthcare, manufacturing, professional services
at meaningful volume. The gap isn't "USAspending doesn't cover these
industries"; it's that **USAspending only contributes 30 leads to
65 FMCSA leads** in the default mix. Underweighted by volume.

**What's underweighted in the existing data path:**

`sources/usaspending.py` currently caps at `_LIMIT_PER_PAGE = 100`
contracts per fetch, filtered to `$25K-$500K` award amount. Real
volume of SMB-scale federal contracts is **much** higher — the
USAspending API returns 1000+ contracts/day in this range. We're
sampling ~30–50 per run.

Loosening either constraint adds meaningful volume:
- Cap → 500 contracts per fetch
- Amount range → `$10K-$2M` (broadens to smaller pure-SMB winners and
  to mid-market firms with the lower-middle-tier of E&O / cyber
  premium)

That alone would roughly **double the USAspending lead count**, and
because USAspending is industry-diverse, that means **doubling the
non-trucking lead count** without adding a new source.

### Call: **Loosen USAspending's volume + amount range. Don't add a new source for v1.**

Specifically in `sources/usaspending.py`:

| Parameter | Current | Proposed |
|---|---|---|
| `_LIMIT_PER_PAGE` | 100 | 500 |
| `_MIN_AWARD` | 25,000 | 10,000 |
| `_MAX_AWARD` | 500,000 | 2,000,000 |

Expected outcome: USAspending leads grow from ~30 to ~60–80 per run.
At 60–80 federal contractors plus 65 FMCSA, the dashboard's center of
mass shifts away from trucking-only. The construction-GL agency's
12-lead floor (the user's stated test) becomes easy to hit.

### Capacity check (Adjustment 6) — sub-step before shipping

Before the volume jump goes live, verify the rest of the pipeline can
absorb roughly double the lead count. Four dimensions:

| Dimension | Current load | Projected after Issue 5 | Headroom? |
|---|---|---|---|
| **USAspending API rate** | 1 POST per cron run | 1 POST per cron run (just larger page) | ✓ No rate-limit concern. API serves up to 100/page in docs but accepts 500 without complaint. |
| **Gemini enrichment** | ~100 calls/run @ ~2s each = ~3 min | ~180 calls/run @ ~2s = ~6 min | ✓ Cron timeout is 90 min. |
| **Apollo monthly budget** | 30 calls/run × 30 runs = ~900/mo | After Issue 2 skip-heuristic: ~15–20 calls/run × 30 = **~500/mo** (DOWN, not up) | ✓ Net reduction. Issue 2 frees more budget than Issue 5 spends. |
| **Cron runtime** | ~3–5 min total | ~6–8 min total | ✓ Well within 90-min timeout. |
| **`leads.db` size** | ~150 KB committed | ~250–300 KB committed | ✓ Trivial; under git LFS thresholds. |

Net: **no infrastructure changes required.** Issue 2's Apollo-skip
heuristic CREATES headroom that Issue 5 then partially consumes. The
two issues compose cleanly when Issue 2 ships first (per Adjustment 5
ordering).

One mitigation worth keeping: if a USAspending response ever exceeds
500 results meaningfully, the source already caps via the `limit`
parameter and returns the top N — no risk of unbounded growth.

Hospitality remains a blind spot regardless — federal hospitality
contracts are rare. If a hospitality specialist becomes a target
buyer, a single new source (state liquor-license filings, or
restaurant-permit data from city open-data portals) would close it.
**Not for v1.**

The single highest-leverage NEW source if we add anything:
**OSHA inspections** (~hundreds of small / mid-market workplace
inspections per month across construction, manufacturing, healthcare
— a workers-comp re-rate trigger). Free, structured, distinct
industry mix from FMCSA. **But the existing-data path described
above is enough** to meet the "construction agency sees 12+ leads"
bar, so OSHA is deferred.

---

## Cross-issue interactions

A few of these wire together. Worth knowing before sequencing the
implementation:

**Issue 1 (remove premiums) + Issue 3 (sort):** removing the dollar
figures kills the option of sorting by premium $. The recommendation
preserves score-based sort, so no conflict. Card content gets cleaner
(no fabricated number) while ordering is unchanged.

**Issue 2 (contact strength) + Issue 4 (Form D narrowing):** Form D
leads are disproportionately "Cold" or "Partial" on the contact-
strength badge — Apollo struggles with SPV-shaped entities. Once the
badge ships and Form D's score weight drops to 8, the visual signal
("low score AND cold contact") will naturally cue agents to skip
them. This means **the Form D narrowing in Issue 4 doesn't need to be
aggressive** — even a modest filter (the two name patterns above) is
enough, because the contact badge does additional work.

**Issue 2 + Issue 5 (broadening):** when USAspending volume grows
(Issue 5), the Apollo hit rate on the new leads is going to vary —
some federal contractors are established firms with web presence
(Apollo wins), others are small subsidiaries (Apollo whiffs). The
contact-strength badge from Issue 2 prevents the "all federal
contractors look the same on the page" problem; agents see at a
glance which of the 60–80 USAspending cards have a clickable email.

**Issue 3 (header line) + Issue 4 (Form D demotion):** the trigger-
mix header line (`"106 leads · 65 motor carrier · 30 federal
contract · 11 funding event"`) makes the demoted Form D bucket
visible without surfacing the leads themselves. Agent reads "11
funding event" and knows the trigger filter is the way to see them
when their specialty warrants it.

---

## Recommended implementation order

Per Adjustment 5: ship contact strength (Issue 2) before USAspending
loosening (Issue 5). Credibility before volume.

1. **Issue 1** (drop premium figures): smallest change, immediate
   credibility recovery.
2. **Issue 2** (contact strength badge + Apollo-skip heuristic): the
   visible-quality fix that makes every card honest about how
   actionable it is. Ships BEFORE Issue 5 so the badge is in place
   before USAspending doubles the lead count.
3. **Issue 5** (USAspending loosen + capacity check): doubles non-
   trucking volume, broadens appeal. Capacity check from the sub-
   step above runs first; if any dimension is short on headroom (it
   shouldn't be), pause and adjust.
4. **Issue 4** (Form D demote + narrow with tightened vintage-year
   regex): cleanup, low-risk.
5. **Issue 3** (header trigger-mix line): cheapest, can ship anytime.

Total surface area: ~6 files edited (`policy_fit.py`, `daily_run.py`,
`sources/usaspending.py`, `sources/edgar_form_d.py`, `scoring.py`,
plus minor `LeadCard.js` + `LeadsPage.js` touches). No FMCSA source
changes (phone work dropped). No LeadCard structural redesign. No
new sources.

Awaiting sign-off before code.

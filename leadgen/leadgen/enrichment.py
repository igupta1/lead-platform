"""leadgen LLM company-enrichment layer.

A pure-code disqualifier pass plus a two-provider web lookup:

- Size cap at 100 employees for every niche. A company larger than that
  is buying full-time in-house, not the fractional / outsourced service
  this inventory sells.
- Service-provider pre-purge (CPA firms, bookkeeping shops, other
  fractional-CFO providers, accounting / advisory firms, recruiters).
  They aren't magnets, they're the competition — the vendor a prospect
  would buy FROM, not a company that would buy.
- Gemini grounded-search lookup for size / location / domain and an
  ``insight`` one-liner describing what the company does, plus an OpenAI
  niche classifier keyed off that insight.

Re-enrichment is signal-aware and tracked by the ``enriched_at`` column:
a lead is (re-)enriched when it has never been enriched OR a buying
signal has been captured since the last enrichment.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone

from pydantic import BaseModel

from leadgen import db, llm, taxonomy
from leadgen.models import Lead, SignalType, SourceName
from leadgen.sources.edgar_form_d import (
    _INVESTMENT_VEHICLE_KEYWORDS_RE,
    _REAL_ESTATE_KEYWORDS_RE,
    _ROMAN_NUMERAL_TAIL_RE,
    _STREET_SPV_RE,
    _TICKER_LLC_RE,
    _TRAILING_DIGIT_SPV_RE,
    _TRANCHE_SUFFIX_RE,
    _VINTAGE_YEAR_RE,
)
from leadgen.sources.jobs import (
    _is_auto_dealer_name,
    _is_hotel_name,
    _is_public_sector,
    _is_recruiter_name,
)

log = logging.getLogger(__name__)


# --- Pure-code disqualification filter ------------------------------------
#
# Runs BEFORE any LLM call inside purge_disqualified, and as a secondary
# gate inside enrich(). Cheap regex pass that drops leads which clearly
# don't fit "SMB buying a fractional CFO" without burning Gemini tokens
# on them.

# Coarse enrichment ceiling (per-niche scoring tightens it: accounting/cfo cap
# at 100, msp/mssp/cloud at 250). 250 is the general ceiling here so mid-size
# companies survive enrichment and reach the IT-niche scorers. A breach is a
# valid signal at ANY size (breached orgs are often large hospitals/systems),
# so breach leads are effectively uncapped — see ``_headcount_cap``.
_SMB_HEADCOUNT_CAP = 250
_BREACH_HEADCOUNT_CAP = 100_000

_BLOCKED_TLDS: tuple[str, ...] = (".gov", ".mil", ".edu")

_BLOCKED_NAME_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:State|City|County|Town) of\b", re.IGNORECASE),
    re.compile(r"\bDepartment of\b", re.IGNORECASE),
    re.compile(r"\bU\.?S\.? (?:House|Senate|Department|Government|Army|Navy|Air Force)\b", re.IGNORECASE),
    re.compile(r"\b(?:University|College)\b", re.IGNORECASE),
    re.compile(r"\bVentures?\b", re.IGNORECASE),
    re.compile(
        r"\b(?:Capital|Holdings?|Investments?) "
        r"(?:Partners|LLC|Inc|LP|L\.P\.|Corp(?:oration)?|Group|Fund|Management|Investments?)\b",
        re.IGNORECASE,
    ),
)

# Mega-corps that shouldn't be SMB leads. They have full finance orgs.
_BLOCKED_DOMAINS: frozenset[str] = frozenset({
    "google.com", "alphabet.com",
    "apple.com",
    "meta.com", "facebook.com",
    "microsoft.com",
    "amazon.com", "aws.amazon.com",
    "netflix.com",
    "tesla.com",
    "openai.com",
    "anthropic.com",
    "nvidia.com",
    "oracle.com",
    "salesforce.com",
    "ibm.com",
    "intel.com",
    "cisco.com",
})

# CFO-competitor names — purged outright. A company whose business IS
# providing fractional-CFO / accounting / bookkeeping services is the
# competition, not the prospect. They have their own finance leadership
# and they're who the prospect would buy FROM, not someone who'd buy.
_CFO_COMPETITOR_NAME_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:Fractional|Outsourced|Virtual|Part[-\s]?Time)\s+CFO\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bCFO\s+(?:Services|Solutions|Partners|Group|Advisory|Consulting)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:CPA|CPAs|Certified\s+Public\s+Accountants?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:Accounting|Bookkeeping|Tax)\s+(?:Services|Solutions|Group|Firm|Partners|Associates|Advisors|Consultants|LLC|Inc)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:Financial|Finance|Wealth)\s+(?:Advisors?|Advisory|Planning|Management|Consultants?|Consulting)\b",
        re.IGNORECASE,
    ),
)


def _domain_blocked(domain: str | None) -> bool:
    if not domain:
        return False
    d = domain.lower().strip().rstrip("/")
    if d in _BLOCKED_DOMAINS:
        return True
    return any(d.endswith(tld) for tld in _BLOCKED_TLDS)


def _name_blocked(name: str) -> bool:
    return any(p.search(name) for p in _BLOCKED_NAME_RES)


def _is_cfo_competitor(name: str) -> bool:
    return any(p.search(name) for p in _CFO_COMPETITOR_NAME_RES)


# Financial-vehicle names that slip past EDGAR's source-level filter
# end up in the DB and persist across runs. Same retroactive cleanup
# pass as insurance_pipeline.
_FINANCIAL_VEHICLE_NAME_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bBancshares\b", re.IGNORECASE),
    re.compile(r"\bBancorp\b", re.IGNORECASE),
    re.compile(r"\bFinance\s+(?:Corp|Company|Inc|LLC|L\.?P\.?)", re.IGNORECASE),
    re.compile(r",\s*L\.?\s?L?\s?P\.?\s*$", re.IGNORECASE),
    re.compile(r"^TPG\b", re.IGNORECASE),
    re.compile(r"^Blackstone\b", re.IGNORECASE),
    re.compile(r"^KKR\b", re.IGNORECASE),
    re.compile(r"^Carlyle\b", re.IGNORECASE),
    re.compile(r"^MidOcean\b", re.IGNORECASE),
    # Tranche-suffix SPV: "AQR Flex 1 Series LLC - Series B9", "Some
    # Vehicle - Series 2024". The dash-separated tranche label at the
    # end is the cheap-to-detect SPV fingerprint.
    re.compile(r"\s+-\s+Series\s+\S+\s*$", re.IGNORECASE),
    # Bare letter+digit "Series" suffix at end: "X Series B9" / "Y
    # Series II". Conservative — only matches when the tranche label
    # looks like a fund/series identifier (letter+digit, Roman numeral,
    # or pure digits).
    re.compile(
        r"\bSeries\s+(?:[A-Z]\d+|[IVX]{1,5}|\d+)\s*$",
        re.IGNORECASE,
    ),
)


def _is_financial_vehicle(name: str) -> bool:
    return any(p.search(name) for p in _FINANCIAL_VEHICLE_NAME_RES)


_MEGACORP_PREFIX_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:AT&?T|ATT)\b", re.IGNORECASE),
    re.compile(r"^Verizon\b", re.IGNORECASE),
    re.compile(r"^T-Mobile\b", re.IGNORECASE),
    re.compile(r"^Sprint\b", re.IGNORECASE),
    re.compile(r"^Comcast\b", re.IGNORECASE),
    re.compile(r"^Johnson\s+Controls\b", re.IGNORECASE),
    re.compile(r"^AECOM\b", re.IGNORECASE),
    re.compile(r"^Honeywell\b", re.IGNORECASE),
    re.compile(r"^Siemens\b", re.IGNORECASE),
    re.compile(r"^Raytheon\b", re.IGNORECASE),
    re.compile(r"^Lockheed\b", re.IGNORECASE),
    re.compile(r"^Northrop\b", re.IGNORECASE),
    re.compile(r"^Boeing\b", re.IGNORECASE),
    re.compile(r"^General\s+Dynamics\b", re.IGNORECASE),
    re.compile(r"^Booz\s+Allen\b", re.IGNORECASE),
    re.compile(r"^Leidos\b", re.IGNORECASE),
    re.compile(r"^SAIC\b", re.IGNORECASE),
    re.compile(r"^IBM\b", re.IGNORECASE),
    re.compile(r"^Oracle\b", re.IGNORECASE),
    re.compile(r"^Microsoft\b", re.IGNORECASE),
    re.compile(r"^Amazon\b", re.IGNORECASE),
    re.compile(r"^Google\b", re.IGNORECASE),
    re.compile(r"^Cisco\b", re.IGNORECASE),
    re.compile(r"^Accenture\b", re.IGNORECASE),
    re.compile(r"^Deloitte\b", re.IGNORECASE),
    re.compile(r"^KPMG\b", re.IGNORECASE),
    re.compile(r"^Ernst\s+&?\s+Young\b", re.IGNORECASE),
    re.compile(r"^PricewaterhouseCoopers\b", re.IGNORECASE),
    re.compile(r"^PwC\b", re.IGNORECASE),
    # Large chains / brands that slip through on unknown headcount
    # (surfaced by the live-feed audit).
    re.compile(r"^Gabe'?s\b", re.IGNORECASE),
    re.compile(r"^Sono\s+Bello\b", re.IGNORECASE),
    re.compile(r"^USA\s+Clinics\b", re.IGNORECASE),
    re.compile(r"^Revelyst\b", re.IGNORECASE),
    re.compile(r"^MasTec\b", re.IGNORECASE),
    re.compile(r"^Bon\s+App", re.IGNORECASE),
    re.compile(r"^CookUnity\b", re.IGNORECASE),
    re.compile(r"^Chrome\s+Hearts\b", re.IGNORECASE),
    re.compile(r"\bErickson\s+Senior\s+Living\b", re.IGNORECASE),
    # Large null-headcount subsidiaries surfaced by the live-feed audit.
    re.compile(r"^Canon\b", re.IGNORECASE),
    re.compile(r"^W(?:ü|u)rth\b", re.IGNORECASE),
    re.compile(r"^JCB\b", re.IGNORECASE),
    re.compile(r"^Karl\s+Storz\b", re.IGNORECASE),
    re.compile(r"^SXSW\b", re.IGNORECASE),
    re.compile(r"^Hengli\b", re.IGNORECASE),
)


# Brand-name deny list. Distinct from _MEGACORP_PREFIX_RES because
# these are CONSUMER-BRAND names (no parent prefix) that surface in
# job scrapes as if they were standalone companies. Tinder slipped
# past the lookup (null headcount), so the upstream size checks aren't
# reliable for this class. Exact-match
# on the normalized lead name is the durable fix — iterate this list
# when new false positives appear on the dashboard.
# Known recruiter / staffing firm names that don't carry the regex
# signature (no "Search Group" / "Talent" / "Staffing" tokens). The
# `is_recruiting_firm` lookup catches these on first enrichment, but an
# already-enriched lead skips re-enrichment (signal-aware `enriched_at`),
# so they need a hard-coded entry to be swept on the next purge pass.
# Iterative deny-list — extend as new false positives surface.
_KNOWN_RECRUITER_NAMES: frozenset[str] = frozenset(
    name.lower() for name in (
        "Hoxton Circle",
        "AmpersandPeople",
        "Ampersand People",
        "Forrer Group",
        "Forrer Group, Inc.",
    )
)


def _is_known_recruiter(name: str) -> bool:
    return name.strip().lower() in _KNOWN_RECRUITER_NAMES


_MEGACORP_BRAND_NAMES: frozenset[str] = frozenset(
    name.lower() for name in (
        # Match Group
        "Tinder", "Hinge", "OkCupid", "Match", "Plenty of Fish", "Meetic", "BLK",
        # Meta / Alphabet / Apple / Microsoft / Amazon brand surfaces
        "Instagram", "WhatsApp", "Threads", "Reality Labs",
        "YouTube", "Waymo", "Verily", "DeepMind", "Wing", "Fitbit", "Nest",
        "LinkedIn", "GitHub", "Xbox", "Bethesda", "Activision",
        "Activision Blizzard", "Mojang", "Skype", "Bing",
        "Twitch", "Whole Foods", "Audible", "IMDb", "Zappos", "Ring", "Eero",
        "Beats", "Beats by Dre",
        # Disney / Comcast / others
        "Hulu", "ESPN", "Marvel", "Lucasfilm", "Pixar", "Disney+",
        "NBC", "Peacock", "Sky", "Universal Pictures", "DreamWorks",
        # Mid-tier captives
        "Red Hat", "NetSuite", "Slack", "MuleSoft", "Tableau", "Heroku",
        "Splunk", "Figma",
        # 3rd-review leaks
        "MLB", "Major League Baseball", "Major League Baseball (MLB)",
        "Formula 1", "Formula 1 Las Vegas Grand Prix", "F1",
        "NFL", "NBA", "NHL", "MLS", "PGA",
        "Goldman Sachs", "Goldman Sachs Private Credit Corp.",
        "Box", "Raytheon", "Viasat", "Hilton", "Temu",
        "Tractor Supply", "Kettering Health Network", "Koch Foods",
        "BronxCare Health System", "Paramount Pictures",
    )
)


def _is_megacorp_subsidiary(name: str) -> bool:
    if any(p.match(name) for p in _MEGACORP_PREFIX_RES):
        return True
    # Exact-match against brand names (case-insensitive, trimmed).
    cleaned = name.strip().lower()
    return cleaned in _MEGACORP_BRAND_NAMES


# National NGO giants that slip the ≤100 headcount cap because they file with
# null headcount (the CARE / care.org class, same failure mode as Canon USA).
# nonprofit stays a legitimate served niche, so this is EXACT-match on the
# national brand only — a local "Catholic Charities of Denver" chapter, which
# can be a real fractional-CFO client, is deliberately NOT matched.
_LARGE_NGO_NAMES: frozenset[str] = frozenset(
    name.lower() for name in (
        "CARE", "CARE USA",
        "Catholic Charities", "Catholic Charities USA",
        "United Way", "United Way Worldwide",
        "American Red Cross", "The American Red Cross", "Red Cross",
        "The Salvation Army", "Salvation Army",
        "Goodwill", "Goodwill Industries", "Goodwill Industries International",
        "Habitat for Humanity", "Habitat for Humanity International",
        "YMCA", "YWCA",
        "Feeding America",
        "Boys & Girls Clubs of America", "Boys and Girls Clubs of America",
        "St. Jude", "St. Jude Children's Research Hospital",
        "Make-A-Wish", "Make-A-Wish Foundation",
        "UNICEF", "USA for UNICEF",
        "World Wildlife Fund", "WWF",
        "Doctors Without Borders",
        "Oxfam", "Oxfam America",
        "Save the Children",
        "American Cancer Society",
        "American Heart Association",
        "Planned Parenthood", "Planned Parenthood Federation of America",
        "AARP",
        "Sierra Club",
        "ACLU", "American Civil Liberties Union",
        "Teach for America",
        "The Nature Conservancy",
        "March of Dimes",
        "Special Olympics",
        "The Humane Society", "Humane Society of the United States",
    )
)


def _is_large_ngo(name: str) -> bool:
    return name.strip().lower() in _LARGE_NGO_NAMES


# Both SEC funding signal types. Treated interchangeably for "has a
# funding signal" gating (Form D private placement + Form C crowdfunding).
_FUNDING_SIGNAL_TYPES: frozenset[SignalType] = frozenset({
    SignalType.FUNDING_FORM_D,
    SignalType.FUNDING_FORM_C,
})


def _has_funding_signal(lead: Lead) -> bool:
    return any(s.type in _FUNDING_SIGNAL_TYPES for s in lead.signals)


def _is_form_d_noise(lead: Lead) -> bool:
    """Vintage-year / street-SPV / tranche / real-estate / vehicle
    regexes from the EDGAR source applied retroactively. Gated to
    leads that actually have a funding signal so we don't accidentally
    drop a real operating company that happens to have a year or
    'series' word in its name.

    The 3rd-review pass exposed that source-only filters left dozens
    of pre-existing rows untouched (Lightstone Direct I, Alpha Wave CI
    V, Cupressus Apartments, Reno City Center Owner, BWM Private
    Equity II, HG SPV1, Level 5 Multifamily, MCR Macon Investco, ZRP
    Avalon Crossing, Vadnais Heights, etc.). This function now
    mirrors the full EDGAR-source filter at purge time."""
    if not _has_funding_signal(lead):
        return False
    name = lead.name
    return bool(
        _VINTAGE_YEAR_RE.search(name)
        or _STREET_SPV_RE.search(name)
        or _TRANCHE_SUFFIX_RE.search(name)
        or _ROMAN_NUMERAL_TAIL_RE.search(name)
        or _TRAILING_DIGIT_SPV_RE.search(name)
        or _TICKER_LLC_RE.search(name)
        or _REAL_ESTATE_KEYWORDS_RE.search(name)
        or _INVESTMENT_VEHICLE_KEYWORDS_RE.search(name)
    )


# Hiring-signal leads — any of the seven job-post signal types — are the
# money leads. A small, obscure company the web lookup can't size or
# geolocate is a PRIME target, not a reject, so an unknown headcount /
# country must not delete it the way it does the weak funding-only class.
# Explicit disqualifiers (non-US, oversized, competitor, megacorp) still
# apply to every lead regardless of signal.
_HIRING_SIGNAL_TYPES: frozenset[SignalType] = frozenset({
    SignalType.JOB_FRACTIONAL_CFO,
    SignalType.JOB_FINANCE_LEAD,
    SignalType.JOB_JUNIOR_FINANCE,
    SignalType.JOB_IT_SUPPORT,
    SignalType.JOB_IT_LEADERSHIP,
    SignalType.JOB_SECURITY,
    SignalType.JOB_CLOUD_DEVOPS,
})


def _has_hiring_signal(lead: Lead) -> bool:
    return any(s.type in _HIRING_SIGNAL_TYPES for s in lead.signals)


# Public-company ticker in the name, e.g. "... Corp (BRQL)" / "(QNCX)".
# A public company isn't a fractional-CFO buyer (spec Part 2 exclusion).
# Match a trailing 2-5 uppercase-letter parenthetical, excluding common
# non-ticker abbreviations (legal suffixes, country codes).
_TICKER_RE = re.compile(r"\(([A-Z]{2,5})\)\s*$")
_TICKER_DENYLIST: frozenset[str] = frozenset({
    "USA", "LLC", "INC", "PLC", "LTD", "LP", "LLP", "CO", "US", "UK",
    "EU", "AI", "IT", "HR", "PR", "NA", "DC", "USB",
})


def _has_ticker(name: str) -> bool:
    m = _TICKER_RE.search(name)
    return bool(m and m.group(1) not in _TICKER_DENYLIST)


# Foreign-parent US subsidiary naming ("Canon U.S.A.", "Würth Industry
# USA", "JCB North America", "Karl Storz — America"). Used ONLY as a
# headcount proxy when headcount is null: these are almost always large,
# and the ≤100 cap can't fire without a number. Gated on null headcount so
# a genuinely small "Acme USA LLC" that we DID size is untouched.
_FOREIGN_SUBSIDIARY_RE = re.compile(
    r"(?:\bu\.?\s?s\.?\s?a\b|\bnorth\s+america\b|\bamericas?\b)\.?"
    r"(?:\s*[-,]?\s*(?:inc|llc|corp(?:oration)?|co|ltd|limited|gmbh|group|holdings?)\.?)?\s*$",
    re.IGNORECASE,
)


def _is_foreign_subsidiary_name(name: str) -> bool:
    return bool(_FOREIGN_SUBSIDIARY_RE.search(name))


# Finance-vertical OPERATORS — banks, insurers, VC/PE, wealth managers.
# They have finance in-house (or ARE finance) and don't buy a fractional
# CFO. Distinct from fintech PRODUCT startups (Stripe, Plaid, Affirm),
# which are brand-named and don't match these operator patterns — so the
# fintech niche is preserved.
_NONFINANCIAL_BANK_RE = re.compile(
    r"\b(?:food|blood|data|sperm|milk|seed|piggy|toy|memory|word|bottle|"
    r"tissue|organ|eye|bone|gene|time|river|left|west)\s+bank",
    re.IGNORECASE,
)
_BANK_RE = re.compile(r"\bbank\b", re.IGNORECASE)
_FINANCE_VERTICAL_RE = re.compile(
    r"\b(?:"
    r"banc(?:orp|shares)|credit\s+union|"
    r"insurance|reinsurance|life\s+assurance|blue\s+(?:cross|shield)|"
    r"private\s+equity|venture\s+capital|"
    r"asset\s+management|investment\s+management|"
    r"wealth\s+(?:management|partners|advisors?|advisory|group)"
    r")\b"
    r"|\bcapital\s*$"     # trailing "... Capital" (VC / PE firm)
    r"|\bfinancial\s*$",  # trailing "... Financial" (advisory firm)
    re.IGNORECASE,
)


def _is_finance_vertical(name: str) -> bool:
    if _BANK_RE.search(name) and not _NONFINANCIAL_BANK_RE.search(name):
        return True
    return bool(_FINANCE_VERTICAL_RE.search(name))


def _headcount_cap(lead: Lead) -> int:
    """The size ceiling that applies to this lead. A breach is a valid signal at
    any size, so breach leads are effectively uncapped; everything else uses the
    general SMB ceiling."""
    if any(s.type == SignalType.BREACH_DISCLOSED for s in lead.signals):
        return _BREACH_HEADCOUNT_CAP
    return _SMB_HEADCOUNT_CAP


def _disqualification_reason(lead: Lead) -> str | None:
    if _domain_blocked(lead.domain):
        return f"blocked_domain={lead.domain}"
    if _name_blocked(lead.name):
        return "blocked_name_pattern"
    if _is_cfo_competitor(lead.name):
        return "cfo_competitor_name"
    if _is_financial_vehicle(lead.name):
        return "financial_vehicle"
    if _is_megacorp_subsidiary(lead.name):
        return "megacorp_subsidiary"
    if _is_large_ngo(lead.name):
        return "oversized_ngo"
    # Recruiting firms + auto dealers — apply the source-level name
    # patterns retroactively so leads ingested under a prior version
    # of the pipeline get swept out. Required by 3rd-review reqs #5
    # and #6.
    if _is_recruiter_name(lead.name) or _is_known_recruiter(lead.name):
        return "recruiter_name_pattern"
    if _is_auto_dealer_name(lead.name):
        return "auto_dealer_name_pattern"
    if _is_hotel_name(lead.name):
        return "hotel_name_pattern"
    if _is_public_sector(lead.name, lead.domain):
        return "public_sector_pattern"
    if _has_ticker(lead.name):
        return "public_company_ticker"
    if _is_finance_vertical(lead.name):
        return "finance_vertical"
    if _is_form_d_noise(lead):
        return "form_d_noise_pattern"
    if lead.headcount is not None and lead.headcount > _headcount_cap(lead):
        return f"oversized={lead.headcount}"
    if lead.headcount == 0:
        return "zero_headcount"
    # Headcount proxy: an unsized "[Name] USA / America / North America"
    # is almost always a large foreign-parent subsidiary the size cap
    # can't catch without a number.
    if lead.headcount is None and _is_foreign_subsidiary_name(lead.name):
        return "likely_oversized_subsidiary"
    # Unknown headcount is NOT a disqualifier on its own — a company the
    # average person hasn't heard of is a fine gift even unsized. Known
    # household names with no discoverable headcount are dropped in enrich()
    # via the lookup's household_name flag (we don't have that signal here).
    return None


def purge_disqualified(conn: sqlite3.Connection) -> int:
    """Pure-code pass. Two phases:

    1. Sweep the leads table for anything whose name_key sits in the
       persistent ``disqualified`` table (sticky CFO-competitor /
       recruiting-firm / auto-dealer marks from enrichment). This is the
       new piece relative to insurance_pipeline.
    2. Run the regex-based disqualifier on each lead (CFO competitors,
       financial vehicles, megacorps, oversized headcount).
    """
    deleted = 0
    leads = list(db.iter_leads(conn))

    # Phase 1: disqualified-table sweep.
    by_key: dict[str, Lead] = {}
    for lead in leads:
        if lead.id is None:
            continue
        by_key[lead.name_key] = lead
    for key, _name, reason in db.iter_disqualified(conn):
        target = by_key.get(key)
        if target is None or target.id is None:
            continue
        log.info(
            "purge: deleting %d (%s) — disqualified=%s", target.id, target.name, reason
        )
        try:
            db.delete_lead(conn, target.id)
            deleted += 1
        except Exception:
            log.exception("purge: delete_lead failed for id=%s", target.id)

    # Phase 2: regex disqualifier on what's left.
    for lead in db.iter_leads(conn):
        if lead.id is None:
            continue
        dq_reason = _disqualification_reason(lead)
        if dq_reason is not None:
            log.info("purge: deleting %d (%s) — %s", lead.id, lead.name, dq_reason)
            try:
                db.delete_lead(conn, lead.id)
                deleted += 1
            except Exception:
                log.exception("purge: delete_lead failed for id=%s", lead.id)
    return deleted


# --- Industry / niche vocabulary -------------------------------------------
#
# The taxonomy (granular niches + their coarse parents) lives in
# taxonomy.py. Leads are classified into a granular niche; the coarse
# ``industry`` is derived from it via ``taxonomy.parent_of``.


def _summarize_signals(lead: Lead) -> str:
    types_by_source: dict[SourceName, list[str]] = {}
    for sig in lead.signals:
        if sig.source == SourceName.COMPUTED:
            continue
        types_by_source.setdefault(sig.source, []).append(sig.type.value)
    if not types_by_source:
        return "(no signals)"
    parts = []
    for source, types in types_by_source.items():
        unique = sorted(set(types))
        parts.append(f"{source.value} ({', '.join(unique)})")
    return "; ".join(parts)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- Combined Gemini lookup -----------------------------------------------


_LOOKUP_PROMPT = """\
Look up the company "{name}" on the web. Reply with EXACTLY these lines and
nothing else (use "unknown" when you cannot determine a value):

HEADCOUNT: <integer employee count or "unknown">
CITY: <city name or "unknown">
STATE: <2-letter US state code (CA, NY, ...) or "unknown">
COUNTRY: <2-letter ISO country code (US, GB, CA, ...) or "unknown">
DOMAIN: <primary website domain like "acme.com" without https or path, or "unknown">
IS_CFO_COMPETITOR: <"yes" if this company itself SELLS CFO / accounting / \
bookkeeping / fractional-CFO / financial-advisory services (i.e. they're \
the competition, not a buyer), otherwise "no">
IS_RECRUITING_FIRM: <"yes" if this company is a recruiting / staffing / \
executive search firm (the lead would be the recruiter's CLIENT, not the \
recruiter itself), otherwise "no". Examples that should be "yes": \
Hoxton Circle, Pyxis Search Partners, AmpersandPeople, Forrer Group, \
Robert Half, any "Search Group" / "Search Partners" / "Search Masters" / \
"Talent Solutions".>
IS_AUTO_DEALER: <"yes" if this company is a car dealership, auto group, \
or vehicle reseller. The 'Finance Director' / 'Finance Manager' there is \
the person who arranges customer auto loans, NOT a corporate finance \
executive. Otherwise "no".>
HOUSEHOLD_NAME: <"yes" if this is a widely-recognized company or brand the \
average person would know — a national household name, a large public \
company, a major retailer / bank / airline / tech company / hospital system \
/ university. "no" for a company the average person has never heard of \
(local business, small startup, obscure private firm). When unsure, answer \
"no".>
INSIGHT: <ONE sentence (max 25 words), present tense, plain language, \
describing what the company does. Examples: "B2B SaaS for HVAC contractors, \
recently raised Series A." / "DTC apparel brand based in Brooklyn." / \
"Boutique consulting firm specializing in healthcare M&A." Avoid marketing \
fluff. Use "unknown" only as a last resort.>
"""

_FIELD_RE = {
    "headcount": re.compile(r"^HEADCOUNT:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "city": re.compile(r"^CITY:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "state": re.compile(r"^STATE:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "country": re.compile(r"^COUNTRY:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "domain": re.compile(r"^DOMAIN:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "is_cfo_competitor": re.compile(
        r"^IS_CFO_COMPETITOR:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
    ),
    "is_recruiting_firm": re.compile(
        r"^IS_RECRUITING_FIRM:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
    ),
    "is_auto_dealer": re.compile(
        r"^IS_AUTO_DEALER:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
    ),
    "household_name": re.compile(
        r"^HOUSEHOLD_NAME:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
    ),
    "insight": re.compile(r"^INSIGHT:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
}

_HEADCOUNT_NUM_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})*|\d+)\b")
_DOMAIN_CLEAN_RE = re.compile(r"^https?://|^www\.|/.*$", re.IGNORECASE)


def _round_to_10(n: int) -> int:
    return int(round(n, -1))


class _Lookup(BaseModel):
    headcount: int | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    domain: str | None = None
    is_cfo_competitor: bool = False
    is_recruiting_firm: bool = False
    is_auto_dealer: bool = False
    household_name: bool = False
    insight: str | None = None


def _parse_field(raw: str, field: str) -> str | None:
    m = _FIELD_RE[field].search(raw)
    if m is None:
        return None
    val = m.group(1).strip()
    if not val or val.lower() == "unknown":
        return None
    return val


def _parse_headcount(raw_value: str | None) -> int | None:
    if raw_value is None:
        return None
    m = _HEADCOUNT_NUM_RE.search(raw_value)
    if m is None:
        return None
    return _round_to_10(int(m.group(1).replace(",", "")))


def _parse_domain(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    cleaned = _DOMAIN_CLEAN_RE.sub("", raw_value).strip().lower()
    if not cleaned or "." not in cleaned or " " in cleaned:
        return None
    return cleaned


def _parse_yesno(raw_value: str | None) -> bool:
    if raw_value is None:
        return False
    return raw_value.strip().lower().startswith("y")


def lookup_company(lead: Lead) -> _Lookup:
    raw = llm.call_gemini(_LOOKUP_PROMPT.format(name=lead.name))
    return _Lookup(
        headcount=_parse_headcount(_parse_field(raw, "headcount")),
        city=_parse_field(raw, "city"),
        state=(_parse_field(raw, "state") or "").upper() or None,
        country=(_parse_field(raw, "country") or "").upper() or None,
        domain=_parse_domain(_parse_field(raw, "domain")),
        is_cfo_competitor=_parse_yesno(_parse_field(raw, "is_cfo_competitor")),
        is_recruiting_firm=_parse_yesno(_parse_field(raw, "is_recruiting_firm")),
        is_auto_dealer=_parse_yesno(_parse_field(raw, "is_auto_dealer")),
        household_name=_parse_yesno(_parse_field(raw, "household_name")),
        insight=_parse_field(raw, "insight"),
    )


# --- OpenAI niche classification ------------------------------------------


_NICHE_PROMPT = """\
Classify the company "{name}" into exactly ONE granular niche from the
catalog below.

What the company actually does (from a web lookup): {insight}

Recent signals (these describe the company's hiring / filing activity —
NOT what it sells; do not classify by the signal): {signals}

Available niches, grouped by their broad parent category:
{niches}

Rules:
- Pick the single best-fitting CHILD niche based on what the company
  sells or does (the insight above), not on the signal. Do not tag a
  supplement brand as a finance niche just because it's hiring a
  Controller.
- "vertical_saas" = software sold to one industry (e.g. dental software);
  "b2b_saas" = horizontal business software; "ai_ml" = the core product
  is an AI/ML model or platform.
- "biotech_pharma" / "medical_devices" are for drug / device / clinical
  companies; a healthcare-SERVICES clinic or practice is
  "medical_practice".
- Consumer product brands are "dtc_brand" / "cpg_food_beverage" /
  "apparel_fashion" / "beauty_personal_care" — NOT "marketplace" (a
  platform connecting third-party buyers and sellers).
- Classify by the company's PRIMARY operating business. A SERVICES or
  operations company is NOT a manufacturer: aircraft charter / aviation
  management / air transport is "fleet_services" or "unknown", NOT
  "aerospace_defense" (which is only for companies that MANUFACTURE aircraft,
  defense hardware, or components). Likewise a company that installs,
  distributes, resells, or services physical goods is not "manufacturing".
- If the company clearly serves MULTIPLE unrelated industries, or you cannot
  confidently pick ONE child, return "unknown" — do not default to the most
  prominent-sounding bucket.
- If the insight is "unknown" or too thin to judge, return "unknown".
  Do NOT guess "other" or a soft default — "unknown" is the correct
  answer when you cannot tell.
"""


class _NicheOut(BaseModel):
    niche: taxonomy.Niche


def _niche_catalog() -> str:
    lines = []
    for parent, children in taxonomy.PARENT_CHILDREN.items():
        if parent in ("other", "unknown"):
            continue
        lines.append(f"- {parent}: {', '.join(children)}")
    lines.append("- (fallbacks): other, unknown")
    return "\n".join(lines)


def classify_niche(lead: Lead, *, insight: str | None = None) -> str:
    """Classify into a granular niche (taxonomy.Niche value). The coarse
    industry is derived by the caller via ``taxonomy.parent_of``."""
    prompt = _NICHE_PROMPT.format(
        name=lead.name,
        niches=_niche_catalog(),
        insight=insight or lead.insight or "unknown",
        signals=_summarize_signals(lead),
    )
    out = llm.call_openai(prompt, response_model=_NicheOut)
    return out.niche.value


# --- Orchestrator ---------------------------------------------------------


def needs_enrichment(lead: Lead, force: bool = False) -> bool:
    """Signal-aware re-enrichment gate. A lead needs an enrichment pass
    when it has never been enriched (``enriched_at is None``) or a buying
    signal has been captured since the last enrichment. ``force`` always
    re-enriches. Tracked by the ``enriched_at`` column — there is no
    marker signal."""
    if force:
        return True
    if lead.enriched_at is None:
        return True
    return any(s.captured_at > lead.enriched_at for s in lead.signals)


def _is_form_c_funding_only(lead: Lead) -> bool:
    """A Form C (Reg Crowdfunding) lead that arrived pre-filled from the
    filing (domain + headcount) and has no hiring signal. Its domain,
    size, and location are already known, and a tiny Reg-CF issuer won't
    be a CFO-services competitor — so it can be enriched WITHOUT a Gemini
    grounded-search call (the light path)."""
    if _has_hiring_signal(lead):
        return False  # bullseye — worth the full lookup
    if lead.domain is None:
        return False  # need Gemini to resolve a domain, or it can't ship
    return any(s.type == SignalType.FUNDING_FORM_C for s in lead.signals)


def needs_gemini_lookup(lead: Lead, force: bool = False) -> bool:
    """True when enriching this lead will spend a Gemini grounded-search
    call. Already-enriched leads and Form C funding-only leads (light
    path) cost no Gemini, so they must not count against the daily
    budget."""
    if not needs_enrichment(lead, force):
        return False
    return not _is_form_c_funding_only(lead)


def _form_c_location(lead: Lead) -> tuple[str | None, str | None]:
    for s in lead.signals:
        if s.type == SignalType.FUNDING_FORM_C:
            p = s.payload or {}
            return p.get("biz_location"), p.get("biz_state")
    return None, None


def _enrich_form_c_light(conn: sqlite3.Connection, lead: Lead) -> bool:
    """Light enrichment for a Form C lead: NO Gemini call. Domain,
    headcount, and location come straight from the filing; only the
    (separate, cheap) OpenAI industry classifier runs. The pure-code
    disqualifier already ran in enrich()."""
    assert lead.id is not None
    niche = classify_niche(lead, insight=None)
    updates: dict[str, object] = {
        "industry": taxonomy.parent_of(niche),
        "niche": niche,
        "country": "US",
        "enriched_at": _utcnow(),
    }
    city, state = _form_c_location(lead)
    if city and not lead.city:
        updates["city"] = city
    if state and not lead.state:
        updates["state"] = state
    db.update_lead(conn, lead.id, **updates)
    log.info(
        "enrich (light/Form C): %d (%s) niche=%s — no Gemini call",
        lead.id, lead.name, niche,
    )
    return True


def enrich(conn: sqlite3.Connection, lead: Lead, *, force: bool = False) -> bool:
    """Web-lookup enrichment. Writes domain / headcount / city / state /
    industry / niche / insight and stamps ``enriched_at``. Returns True
    when the lead was enriched, False when it was skipped (already
    enriched, no new signal) or deleted as disqualified."""
    if lead.id is None:
        raise ValueError("enrich() requires a persisted lead (lead.id is None)")

    if not needs_enrichment(lead, force):
        log.debug("enrich: skip lead %d (%s) — already enriched, no new signals",
                  lead.id, lead.name)
        return False

    reason = _disqualification_reason(lead)
    if reason is not None:
        log.info("enrich: deleting lead %d (%s) — %s", lead.id, lead.name, reason)
        db.delete_lead(conn, lead.id)
        return False

    # Form C funding-only leads take the light path — the filing already
    # carries domain / headcount / location, so no Gemini call is spent.
    if _is_form_c_funding_only(lead):
        return _enrich_form_c_light(conn, lead)

    lookup = lookup_company(lead)

    effective_headcount = lookup.headcount or lead.headcount

    oversized = (
        effective_headcount is not None and effective_headcount > _headcount_cap(lead)
    )
    # Unknown headcount is KEPT — a company the average person hasn't heard of
    # is a fine gift even unsized. A household name (with or without a
    # discoverable headcount) is dropped: everyone's heard of it, so it's a bad
    # gift. Explicit non-US and oversized still delete everyone.
    keep_unsized = _has_hiring_signal(lead)
    non_us = lookup.country is not None and lookup.country != "US"
    unknown_country = lookup.country is None
    disqualifier_reason = None
    if non_us:
        disqualifier_reason = f"non_us_country={lookup.country!r}"
    elif unknown_country and not keep_unsized:
        disqualifier_reason = "unknown_country"
    elif oversized:
        disqualifier_reason = f"oversized_headcount={effective_headcount}"
    elif lookup.household_name:
        disqualifier_reason = "known_household_name"
    elif lookup.is_cfo_competitor:
        disqualifier_reason = "cfo_competitor"
    elif lookup.is_recruiting_firm:
        disqualifier_reason = "recruiting_firm"
    elif lookup.is_auto_dealer:
        disqualifier_reason = "auto_dealer"

    if disqualifier_reason is not None:
        log.info(
            "enrich: deleting lead %d (%s) — %s",
            lead.id, lead.name, disqualifier_reason,
        )
        db.delete_lead(conn, lead.id)
        # Sticky disqualifiers — service providers that should stay out
        # across runs even if a fresh signal appears.
        if disqualifier_reason in (
            "recruiting_firm", "auto_dealer", "cfo_competitor",
        ):
            from leadgen.models import Disqualifier
            db.mark_disqualified(
                conn,
                Disqualifier(
                    name=lead.name,
                    reason=f"{disqualifier_reason}_per_llm",
                    source=SourceName.COMPUTED,
                    payload={},
                ),
            )
        return False

    # Classify the granular niche from the freshly-fetched insight (so a
    # SUPRANATURALS-style supplements brand doesn't get tagged by its
    # finance-lead signal); derive the coarse industry from it.
    niche = classify_niche(lead, insight=lookup.insight)

    updates: dict[str, object] = {
        "industry": taxonomy.parent_of(niche),
        "niche": niche,
        "country": "US" if lookup.country == "US" else lead.country,
        "insight": lookup.insight,
        "headcount": lookup.headcount if lookup.headcount is not None else lead.headcount,
        "domain": lookup.domain or lead.domain,
        "city": lookup.city or lead.city,
        "state": lookup.state or lead.state,
        "enriched_at": _utcnow(),
    }
    db.update_lead(conn, lead.id, **updates)

    return True

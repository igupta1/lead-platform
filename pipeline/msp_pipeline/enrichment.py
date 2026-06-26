"""Enrichment pass: industry, headcount, country (US-only filter), city/state.

One Gemini grounded call per lead for the lookup (headcount + city + state +
country), one OpenAI structured call for industry classification. Non-US
leads are deleted. Re-enrichment is signal-aware: skip when industry is set
and no source signal has arrived since the last ``ENRICHMENT_RUN`` marker.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel

from msp_pipeline import db, llm
from msp_pipeline.models import Lead, Signal, SignalType, SourceName
from msp_pipeline.sources import funding

log = logging.getLogger(__name__)


# --- Pure-code disqualification filter ------------------------------------
#
# These checks run BEFORE any LLM call (in purge_disqualified) and as a
# secondary gate inside enrich(). The goal is to drop leads that obviously
# don't fit "SMB IT-services buyer" — government, mega-corps, VC funds, the
# user's own competitors — without burning Gemini/OpenAI tokens on them.

# Domain TLDs that mark non-SMB targets. Government, military, public
# universities don't transact with small MSPs in any reasonable timeframe.
_BLOCKED_TLDS: tuple[str, ...] = (".gov", ".mil", ".edu")

# Token-aware name patterns for entities that can't be SMB buyers. Each
# regex must match a whole-word portion of the company name (case
# insensitive). Crafted to NOT match legitimate SMBs that happen to have
# substring overlap (e.g. "Capital City Auto" should not match "Capital").
_BLOCKED_NAME_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:State|City|County|Town) of\b", re.IGNORECASE),
    re.compile(r"\bDepartment of\b", re.IGNORECASE),
    re.compile(r"\bU\.?S\.? (?:House|Senate|Department|Government|Army|Navy|Air Force)\b", re.IGNORECASE),
    re.compile(r"\b(?:University|College)\b", re.IGNORECASE),
    re.compile(r"\bVentures?\b", re.IGNORECASE),  # VC fund names
    # Investment-fund LP-style names — Stellus Capital Investment Corp, etc.
    # Match "Capital" / "Holdings" / "Investments" only when followed by a
    # fund-style suffix nearby, to avoid false-matching small businesses.
    re.compile(
        r"\b(?:Capital|Holdings?|Investments?) "
        r"(?:Partners|LLC|Inc|LP|L\.P\.|Corp(?:oration)?|Group|Fund|Management|Investments?)\b",
        re.IGNORECASE,
    ),
)

# Public-sector entities: county / municipal governments, school districts,
# and special-purpose public authorities. The "State/City/County/Town of X"
# form is in _BLOCKED_NAME_RES above; these catch the suffix and agency forms
# ("Winona County", "X Township", "Jacksonville Transportation Authority",
# "Chaffey Joint Union High School District"). A small MSP/MSSP/cloud shop
# can't transact with these in a normal sales cycle (RFP / procurement).
_GOV_NAME_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bCounty\s*$", re.IGNORECASE),
    re.compile(r"\b(?:Township|Borough|Municipality)\b", re.IGNORECASE),
    re.compile(r"\b(?:School District|Public Schools|Board of Education)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:Transit|Transportation|Housing|Port|Water|Sewer|Sanitation|Utility|Parks?)"
        r"\s+(?:Authority|District)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:Sheriff|Police Department|Fire Department)\b", re.IGNORECASE),
)

# Mega-corps that should never appear in an SMB lead set. Match by domain
# (more reliable than name).
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
    # Large enterprises that surfaced in the lead set with NULL headcount,
    # so the >250 cap couldn't catch them. An SMB MSP/MSSP/cloud shop can't
    # sell into these.
    "capitalone.com",
    "raytheon.com", "rtx.com",
    "mlb.com",
    "toyota.com",
    "panasonic.com",
    "manpowergroup.com", "manpower.com",
    "adecco.com", "adeccogroup.com",
    "bestwestern.com", "bwhhotelgroup.com",
    "plannedparenthood.org",
    "discord.com",
    "swifttrans.com",
    "delawarenorth.com",
    "elevancehealth.com", "wellpoint.com",
    "lacare.org",
    "aaa.com", "ace.aaa.com", "calif.aaa.com",
    # Large orgs that slipped the >250 cap with NULL/unverified headcount:
    # a national mortgage lender, an auto-finance arm, and an MLB club.
    "pennymac.com", "pennymacusa.com",
    "gmfinancial.com",
    "marlins.com",
})

# Enterprises that often arrive with NO domain (recruiter-posted roles, AG
# breach rows) so the domain block can't see them. Matched against the
# normalized company name as a whole-token sequence (see
# ``_is_blocked_enterprise_name``) to avoid clipping a small business that
# merely shares a word.
_BLOCKED_ENTERPRISE_NAMES: tuple[str, ...] = (
    "capital one",
    "raytheon",
    "major league baseball",
    "toyota",
    "panasonic",
    "manpowergroup",
    "manpower",
    "adecco",
    "best western",
    "bwh hotels",
    "planned parenthood",
    "la care", "l a care",
    "discord",
    "swift transportation",
    "delaware north",
    "elevance", "wellpoint",
    "dalio family office",
    "automobile club of southern california",
    # Large orgs that arrived with no domain + NULL headcount, so neither the
    # domain block nor the >250 cap could catch them.
    "pennymac",
    "gm financial", "general motors",
    "miami marlins",
)

# Staffing / recruiting / executive-search firms post roles on behalf of an
# unnamed client — the lead would be the recruiter, not the hiring company.
# This pure-code regex catches the obvious naming conventions before any LLM
# call; the Gemini lookup's IS_RECRUITER flag (below) catches the rest.
_RECRUITER_NAME_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(staffing|recruit(?:ing|er|ers|ment)|headhunter|"
        r"personnel\s+services?|talent\s+(?:group|agency|partners|solutions|acquisition))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:executive\s+search|search\s+(?:group|partners?|associates|consultants|firm))\b",
        re.IGNORECASE,
    ),
)

# Specific recruiter brands whose names give no generic signal (no "Staffing"
# / "Search" token). The user flagged these explicitly.
_RECRUITER_NAMES: tuple[str, ...] = (
    "jobot",
    "tailored management",
    "confidential careers",
    "self opportunity",
    "mackenzie stuart",
)

# Substrings inside the company NAME (not domain) that strongly suggest
# the entity sells IT services itself (i.e. is a competitor, not a buyer).
# Match on whole-name basis: "X IT Solutions", "X Technology Services", etc.
_IT_VENDOR_NAME_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bIT (?:Solutions|Services|Consulting|Support)\b", re.IGNORECASE),
    re.compile(r"\b(?:Managed|Cloud) (?:Services|Solutions|Consulting)\b", re.IGNORECASE),
    re.compile(r"\bCybersecurity\b", re.IGNORECASE),
    re.compile(r"\bSoftware (?:Solutions|Services)\b", re.IGNORECASE),
    re.compile(r"\bTech(?:nology)? (?:Solutions|Services|Consulting)\b", re.IGNORECASE),
    re.compile(r"\bComplete IT\b", re.IGNORECASE),
)

# Names that look like raw RSS article headlines instead of actual company
# names — symptom of a funding-source extraction miss before the LLM
# extractor was added. Catches "Khosla-backed robotics startup Genesis AI
# has gone full stack, demo shows", "NHI Announces $106.9 Million SHOP
# Investment", "Stellus Capital Schedules First Quarter ... Conference Call".
_HEADLINE_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\$[\d.]+\s*(?:M|B|million|billion)", re.IGNORECASE),
    re.compile(r"\bConference Call\b|\bquarter(?:ly)?\s+results\b|\bSchedules\b", re.IGNORECASE),
    re.compile(r"\bdemo shows\b|\bgone full stack\b|\bfounder says\b", re.IGNORECASE),
    re.compile(r"\b(?:raises|raised|secured|secures|backed|sells|sold|announces|hits)\b\s+\$", re.IGNORECASE),
)


def _looks_like_article_headline(name: str) -> bool:
    if len(name) > 80:
        return True
    return any(p.search(name) for p in _HEADLINE_NAME_PATTERNS)


# Qualifying scoring signals OTHER than funding. A lead carrying any of these
# stands on its own merit, so it's never dropped for a bad funding headline.
_NON_FUNDING_QUALIFYING_SIGNALS: frozenset[SignalType] = frozenset({
    SignalType.JOB_IT_SUPPORT,
    SignalType.JOB_IT_LEADERSHIP,
    SignalType.JOB_SECURITY,
    SignalType.JOB_CLOUD_DEVOPS,
    SignalType.EXEC_HIRED,
    SignalType.BREACH_DISCLOSED,
})


def _has_only_bogus_funding_signal(lead: Lead) -> bool:
    """True when a lead's ONLY qualifying signal is a funding headline the
    funding source would now reject — fake-IPO hype ("...World's Largest IPO"),
    a share-price move ("...stock tumbling 37%"), etc. These were ingested
    before the funding-title guard existed and render the raw headline on the
    card as if it were a real raise. A lead with any other qualifying signal is
    kept on that signal's merit."""
    funding_titles = [
        str(s.payload.get("feed_title") or "").strip()
        for s in lead.signals
        if s.type == SignalType.FUNDING_RAISED
    ]
    if not funding_titles:
        return False
    if any(s.type in _NON_FUNDING_QUALIFYING_SIGNALS for s in lead.signals):
        return False
    # Drop only when EVERY funding headline we can read is one the source
    # rejects; a blank/unreadable headline is left alone, conservatively.
    return all(
        bool(title) and not funding.is_buying_signal_title(title)
        for title in funding_titles
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


def _is_it_vendor_name(name: str) -> bool:
    return any(p.search(name) for p in _IT_VENDOR_NAME_RES)


def _normalize_name(name: str) -> list[str]:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return s.split()


def _is_blocked_enterprise_name(name: str) -> bool:
    tokens = _normalize_name(name)
    if not tokens:
        return False
    for phrase in _BLOCKED_ENTERPRISE_NAMES:
        needle = phrase.split()
        n = len(needle)
        if n == 0:
            continue
        for i in range(len(tokens) - n + 1):
            if tokens[i : i + n] == needle:
                return True
    return False


def _is_recruiter_name(name: str) -> bool:
    if any(p.search(name) for p in _RECRUITER_NAME_RES):
        return True
    normalized = " ".join(_normalize_name(name))
    return any(brand in normalized for brand in _RECRUITER_NAMES)


def _email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower() or None


def _is_recruiter_domain(domain: str | None) -> bool:
    """True when a website / email domain belongs to a known recruiter brand.
    Catches leads like "MACKENZIE" whose displayed name carries no recruiter
    signal but whose contact email (david.stone@mackenziestuart.com) does."""
    if not domain:
        return False
    host = domain.lower().strip().rstrip("/").split("/")[0]
    labels = host.split(".")
    if len(labels) > 1:
        labels = labels[:-1]  # drop the TLD: "mackenziestuart.com" -> name
    flat = re.sub(r"[^a-z0-9]", "", "".join(labels))
    if not flat:
        return False
    return any(brand.replace(" ", "") in flat for brand in _RECRUITER_NAMES)


def _is_government_name(name: str) -> bool:
    return any(p.search(name) for p in _GOV_NAME_RES)


def _disqualification_reason(lead: Lead) -> str | None:
    if _domain_blocked(lead.domain):
        return f"blocked_domain={lead.domain}"
    if _is_blocked_enterprise_name(lead.name):
        return "blocked_enterprise_name"
    if _is_government_name(lead.name):
        return "government_entity"
    if _name_blocked(lead.name):
        return "blocked_name_pattern"
    if (
        _is_recruiter_name(lead.name)
        or _is_recruiter_domain(lead.domain)
        or _is_recruiter_domain(_email_domain(lead.dm_email))
    ):
        return "recruiter_name"
    if _is_it_vendor_name(lead.name):
        return "it_vendor_name"
    if _looks_like_article_headline(lead.name):
        return "article_headline_as_name"
    if lead.headcount is not None and lead.headcount > 250:
        return f"oversized={lead.headcount}"
    if lead.headcount == 0:
        return "zero_headcount"
    if _has_only_bogus_funding_signal(lead):
        return "bogus_funding_signal"
    return None


def purge_disqualified(conn: sqlite3.Connection) -> int:
    """Pure-code pass: drop leads that clearly don't fit the SMB target.
    Runs before scoring so disqualified leads can't crowd the top-30. No
    LLM calls. Safe to run every night."""
    deleted = 0
    leads = list(db.iter_leads(conn))
    for lead in leads:
        if lead.id is None:
            continue
        reason = _disqualification_reason(lead)
        if reason is not None:
            log.info(
                "purge: deleting %d (%s) — %s",
                lead.id, lead.name, reason,
            )
            try:
                db.delete_lead(conn, lead.id)
                deleted += 1
            except Exception:
                log.exception("purge: delete_lead failed for id=%s", lead.id)
    return deleted


# --- Industry vocabulary ---------------------------------------------------


class Industry(str, Enum):
    SOFTWARE_SAAS = "software_saas"
    FINTECH = "fintech"
    HEALTHCARE = "healthcare"
    ECOMMERCE_RETAIL = "ecommerce_retail"
    MANUFACTURING = "manufacturing"
    LOGISTICS_TRANSPORT = "logistics_transport"
    REAL_ESTATE = "real_estate"
    INSURANCE = "insurance"
    LEGAL_PROFESSIONAL = "legal_professional"
    EDUCATION = "education"
    MEDIA_ENTERTAINMENT = "media_entertainment"
    HOSPITALITY_FOOD = "hospitality_food"
    GOVERNMENT_NONPROFIT = "government_nonprofit"
    CONSTRUCTION = "construction"
    OTHER = "other"


# --- Pure helpers ---------------------------------------------------------


_BANDS: tuple[tuple[int, str], ...] = (
    (10, "1-10"),
    (50, "11-50"),
    (200, "51-200"),
    (1000, "201-1000"),
    (5000, "1001-5000"),
)


def compute_band(headcount: int | None) -> str | None:
    if headcount is None:
        return None
    for upper, label in _BANDS:
        if headcount < upper:
            return label
    return "5000+"


def _round_to_10(n: int) -> int:
    return int(round(n, -1))


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
IS_IT_VENDOR: <"yes" if this company is itself an IT services / IT staffing / \
IT consulting / managed-service-provider / cloud-consultancy / cybersecurity \
firm (i.e. they SELL IT services), otherwise "no">
IS_RECRUITER: <"yes" if this company is a staffing agency, recruiting / \
executive-search firm, or RPO that posts jobs on behalf of OTHER client \
companies (so the real hiring company is hidden), otherwise "no">
DM_NAME: <full name of the most likely IT / security / technology decision \
maker (CIO, CTO, CISO, VP/Director of IT, Head of IT). For very small \
companies that don't have a tech exec, use the CEO / Founder / COO who \
would handle vendor decisions. Otherwise "unknown".>
DM_TITLE: <their job title (e.g. "Director of IT", "COO", "Founder & CEO") \
or "unknown">
VALUE_PROP: <ONE sentence (max 25 words), present tense, plain language, \
describing what the company does or sells. Examples: "Sells subscription \
billing software to Shopify stores." / "Family-owned auto dealership in \
Pennsylvania." / "Pediatric clinic with 3 locations in Spokane." Avoid \
marketing fluff and superlatives. Use "unknown" only as a last resort.>
"""

_FIELD_RE = {
    "headcount": re.compile(r"^HEADCOUNT:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "city": re.compile(r"^CITY:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "state": re.compile(r"^STATE:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "country": re.compile(r"^COUNTRY:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "domain": re.compile(r"^DOMAIN:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "is_it_vendor": re.compile(
        r"^IS_IT_VENDOR:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
    ),
    "is_recruiter": re.compile(
        r"^IS_RECRUITER:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
    ),
    "dm_name": re.compile(r"^DM_NAME:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "dm_title": re.compile(r"^DM_TITLE:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "value_prop": re.compile(r"^VALUE_PROP:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
}

_HEADCOUNT_NUM_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})*|\d+)\b")
_DOMAIN_CLEAN_RE = re.compile(r"^https?://|^www\.|/.*$", re.IGNORECASE)


class _Lookup(BaseModel):
    headcount: int | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    domain: str | None = None
    is_it_vendor: bool = False
    is_recruiter: bool = False
    dm_name: str | None = None
    dm_title: str | None = None
    value_prop: str | None = None


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
    headcount_raw = _parse_field(raw, "headcount")
    city = _parse_field(raw, "city")
    state_raw = _parse_field(raw, "state")
    country_raw = _parse_field(raw, "country")
    domain_raw = _parse_field(raw, "domain")
    vendor_raw = _parse_field(raw, "is_it_vendor")
    recruiter_raw = _parse_field(raw, "is_recruiter")
    dm_name_raw = _parse_field(raw, "dm_name")
    dm_title_raw = _parse_field(raw, "dm_title")
    value_prop_raw = _parse_field(raw, "value_prop")
    return _Lookup(
        headcount=_parse_headcount(headcount_raw),
        city=city,
        state=state_raw.upper() if state_raw else None,
        country=country_raw.upper() if country_raw else None,
        domain=_parse_domain(domain_raw),
        is_it_vendor=_parse_yesno(vendor_raw),
        is_recruiter=_parse_yesno(recruiter_raw),
        dm_name=dm_name_raw,
        dm_title=dm_title_raw,
        value_prop=value_prop_raw,
    )


# --- OpenAI industry classification ---------------------------------------


_INDUSTRY_PROMPT = """\
Classify the company "{name}" into exactly one of these industry tags:
{tags}

What they do: {value_prop}
Recent signals about this company: {signals}

Pick the tag that matches what the company actually sells. An insurance
agency / carrier / brokerage is "insurance", NOT "fintech" (fintech is
software that powers financial services). Respond with the single
best-fitting tag. Use "other" only when no other tag reasonably applies.
"""


class _IndustryOut(BaseModel):
    industry: Industry


def classify_industry(lead: Lead, *, value_prop: str | None = None) -> Industry:
    tags = ", ".join(t.value for t in Industry)
    prompt = _INDUSTRY_PROMPT.format(
        name=lead.name,
        tags=tags,
        value_prop=value_prop or lead.value_prop or "unknown",
        signals=_summarize_signals(lead),
    )
    out = llm.call_openai(prompt, response_model=_IndustryOut)
    return out.industry


# --- Orchestrator ---------------------------------------------------------


def _last_enrichment_at(lead: Lead) -> datetime | None:
    times = [s.captured_at for s in lead.signals if s.type == SignalType.ENRICHMENT_RUN]
    return max(times) if times else None


def _has_signal_after(lead: Lead, cutoff: datetime) -> bool:
    return any(
        s.captured_at > cutoff
        for s in lead.signals
        if s.type != SignalType.ENRICHMENT_RUN
    )


def _should_skip(lead: Lead, force: bool) -> bool:
    if force:
        return False
    if lead.industry is None:
        return False
    last = _last_enrichment_at(lead)
    if last is None:
        return False
    return not _has_signal_after(lead, last)


def enrich(conn: sqlite3.Connection, lead: Lead, *, force: bool = False) -> bool:
    if lead.id is None:
        raise ValueError("enrich() requires a persisted lead (lead.id is None)")

    if _should_skip(lead, force):
        log.debug("enrich: skip lead %d (%s) — no new signals since last run",
                  lead.id, lead.name)
        return True

    # First gate: pure-code disqualification. Cheap, runs on whatever
    # data we already have. Catches government / mega-corps / VC funds /
    # competitor names without needing a Gemini call.
    reason = _disqualification_reason(lead)
    if reason is not None:
        log.info(
            "enrich: deleting lead %d (%s) — %s",
            lead.id, lead.name, reason,
        )
        db.delete_lead(conn, lead.id)
        return False

    lookup = lookup_company(lead)

    # If Apollo has already enriched this lead, its headcount + DM data are
    # more reliable than Gemini's. Trust the existing values for the SMB-cap
    # check and don't overwrite them below.
    has_apollo = any(s.type == SignalType.APOLLO_ENRICHED for s in lead.signals)
    effective_headcount = (
        lead.headcount if has_apollo and lead.headcount is not None
        else lookup.headcount
    )

    oversized = effective_headcount is not None and effective_headcount > 250
    if lookup.country != "US" or oversized or lookup.is_it_vendor or lookup.is_recruiter:
        log.info(
            "enrich: deleting lead %d (%s) — country=%r headcount=%r vendor=%s recruiter=%s",
            lead.id, lead.name, lookup.country, effective_headcount,
            lookup.is_it_vendor, lookup.is_recruiter,
        )
        db.delete_lead(conn, lead.id)
        return False

    if lookup.city or lookup.state:
        db.append_signal(
            conn,
            lead.id,
            Signal(
                type=SignalType.LOCATION_CAPTURED,
                source=SourceName.COMPUTED,
                captured_at=_utcnow(),
                payload={"city": lookup.city, "state": lookup.state},
            ),
        )

    industry = classify_industry(lead, value_prop=lookup.value_prop)

    updates: dict[str, object] = {
        "industry": industry.value,
        "country": "US",
        "value_prop": lookup.value_prop,
    }
    # Apollo wins for these fields when its marker is present. Otherwise
    # take whatever Gemini found (None overwrites are fine here — original
    # behavior).
    if not has_apollo:
        updates.update(
            headcount=lookup.headcount,
            domain=lookup.domain,
            dm_name=lookup.dm_name,
            dm_title=lookup.dm_title,
        )
    elif lookup.domain and not lead.domain:
        # Apollo didn't fill in a domain (org-search miss); accept Gemini's.
        updates["domain"] = lookup.domain
    db.update_lead(conn, lead.id, **updates)

    db.append_signal(
        conn,
        lead.id,
        Signal(
            type=SignalType.ENRICHMENT_RUN,
            source=SourceName.COMPUTED,
            captured_at=_utcnow(),
            payload={},
        ),
    )

    return True

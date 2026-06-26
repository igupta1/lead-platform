"""Insurance-pipeline enrichment.

Two-stage, mirrors msp_pipeline.enrichment but with:
- Insurance-vendor pre-purge (carriers, brokers, MGAs, TPAs, adjusters
  — they shouldn't be sold insurance).
- Finance/ops/owner Gemini DM prompt (no IT-first bias).
- Apollo enrichment is load-bearing for FL SunBiz leads (registered-
  agent service-company DM panel is the dashboard's visible defect
  without it).

Re-enrichment is signal-aware: skip when industry is set and no source
signal has arrived since the last ``ENRICHMENT_RUN`` marker.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel

from insurance_pipeline import db, llm
from insurance_pipeline.models import Lead, Signal, SignalType, SourceName
from insurance_pipeline.sources.edgar_form_d import (
    _STREET_SPV_RE,
    _VINTAGE_YEAR_RE,
)

log = logging.getLogger(__name__)


# --- Pure-code disqualification filter ------------------------------------
#
# Runs BEFORE any LLM call in purge_disqualified, and as a secondary gate
# inside enrich(). Drops leads that obviously don't fit "SMB commercial
# insurance buyer" without burning Gemini tokens on them.

_SMB_HEADCOUNT_CAP = 250

# Sources that are US-only by definition. When Gemini fails to find a
# small carrier on the web and returns country="unknown", we trust the
# source and treat the lead as US rather than deleting it.
_US_ONLY_SOURCES: frozenset[SourceName] = frozenset({
    SourceName.FMCSA,
    SourceName.SOS_FL,
    SourceName.SOS_CO,
    SourceName.SOS_WA,
    SourceName.OSHA,
    SourceName.BUILDING_PERMITS,
})

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

# Mega-corps that shouldn't be SMB leads. Domain match (more reliable).
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

# Insurance-industry names — purged outright. The single-niche
# architecture means we don't need to preserve them for another niche
# (cf. msp_pipeline where they live on as MSP/MSSP leads).
_INSURANCE_VENDOR_NAME_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bInsurance\s+(?:Agency|Brokers?|Services|Company|Companies|"
        r"Corp(?:oration)?|Group|Solutions|Holdings|Partners|Advisors|"
        r"Associates|Specialists|Consultants)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:Mutual|Casualty|Indemnity|Surety)\s+(?:Insurance|Company|"
        r"Companies|Corp(?:oration)?|Group|Holdings)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bRe-?insurance\b", re.IGNORECASE),
    re.compile(
        r"\b(?:MGA|Managing\s+General\s+Agent|Wholesale\s+Insurance|"
        r"Insurance\s+Wholesalers?|Underwriters)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:TPA|Third[-\s]?Party\s+Administrator)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:Claims\s+Adjusters?|Adjusting\s+(?:Services|Company|Group)|"
        r"Independent\s+Adjusters?)\b",
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


def _is_insurance_vendor(name: str) -> bool:
    return any(p.search(name) for p in _INSURANCE_VENDOR_NAME_RES)


# Financial-vehicle names that slip past EDGAR's source-level filter
# end up in the DB and persist across runs. This list purges them at
# enrichment time so existing rows get cleaned out on the next cron.
# Pattern is intentionally narrower than EDGAR's — we don't want to
# delete a real "Smith Capital LLC" that's an operating company.
_FINANCIAL_VEHICLE_NAME_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bBancshares\b", re.IGNORECASE),
    re.compile(r"\bBancorp\b", re.IGNORECASE),
    re.compile(r"\bFinance\s+(?:Corp|Company|Inc|LLC|L\.?P\.?)", re.IGNORECASE),
    re.compile(r",\s*L\.?\s?L?\s?P\.?\s*$", re.IGNORECASE),  # name ending in ", L.P." / ", LP" / ", LLP"
    re.compile(r"^TPG\b", re.IGNORECASE),
    re.compile(r"^Blackstone\b", re.IGNORECASE),
    re.compile(r"^KKR\b", re.IGNORECASE),
    re.compile(r"^Carlyle\b", re.IGNORECASE),
    re.compile(r"^MidOcean\b", re.IGNORECASE),
)


def _is_financial_vehicle(name: str) -> bool:
    return any(p.search(name) for p in _FINANCIAL_VEHICLE_NAME_RES)


# Mega-corp prefix deny-list. Many of these have hundreds of named
# subsidiaries that show up in federal-contract or FMCSA filings with
# small reported employee counts (because they report at the
# subsidiary level), passing the SMB-cap check despite being huge
# enterprises with their own captive insurance programs.
#
# Pattern is intentionally conservative — we'd rather miss a few
# enterprise subsidiaries than nuke a real SMB whose name happens to
# contain "Verizon" or similar. Prefix-anchored, case-insensitive.
_MEGACORP_PREFIX_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:AT&?T|ATT)\b", re.IGNORECASE),
    re.compile(r"^Verizon\b", re.IGNORECASE),
    re.compile(r"^T-Mobile\b", re.IGNORECASE),
    re.compile(r"^Sprint\b", re.IGNORECASE),
    re.compile(r"^Comcast\b", re.IGNORECASE),
    re.compile(r"^Johnson\s+Controls\b", re.IGNORECASE),
    re.compile(r"^AECOM\b", re.IGNORECASE),
    re.compile(r"^CDM\s+(?:Smith|Federal)\b", re.IGNORECASE),
    re.compile(r"^Tetra\s+Tech\b", re.IGNORECASE),
    re.compile(r"^Compass\s+PTS\b", re.IGNORECASE),  # AECOM JV
    re.compile(r"^Laboratory\s+Corporation\b", re.IGNORECASE),  # LabCorp
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
    re.compile(r"^Aramark\b", re.IGNORECASE),
)


def _is_megacorp_subsidiary(name: str) -> bool:
    return any(p.match(name) for p in _MEGACORP_PREFIX_RES)


# Apex domains of large multinationals whose US/regional subsidiaries
# slip through the SMB filters when Gemini fails to return headcount
# (Canon U.S.A., Quest Diagnostics TB LLC, CoreLogic Solutions LLC all
# had headcount=None at enrichment time). The subsidiary LLC name
# itself looks SMB-shaped; the parent's domain is the giveaway.
#
# Intentionally narrow. Not a comprehensive corporate-parent DB.
_LARGE_ENTERPRISE_DOMAINS: frozenset[str] = frozenset({
    "canon.com",
    "questdiagnostics.com",
    "corelogic.com",
    "siemens.com",
    "ge.com",
    "abb.com",
    "honeywell.com",
    "3m.com",
    "bosch.com",
    "panasonic.com",
    "samsung.com",
    "hitachi.com",
    "philips.com",
    "schneider-electric.com",
    "emerson.com",
    "rockwellautomation.com",
    "thermofisher.com",
    "labcorp.com",
    "danaher.com",
    "caterpillar.com",
    "johndeere.com",
    "fmc.com",
    "fluor.com",
    "kbr.com",
})


def _apex_domain(domain: str | None) -> str | None:
    """Strip subdomains. 'usa.canon.com' → 'canon.com'.
    Keeps the last two labels; doesn't handle multi-part TLDs like
    .co.uk (those don't show up in US lead data)."""
    if not domain:
        return None
    parts = domain.lower().strip().rstrip("/").split(".")
    if len(parts) < 2:
        return None
    return ".".join(parts[-2:])


def _enterprise_apex(domain: str | None) -> bool:
    apex = _apex_domain(domain)
    return apex is not None and apex in _LARGE_ENTERPRISE_DOMAINS


# Regional-subsidiary suffix: "Canon U.S.A., Inc.", "Bosch USA Inc",
# "Siemens Americas LLC". Anchored to the end of the name, gated to
# the recognizable region tokens.
_ENTERPRISE_SUBSIDIARY_NAME_RE = re.compile(
    r"\b(?:U\.?\s?S\.?\s?A\.?|USA|Americas?|North\s+America|Worldwide)"
    r"(?:\s*[,.]?\s*(?:Inc\.?|LLC|Corp(?:oration)?|Ltd))?\s*$",
    re.IGNORECASE,
)


def _is_enterprise_subsidiary(lead: Lead) -> bool:
    if _enterprise_apex(lead.domain):
        return True
    return bool(_ENTERPRISE_SUBSIDIARY_NAME_RE.search(lead.name))


def _is_form_d_noise(lead: Lead) -> bool:
    """Vintage-year / street-SPV regexes from the EDGAR source applied
    retroactively. Source-side filter only blocks new ingests; without
    this, leads that predate the regex (`Summit Ridge 2024 LLC`,
    `JR Hyde Park Blvd LLC`) stay in the DB forever. Gated to Form D
    leads so the patterns can't accidentally nuke a real trucking
    company with a year or street word in its name."""
    has_form_d = any(
        s.type == SignalType.FUNDING_RAISED
        and s.payload.get("filing_type") == "Form D"
        for s in lead.signals
    )
    if not has_form_d:
        return False
    return bool(
        _VINTAGE_YEAR_RE.search(lead.name) or _STREET_SPV_RE.search(lead.name)
    )


def _disqualification_reason(lead: Lead) -> str | None:
    if _domain_blocked(lead.domain):
        return f"blocked_domain={lead.domain}"
    if _name_blocked(lead.name):
        return "blocked_name_pattern"
    if _is_insurance_vendor(lead.name):
        return "insurance_vendor_name"
    if _is_financial_vehicle(lead.name):
        return "financial_vehicle"
    if _is_megacorp_subsidiary(lead.name):
        return "megacorp_subsidiary"
    if _is_enterprise_subsidiary(lead):
        return "enterprise_subsidiary"
    if _is_form_d_noise(lead):
        return "form_d_noise_pattern"
    if lead.headcount is not None and lead.headcount > _SMB_HEADCOUNT_CAP:
        return f"oversized={lead.headcount}"
    if lead.headcount == 0:
        return "zero_headcount"
    return None


def purge_disqualified(conn: sqlite3.Connection) -> int:
    """Pure-code pass. Runs before scoring so disqualified leads can't
    crowd the top of the page. No LLM calls."""
    deleted = 0
    leads = list(db.iter_leads(conn))
    for lead in leads:
        if lead.id is None:
            continue
        reason = _disqualification_reason(lead)
        if reason is not None:
            log.info("purge: deleting %d (%s) — %s", lead.id, lead.name, reason)
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
    LEGAL_PROFESSIONAL = "legal_professional"
    EDUCATION = "education"
    MEDIA_ENTERTAINMENT = "media_entertainment"
    HOSPITALITY_FOOD = "hospitality_food"
    GOVERNMENT_NONPROFIT = "government_nonprofit"
    CONSTRUCTION = "construction"
    OTHER = "other"


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
IS_INSURANCE_VENDOR: <"yes" if this company is itself an insurance \
carrier / broker / agency / MGA / wholesaler / reinsurer / TPA / \
claims adjuster (i.e. they SELL insurance), otherwise "no">
DM_NAME: <full name of the person most likely to handle insurance / \
vendor purchasing at this company. Pick based on size and industry: \
- Companies under 25 employees or owner-operator businesses: prefer \
  Owner, Founder, President, CEO. \
- Mid-size companies (25-250 employees): prefer CFO, COO, Controller, \
  VP / Director of Finance, VP / Director of Operations, Office \
  Manager, HR Director. \
- Trucking / logistics companies: also consider Safety Director, \
  Director of Safety, Compliance Officer. \
Use "unknown" only if you genuinely cannot identify a likely buyer.>
DM_TITLE: <their job title (e.g. "Owner", "CFO", "Safety Director") \
or "unknown">
VALUE_PROP: <ONE sentence (max 25 words), present tense, plain language, \
describing what the company does or sells. Examples: "Family-owned auto \
dealership in Pennsylvania." / "Long-haul trucking company with 12 trucks \
based in Florida." / "Newly-filed Florida LLC; business activity unknown." \
Avoid marketing fluff and superlatives. Use "unknown" only as a last resort.>
"""

_FIELD_RE = {
    "headcount": re.compile(r"^HEADCOUNT:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "city": re.compile(r"^CITY:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "state": re.compile(r"^STATE:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "country": re.compile(r"^COUNTRY:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "domain": re.compile(r"^DOMAIN:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE),
    "is_insurance_vendor": re.compile(
        r"^IS_INSURANCE_VENDOR:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
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
    is_insurance_vendor: bool = False
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
    return _Lookup(
        headcount=_parse_headcount(_parse_field(raw, "headcount")),
        city=_parse_field(raw, "city"),
        state=(_parse_field(raw, "state") or "").upper() or None,
        country=(_parse_field(raw, "country") or "").upper() or None,
        domain=_parse_domain(_parse_field(raw, "domain")),
        is_insurance_vendor=_parse_yesno(_parse_field(raw, "is_insurance_vendor")),
        dm_name=_parse_field(raw, "dm_name"),
        dm_title=_parse_field(raw, "dm_title"),
        value_prop=_parse_field(raw, "value_prop"),
    )


# --- OpenAI industry classification ---------------------------------------


_INDUSTRY_PROMPT = """\
Classify the company "{name}" into exactly one of these industry tags:
{tags}

Recent signals about this company: {signals}

Respond with the single best-fitting tag. Use "other" only when no other tag
reasonably applies.
"""


class _IndustryOut(BaseModel):
    industry: Industry


def classify_industry(lead: Lead) -> Industry:
    tags = ", ".join(t.value for t in Industry)
    prompt = _INDUSTRY_PROMPT.format(
        name=lead.name,
        tags=tags,
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

    reason = _disqualification_reason(lead)
    if reason is not None:
        log.info("enrich: deleting lead %d (%s) — %s", lead.id, lead.name, reason)
        db.delete_lead(conn, lead.id)
        return False

    lookup = lookup_company(lead)

    has_apollo = any(s.type == SignalType.APOLLO_ENRICHED for s in lead.signals)
    effective_headcount = (
        lead.headcount if has_apollo and lead.headcount is not None
        else lookup.headcount
    )
    has_us_only_source = any(s.source in _US_ONLY_SOURCES for s in lead.signals)

    oversized = (
        effective_headcount is not None and effective_headcount > _SMB_HEADCOUNT_CAP
    )
    # Country check: explicit non-US deletes. "unknown" only deletes
    # when no US-only source is present — owner-operator carriers from
    # FMCSA often aren't discoverable on the web, so Gemini's "unknown"
    # is information, not a verdict.
    explicit_non_us = lookup.country is not None and lookup.country != "US"
    unknown_and_unconfirmed = lookup.country is None and not has_us_only_source
    if (
        explicit_non_us or unknown_and_unconfirmed
        or oversized or lookup.is_insurance_vendor
    ):
        log.info(
            "enrich: deleting lead %d (%s) — country=%r headcount=%r vendor=%s",
            lead.id, lead.name, lookup.country, effective_headcount,
            lookup.is_insurance_vendor,
        )
        db.delete_lead(conn, lead.id)
        return False

    # Prefer Gemini city/state when available; fall back to the source's
    # own payload (FMCSA carries phy_city / phy_state).
    captured_city = lookup.city
    captured_state = lookup.state
    if not captured_city and not captured_state and has_us_only_source:
        for s in lead.signals:
            if s.source in _US_ONLY_SOURCES:
                p = s.payload
                if p.get("city") or p.get("state"):
                    captured_city = p.get("city") or None
                    captured_state = p.get("state") or None
                    break

    if captured_city or captured_state:
        db.append_signal(
            conn,
            lead.id,
            Signal(
                type=SignalType.LOCATION_CAPTURED,
                source=SourceName.COMPUTED,
                captured_at=_utcnow(),
                payload={"city": captured_city, "state": captured_state},
            ),
        )

    industry = classify_industry(lead)

    # Country=US when Gemini confirms OR when a US-only source vouches.
    country_value = "US" if (lookup.country == "US" or has_us_only_source) else None
    updates: dict[str, object] = {
        "industry": industry.value,
        "country": country_value,
        "value_prop": lookup.value_prop,
    }
    if not has_apollo:
        updates.update(
            headcount=lookup.headcount,
            domain=lookup.domain,
            dm_name=lookup.dm_name,
            dm_title=lookup.dm_title,
        )
    elif lookup.domain and not lead.domain:
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

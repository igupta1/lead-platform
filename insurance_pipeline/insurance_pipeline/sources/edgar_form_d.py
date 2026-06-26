"""SEC EDGAR Form D filings source.

Form D is the federal filing for private securities offerings —
companies raising money under Reg D exemptions. For an independent
insurance agent, a fresh Form D means:
- D&O exposure (board now has fiduciary duty to investors)
- EPLI exposure (the company is hiring with fresh capital)
- Sometimes cyber + key-person life

Free, keyless, no Cloudflare. Hits EDGAR's ``getcurrent`` Atom feed
filtered to ``type=D``. SEC's fair-use policy asks scrapers to
identify with a contact email in the User-Agent.

Heavy noise filter: Form D is dominated by VC funds, partnerships,
and investment vehicles ("Acme Ventures III LP", "X Capital
Partners"). The ``_FINANCIAL_ENTITY_RE`` regex drops the obvious
filers so what surfaces is operating-company offerings only.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser

from insurance_pipeline.models import (
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)

_log = logging.getLogger(__name__)

_EDGAR_GETCURRENT_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=D&count=100&output=atom"
)
_USER_AGENT = (
    "ishaan-personal-website insurance-lead-magnet/0.1 (ishaangpta@g.ucla.edu)"
)

# Atom title format: "D - <Company Name> (cik) (Filer)"
_TITLE_RE = re.compile(
    r"^\s*D\s*-\s*(?P<name>.+?)\s*\(\d+\)\s*\(Filer\)\s*$",
    re.IGNORECASE,
)

# Drop obvious financial-vehicle names. Form D filings are dominated
# by funds, partnerships, REITs, and PE vehicles. These file Form D
# constantly and aren't insurance buyers (they invest, they don't
# operate).
_FINANCIAL_ENTITY_RE = re.compile(
    r"\b("
    r"venture[s]?|capital|partner[s]?|partnership|"
    r"holdings?|investors?|invest|investment[s]?|"
    r"reit|trust|bancshares|bancorp|"
    r"fund[s]?|funding|"
    r"opportunity|opportunities|"
    r"asset\s+management|management\s+l\.?p\.?|"
    r"family\s+office|"
    r"finance\s+(?:corp|company|inc|llc|l\.?p\.?)"
    r")\b",
    re.IGNORECASE,
)

# Names ending in LP / L.P. / LLP / L.L.P. (with optional comma/space)
# are partnerships — always financial vehicles in Form D context.
_LP_SUFFIX_RE = re.compile(
    r",?\s*L\.?\s?L?\s?P\.?\s*$",
    re.IGNORECASE,
)

# Hard-coded prefix filter for major PE / VC firms whose subsidiary
# entity names sometimes slip past the keyword filter (TPG XYZ Equity
# II without the "Capital" or "Partners" word).
_FINANCIAL_FIRM_PREFIX_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^TPG\b", re.IGNORECASE),
    re.compile(r"^Blackstone\b", re.IGNORECASE),
    re.compile(r"^KKR\b", re.IGNORECASE),
    re.compile(r"^Apollo\s+Global", re.IGNORECASE),
    re.compile(r"^Carlyle\b", re.IGNORECASE),
    re.compile(r"^GS\s+(?:Finance|Capital|Investment)", re.IGNORECASE),
    re.compile(r"^MidOcean\b", re.IGNORECASE),
)

# Issue 4 narrowing: real-estate SPV / vintage-year fund patterns.
# Independent commercial agencies don't compete for these — they're
# wholesale-market or specialty-property territory.

# Year-immediately-before-suffix: "Summit Ridge 2024 LLC" / "Acme 1986 Inc".
# Tightened per review — only matches year (1900-2199) directly before
# the legal suffix at end of name. Tested against 106 live names:
# 1 intended match ("Summit Ridge 2024 LLC"), zero false positives.
# Deliberate non-match: "Acme 2024 Holdings LLC" (year + word + suffix).
_VINTAGE_YEAR_RE = re.compile(
    r"\b(?:19|20|21)\d{2}\s*[,.\s]*(?:LLC|Inc|Corp(?:oration)?|LP|LLP|Ltd|Co)\.?\s*$",
    re.IGNORECASE,
)

# Street-name SPV pattern: "JR Hyde Park Blvd LLC" / "Marina Bay Ave LLC".
# Single-property real-estate vehicles. Conservative — anchored to the
# specific street-suffix-then-LLC pattern at end of name.
_STREET_SPV_RE = re.compile(
    r"\b(?:Blvd|Avenue|Ave|Street|St|Road|Rd|Way|Drive|Dr|Lane|Ln|Court|Ct|Place|Pl)\s+(?:LLC|Inc|Corp|LP)\.?\s*$",
    re.IGNORECASE,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_rss_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _is_operating_company(name: str) -> bool:
    if _FINANCIAL_ENTITY_RE.search(name):
        return False
    if _LP_SUFFIX_RE.search(name):
        return False
    if any(p.match(name) for p in _FINANCIAL_FIRM_PREFIX_RES):
        return False
    if _VINTAGE_YEAR_RE.search(name):
        # "Summit Ridge 2024 LLC" — vintage-year fund vehicle.
        return False
    if _STREET_SPV_RE.search(name):
        # "JR Hyde Park Blvd LLC" — single-property real-estate SPV.
        return False
    return True


def _extract_company_name(title: str) -> str | None:
    m = _TITLE_RE.match(title)
    if m is None:
        return None
    return m.group("name").strip() or None


def fetch(*, since: datetime, limit: int | None = None) -> list[LeadCandidate]:
    captured_at = _utcnow()

    try:
        # feedparser supports request_headers for User-Agent override.
        feed = feedparser.parse(_EDGAR_GETCURRENT_URL, request_headers={"User-Agent": _USER_AGENT})
    except Exception:
        _log.exception("edgar form-d fetch failed")
        return []

    candidates: list[LeadCandidate] = []
    for entry in feed.entries:
        # Form-type comes through as a category term; the feed sometimes
        # returns mixed types even with &type=D, so we re-filter here.
        terms = {t.get("term", "").upper() for t in (entry.get("tags") or [])}
        if "D" not in terms:
            # Also accept entries that match the title prefix (some
            # feedparser versions don't populate `tags` for atom).
            if not (entry.get("title") or "").strip().lower().startswith("d -"):
                continue

        title = (entry.get("title") or "").strip()
        company = _extract_company_name(title)
        if not company:
            continue
        if not _is_operating_company(company):
            _log.info("edgar: skipping financial-entity filer: %r", company)
            continue

        updated = entry.get("updated") or entry.get("published")
        updated_dt = _parse_rss_date(updated)
        if updated_dt and updated_dt < since:
            continue

        candidates.append(
            LeadCandidate(
                name=company,
                domain=None,
                initial_signal=Signal(
                    type=SignalType.FUNDING_RAISED,
                    source=SourceName.FUNDING,
                    captured_at=captured_at,
                    payload={
                        "title": title,
                        "filing_type": "Form D",
                        "filed_on": (updated_dt.date().isoformat()
                                     if updated_dt else ""),
                        "link": str(entry.get("link") or ""),
                    },
                ),
            )
        )

    if limit is not None:
        candidates = candidates[:limit]
    return candidates

"""SEC EDGAR Form D filings source.

Two backends:

1. EFTS full-text search (``efts.sec.gov/LATEST/search-index``) —
   primary. Supports date-range queries, pages 100 hits at a time,
   covers the full 90-day window the spec calls for. The previous
   getcurrent-only path was a lie: it returned the last ~100 filings
   of *all* types and our 90-day ``since`` filter was aspirational.

2. ``getcurrent`` Atom feed — backstop, kept for the freshest filings
   in case EFTS indexing lags by a few hours.

Both emit the same ``FUNDING_RAISED`` signal shape. Operating-company
filter (drops funds / SPVs / partnerships / vintage-year vehicles)
applies to both.

Independent from insurance_pipeline. Mirror code, mirror filters;
no cross-imports.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
import requests

from cfo_pipeline.models import (
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)

_log = logging.getLogger(__name__)

_EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
_EFTS_PAGE_SIZE = 100  # EFTS hard cap.
_EFTS_DEFAULT_PAGES = 5  # 500 most-recent Form Ds; ~10% survive the operating-company filter.

# Item 6 (pooled investment fund) flag patterns. Form D XML reliably
# carries one or both of these fields; matching either is enough.
_POOLED_FUND_FLAG_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"<isPooledInvestmentFundType>\s*true\s*</isPooledInvestmentFundType>",
        re.IGNORECASE,
    ),
    re.compile(
        r"<industryGroupType>\s*Pooled\s+Investment\s+Fund\s*</industryGroupType>",
        re.IGNORECASE,
    ),
)

_EDGAR_GETCURRENT_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=D&count=100&output=atom"
)
_USER_AGENT = (
    "ishaan-personal-website cfo-lead-magnet/0.1 (ishaangpta@g.ucla.edu)"
)

# Atom title format from getcurrent: "D - <Company Name> (cik) (Filer)"
_TITLE_RE = re.compile(
    r"^\s*D\s*-\s*(?P<name>.+?)\s*\(\d+\)\s*\(Filer\)\s*$",
    re.IGNORECASE,
)

# EFTS display_names format: "<Name>  (CIK <0001234567>)"
_DISPLAY_NAME_RE = re.compile(
    r"^\s*(?P<name>.+?)\s*\(CIK\s*\d+\)\s*$",
    re.IGNORECASE,
)

# Form D filings are dominated by funds, partnerships, REITs, and PE
# vehicles. These file Form D constantly and aren't fractional-CFO
# buyers (they invest, they don't operate).
_FINANCIAL_ENTITY_RE = re.compile(
    r"\b("
    r"venture[s]?|capital|partner[s]?|partnership|"
    r"holdings?|investors?|invest|investment[s]?|"
    r"reit|trust|bancshares|bancorp|"
    r"fund[s]?|funding|"
    r"opportunity|opportunities|"
    r"asset\s+management|management\s+l\.?p\.?|"
    r"family\s+office|"
    r"finance\s+(?:corp|company|inc|llc|l\.?p\.?)|"
    r"spv|series\s+of"
    r")\b",
    re.IGNORECASE,
)

_LP_SUFFIX_RE = re.compile(
    r",?\s*L\.?\s?L?\s?P\.?\s*$",
    re.IGNORECASE,
)

_FINANCIAL_FIRM_PREFIX_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^TPG\b", re.IGNORECASE),
    re.compile(r"^Blackstone\b", re.IGNORECASE),
    re.compile(r"^KKR\b", re.IGNORECASE),
    re.compile(r"^Apollo\s+Global", re.IGNORECASE),
    re.compile(r"^Carlyle\b", re.IGNORECASE),
    re.compile(r"^GS\s+(?:Finance|Capital|Investment)", re.IGNORECASE),
    re.compile(r"^MidOcean\b", re.IGNORECASE),
)

_VINTAGE_YEAR_RE = re.compile(
    r"\b(?:19|20|21)\d{2}\s*[,.\s]*(?:LLC|Inc|Corp(?:oration)?|LP|LLP|Ltd|Co)\.?\s*$",
    re.IGNORECASE,
)
_STREET_SPV_RE = re.compile(
    r"\b(?:Blvd|Avenue|Ave|Street|St|Road|Rd|Way|Drive|Dr|Lane|Ln|Court|Ct|Place|Pl)\s+(?:LLC|Inc|Corp|LP)\.?\s*$",
    re.IGNORECASE,
)

# Tranche-suffix SPV — e.g. "AQR Flex 1 Series LLC - Series B9".
# Hedge-fund / PE shops sequence their Reg-D-exempt SPVs by letter+
# digit tranche (Series A1, B9, etc.). Source-level filter so we
# don't waste Gemini tokens on them.
_TRANCHE_SUFFIX_RE = re.compile(
    r"\s+-\s+Series\s+\S+\s*$"
    r"|\bSeries\s+(?:[A-Z]\d+|[IVX]{1,5}|\d+)\s*$",
    re.IGNORECASE,
)

# 3rd-review expansion: numbered series indicators (II / III / IV /
# trailing digits / "XI" / "XVI") at end of name. Conservative — must
# be at the END before legal suffix (or as the legal suffix's
# neighbor).
_ROMAN_NUMERAL_TAIL_RE = re.compile(
    r"\b(?:II|III|IV|V|VI|VII|VIII|IX|X|XI|XII|XIII|XIV|XV|XVI|XVII|XVIII|XIX|XX)"
    r"(?:\s+(?:LLC|Inc|Corp|Ltd|LP|LLP|Co))?\s*$",
    re.IGNORECASE,
)
# "Acme 278 LLC" — trailing-number SPV identifier.
_TRAILING_DIGIT_SPV_RE = re.compile(
    r"\b\d{2,4}\s+(?:LLC|Inc|Corp|Ltd|LP|LLP|Co)\.?\s*$",
    re.IGNORECASE,
)
# Ticker-style 3-5 char alphanumeric "name" like ACE5, APE5, LCH 4, EMT XI.
# Conservative: only flag when the whole name (modulo legal suffix) is
# a short ticker-ish token possibly with a 1-2 digit/letter suffix.
_TICKER_LLC_RE = re.compile(
    r"^\s*[A-Z]{2,5}\s*[\dIVX]{0,3}\s+(?:LLC|Inc|Corp|Ltd|LP|LLP|Co)\.?\s*$",
)

# Real-estate / property fund keywords (req #4). Conservative —
# multiple of these often co-occur in fund names.
_REAL_ESTATE_KEYWORDS_RE = re.compile(
    r"\b(Apartments?|Properties|Real\s+Estate|Hospitality|Hotels?|"
    r"Housing|Multifamily|Industrial\s+Trust|Residential|"
    r"Commercial\s+Management|Crossing|Stone\s+Ridge|Centerville|"
    r"City\s+Center|Owner\s+(?:LLC|LP|Inc|Corp)|Lessor\s+(?:LLC|LP)|"
    r"Realty)\b",
    re.IGNORECASE,
)

# Investment vehicle keywords (req #4).
_INVESTMENT_VEHICLE_KEYWORDS_RE = re.compile(
    r"\b(SPV\d*|"
    r"(?:Investment|Investor|Investing)\s+Vehicle|"
    r"Investco|Equities|"
    r"Capital|Holdings?|"
    r"Private\s+Credit|"
    r"Investments?\s+LLC|"
    r"Multi[-\s]?Strategy|"
    r"Co[-\s]?Invest(?:ment)?|Coinvest|"
    r"(?:Direct|Access)(?:\s+[IVX]+)?\s+(?:LLC|Inc|Fund|Vehicle))\b",
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
        return False
    if _STREET_SPV_RE.search(name):
        return False
    if _TRANCHE_SUFFIX_RE.search(name):
        return False
    if _ROMAN_NUMERAL_TAIL_RE.search(name):
        return False
    if _TRAILING_DIGIT_SPV_RE.search(name):
        return False
    if _TICKER_LLC_RE.search(name):
        return False
    if _REAL_ESTATE_KEYWORDS_RE.search(name):
        return False
    if _INVESTMENT_VEHICLE_KEYWORDS_RE.search(name):
        return False
    return True


def _extract_company_name(title: str) -> str | None:
    """Used for the getcurrent Atom feed."""
    m = _TITLE_RE.match(title)
    if m is None:
        return None
    return m.group("name").strip() or None


def _extract_name_from_display(display: str) -> str | None:
    """Used for the EFTS ``display_names`` field."""
    m = _DISPLAY_NAME_RE.match(display)
    if m is None:
        return display.strip() or None
    return m.group("name").strip() or None


# --- EFTS primary path -----------------------------------------------------


def _form_d_xml_url(adsh: str, cik: str) -> str | None:
    if not adsh or not cik:
        return None
    try:
        cik_int = int(cik)
    except (TypeError, ValueError):
        return None
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
        f"{adsh.replace('-', '')}/primary_doc.xml"
    )


def _is_pooled_fund_xml(adsh: str, cik: str) -> bool | None:
    """Fetch the Form D XML and check Item 6 / industryGroupType for
    pooled-fund flag. Returns True/False on a successful fetch, None
    on network error (treat as 'unknown — fall through to other
    filters')."""
    url = _form_d_xml_url(adsh, cik)
    if url is None:
        return None
    try:
        r = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=15,
        )
        r.raise_for_status()
    except requests.RequestException as exc:
        _log.warning("edgar form-d xml fetch failed adsh=%s: %s", adsh, exc)
        return None
    xml = r.text
    return any(p.search(xml) for p in _POOLED_FUND_FLAG_RES)


def _fetch_efts_page(
    *, start_date: str, end_date: str, offset: int, hits: int
) -> dict[str, Any] | None:
    """Single page of EFTS results. Returns the parsed JSON or None on
    error. EFTS expects ``dateRange=custom`` with ``startdt`` / ``enddt``
    in YYYY-MM-DD."""
    params = {
        "q": "",
        "forms": "D",
        "dateRange": "custom",
        "startdt": start_date,
        "enddt": end_date,
        "from": offset,
        "hits": hits,
    }
    try:
        r = requests.get(
            _EFTS_URL,
            params=params,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as exc:
        _log.warning("edgar efts page failed offset=%d: %s", offset, exc)
        return None


def _fetch_from_efts(
    since: datetime, *, max_pages: int = _EFTS_DEFAULT_PAGES
) -> list[LeadCandidate]:
    captured_at = _utcnow()
    end_date = captured_at.date().isoformat()
    start_date = since.date().isoformat()

    candidates: list[LeadCandidate] = []
    for page in range(max_pages):
        offset = page * _EFTS_PAGE_SIZE
        data = _fetch_efts_page(
            start_date=start_date, end_date=end_date,
            offset=offset, hits=_EFTS_PAGE_SIZE,
        )
        if data is None:
            break
        hits = (data.get("hits") or {}).get("hits") or []
        if not hits:
            break
        for hit in hits:
            src = hit.get("_source") or {}
            display_names = src.get("display_names") or []
            if not display_names:
                continue
            company = _extract_name_from_display(display_names[0])
            if not company:
                continue
            if not _is_operating_company(company):
                continue

            file_date = str(src.get("file_date") or "")
            adsh = str(src.get("adsh") or "")
            ciks = src.get("ciks") or []

            # Item 6 / industryGroupType check. Only fires for names
            # that survived the regex filter — those are most likely
            # to be misclassified operating-cos that are actually
            # pooled funds. Adds ~0.3s per surviving candidate.
            if ciks and adsh:
                is_fund = _is_pooled_fund_xml(adsh, ciks[0])
                if is_fund is True:
                    _log.info("edgar efts: skipping pooled fund: %s", company)
                    continue

            link = (
                f"https://www.sec.gov/Archives/edgar/data/{int(ciks[0])}/{adsh.replace('-', '')}/{adsh}-index.htm"
                if ciks and adsh else ""
            )

            candidates.append(
                LeadCandidate(
                    name=company,
                    domain=None,
                    initial_signal=Signal(
                        type=SignalType.FUNDING_RAISED,
                        source=SourceName.EDGAR_FORM_D,
                        captured_at=captured_at,
                        payload={
                            "title": f"D - {company}",
                            "filing_type": "Form D",
                            "filed_on": file_date,
                            "link": link,
                            "biz_state": (src.get("biz_states") or [None])[0],
                            "biz_location": (src.get("biz_locations") or [None])[0],
                        },
                    ),
                )
            )
        # Short-circuit when we've reached the end of the result set.
        total = (data.get("hits") or {}).get("total") or {}
        total_value = int(total.get("value") or 0)
        if offset + _EFTS_PAGE_SIZE >= total_value:
            break

    _log.info(
        "edgar efts: %d operating-company candidates from %s..%s",
        len(candidates), start_date, end_date,
    )
    return candidates


# --- getcurrent backstop ---------------------------------------------------


def _fetch_from_getcurrent(since: datetime) -> list[LeadCandidate]:
    """Last ~100 filings of all types from the getcurrent Atom feed.
    Catches Form Ds that EFTS hasn't indexed yet (typically a few hours
    of lag). Filtered to type=D entries via the title prefix."""
    captured_at = _utcnow()

    try:
        feed = feedparser.parse(
            _EDGAR_GETCURRENT_URL, request_headers={"User-Agent": _USER_AGENT}
        )
    except Exception:
        _log.exception("edgar getcurrent fetch failed")
        return []

    candidates: list[LeadCandidate] = []
    for entry in feed.entries:
        terms = {t.get("term", "").upper() for t in (entry.get("tags") or [])}
        if "D" not in terms:
            if not (entry.get("title") or "").strip().lower().startswith("d -"):
                continue

        title = (entry.get("title") or "").strip()
        company = _extract_company_name(title)
        if not company:
            continue
        if not _is_operating_company(company):
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
                    source=SourceName.EDGAR_FORM_D,
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

    return candidates


# --- Public ----------------------------------------------------------------


def fetch(*, since: datetime, limit: int | None = None) -> list[LeadCandidate]:
    """EFTS pages (deep backfill) ∪ getcurrent (freshness backstop),
    deduped on the operating-company name. Capped by ``limit`` if set."""
    out: list[LeadCandidate] = []
    seen_names: set[str] = set()

    for source_name, getter in (
        ("efts", lambda: _fetch_from_efts(since)),
        ("getcurrent", lambda: _fetch_from_getcurrent(since)),
    ):
        try:
            for cand in getter():
                key = cand.name.strip().lower()
                if key in seen_names:
                    continue
                seen_names.add(key)
                out.append(cand)
        except Exception:
            _log.exception("edgar %s failed", source_name)

    if limit is not None:
        out = out[:limit]
    return out

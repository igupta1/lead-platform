"""SEC EDGAR Form D filings source.

Two backends:

1. EFTS full-text search (``efts.sec.gov/LATEST/search-index``) —
   primary. Supports date-range queries, pages 100 hits at a time,
   covers the full 90-day window the spec calls for. The previous
   getcurrent-only path was a lie: it returned the last ~100 filings
   of *all* types and our 90-day ``since`` filter was aspirational.

2. ``getcurrent`` Atom feed — backstop, kept for the freshest filings
   in case EFTS indexing lags by a few hours.

Both emit the same ``FUNDING_FORM_D`` signal shape. Operating-company
filter (drops funds / SPVs / partnerships / vintage-year vehicles)
applies to both.

The EFTS path fetches each surviving filing's primary_doc.xml (it
always did, for the pooled-fund check) and now mines it instead of
discarding it: offering amount, related persons (officer names and
titles — free DM data), industry group, revenue range. ``fetch``
returns ``(candidates, disqualifiers)`` for a uniform source contract,
but this source produces no disqualifiers (the list is always empty).

Independent from insurance_pipeline. Mirror code, mirror filters;
no cross-imports.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
import requests

from leadgen.models import (
    Disqualifier,
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


def _parse_iso_date(value: str | None) -> datetime | None:
    """Parse a ``YYYY-MM-DD`` filing date into a naive-UTC datetime."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


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


# Operator titles worth surfacing as the DM (enrichment consumes this
# ordering implicitly — first operator-titled officer wins).
_MAX_OFFICERS_IN_PAYLOAD = 8


def _form_d_evidence(
    *,
    company: str,
    filed_on: str,
    offering_amount: float | None = None,
    amount_sold: float | None = None,
    industry_group: str | None = None,
    revenue_range: str | None = None,
) -> str:
    """Concise verbatim-factual evidence line built only from fields the
    filing actually carries — no invention. Always non-empty."""
    lead = f"Form D filed {filed_on}" if filed_on else "Form D filing"
    extras: list[str] = []
    if offering_amount is not None:
        extras.append(f"${offering_amount:,.0f} offering")
    if amount_sold is not None:
        extras.append(f"${amount_sold:,.0f} sold to date")
    if industry_group:
        extras.append(f"industry {industry_group}")
    if revenue_range:
        extras.append(f"revenue range {revenue_range}")
    body = f"{lead}: " + ", ".join(extras) if extras else lead
    return f"{company} — {body}" if company else body


def _parse_form_d_xml(xml: str) -> dict[str, Any]:
    """Extract everything useful from a Form D primary_doc.xml in one
    pass. The documents are small, machine-generated, and schema-
    stable (no namespaces), so ElementTree over the raw text is safe;
    a parse failure degrades to the regex pooled-fund check only."""
    details: dict[str, Any] = {
        "is_pooled_fund": any(p.search(xml) for p in _POOLED_FUND_FLAG_RES),
        "offering_amount": None,
        "amount_sold": None,
        "industry_group": None,
        "revenue_range": None,
        "officers": [],
    }
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return details

    def _text(path: str) -> str | None:
        el = root.find(path)
        if el is None or el.text is None:
            return None
        return el.text.strip() or None

    def _amount(path: str) -> float | None:
        raw = _text(path)
        if raw is None:
            return None
        try:
            return float(raw.replace(",", ""))
        except ValueError:
            return None  # "Indefinite" — continuous offering.

    details["offering_amount"] = _amount(
        ".//offeringSalesAmounts/totalOfferingAmount"
    )
    details["amount_sold"] = _amount(".//offeringSalesAmounts/totalAmountSold")
    details["industry_group"] = _text(".//industryGroup/industryGroupType")
    details["revenue_range"] = _text(".//issuerSize/revenueRange")

    officers: list[dict[str, str]] = []
    for rp in root.findall(".//relatedPersonsList/relatedPersonInfo"):
        first = (rp.findtext("relatedPersonName/firstName") or "").strip()
        last = (rp.findtext("relatedPersonName/lastName") or "").strip()
        name = f"{first} {last}".strip()
        if not name:
            continue
        clarification = (rp.findtext("relationshipClarification") or "").strip()
        relationships = [
            (r.text or "").strip()
            for r in rp.findall("relatedPersonRelationshipList/relationship")
            if (r.text or "").strip()
        ]
        title = clarification or ", ".join(relationships)
        officers.append({"name": name, "title": title})
    details["officers"] = officers[:_MAX_OFFICERS_IN_PAYLOAD]
    return details


def _fetch_form_d_details(adsh: str, cik: str) -> dict[str, Any] | None:
    """Fetch + parse the Form D XML. Returns None on network error
    (treat as 'unknown — fall through to other filters')."""
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
    return _parse_form_d_xml(r.text)


# A transient SEC hiccup on the EFTS endpoint used to silently zero out a
# whole night's funding: the error was swallowed to None and logged as a
# normal "0 candidates". Retry a few times with backoff before giving up.
_EFTS_MAX_RETRIES = 3
_EFTS_RETRY_BACKOFF_S = 2.0


def _efts_get(params: dict[str, str | int]) -> dict[str, Any] | None:
    """GET one EFTS search page with retry/backoff. Returns parsed JSON, or
    None only after every retry fails. Shared by the Form D and Form C
    sources (which differ only in the ``forms`` param)."""
    for attempt in range(_EFTS_MAX_RETRIES):
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
            _log.warning(
                "edgar efts page failed (attempt %d/%d): %s",
                attempt + 1, _EFTS_MAX_RETRIES, exc,
            )
            if attempt + 1 == _EFTS_MAX_RETRIES:
                return None
            time.sleep(_EFTS_RETRY_BACKOFF_S * (attempt + 1))
    return None


def _fetch_efts_page(
    *, start_date: str, end_date: str, offset: int, hits: int
) -> dict[str, Any] | None:
    """Single page of EFTS results. Returns the parsed JSON or None on
    error. EFTS expects ``dateRange=custom`` with ``startdt`` / ``enddt``
    in YYYY-MM-DD."""
    return _efts_get({
        "q": "",
        "forms": "D",
        "dateRange": "custom",
        "startdt": start_date,
        "enddt": end_date,
        "from": offset,
        "hits": hits,
    })


def _fetch_from_efts(
    since: datetime, *, max_pages: int = _EFTS_DEFAULT_PAGES
) -> tuple[list[LeadCandidate], list[Disqualifier]]:
    captured_at = _utcnow()
    end_date = captured_at.date().isoformat()
    start_date = since.date().isoformat()

    candidates: list[LeadCandidate] = []
    disqualifiers: list[Disqualifier] = []
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

            # Fetch + mine the filing XML. Only fires for names that
            # survived the regex filter — those are most likely to be
            # misclassified operating-cos that are actually pooled
            # funds. Adds ~0.3s per surviving candidate and yields
            # offering amount + officer names for free.
            details: dict[str, Any] | None = None
            if ciks and adsh:
                details = _fetch_form_d_details(adsh, ciks[0])
                if details is not None and details["is_pooled_fund"]:
                    _log.info("edgar efts: skipping pooled fund: %s", company)
                    continue

            link = (
                f"https://www.sec.gov/Archives/edgar/data/{int(ciks[0])}/{adsh.replace('-', '')}/{adsh}-index.htm"
                if ciks and adsh else ""
            )
            # No filing link -> no source_url -> the Signal cannot be
            # constructed, so drop the record instead of fabricating one.
            if not link:
                _log.info("edgar efts: dropping %s (no filing link)", company)
                continue

            biz_state = (src.get("biz_states") or [None])[0]
            biz_location = (src.get("biz_locations") or [None])[0]
            offering_amount = (details or {}).get("offering_amount")
            amount_sold = (details or {}).get("amount_sold")
            industry_group = (details or {}).get("industry_group")
            revenue_range = (details or {}).get("revenue_range")

            candidates.append(
                LeadCandidate(
                    name=company,
                    domain=None,
                    city=biz_location,
                    state=biz_state,
                    initial_signal=Signal(
                        type=SignalType.FUNDING_FORM_D,
                        source=SourceName.EDGAR_FORM_D,
                        captured_at=captured_at,
                        event_date=_parse_iso_date(file_date),
                        evidence_text=_form_d_evidence(
                            company=company,
                            filed_on=file_date,
                            offering_amount=offering_amount,
                            amount_sold=amount_sold,
                            industry_group=industry_group,
                            revenue_range=revenue_range,
                        ),
                        source_url=link,
                        payload={
                            "title": f"D - {company}",
                            "filing_type": "Form D",
                            "filed_on": file_date,
                            "link": link,
                            "biz_state": biz_state,
                            "biz_location": biz_location,
                            "offering_amount": offering_amount,
                            "amount_sold": amount_sold,
                            "industry_group": industry_group,
                            "revenue_range": revenue_range,
                            "officers": (details or {}).get("officers") or [],
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
    return candidates, disqualifiers


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

        link = str(entry.get("link") or "")
        # No filing link -> no source_url -> drop rather than fabricate.
        if not link:
            continue
        filed_on = updated_dt.date().isoformat() if updated_dt else ""
        candidates.append(
            LeadCandidate(
                name=company,
                domain=None,
                initial_signal=Signal(
                    type=SignalType.FUNDING_FORM_D,
                    source=SourceName.EDGAR_FORM_D,
                    captured_at=captured_at,
                    event_date=updated_dt,
                    evidence_text=_form_d_evidence(
                        company=company, filed_on=filed_on,
                    ),
                    source_url=link,
                    payload={
                        "title": title,
                        "filing_type": "Form D",
                        "filed_on": filed_on,
                        "link": link,
                    },
                ),
            )
        )

    return candidates


# --- Public ----------------------------------------------------------------


def fetch(
    *, since: datetime, limit: int | None = None
) -> tuple[list[LeadCandidate], list[Disqualifier]]:
    """EFTS pages (deep backfill) ∪ getcurrent (freshness backstop),
    deduped on the operating-company name. Capped by ``limit`` if set.

    Returns ``(candidates, disqualifiers)`` for a uniform source
    contract; this source produces no disqualifiers (always empty)."""
    out: list[LeadCandidate] = []
    disqualifiers: list[Disqualifier] = []
    seen_names: set[str] = set()

    try:
        efts_candidates, disqualifiers = _fetch_from_efts(since)
    except Exception:
        _log.exception("edgar efts failed")
        efts_candidates = []
    for cand in efts_candidates:
        key = cand.name.strip().lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        out.append(cand)

    try:
        for cand in _fetch_from_getcurrent(since):
            key = cand.name.strip().lower()
            if key in seen_names:
                continue
            seen_names.add(key)
            out.append(cand)
    except Exception:
        _log.exception("edgar getcurrent failed")

    if limit is not None:
        out = out[:limit]
    return out, disqualifiers

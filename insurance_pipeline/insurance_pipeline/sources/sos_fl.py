"""Florida SunBiz new-business-filing source.

SunBiz exposes a "Search by Filing Date" form. We hit it with a date
range covering the lookback window and parse the results HTML for
entity name + filing type + filed-on date. Apollo enrichment later
fills in the DM (registered-agent service companies are useless as
DM panels, see plan).

**v1 caveat.** This is the source most likely to need parser
iteration after first run — SunBiz's HTML structure isn't documented
and may differ from what we've coded against. Per-source isolation
in daily_run means a failure here doesn't break the rest of the
pipeline; the dashboard would just show funding-only leads until the
parser lands.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup, Tag

from insurance_pipeline.models import (
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)

_log = logging.getLogger(__name__)

# SunBiz's date-range search endpoint. The site is ASP.NET-shaped but
# the SearchResults page accepts inquiryType+date params on GET in
# many flows. If this URL needs to be POST or needs additional state
# tokens, the request will fall through to our defensive empty-return.
_SEARCH_URL = "https://search.sunbiz.org/Inquiry/CorporationSearch/SearchResults"

_MAX_FILING_AGE_DAYS = 60

_USER_AGENT = "Mozilla/5.0 (compatible; insurance-lead-magnet/0.1)"

# Defensive maximum on how many pages to walk if pagination is present.
# 5 pages × ~50 results/page = ~250 candidates per run, plenty for v1.
_MAX_PAGES = 5
_PER_PAGE = 50
_TIMEOUT_S = 30

# Tightened so we don't accidentally pick up a marketing slug. SunBiz
# entity-detail URLs look like
# /Inquiry/CorporationSearch/SearchResultDetail?inquirytype=...&document_number=L25000123456
_DETAIL_HREF_RE = re.compile(r"SearchResultDetail.*document_number=", re.IGNORECASE)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _format_date_param(dt: datetime) -> str:
    # SunBiz's date-input fields take MM/DD/YYYY.
    return dt.strftime("%m/%d/%Y")


def _fetch_page(
    *, filed_from: datetime, filed_to: datetime, page: int
) -> str | None:
    params = {
        "inquiryType": "FilingDate",
        "filedDateStart": _format_date_param(filed_from),
        "filedDateEnd": _format_date_param(filed_to),
        "SearchTerm": "",
        "ResultPageSize": _PER_PAGE,
        "ResultPageIndex": page,
    }
    try:
        resp = requests.get(
            _SEARCH_URL,
            params=params,
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        return resp.text
    except Exception:
        _log.exception("sos_fl page %d fetch failed", page)
        return None


def _parse_results_page(html: str, captured_at: datetime) -> list[LeadCandidate]:
    """Parse one results page. Defensive: SunBiz HTML structure may
    vary, so we accept any table row that has a recognizable
    SearchResultDetail link and at least one adjacent cell of text."""
    candidates: list[LeadCandidate] = []
    soup = BeautifulSoup(html, "html.parser")

    for row in soup.find_all("tr"):
        link = row.find("a", href=_DETAIL_HREF_RE)
        if not isinstance(link, Tag):
            continue
        name = link.get_text(strip=True)
        if not name:
            continue

        cells = [c.get_text(strip=True) for c in row.find_all("td")]
        filing_type = None
        filed_on = None
        for c in cells:
            if c == name:
                continue
            # Filing-type cells: "Florida Limited Liability", "Florida
            # Profit Corporation", "Foreign Profit", etc.
            if filing_type is None and ("Limited" in c or "Corporation" in c or "Foreign" in c or "Profit" in c or "LLC" in c):
                filing_type = c
                continue
            # Date cells: MM/DD/YYYY.
            if filed_on is None and re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", c):
                filed_on = c
                continue

        candidates.append(
            LeadCandidate(
                name=name,
                domain=None,
                initial_signal=Signal(
                    type=SignalType.NEW_BUSINESS_FILED,
                    source=SourceName.SOS_FL,
                    captured_at=captured_at,
                    payload={
                        "state": "FL",
                        "filing_type": filing_type or "Entity",
                        "filed_on": filed_on or "",
                    },
                ),
            )
        )
    return candidates


def fetch(*, since: datetime, limit: int | None = None) -> list[LeadCandidate]:
    captured_at = _utcnow()
    # Clamp the lookback to at most _MAX_FILING_AGE_DAYS — protects
    # against a stale `since` from --reenrich runs sweeping in old
    # filings.
    effective_since = max(
        since, captured_at - timedelta(days=_MAX_FILING_AGE_DAYS)
    )

    candidates: list[LeadCandidate] = []
    for page in range(1, _MAX_PAGES + 1):
        html = _fetch_page(
            filed_from=effective_since,
            filed_to=captured_at,
            page=page,
        )
        if html is None:
            break
        page_candidates = _parse_results_page(html, captured_at)
        if not page_candidates:
            # Either the parser missed the row shape OR there are no
            # more results. Either way, stop walking.
            break
        candidates.extend(page_candidates)
        if limit is not None and len(candidates) >= limit:
            break

    if limit is not None:
        candidates = candidates[:limit]
    return candidates

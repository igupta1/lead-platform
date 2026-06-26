import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup, Tag

from msp_pipeline.models import (
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)

_log = logging.getLogger(__name__)

_CA_AG_URL = "https://oag.ca.gov/privacy/databreach/list"
_ME_AG_URL = (
    "https://www.maine.gov/agviewer/content/ag/"
    "985235c7-cb95-4be2-8792-a1252b4f8318/list.html"
)
_WA_AG_URL = "https://www.atg.wa.gov/data-breach-notifications"

_USER_AGENT = "Mozilla/5.0 (compatible; msp-lead-magnet/0.1)"

# Human-readable names for the raw agency codes we stamp onto a breach
# signal's payload. Consumers (the insight-copy prompt in ``outreach.py``, the
# stale-insight check in ``daily_run.py``) map the code to one of these so the
# raw "me_ag" / "ca_ag" string never reaches a prospect-facing summary. The
# React card has its own short-form labels (``AGENCY_LABELS`` in LeadCard.js);
# these longer forms read naturally inside a sentence.
AGENCY_DISPLAY_NAMES: dict[str, str] = {
    "ca_ag": "the California Attorney General",
    "me_ag": "the Maine Attorney General",
    "wa_ag": "the Washington State Attorney General",
}

# Fallback for any agency code we don't have a name for — keeps the summary
# readable instead of leaking a raw scraper code.
_AGENCY_FALLBACK = "a state regulator"


def agency_display_name(code: str | None) -> str:
    """Map a raw breach-agency code (``me_ag``) to a readable name for use in
    prose. Unknown / missing codes degrade to ``"a state regulator"``."""
    if not code:
        return _AGENCY_FALLBACK
    return AGENCY_DISPLAY_NAMES.get(code.strip().lower(), _AGENCY_FALLBACK)


# (entity_name, date_str) extracted from a row, or None to skip the row.
RowParser = Callable[[list[Tag]], tuple[str, str] | None]


# State AGs sometimes list a breach under the reporting agent (a law firm or
# breach-response vendor) "on behalf of" the company that was actually
# breached. The lead is the breached company, not the agent — so we keep the
# text after "on behalf of" and drop the agent prefix. When no real company
# name follows (e.g. "...on behalf of its clients"), the row is unusable.
_ON_BEHALF_RE = re.compile(r"\bon behalf of\b|\bo/?b/?o\b", re.IGNORECASE)
_GENERIC_CLIENT_RE = re.compile(
    r"^(?:its?|their|our|the|an?|multiple|numerous|several|various|certain|\d+)?\s*"
    r"(?:client|customer|member|patient|individual|employee|consumer|policyholder|"
    r"person|people|account\s*holder)s?\b",
    re.IGNORECASE,
)


def _clean_breach_entity(entity: str) -> str | None:
    entity = entity.strip()
    m = _ON_BEHALF_RE.search(entity)
    if m is None:
        return entity or None
    tail = entity[m.end():].strip().lstrip(",:").strip()
    tail = re.sub(
        r"^(?:its?|their|our|the)\s+clients?\b[,:]?\s*", "", tail, flags=re.IGNORECASE
    ).strip()
    if not tail or _GENERIC_CLIENT_RE.match(tail):
        return None  # reporting agent only; no identifiable breached company
    return tail


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_us_date(value: str) -> datetime | None:
    if not value:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def _fetch_html_table(
    url: str,
    agency: str,
    since: datetime,
    row_parser: RowParser,
) -> list[LeadCandidate]:
    captured_at = _utcnow()
    candidates: list[LeadCandidate] = []
    try:
        response = requests.get(
            url, timeout=30, headers={"User-Agent": _USER_AGENT}
        )
        response.raise_for_status()
        html = response.content
    except Exception:
        _log.exception("%s fetch failed", agency)
        return []

    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        for tr in table.find_all("tr")[1:]:
            tds = tr.find_all("td")
            parsed = row_parser(tds)
            if parsed is None:
                continue
            entity, date_str = parsed
            cleaned = _clean_breach_entity(entity) if entity else None
            if not cleaned:
                continue
            disclosure = _parse_us_date(date_str)
            if disclosure and disclosure < since:
                continue
            candidates.append(
                LeadCandidate(
                    name=cleaned,
                    domain=None,
                    initial_signal=Signal(
                        type=SignalType.BREACH_DISCLOSED,
                        source=SourceName.BREACHES,
                        captured_at=captured_at,
                        payload={
                            "agency": agency,
                            "reported_date": date_str,
                        },
                    ),
                )
            )
    return candidates


def _ca_ag_row_parser(tds: list[Tag]) -> tuple[str, str] | None:
    # Columns: Organization Name | Date(s) of Breach | Reported Date
    if len(tds) < 3:
        return None
    return tds[0].get_text(strip=True), tds[-1].get_text(strip=True)


def _me_ag_row_parser(tds: list[Tag]) -> tuple[str, str] | None:
    # Columns: Date Reported | Organization Name
    if len(tds) < 2:
        return None
    return tds[1].get_text(strip=True), tds[0].get_text(strip=True)


def _wa_ag_row_parser(tds: list[Tag]) -> tuple[str, str] | None:
    # Columns: Date Reported | Organization Name | Date of Breach | ...
    if len(tds) < 2:
        return None
    return tds[1].get_text(strip=True), tds[0].get_text(strip=True)


def _fetch_from_ca_ag(since: datetime) -> list[LeadCandidate]:
    return _fetch_html_table(_CA_AG_URL, "ca_ag", since, _ca_ag_row_parser)


def _fetch_from_me_ag(since: datetime) -> list[LeadCandidate]:
    return _fetch_html_table(_ME_AG_URL, "me_ag", since, _me_ag_row_parser)


def _fetch_from_wa_ag(since: datetime) -> list[LeadCandidate]:
    return _fetch_html_table(_WA_AG_URL, "wa_ag", since, _wa_ag_row_parser)


def fetch(*, since: datetime, limit: int | None = None) -> list[LeadCandidate]:
    candidates: list[LeadCandidate] = []
    fetchers = (
        ("ca_ag", _fetch_from_ca_ag),
        ("me_ag", _fetch_from_me_ag),
        ("wa_ag", _fetch_from_wa_ag),
    )
    for name, fetcher in fetchers:
        try:
            candidates.extend(fetcher(since))
        except Exception:
            _log.exception("fetcher %s failed entirely", name)
    if limit is not None:
        candidates = candidates[:limit]
    return candidates

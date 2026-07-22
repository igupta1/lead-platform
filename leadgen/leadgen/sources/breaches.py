"""Publicly-disclosed data-breach source.

A disclosed breach is the hottest signal an MSSP can act on: the company
has a demonstrated security gap, a regulator-facing paper trail, and
(usually) a board asking "how do we make sure this never happens again".
Every candidate here emits ``BREACH_DISCLOSED``, marked ``non_exclusive``
(these are public lists — anyone can scrape them, so they're a weaker
moat than a private job-post signal, and the scorer treats them as such).

Sources (all free, public, structured government lists):

1. **HHS OCR Breach Portal** — the healthcare "wall of shame" of breaches
   affecting 500+ individuals (``ocrportal.hhs.gov``). The primary source:
   it carries a real ``Individuals Affected`` count, the covered-entity
   state, the breach type, and the submission date. The portal is a
   JSF/PrimeFaces app; we GET the rendered report table directly and walk
   its PrimeFaces pagination (no reliance on the flaky CSV-export button).
2. **California AG** (``oag.ca.gov``) — every row links to the specific
   breach-notice report page, which we use as the ``source_url``.
3. **Washington State AG** (``atg.wa.gov``) — carries a
   "Number of Washingtonians Affected" count and links to the notice PDF.
4. **Oregon DOJ** (``justice.oregon.gov``) — carries a "Number Affected"
   count in a clean single table.

Ported from ``pipeline/msp_pipeline/sources/breaches.py`` and adapted to
the leadgen Signal contract (verbatim ``evidence_text`` + required
``source_url``). The entity-cleaning ("on behalf of" -> the breached
company, not the reporting law firm) is preserved verbatim; it's what
keeps a breach-response vendor's name off a prospect card.

Every network call fails closed — one dead source is logged and skipped,
never breaks a run — mirroring the msp original's resilience.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from bs4 import BeautifulSoup, Tag

from leadgen.models import (
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)

_log = logging.getLogger("mssp.sources.breaches")

# --- URLs -------------------------------------------------------------------

# HHS OCR "wall of shame" — the report page renders the breaches-under-
# investigation table server-side (the direct portal URL redirects to the
# front page; ``breach_report_hip.jsf`` serves the table directly).
_HHS_PORTAL_URL = "https://ocrportal.hhs.gov/ocr/breach/breach_report.jsf"
_HHS_REPORT_URL = "https://ocrportal.hhs.gov/ocr/breach/breach_report_hip.jsf"
_HHS_TABLE_ID = "ocrForm:reportResultTable"

_CA_AG_URL = "https://oag.ca.gov/privacy/databreach/list"
_WA_AG_URL = "https://www.atg.wa.gov/data-breach-notifications"
_OR_DOJ_URL = "https://justice.oregon.gov/consumer/DataBreach/"

_USER_AGENT = "ishaan-personal-website mssp-lead-magnet/0.1 (ishaangpta@g.ucla.edu)"

# "Last N days" recency window (the task's N). Applied to every source's
# reported / submission date. When ``fetch`` is called with an explicit
# ``since``, that wins; otherwise breaches older than this are dropped.
_LOOKBACK_DAYS = 90

# HHS pagination safety cap. One page (100 rows, newest-first) spans ~2.5
# months of submissions, so the 90-day window is almost always covered by
# page 1; the cap just bounds a pathological deep-walk.
_HHS_ROWS_PER_PAGE = 100
_HHS_MAX_PAGES = 12

# Sources we deliberately do NOT ship a scraper for, and why. Revisit
# before wiring — do not add a flaky scraper to the run.
_TODO_SOURCES: dict[str, str] = {
    "me_ag": (
        "Maine AG took its public breach database offline (2026-07) after "
        "'an apparent abuse of our data breach reporting system'; the list.html "
        "page no longer renders a table. Was in the msp original; dropped here."
    ),
    "vt_ag": (
        "Vermont AG (ago.vermont.gov/data-breaches) returns HTTP 403 to "
        "programmatic requests (WAF-protected). Needs a browser session; not "
        "reliable enough to wire."
    ),
    "ny_ag": (
        "New York AG publishes a breach list but behind a JS-rendered search "
        "form with no clean table/CSV endpoint. Candidate if a data feed appears."
    ),
}

# Human-readable names for the raw agency codes we stamp onto a breach
# signal's payload, so a downstream summary never leaks a raw "wa_ag" code.
AGENCY_DISPLAY_NAMES: dict[str, str] = {
    "hhs_ocr": "HHS OCR",
    "ca_ag": "California AG",
    "wa_ag": "Washington State AG",
    "or_doj": "Oregon DOJ",
    # Kept for continuity even though Maine is no longer scraped (see
    # ``_TODO_SOURCES``) — old stored signals may still carry the code.
    "me_ag": "Maine AG",
}
_AGENCY_FALLBACK = "a state regulator"


def agency_display_name(code: str | None) -> str:
    """Map a raw breach-agency code (``wa_ag``) to a readable name for use
    in prose. Unknown / missing codes degrade to ``"a state regulator"``."""
    if not code:
        return _AGENCY_FALLBACK
    return AGENCY_DISPLAY_NAMES.get(code.strip().lower(), _AGENCY_FALLBACK)


# --- entity cleaning (ported verbatim from the msp original) ----------------

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


# --- small helpers ----------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _cutoff(since: datetime | None) -> datetime:
    """The oldest reported/submission date we keep. Explicit ``since`` wins;
    otherwise fall back to the module ``_LOOKBACK_DAYS`` window."""
    s = _naive(since)
    return s if s is not None else (_utcnow() - timedelta(days=_LOOKBACK_DAYS))


def _parse_us_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def _parse_record_count(value: str | None) -> int | None:
    """Parse an "individuals affected" figure. Strips commas/whitespace and
    ignores non-numeric placeholders ("Unknown", "", "N/A"). Returns None
    when there's no positive integer to report."""
    if not value:
        return None
    digits = re.sub(r"[^\d]", "", value)
    if not digits:
        return None
    try:
        n = int(digits)
    except ValueError:
        return None
    return n if n > 0 else None


def _evidence_text(
    company: str,
    record_count: int | None,
    reported_date: str,
    agency_code: str,
) -> str:
    """A concise factual line built only from fields actually present. Always
    non-empty (company is always present), so the Signal always constructs."""
    text = f"{company} reported a data breach"
    if record_count is not None:
        text += f" affecting {record_count:,} individuals"
    meta = [p for p in (reported_date.strip() if reported_date else "", agency_display_name(agency_code)) if p]
    if meta:
        text += f" ({', '.join(meta)})"
    return text


def _make_candidate(
    *,
    company: str,
    agency_code: str,
    source_url: str,
    reported_date: str,
    record_count: int | None,
    breach_type: str | None,
    state: str | None,
    event_date: datetime | None,
    captured_at: datetime,
    extra_payload: dict[str, Any] | None = None,
) -> LeadCandidate | None:
    """Build a breach LeadCandidate, or None if the row can't carry a real
    ``source_url`` (a Signal with an empty source_url raises at construction,
    so we drop the row rather than let it blow up the run)."""
    company = (company or "").strip()
    source_url = (source_url or "").strip()
    if not company or not source_url:
        return None
    payload: dict[str, Any] = {
        "agency": agency_code,
        "record_count": record_count,
        "reported_date": reported_date,
        "breach_type": breach_type,
        "non_exclusive": True,  # public list — weaker moat than a private signal
    }
    if state:
        payload["state"] = state
    if extra_payload:
        payload.update(extra_payload)
    try:
        signal = Signal(
            type=SignalType.BREACH_DISCLOSED,
            source=SourceName.BREACHES,
            captured_at=captured_at,
            event_date=event_date,
            evidence_text=_evidence_text(company, record_count, reported_date, agency_code),
            source_url=source_url,
            payload=payload,
        )
    except Exception:
        # Belt-and-suspenders: a validator raised (empty evidence/url). Drop
        # the row, never the run.
        _log.debug("breaches: dropping unbuildable %s row for %r", agency_code, company)
        return None
    return LeadCandidate(name=company, state=state, initial_signal=signal)


def _get(url: str, session: requests.Session | None = None) -> bytes | None:
    getter = session.get if session is not None else requests.get
    try:
        resp = getter(url, timeout=30, headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
        return resp.content
    except Exception:
        _log.exception("breaches: fetch failed for %s", url)
        return None


# --- HHS OCR (primary) ------------------------------------------------------

# tbody columns:
#   0 expander | 1 Name of Covered Entity | 2 State | 3 Covered Entity Type
#   4 Individuals Affected | 5 Breach Submission Date | 6 Type of Breach
#   7 Location of Breached Information | 8 Web Description flag
def _hhs_parse_rows(container: Tag | None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if container is None:
        return rows
    for tr in container.find_all("tr", recursive=False):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 7:
            continue
        cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tds]
        rows.append(
            {
                "name": cells[1],
                "state": cells[2],
                "entity_type": cells[3],
                "individuals": cells[4],
                "submitted": cells[5],
                "breach_type": cells[6],
                "location": cells[7] if len(cells) > 7 else "",
            }
        )
    return rows


_HHS_VIEWSTATE_RE = re.compile(
    r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"'
)
_HHS_VIEWSTATE_AJAX_RE = re.compile(
    r'<update id="[^"]*ViewState[^"]*"><!\[CDATA\[(.*?)\]\]></update>', re.S
)
_HHS_TABLE_UPDATE_RE = re.compile(
    r'<update id="' + re.escape(_HHS_TABLE_ID) + r'"><!\[CDATA\[(.*?)\]\]></update>',
    re.S,
)


def _hhs_min_date(rows: list[dict[str, str]]) -> datetime | None:
    dates = [_parse_us_date(r["submitted"]) for r in rows]
    real = [d for d in dates if d is not None]
    return min(real) if real else None


def _fetch_from_hhs_ocr(since: datetime | None) -> list[LeadCandidate]:
    """Walk the HHS OCR breach report table (newest-first), paginating via
    PrimeFaces AJAX only as far back as the cutoff, and emit a candidate per
    breach. ``source_url`` is the portal URL (there is no per-entry
    permalink)."""
    captured_at = _utcnow()
    cutoff = _cutoff(since)
    session = requests.Session()
    session.headers.update({"User-Agent": _USER_AGENT})

    html = _get(_HHS_REPORT_URL, session=session)
    if html is None:
        return []
    text = html.decode("utf-8", "replace")
    soup = BeautifulSoup(html, "html.parser")
    vs_match = _HHS_VIEWSTATE_RE.search(text)
    view_state = vs_match.group(1) if vs_match else None
    all_rows = _hhs_parse_rows(soup.find(id=_HHS_TABLE_ID + "_data"))

    first = _HHS_ROWS_PER_PAGE
    page = 1
    while view_state and page < _HHS_MAX_PAGES:
        oldest = _hhs_min_date(all_rows)
        if oldest is not None and oldest < cutoff:
            break  # we've paged past the recency window
        data = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": _HHS_TABLE_ID,
            "javax.faces.partial.execute": _HHS_TABLE_ID,
            "javax.faces.partial.render": _HHS_TABLE_ID,
            _HHS_TABLE_ID: _HHS_TABLE_ID,
            _HHS_TABLE_ID + "_pagination": "true",
            _HHS_TABLE_ID + "_first": str(first),
            _HHS_TABLE_ID + "_rows": str(_HHS_ROWS_PER_PAGE),
            "ocrForm": "ocrForm",
            "javax.faces.ViewState": view_state,
        }
        try:
            resp = session.post(
                _HHS_REPORT_URL,
                data=data,
                timeout=45,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Faces-Request": "partial/ajax",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            resp.raise_for_status()
        except Exception:
            _log.exception("hhs_ocr: pagination request failed (page %d)", page + 1)
            break
        body = resp.text
        nv = _HHS_VIEWSTATE_AJAX_RE.search(body)
        if nv:
            view_state = nv.group(1)
        tbl = _HHS_TABLE_UPDATE_RE.search(body)
        if not tbl:
            break
        frag = BeautifulSoup(tbl.group(1), "html.parser")
        batch = _hhs_parse_rows(frag.find(id=_HHS_TABLE_ID + "_data") or frag)
        if not batch:
            break
        all_rows.extend(batch)
        first += _HHS_ROWS_PER_PAGE
        page += 1

    candidates: list[LeadCandidate] = []
    for row in all_rows:
        reported = _parse_us_date(row["submitted"])
        if reported is not None and reported < cutoff:
            continue
        cleaned = _clean_breach_entity(row["name"])
        if not cleaned:
            continue
        cand = _make_candidate(
            company=cleaned,
            agency_code="hhs_ocr",
            source_url=_HHS_PORTAL_URL,
            reported_date=row["submitted"],
            record_count=_parse_record_count(row["individuals"]),
            breach_type=row["breach_type"] or None,
            state=(row["state"] or "").strip() or None,
            event_date=reported,
            captured_at=captured_at,
            extra_payload={
                "covered_entity_type": row["entity_type"] or None,
                "breach_location": row["location"] or None,
            },
        )
        if cand is not None:
            candidates.append(cand)
    _log.info("hhs_ocr: %d breach candidates (%d rows scanned)", len(candidates), len(all_rows))
    return candidates


# --- State AG HTML tables ---------------------------------------------------

# (entity, reported_date_str, record_count|None, notice_url|None) or None.
_RowParser = Callable[[list[Tag]], tuple[str, str, int | None, str | None] | None]


def _fetch_ag_table(
    *,
    url: str,
    agency_code: str,
    state: str,
    since: datetime | None,
    row_parser: _RowParser,
) -> list[LeadCandidate]:
    captured_at = _utcnow()
    cutoff = _cutoff(since)
    html = _get(url)
    if html is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[LeadCandidate] = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr")[1:]:
            tds = tr.find_all("td")
            parsed = row_parser(tds)
            if parsed is None:
                continue
            entity, date_str, record_count, notice_url = parsed
            cleaned = _clean_breach_entity(entity) if entity else None
            if not cleaned:
                continue
            reported = _parse_us_date(date_str)
            if reported is not None and reported < cutoff:
                continue
            cand = _make_candidate(
                company=cleaned,
                agency_code=agency_code,
                # Prefer the per-notice URL; fall back to the state list page.
                source_url=notice_url or url,
                reported_date=date_str,
                record_count=record_count,
                breach_type=None,
                state=state,
                event_date=reported,
                captured_at=captured_at,
            )
            if cand is not None:
                candidates.append(cand)
    _log.info("%s: %d breach candidates", agency_code, len(candidates))
    return candidates


def _cell_link(td: Tag) -> str | None:
    a = td.find("a", href=True)
    if a is None:
        return None
    href = str(a["href"]).strip()
    return href or None


def _ca_ag_row_parser(tds: list[Tag]) -> tuple[str, str, int | None, str | None] | None:
    # Columns: Organization Name (links to the notice) | Date(s) of Breach |
    # Reported Date. No affected-count on the list page -> record_count=None.
    if len(tds) < 3:
        return None
    name = tds[0].get_text(strip=True)
    reported = tds[-1].get_text(strip=True)
    return name, reported, None, _cell_link(tds[0])


def _wa_ag_row_parser(tds: list[Tag]) -> tuple[str, str, int | None, str | None] | None:
    # Columns: Date Reported | Organization Name (links to the notice PDF) |
    # Date of Breach | Number of Washingtonians Affected | Information Compromised
    if len(tds) < 2:
        return None
    reported = tds[0].get_text(strip=True)
    name = tds[1].get_text(strip=True)
    count = _parse_record_count(tds[3].get_text(strip=True)) if len(tds) > 3 else None
    return name, reported, count, _cell_link(tds[1])


def _or_doj_row_parser(tds: list[Tag]) -> tuple[str, str, int | None, str | None] | None:
    # Columns: Organization | Reported Date | Dates of Breach | Dates of
    # Discovery | Date Notice Sent | Number Affected. No per-row link.
    if len(tds) < 6:
        return None
    name = tds[0].get_text(strip=True)
    reported = tds[1].get_text(strip=True)
    count = _parse_record_count(tds[5].get_text(strip=True))
    return name, reported, count, None


def _fetch_from_ca_ag(since: datetime | None) -> list[LeadCandidate]:
    return _fetch_ag_table(
        url=_CA_AG_URL, agency_code="ca_ag", state="CA",
        since=since, row_parser=_ca_ag_row_parser,
    )


def _fetch_from_wa_ag(since: datetime | None) -> list[LeadCandidate]:
    return _fetch_ag_table(
        url=_WA_AG_URL, agency_code="wa_ag", state="WA",
        since=since, row_parser=_wa_ag_row_parser,
    )


def _fetch_from_or_doj(since: datetime | None) -> list[LeadCandidate]:
    return _fetch_ag_table(
        url=_OR_DOJ_URL, agency_code="or_doj", state="OR",
        since=since, row_parser=_or_doj_row_parser,
    )


# --- Public -----------------------------------------------------------------

# The wired sources, in priority order. HHS OCR first (it carries record
# counts and is the largest structured feed).
SOURCES: tuple[tuple[str, Callable[[datetime | None], list[LeadCandidate]]], ...] = (
    ("hhs_ocr", _fetch_from_hhs_ocr),
    ("ca_ag", _fetch_from_ca_ag),
    ("wa_ag", _fetch_from_wa_ag),
    ("or_doj", _fetch_from_or_doj),
)


def fetch(
    since: datetime | None = None, *, limit: int | None = None
) -> list[LeadCandidate]:
    """Run every wired breach source and return the combined candidates.

    ``since`` is the oldest reported/submission date to keep (defaults to the
    module ``_LOOKBACK_DAYS`` window). Each source is wrapped in try/except so
    one dead list never kills the run. ``limit`` caps the combined result."""
    candidates: list[LeadCandidate] = []
    for name, fetcher in SOURCES:
        try:
            found = fetcher(since)
            _log.info("breaches source %s returned %d candidates", name, len(found))
            candidates.extend(found)
        except Exception:
            _log.exception("breaches source %s failed entirely", name)
    if limit is not None:
        candidates = candidates[:limit]
    return candidates

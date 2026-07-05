"""SEC EDGAR Reg Crowdfunding (Form C) source.

Form C issuers are the textbook fractional-CFO buyer: tiny companies
(< ~50 employees, early revenue) raising via Wefunder / StartEngine /
Republic / Honeycomb that just took on many small investors plus annual
reporting obligations, with no budget for a full-time CFO. Higher-signal
than Form D, which is dominated by funds / SPVs.

Same EFTS path as ``edgar_form_d`` but ``forms=C``. The Form C
primary_doc.xml conveniently carries the issuer's **website (domain),
employee count, revenue, and offering amount directly**, so these leads
arrive already enriched — no Gemini lookup needed to resolve the domain.

Emits ``FUNDING_RAISED`` with ``filing_type="Form C"``. The daily_run
funding-only gate floors only *Form D* offering size, so Form C leads
(inherently small Reg-CF raises) pass on the domain requirement alone.

Independent from insurance_pipeline. Reuses edgar_form_d's operating-
company filter and EFTS constants (same package).
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import requests

from cfo_pipeline.models import (
    Disqualifier,
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)
from cfo_pipeline.sources.edgar_form_d import (
    _EFTS_PAGE_SIZE,
    _EFTS_URL,
    _USER_AGENT,
    _extract_name_from_display,
    _is_operating_company,
)

_log = logging.getLogger(__name__)

_EFTS_DEFAULT_PAGES = 5  # 500 most-recent Form Cs; the operating-company filter thins them.

# Form C issuers above this employee count aren't fractional-CFO buyers
# (Reg CF is a small-company vehicle; a large filer is an outlier).
_SMB_HEADCOUNT_CAP = 75


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _local(tag: str) -> str:
    """Strip the ``{namespace}`` prefix ElementTree prepends."""
    return tag.rsplit("}", 1)[-1]


def _first_text(root: ET.Element, localname: str) -> str | None:
    """Namespace-agnostic first-match text lookup (Form C mixes the
    ``formc`` and ``com`` namespaces, so we match on local name)."""
    for el in root.iter():
        if _local(el.tag) == localname and el.text and el.text.strip():
            return el.text.strip()
    return None


def _clean_domain(website: str | None) -> str | None:
    if not website:
        return None
    d = website.strip().lower()
    d = d.split("//", 1)[-1]          # drop scheme
    d = d.split("/", 1)[0]            # drop path
    if d.startswith("www."):
        d = d[4:]
    d = d.strip()
    if not d or "." not in d or " " in d:
        return None
    return d


def _to_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw.replace(",", "").replace("$", "").strip())
    except (ValueError, AttributeError):
        return None


def _to_int(raw: str | None) -> int | None:
    f = _to_float(raw)
    return int(f) if f is not None else None


def _parse_form_c_xml(xml: str) -> dict[str, Any] | None:
    """Extract issuer + offering + financials from a Form C
    primary_doc.xml. Returns None on parse failure."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    name = _first_text(root, "nameOfIssuer")
    if not name:
        return None
    return {
        "name": name.strip(),
        "domain": _clean_domain(_first_text(root, "issuerWebsite")),
        "city": _first_text(root, "city"),
        "state": _first_text(root, "stateOrCountry"),
        "current_employees": _to_int(_first_text(root, "currentEmployees")),
        "revenue": _to_float(_first_text(root, "revenueMostRecentFiscalYear")),
        "offering_amount": _to_float(_first_text(root, "maximumOfferingAmount")),
        "deadline": _first_text(root, "deadlineDate"),
    }


def _form_c_xml_url(adsh: str, cik: str) -> str | None:
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


def _fetch_form_c_details(adsh: str, cik: str) -> dict[str, Any] | None:
    url = _form_c_xml_url(adsh, cik)
    if url is None:
        return None
    try:
        r = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=15)
        r.raise_for_status()
    except requests.RequestException as exc:
        _log.warning("edgar form-c xml fetch failed adsh=%s: %s", adsh, exc)
        return None
    return _parse_form_c_xml(r.text)


def _fetch_efts_page(
    *, start_date: str, end_date: str, offset: int, hits: int
) -> dict[str, Any] | None:
    params: dict[str, str | int] = {
        "q": "",
        "forms": "C",
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
        _log.warning("edgar form-c efts page failed offset=%d: %s", offset, exc)
        return None


def _fetch_from_efts(
    since: datetime, *, max_pages: int = _EFTS_DEFAULT_PAGES
) -> list[LeadCandidate]:
    captured_at = _utcnow()
    end_date = captured_at.date().isoformat()
    start_date = since.date().isoformat()

    candidates: list[LeadCandidate] = []
    seen_names: set[str] = set()
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
            if not company or not _is_operating_company(company):
                continue

            file_date = str(src.get("file_date") or "")
            adsh = str(src.get("adsh") or "")
            ciks = src.get("ciks") or []

            details: dict[str, Any] | None = None
            if ciks and adsh:
                details = _fetch_form_c_details(adsh, ciks[0])

            # Prefer the issuer name from the parsed XML (cleaner than
            # the EFTS display name, which can be the funding portal).
            name = (details or {}).get("name") or company
            key = name.strip().lower()
            if key in seen_names:
                continue

            headcount = (details or {}).get("current_employees")
            if headcount is not None and headcount > _SMB_HEADCOUNT_CAP:
                _log.info("edgar form-c: skipping oversized issuer %s (hc=%d)", name, headcount)
                continue

            seen_names.add(key)
            link = (
                f"https://www.sec.gov/Archives/edgar/data/{int(ciks[0])}/"
                f"{adsh.replace('-', '')}/{adsh}-index.htm"
                if ciks and adsh else ""
            )
            candidates.append(
                LeadCandidate(
                    name=name,
                    domain=(details or {}).get("domain"),
                    headcount=headcount,
                    initial_signal=Signal(
                        type=SignalType.FUNDING_RAISED,
                        source=SourceName.EDGAR_FORM_C,
                        captured_at=captured_at,
                        payload={
                            "title": f"Reg CF raise - {name}",
                            "filing_type": "Form C",
                            "filed_on": file_date,
                            "link": link,
                            "biz_state": (details or {}).get("state"),
                            "biz_location": (details or {}).get("city"),
                            "offering_amount": (details or {}).get("offering_amount"),
                            "revenue_amount": (details or {}).get("revenue"),
                            "current_employees": headcount,
                        },
                    ),
                )
            )
        total = int(((data.get("hits") or {}).get("total") or {}).get("value") or 0)
        if offset + _EFTS_PAGE_SIZE >= total:
            break

    _log.info(
        "edgar form-c: %d operating-company candidates from %s..%s",
        len(candidates), start_date, end_date,
    )
    return candidates


def fetch(
    *, since: datetime, limit: int | None = None
) -> tuple[list[LeadCandidate], list[Disqualifier]]:
    """Returns ``(candidates, disqualifiers)`` to match the two-return
    shape the runner branches on. Form C carries no related-persons
    list, so disqualifiers is always empty (the Gemini has_full_time_cfo
    check still applies downstream)."""
    try:
        candidates = _fetch_from_efts(since)
    except Exception:
        _log.exception("edgar form-c fetch failed")
        candidates = []
    if limit is not None:
        candidates = candidates[:limit]
    return candidates, []

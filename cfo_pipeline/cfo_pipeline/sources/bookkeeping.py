"""Junior finance-hire capture for the bookkeeping sub-inventory.

The CFO pipeline deliberately *drops* junior IC finance titles (bookkeeper,
staff accountant, AP/AR clerk): they aren't a fractional-CFO buying signal and
the CFO scorer zeroes them. But a lone junior finance hire at a small company is
the hottest signal a bookkeeping / outsourced-accounting firm can act on — the
company has an accounting need but hasn't committed to an in-house department.

This source runs the same job-board scrape but with junior queries and its own
classifier, emitting ``JOB_POSTED_BOOKKEEPING`` candidates tagged
``role_tier="junior"``. It is fully independent of the CFO classification path —
it only *imports* jobs.py's pure exclusion predicates (recruiter / auto-dealer /
hotel / public-sector / stale-posting filters), which does not change any CFO
behavior. The candidates it returns are written to their own inventory by
``bookkeeping_run``; they never enter the CFO scoring / purge / output gates.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Callable

from cfo_pipeline.models import LeadCandidate, Signal, SignalType, SourceName
from cfo_pipeline.sources.jobs import (
    _MAX_POSTING_AGE_DAYS,
    _is_auto_dealer_name,
    _is_automotive_title,
    _is_cfo_disqualifier_title,
    _is_finance_lead_title,
    _is_fractional_cfo_title,
    _is_hotel_name,
    _is_public_sector,
    _is_recruiter_name,
    _is_too_old,
    _utcnow,
)

_log = logging.getLogger("cfo.sources.bookkeeping")

# Junior-tier search terms. Cast for the IC accounting roles a small business
# hires *before* it has finance leadership — exactly the outsource window.
BOOKKEEPING_QUERIES: tuple[str, ...] = (
    "Bookkeeper",
    "Staff Accountant",
    "Junior Accountant",
    "Accounting Clerk",
    "Accounts Payable Specialist",
    "Accounts Receivable Specialist",
    "Accounts Payable Clerk",
    "Payroll Specialist",
    "Billing Specialist",
    "Accounting Assistant",
)

# Junior IC finance titles. Word-boundary regexes (titles come back messy).
_BOOKKEEPER_RE = re.compile(r"\bbook\s?keep(?:er|ing)?\b", re.IGNORECASE)
_STAFF_ACCOUNTANT_RE = re.compile(
    r"\b(?:staff|junior|jr\.?|entry[\s-]level|associate)\s+accountant\b", re.IGNORECASE
)
_PLAIN_ACCOUNTANT_RE = re.compile(r"\baccountant\b", re.IGNORECASE)
_AP_AR_RE = re.compile(
    r"\b(?:accounts?\s+payable|accounts?\s+receivable|a/?p|a/?r)\s+"
    r"(?:specialist|clerk|coordinator|associate|analyst|administrator)\b",
    re.IGNORECASE,
)
_PAYROLL_RE = re.compile(
    r"\bpayroll\s+(?:specialist|clerk|coordinator|administrator|associate)\b", re.IGNORECASE
)
_BILLING_RE = re.compile(
    r"\bbilling\s+(?:specialist|clerk|coordinator|associate)\b", re.IGNORECASE
)
_ACCOUNTING_ASSISTANT_RE = re.compile(r"\baccounting\s+assistant\b", re.IGNORECASE)
_ACCOUNTING_CLERK_RE = re.compile(r"\baccounting\s+clerk\b", re.IGNORECASE)

# A plain "Accountant" counts as junior only when it isn't narrowed to a
# specialist / senior track that the CFO finance-lead tier already owns.
_ACCOUNTANT_EXCLUDE_RE = re.compile(
    r"\b(?:senior|sr\.?|lead|principal|chief|tax|audit|cost|forensic|"
    r"fixed[\s-]asset|fund|corporate)\b",
    re.IGNORECASE,
)

_JUNIOR_RES: tuple[re.Pattern[str], ...] = (
    _BOOKKEEPER_RE,
    _STAFF_ACCOUNTANT_RE,
    _AP_AR_RE,
    _PAYROLL_RE,
    _BILLING_RE,
    _ACCOUNTING_ASSISTANT_RE,
    _ACCOUNTING_CLERK_RE,
)


def is_bookkeeping_title(title: str) -> bool:
    """True for a junior IC finance title. Callers must first confirm the title
    is NOT a CFO / fractional / finance-lead title (fetch() does), so the tiers
    never overlap — a lead is at most one of {CFO, finance-lead, bookkeeping}."""
    if not title:
        return False
    if any(r.search(title) for r in _JUNIOR_RES):
        return True
    # plain "Accountant" (no seniority/specialist qualifier) is junior IC.
    if _PLAIN_ACCOUNTANT_RE.search(title) and not _ACCOUNTANT_EXCLUDE_RE.search(title):
        return True
    return False


# --- location parsing ------------------------------------------------------

_US_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}


def _split_location(location: str) -> tuple[str | None, str | None]:
    """Best-effort 'City, ST' -> (city, state). Returns (None, None) for remote
    or unparseable strings."""
    loc = (location or "").strip()
    if not loc or loc.lower().startswith("remote"):
        return None, None
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    if not parts:
        return None, None
    city = parts[0]
    state = None
    if len(parts) >= 2:
        tok = parts[1].split()[0].upper() if parts[1].split() else ""
        if tok in _US_STATE_ABBR:
            state = tok
    return (city or None), state


# --- candidate emission ----------------------------------------------------

def _make_candidate(
    *, company: str, title: str, url: str, date_posted: str, site: str,
    city: str | None, state: str | None, headcount: int | None, captured_at: datetime,
) -> LeadCandidate:
    return LeadCandidate(
        name=company,
        domain=None,
        headcount=headcount,
        initial_signal=Signal(
            type=SignalType.JOB_POSTED_BOOKKEEPING,
            source=SourceName.JOBS,
            captured_at=captured_at,
            payload={
                "title": title,
                "url": url,
                "date_posted": date_posted,
                "site": site,
                "role_tier": "junior",
                "city": city,
                "state": state,
            },
        ),
    )


# A scrape returns plain row dicts (title/company/job_url/date_posted/site/
# location/…). Injectable so fetch() is unit-testable without network.
ScrapeFn = Callable[[datetime, tuple[str, ...], int], list[dict[str, Any]]]


def _classify_row(row: dict[str, Any], captured_at: datetime, max_age_days: int) -> LeadCandidate | None:
    title = str(row.get("title") or "").strip()
    company = str(row.get("company") or "").strip()
    if not title or not company:
        return None
    if (
        _is_recruiter_name(company)
        or _is_auto_dealer_name(company)
        or _is_hotel_name(company)
        or _is_public_sector(company)
        or _is_automotive_title(title)
    ):
        return None
    # Never double-classify: a title the CFO tiers own is not a bookkeeping lead.
    if (
        _is_cfo_disqualifier_title(title)
        or _is_fractional_cfo_title(title)
        or _is_finance_lead_title(title)
    ):
        return None
    if not is_bookkeeping_title(title):
        return None
    date_posted = str(row.get("date_posted") or "")
    if _is_too_old(date_posted, captured_at, max_age_days):
        return None
    city, state = _split_location(str(row.get("location") or ""))
    hc = row.get("headcount")
    return _make_candidate(
        company=company, title=title, url=str(row.get("job_url") or ""),
        date_posted=date_posted, site=str(row.get("site") or ""),
        city=city, state=state,
        headcount=int(hc) if isinstance(hc, int) else None,
        captured_at=captured_at,
    )


def fetch(
    *, since: datetime, limit: int | None = None,
    scrape: ScrapeFn | None = None, max_age_days: int = _MAX_POSTING_AGE_DAYS,
) -> list[LeadCandidate]:
    """Scrape junior finance postings and return bookkeeping candidates.

    `scrape` defaults to a jobspy run; inject a fake in tests."""
    captured_at = _utcnow()
    rows = (scrape or _default_scrape)(since, BOOKKEEPING_QUERIES, max_age_days)
    out: list[LeadCandidate] = []
    for row in rows:
        cand = _classify_row(row, captured_at, max_age_days)
        if cand is not None:
            out.append(cand)
    if limit is not None:
        out = out[:limit]
    return out


def _default_scrape(
    since: datetime, queries: tuple[str, ...], max_age_days: int
) -> list[dict[str, Any]]:
    """jobspy scrape → normalized row dicts. Imported lazily so importing this
    module (e.g. for the pure classifier in tests) never requires jobspy."""
    import jobspy  # noqa: PLC0415

    captured_at = _utcnow()
    hours_old = max(1, min(int((captured_at - since).total_seconds() / 3600), max_age_days * 24))
    recency = "in the last month"
    rows: list[dict[str, Any]] = []
    for query in queries:
        try:
            df = jobspy.scrape_jobs(
                site_name=["indeed", "google", "zip_recruiter"],
                search_term=query,
                google_search_term=f"{query} jobs in United States {recency}",
                location="United States",
                results_wanted=40,
                hours_old=hours_old,
                country_indeed="usa",
            )
        except Exception:
            _log.exception("bookkeeping jobspy query failed: %s", query)
            continue
        if df is None or len(df) == 0:
            continue
        for _, r in df.iterrows():
            rows.append({
                "title": r.get("title"),
                "company": r.get("company"),
                "job_url": r.get("job_url"),
                "date_posted": r.get("date_posted"),
                "site": r.get("site"),
                "location": r.get("location"),
            })
    return rows

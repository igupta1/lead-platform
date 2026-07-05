"""Fractional-specific job boards.

Companies posting on fractional-talent boards are shopping for exactly
the service being sold, so everything here emits
``JOB_POSTED_FRACTIONAL_CFO`` — the in-market top tier.

Both backends are **date-filtered**. We refuse to ingest an undated
posting into the in-market tier: with no event date it would score as
"fresh" and float a possibly-filled role to the very top of the page.

1. We Work Remotely (``remote-jobs.rss``): live listings, dated via
   pubDate. Titles read ``"<Company>: <Role>"``; we keep only roles
   that read fractional / interim / part-time (a plain remote
   "Controller" is a full-time hire, not this signal).
2. FractionalJobs.io: the sitemap lists ``/jobs/<role>-at-<company>``
   URLs but carries no dates, so we fetch each finance-role page for
   its ``Published:`` date and keep only recent ones. Company name is
   read from the slug; anonymized listings ("...-at-a-saas-tool") are
   skipped.

Every network call fails closed — a broken board is logged and skipped,
never breaks a run.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
import requests

from cfo_pipeline.models import (
    Disqualifier,
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)
from cfo_pipeline.sources.jobs import (
    _is_auto_dealer_name,
    _is_fractional_cfo_title,
    _is_recruiter_name,
)

_log = logging.getLogger(__name__)

_USER_AGENT = "ishaan-personal-website cfo-lead-magnet/0.1 (ishaangpta@g.ucla.edu)"
_MAX_AGE_DAYS = 60  # matches the fractional scrape window in jobs.py

# --- We Work Remotely -------------------------------------------------------

_WWR_RSS = "https://weworkremotely.com/remote-jobs.rss"


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


def _fetch_wwr(since: datetime, max_age_days: int) -> list[LeadCandidate]:
    captured_at = _utcnow()
    try:
        feed = feedparser.parse(_WWR_RSS, request_headers={"User-Agent": _USER_AGENT})
    except Exception:
        _log.exception("wwr rss fetch failed")
        return []
    candidates: list[LeadCandidate] = []
    for entry in feed.entries:
        raw_title = str(entry.get("title") or "").strip()
        if ":" not in raw_title:
            continue
        company, _, role = raw_title.partition(":")
        company = company.strip()
        role = role.strip()
        if not company or not role:
            continue
        if not _is_fractional_cfo_title(role):
            continue
        if _is_recruiter_name(company) or _is_auto_dealer_name(company):
            continue
        published = _parse_rss_date(entry.get("published") or entry.get("updated"))
        if published is not None:
            age = (captured_at - published).days
            if age > max_age_days or published < since:
                continue
        date_posted = published.date().isoformat() if published else ""
        candidates.append(
            _make_candidate(
                company=company,
                title=role,
                url=str(entry.get("link") or ""),
                date_posted=date_posted,
                site="weworkremotely",
                captured_at=captured_at,
            )
        )
    return candidates


# --- FractionalJobs.io ------------------------------------------------------

_FJ_SITEMAP = "https://www.fractionaljobs.io/sitemap.xml"
_FJ_JOB_RE = re.compile(r"https://www\.fractionaljobs\.io/jobs/([a-z0-9-]+)")
# Finance-leadership role slugs on a fractional board (drops CMO/CTO/COO
# and IC/bookkeeper roles).
_FJ_FINANCE_SLUG_RE = re.compile(
    r"(cfo|chief-financ|chief-finance|chief-accounting|controller|"
    r"vp-finance|head-of-finance|director-of-finance|finance-director|fp-a|fpa)",
    re.IGNORECASE,
)
_FJ_PUBLISHED_RE = re.compile(
    r"Published:\s*([A-Za-z]{3}\s+[A-Za-z]{3}\s+\d{1,2}\s+\d{4})"
)
_FJ_MAX_FETCHES = 150       # bound nightly load on the board
_FJ_FETCH_SPACING_S = 0.3


def _fj_split_slug(slug: str) -> tuple[str, str] | None:
    """``controller-at-anderson-lock-safe`` -> ("Controller",
    "Anderson Lock Safe"). Skips anonymized listings
    ("...-at-a-saas-tool")."""
    if "-at-" not in slug:
        return None
    role_slug, company_slug = slug.rsplit("-at-", 1)
    if not role_slug or not company_slug:
        return None
    first = company_slug.split("-", 1)[0]
    if first in ("a", "an"):
        return None  # anonymized company reference ("...-at-a-saas-tool")
    role = role_slug.replace("-", " ").strip().title()
    company = company_slug.replace("-", " ").strip().title()
    return role, company


def _fj_page_date(html: str) -> datetime | None:
    m = _FJ_PUBLISHED_RE.search(html)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%a %b %d %Y")
    except ValueError:
        return None


def _fetch_fractionaljobs(since: datetime, max_age_days: int) -> list[LeadCandidate]:
    captured_at = _utcnow()
    try:
        resp = requests.get(_FJ_SITEMAP, headers={"User-Agent": _USER_AGENT}, timeout=20)
        resp.raise_for_status()
        sitemap = resp.text
    except requests.RequestException:
        _log.exception("fractionaljobs sitemap fetch failed")
        return []

    # Collect finance-role job slugs (deduped, order-preserving).
    slugs: list[str] = []
    seen: set[str] = set()
    for m in _FJ_JOB_RE.finditer(sitemap):
        slug = m.group(1)
        if slug in seen or not _FJ_FINANCE_SLUG_RE.search(slug):
            continue
        seen.add(slug)
        slugs.append(slug)

    if len(slugs) > _FJ_MAX_FETCHES:
        _log.info(
            "fractionaljobs: %d finance slugs found, capping page-fetches at %d",
            len(slugs), _FJ_MAX_FETCHES,
        )
        slugs = slugs[:_FJ_MAX_FETCHES]

    candidates: list[LeadCandidate] = []
    for i, slug in enumerate(slugs):
        parsed = _fj_split_slug(slug)
        if parsed is None:
            continue
        role, company = parsed
        if _is_recruiter_name(company) or _is_auto_dealer_name(company):
            continue
        if i:
            time.sleep(_FJ_FETCH_SPACING_S)  # be polite
        url = f"https://www.fractionaljobs.io/jobs/{slug}"
        try:
            r = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=15)
            r.raise_for_status()
        except requests.RequestException:
            continue
        posted = _fj_page_date(r.text)
        if posted is None:
            continue  # undated -> refuse to ingest into the in-market tier
        if (captured_at - posted).days > max_age_days or posted < since:
            continue
        candidates.append(
            _make_candidate(
                company=company,
                title=role,
                url=url,
                date_posted=posted.date().isoformat(),
                site="fractionaljobs",
                captured_at=captured_at,
            )
        )
    _log.info("fractionaljobs: %d fresh finance postings", len(candidates))
    return candidates


# --- shared -----------------------------------------------------------------


def _make_candidate(
    *,
    company: str,
    title: str,
    url: str,
    date_posted: str,
    site: str,
    captured_at: datetime,
) -> LeadCandidate:
    return LeadCandidate(
        name=company,
        domain=None,
        headcount=None,
        initial_signal=Signal(
            type=SignalType.JOB_POSTED_FRACTIONAL_CFO,
            source=SourceName.FRACTIONAL_BOARD,
            captured_at=captured_at,
            payload={
                "title": title,
                "url": url,
                "date_posted": date_posted,
                "site": site,
            },
        ),
    )


def fetch(
    *, since: datetime, limit: int | None = None
) -> tuple[list[LeadCandidate], list[Disqualifier]]:
    """Returns ``(candidates, disqualifiers)``; disqualifiers is always
    empty (boards produce no CFO disqualifiers). Two-return shape so the
    runner can treat it like the jobs / edgar sources."""
    max_age_days = max(1, min((_utcnow() - since).days, _MAX_AGE_DAYS))
    candidates: list[LeadCandidate] = []
    for name, fn in (("weworkremotely", _fetch_wwr), ("fractionaljobs", _fetch_fractionaljobs)):
        try:
            candidates.extend(fn(since, max_age_days))
        except Exception:
            _log.exception("fractional board %s failed entirely", name)
    if limit is not None:
        candidates = candidates[:limit]
    return candidates, []

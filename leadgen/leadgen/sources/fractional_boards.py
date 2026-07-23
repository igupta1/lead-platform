"""Fractional-specific job boards.

A company posting on a fractional-talent board is shopping for exactly the
kind of outside help these inventories sell — the in-market top signal. The
board is niche-blind: each posting is routed through the shared jobs
``classify`` and can feed ANY niche —

* fractional CFO / controller        -> ``job_fractional_cfo`` (cfo)
* fractional / virtual CISO          -> ``job_security``       (mssp)
* fractional CIO / IT director       -> ``job_it_leadership``  (msp)
* fractional DevOps / cloud / SRE    -> ``job_cloud_devops``   (cloud)

A posting that doesn't map to any niche (a fractional CMO, head of sales, …)
is dropped.

Both backends are **date-filtered**. We refuse to ingest an undated posting
into the in-market tier: with no event date it would score as "fresh" and
float a possibly-filled role to the very top of the page.

1. We Work Remotely (``remote-jobs.rss``): a GENERAL remote board, so we keep
   only titles that are explicitly fractional / interim / part-time (a plain
   remote "Security Engineer" is a full-time hire, not this signal).
2. FractionalJobs.io: a fractional-only board. The sitemap lists
   ``/jobs/<role>-at-<company>`` URLs but carries no dates, so we fetch each
   in-scope role page for its ``Published:`` date and keep only recent ones.
   Company name is read from the slug; anonymized listings
   ("...-at-a-saas-tool") are skipped.

Every network call fails closed — a broken board is logged and skipped, never
breaks a run.
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

from leadgen.models import (
    Disqualifier,
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)
from leadgen.filters import (
    _is_auto_dealer_name,
    _is_recruiter_name,
)
from leadgen.sources.jobs import (
    _PART_TIME_QUALIFIER_RE,
    classify,
)

_log = logging.getLogger(__name__)

_USER_AGENT = "ishaan-personal-website cfo-lead-magnet/0.1 (ishaangpta@g.ucla.edu)"
_MAX_AGE_DAYS = 60  # matches the fractional scrape window in jobs.py


def _signal_type_for(role: str, *, assume_fractional: bool) -> SignalType | None:
    """Map a board posting's role title to its signal type via the shared
    ``jobs.classify``, so one board feeds every niche.

    ``classify``'s CFO bucket requires a part-time qualifier in the title. On
    a fractional-only board (``assume_fractional=True``) a bare exec title
    ("Chief Financial Officer") IS fractional, so we prepend the qualifier to
    stop it dropping. On a general board (``assume_fractional=False`` — We
    Work Remotely) we require the title to read fractional/interim itself, or
    the role is a full-time hire and not this signal."""
    is_fractional = bool(_PART_TIME_QUALIFIER_RE.search(role))
    if not assume_fractional and not is_fractional:
        return None
    title = role if is_fractional else f"fractional {role}"
    return classify(title)


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


def _parse_posted_date(value: str) -> datetime | None:
    """Parse a ``YYYY-MM-DD`` posting date into a naive-UTC datetime."""
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


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
        # General board: only explicitly fractional/interim titles count, then
        # route to whichever niche the role maps to.
        sig_type = _signal_type_for(role, assume_fractional=False)
        if sig_type is None:
            continue
        if _is_recruiter_name(company) or _is_auto_dealer_name(company):
            continue
        published = _parse_rss_date(entry.get("published") or entry.get("updated"))
        if published is not None:
            age = (captured_at - published).days
            if age > max_age_days or published < since:
                continue
        date_posted = published.date().isoformat() if published else ""
        cand = _make_candidate(
            company=company,
            title=role,
            sig_type=sig_type,
            url=str(entry.get("link") or ""),
            date_posted=date_posted,
            site="weworkremotely",
            captured_at=captured_at,
        )
        if cand is not None:
            candidates.append(cand)
    return candidates


# --- FractionalJobs.io ------------------------------------------------------

_FJ_SITEMAP = "https://www.fractionaljobs.io/sitemap.xml"
_FJ_JOB_RE = re.compile(r"https://www\.fractionaljobs\.io/jobs/([a-z0-9-]+)")
# In-scope role slugs on the fractional board: finance (cfo / accounting),
# security (mssp), IT leadership (msp) and cloud/devops (cloud). Marketing /
# sales / ops / product / design / CMO / COO / CRO / CTO are left out (no
# niche). This is a coarse pre-filter to bound the per-page fetches; the exact
# routing (and the final drop of anything off-niche) is done by ``classify``.
_FJ_ROLE_SLUG_RE = re.compile(
    r"(cfo|chief-financ|chief-accounting|controller|vp-finance|head-of-finance|"
    r"director-of-finance|finance-director|fp-a|fpa|"                              # finance
    r"ciso|chief-information-security|security-officer|infosec|"                   # security -> mssp
    r"cio|chief-information-officer|head-of-it|it-director|director-of-it|vp-of-it|"  # IT -> msp
    r"devops|site-reliability|cloud-engineer|cloud-architect|platform-engineer)",  # cloud
    re.IGNORECASE,
)
_FJ_PUBLISHED_RE = re.compile(
    r"Published:\s*([A-Za-z]{3}\s+[A-Za-z]{3}\s+\d{1,2}\s+\d{4})"
)
_FJ_MAX_FETCHES = 250       # bound nightly load (covers four niches now)
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

    # Collect in-scope role job slugs (deduped, order-preserving).
    slugs: list[str] = []
    seen: set[str] = set()
    for m in _FJ_JOB_RE.finditer(sitemap):
        slug = m.group(1)
        if slug in seen or not _FJ_ROLE_SLUG_RE.search(slug):
            continue
        seen.add(slug)
        slugs.append(slug)

    if len(slugs) > _FJ_MAX_FETCHES:
        _log.info(
            "fractionaljobs: %d in-scope slugs found, capping page-fetches at %d",
            len(slugs), _FJ_MAX_FETCHES,
        )
        slugs = slugs[:_FJ_MAX_FETCHES]

    candidates: list[LeadCandidate] = []
    fetched = 0
    for slug in slugs:
        parsed = _fj_split_slug(slug)
        if parsed is None:
            continue
        role, company = parsed
        # Route to a niche BEFORE the page fetch — skip off-niche roles cheaply
        # so they don't burn fetch budget.
        sig_type = _signal_type_for(role, assume_fractional=True)
        if sig_type is None:
            continue
        if _is_recruiter_name(company) or _is_auto_dealer_name(company):
            continue
        if fetched:
            time.sleep(_FJ_FETCH_SPACING_S)  # be polite
        fetched += 1
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
        cand = _make_candidate(
            company=company,
            title=role,
            sig_type=sig_type,
            url=url,
            date_posted=posted.date().isoformat(),
            site="fractionaljobs",
            captured_at=captured_at,
        )
        if cand is not None:
            candidates.append(cand)
    _log.info("fractionaljobs: %d fresh in-market postings", len(candidates))
    return candidates


# --- shared -----------------------------------------------------------------


def _make_candidate(
    *,
    company: str,
    title: str,
    sig_type: SignalType,
    url: str,
    date_posted: str,
    site: str,
    captured_at: datetime,
) -> LeadCandidate | None:
    # No posting URL -> no source_url -> the Signal cannot be constructed,
    # so drop the record instead of fabricating one.
    if not url:
        return None
    return LeadCandidate(
        name=company,
        domain=None,
        headcount=None,
        initial_signal=Signal(
            type=sig_type,
            source=SourceName.FRACTIONAL_BOARD,
            captured_at=captured_at,
            event_date=_parse_posted_date(date_posted),
            evidence_text=title,
            source_url=url,
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
    """Returns ``(candidates, disqualifiers)``; disqualifiers is always empty
    (boards produce no disqualifiers). Two-return shape so the runner can
    treat it like the jobs / edgar sources."""
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

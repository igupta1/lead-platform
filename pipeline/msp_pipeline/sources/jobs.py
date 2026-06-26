import logging
import os
import re
from datetime import datetime, timezone

import jobspy
import requests

from msp_pipeline.models import (
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)

_log = logging.getLogger(__name__)

_JOB_QUERIES = (
    "help desk",
    "IT support",
    "system administrator",
    "CISO",
    "security engineer",
    "DevOps engineer",
    "cloud engineer",
    "VP of IT",
    "Director of IT",
)

_EXEC_PATTERN = re.compile(
    r"\b(cio|ciso|cto|vp|vice president|head of|chief)\b",
    re.IGNORECASE,
)
_SECURITY_PATTERN = re.compile(
    r"\b(security|infosec|soc analyst|ciso)\b",
    re.IGNORECASE,
)
_CLOUD_PATTERN = re.compile(
    r"\b(devops|site reliability|sre|cloud engineer|aws|azure|gcp|kubernetes)\b",
    re.IGNORECASE,
)
_IT_LEADERSHIP_PATTERN = re.compile(
    r"\b(director of it|it manager|it director|head of it|head of technology|"
    r"vp of it|vp it|vice president of it)\b",
    re.IGNORECASE,
)
_IT_SUPPORT_PATTERN = re.compile(
    r"\b(help desk|helpdesk|it support|desktop support|system admin|sysadmin|network admin)\b",
    re.IGNORECASE,
)
_TECH_EXEC_PATTERN = re.compile(r"\b(cio|ciso|cto)\b", re.IGNORECASE)

_ADZUNA_API = "https://api.adzuna.com/v1/api/jobs/us/search/1"
_HN_ALGOLIA_API = "https://hn.algolia.com/api/v1/search_by_date"

_HN_COMPANY_PATTERN = re.compile(r"\s+at\s+(.+?)(?:\s*[-–|(]|\s*$)", re.IGNORECASE)

# Companies whose names match this pattern are almost always staffing /
# recruiting firms reposting roles on behalf of unnamed clients. The lead
# would be the recruiter, not the actual hiring company — useless for MSP
# outreach. The is_it_vendor enrichment check catches the rest of the
# vendors that don't have an obvious naming convention.
_RECRUITER_NAME_PATTERN = re.compile(
    r"\b(staffing|recruit(?:ing|er|ers|ment)|headhunter|jobot|"
    r"personnel\s+services?|talent\s+(?:group|agency|partners|solutions|acquisition)|"
    r"executive\s+search|search\s+(?:group|partners?|associates|consultants)|"
    r"\btalent$)",
    re.IGNORECASE,
)


def _is_recruiter_name(name: str) -> bool:
    return bool(_RECRUITER_NAME_PATTERN.search(name))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _classify_job_title(title: str) -> SignalType | None:
    has_exec = bool(_EXEC_PATTERN.search(title))
    has_security = bool(_SECURITY_PATTERN.search(title))
    has_cloud = bool(_CLOUD_PATTERN.search(title))
    has_it_lead = bool(_IT_LEADERSHIP_PATTERN.search(title))
    has_it_sup = bool(_IT_SUPPORT_PATTERN.search(title))
    is_tech_exec_title = bool(_TECH_EXEC_PATTERN.search(title))
    has_it_context = has_security or has_cloud or has_it_lead or has_it_sup

    if has_exec and (is_tech_exec_title or has_it_context):
        return SignalType.EXEC_HIRED
    if has_security:
        return SignalType.JOB_SECURITY
    if has_cloud:
        return SignalType.JOB_CLOUD_DEVOPS
    if has_it_lead:
        return SignalType.JOB_IT_LEADERSHIP
    if has_it_sup:
        return SignalType.JOB_IT_SUPPORT
    return None


def _extract_company_from_hn_title(title: str) -> str | None:
    m = _HN_COMPANY_PATTERN.search(title)
    if m:
        return m.group(1).strip() or None
    return None


def _fetch_from_jobspy(since: datetime) -> list[LeadCandidate]:
    captured_at = _utcnow()
    hours_old = max(1, int((captured_at - since).total_seconds() / 3600))
    candidates: list[LeadCandidate] = []
    for query in _JOB_QUERIES:
        try:
            df = jobspy.scrape_jobs(
                site_name=["indeed", "linkedin"],
                search_term=query,
                location="United States",
                results_wanted=10,
                hours_old=hours_old,
                country_indeed="usa",
            )
        except Exception:
            _log.exception("jobspy query failed: %s", query)
            continue
        if df is None or len(df) == 0:
            continue
        for _, row in df.iterrows():
            title = str(row.get("title") or "").strip()
            company = str(row.get("company") or "").strip()
            if not title or not company:
                continue
            sig_type = _classify_job_title(title)
            if sig_type is None:
                continue
            if _is_recruiter_name(company):
                continue
            candidates.append(
                LeadCandidate(
                    name=company,
                    domain=None,
                    initial_signal=Signal(
                        type=sig_type,
                        source=SourceName.JOBS,
                        captured_at=captured_at,
                        payload={
                            "title": title,
                            "url": str(row.get("job_url") or ""),
                            "date_posted": str(row.get("date_posted") or ""),
                            "site": str(row.get("site") or ""),
                        },
                    ),
                )
            )
    return candidates


def _fetch_from_adzuna(since: datetime) -> list[LeadCandidate]:
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        _log.warning("ADZUNA_APP_ID/ADZUNA_APP_KEY not set; skipping Adzuna")
        return []

    captured_at = _utcnow()
    max_days_old = max(1, (captured_at - since).days)
    candidates: list[LeadCandidate] = []
    for query in _JOB_QUERIES:
        try:
            response = requests.get(
                _ADZUNA_API,
                params={
                    "app_id": app_id,
                    "app_key": app_key,
                    "what": query,
                    "max_days_old": max_days_old,
                    "results_per_page": 20,
                },
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
        except Exception:
            _log.exception("adzuna query failed: %s", query)
            continue
        for item in data.get("results", []):
            title = str(item.get("title") or "").strip()
            company = str((item.get("company") or {}).get("display_name") or "").strip()
            if not title or not company:
                continue
            sig_type = _classify_job_title(title)
            if sig_type is None:
                continue
            if _is_recruiter_name(company):
                continue
            candidates.append(
                LeadCandidate(
                    name=company,
                    domain=None,
                    initial_signal=Signal(
                        type=sig_type,
                        source=SourceName.JOBS,
                        captured_at=captured_at,
                        payload={
                            "title": title,
                            "url": str(item.get("redirect_url") or ""),
                            "date_posted": str(item.get("created") or ""),
                            "site": "adzuna",
                        },
                    ),
                )
            )
    return candidates


def _fetch_from_hn(since: datetime) -> list[LeadCandidate]:
    captured_at = _utcnow()
    since_ts = int(since.replace(tzinfo=timezone.utc).timestamp())
    candidates: list[LeadCandidate] = []
    for query in _JOB_QUERIES:
        try:
            response = requests.get(
                _HN_ALGOLIA_API,
                params={
                    "query": query,
                    "tags": "story",
                    "numericFilters": f"created_at_i>{since_ts}",
                    "hitsPerPage": 20,
                },
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
        except Exception:
            _log.exception("hn algolia query failed: %s", query)
            continue
        for hit in data.get("hits", []):
            title = str(hit.get("title") or "").strip()
            if not title:
                continue
            company = _extract_company_from_hn_title(title)
            if not company:
                continue
            sig_type = _classify_job_title(title)
            if sig_type is None:
                continue
            if _is_recruiter_name(company):
                continue
            candidates.append(
                LeadCandidate(
                    name=company,
                    domain=None,
                    initial_signal=Signal(
                        type=sig_type,
                        source=SourceName.JOBS,
                        captured_at=captured_at,
                        payload={
                            "title": title,
                            "url": str(hit.get("url") or ""),
                            "date_posted": str(hit.get("created_at") or ""),
                            "site": "hn_algolia",
                        },
                    ),
                )
            )
    return candidates


def fetch(*, since: datetime, limit: int | None = None) -> list[LeadCandidate]:
    candidates: list[LeadCandidate] = []
    fetchers = (
        ("jobspy", _fetch_from_jobspy),
        ("adzuna", _fetch_from_adzuna),
        ("hn", _fetch_from_hn),
    )
    for name, fetcher in fetchers:
        try:
            candidates.extend(fetcher(since))
        except Exception:
            _log.exception("fetcher %s failed entirely", name)
    if limit is not None:
        candidates = candidates[:limit]
    return candidates

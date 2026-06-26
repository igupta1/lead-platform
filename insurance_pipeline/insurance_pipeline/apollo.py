"""Apollo.io decision-maker enrichment for the insurance pipeline.

Three-step lookup (org enrich → people search → people match), same
shape as ``msp_pipeline.apollo`` but with insurance-buyer title
preferences. **No IT keyword scoring** — the insurance buyer is the
Owner / Founder / CFO / COO / Controller / Office Manager / HR
Director. A CIO at an insurance prospect is the wrong DM.

This module is the visible-quality lever for the dashboard. FL SunBiz
filings list a registered-agent service company (CT Corporation,
Northwest, etc.) as the contact 70%+ of the time. Without Apollo,
those service companies appear as DM panels — useless to a sales rep.
``APOLLO_API_KEY`` is **required** at runtime for insurance v1; the
daily-run stage skips silently if unset but the dashboard quality
craters.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import requests
from pydantic import BaseModel

log = logging.getLogger(__name__)

_BASE = "https://api.apollo.io/api/v1"
_TIMEOUT = 20

# Title filter sent to Apollo's people-search. Insurance-buyer-first.
# Owner / President / Founder pinned to the top because most FL SunBiz
# leads are small LLCs with no separate finance person.
_DM_TITLES: tuple[str, ...] = (
    # Owner-operator / small-biz primary buyer.
    "Owner", "Founder", "Co-Founder",
    "President",
    "Chief Executive Officer", "CEO",
    # Finance / ops — the insurance buyer at mid-size companies.
    "Chief Financial Officer", "CFO",
    "Controller",
    "VP of Finance", "Vice President of Finance",
    "Director of Finance", "Finance Director",
    "Chief Operating Officer", "COO",
    "VP of Operations", "Vice President of Operations",
    "Director of Operations", "Operations Director",
    "HR Director", "Director of Human Resources",
    "VP of Human Resources", "Head of People", "Head of HR",
    "Office Manager",
    # Trucking-specific fallbacks for FMCSA leads.
    "Safety Director", "Director of Safety", "Compliance Officer",
)


class Result(BaseModel):
    org_found: bool = False
    dm_found: bool = False
    dm_name: str | None = None
    dm_title: str | None = None
    dm_email: str | None = None
    dm_linkedin_url: str | None = None
    apollo_person_id: str | None = None
    headcount: int | None = None


def is_configured() -> bool:
    return bool(os.environ.get("APOLLO_API_KEY"))


def _headers(api_key: str) -> dict[str, str]:
    return {
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "Accept": "application/json",
    }


def _enrich_org_by_domain(api_key: str, domain: str) -> dict[str, Any] | None:
    try:
        r = requests.post(
            f"{_BASE}/organizations/enrich",
            params={"domain": domain},
            headers=_headers(api_key),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("organization")
    except requests.RequestException as exc:
        log.warning("apollo enrich-by-domain %r failed: %s", domain, exc)
        return None


def _search_org_by_name(api_key: str, name: str) -> dict[str, Any] | None:
    try:
        r = requests.post(
            f"{_BASE}/mixed_companies/search",
            json={"q_organization_name": name, "page": 1, "per_page": 1},
            headers=_headers(api_key),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        orgs = r.json().get("organizations") or []
        return orgs[0] if orgs else None
    except requests.RequestException as exc:
        log.warning("apollo search-by-name %r failed: %s", name, exc)
        return None


def _search_people(api_key: str, org_id: str) -> list[dict[str, Any]]:
    try:
        r = requests.post(
            f"{_BASE}/mixed_people/api_search",
            json={
                "organization_ids": [org_id],
                "person_titles": list(_DM_TITLES),
                "page": 1,
                "per_page": 5,
            },
            headers=_headers(api_key),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("people") or []
    except requests.RequestException as exc:
        log.warning("apollo api_search org_id=%s failed: %s", org_id, exc)
        return []


def _match_person(api_key: str, person_id: str) -> dict[str, Any] | None:
    try:
        r = requests.post(
            f"{_BASE}/people/match",
            json={"id": person_id},
            headers=_headers(api_key),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("person")
    except requests.RequestException as exc:
        log.warning("apollo people/match id=%s failed: %s", person_id, exc)
        return None


_SENIORITY_WEIGHT: dict[str, int] = {
    "c_suite": 100,
    "founder": 100,  # tied with c_suite — small-biz owner is the buyer
    "owner": 100,
    "partner": 70,
    "vp": 60,
    "head": 50,
    "director": 45,
    "manager": 30,
    "senior": 15,
}

# Finance / ops keywords — the insurance buyer. Heavily weighted; this
# is the opposite of msp_pipeline's IT bias.
_FINANCE_PHRASES: tuple[str, ...] = ("finance", "financial")
_FINANCE_ABBREV_RE = re.compile(r"\b(cfo|controller)\b", re.IGNORECASE)

_OPS_PHRASES: tuple[str, ...] = (
    "operations", "operating", "people", "human resources",
    "office manager", "safety", "compliance",
)
_OPS_ABBREV_RE = re.compile(r"\b(coo|chro|hr)\b", re.IGNORECASE)

# IT keywords at an insurance prospect are NOT the buyer. We don't
# negatively score them (a small IT shop's CIO might also be the owner)
# but we don't bonus them either. The seniority weight alone applies.

_DISQUALIFYING_TITLE_KEYWORDS: tuple[str, ...] = (
    "recruitment", "recruiting", "talent acquisition",
    "marketing", "communications",
    "sales", "business development",
    "medical officer", "clinical",
    "diversity", "equity and inclusion",
    "facilities",
)


def _score_person(person: dict[str, Any]) -> int:
    """Higher = better insurance DM. Owner / Founder / CFO at the top;
    HR / Office Manager fall back when there's nothing better. IT roles
    get no bonus — they're not the buyer here.
    """
    title = (person.get("title") or "").lower()
    seniority = (person.get("seniority") or "").lower()
    score = _SENIORITY_WEIGHT.get(seniority, 0)
    if any(k in title for k in _FINANCE_PHRASES) or _FINANCE_ABBREV_RE.search(title):
        score += 60
    elif any(k in title for k in _OPS_PHRASES) or _OPS_ABBREV_RE.search(title):
        score += 30
    if any(k in title for k in _DISQUALIFYING_TITLE_KEYWORDS):
        score -= 1000
    return score


def _pick_best(people: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not people:
        return None
    best = max(people, key=_score_person)
    if _score_person(best) < 0:
        return None
    return best


def _sanitize_title(title: str | None) -> str | None:
    if not title:
        return None
    if len(title) > 80 or "|" in title:
        return None
    return title.strip()


def find_decision_maker(name: str, domain: str | None) -> Result:
    api_key = os.environ.get("APOLLO_API_KEY")
    if not api_key:
        raise RuntimeError("APOLLO_API_KEY not set")

    org = None
    if domain:
        org = _enrich_org_by_domain(api_key, domain)
    if not org:
        org = _search_org_by_name(api_key, name)
    if not org or not org.get("id"):
        return Result()

    org_id = str(org["id"])
    headcount = org.get("estimated_num_employees")
    if isinstance(headcount, str):
        try:
            headcount = int(headcount)
        except ValueError:
            headcount = None

    people = _search_people(api_key, org_id)
    chosen = _pick_best(people)
    if chosen is None or not chosen.get("id"):
        return Result(org_found=True, headcount=headcount)

    person_id = str(chosen["id"])
    matched = _match_person(api_key, person_id)
    if matched is None:
        return Result(
            org_found=True,
            apollo_person_id=person_id,
            headcount=headcount,
        )

    full_name = matched.get("name")
    title = _sanitize_title(matched.get("title")) or _sanitize_title(chosen.get("title"))
    email = matched.get("email")
    linkedin_url = matched.get("linkedin_url")

    return Result(
        org_found=True,
        dm_found=bool(full_name),
        dm_name=full_name,
        dm_title=title,
        dm_email=email,
        dm_linkedin_url=linkedin_url,
        apollo_person_id=person_id,
        headcount=headcount,
    )

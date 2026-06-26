"""USAspending.gov federal contract awards source.

Free, keyless, no rate limit (within reason). New federal contract
recipients need insurance triggers:
- Cyber + professional liability (for any federal data work)
- E&O (services contractors)
- D&O (corporate governance step-up after a meaningful gov contract)

Filters to small/mid-tier awards ($10K-$500K) and the last 60 days,
which dodges the Boeing/Lockheed enterprise tier while catching real
SMB federal contractors.

Emits FUNDING_RAISED — same scoring weight as Form D / TechCrunch
funding (25). A separate GOV_CONTRACT_AWARDED type could land later
if we want distinct scoring; for v1, reusing the bucket keeps scope
tight.

Skips government entities by name pattern — many "COUNTY OF X" /
"CITY OF X" winners are not insurance buyers.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import requests

from insurance_pipeline.models import (
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)

_log = logging.getLogger(__name__)

_USASPENDING_URL = (
    "https://api.usaspending.gov/api/v2/search/spending_by_award/"
)
_MAX_FILING_AGE_DAYS = 60
_TIMEOUT_S = 60

# USAspending caps each search response at 100 results regardless of
# what `limit` we pass (we verified empirically: limit=200 silently
# returns 0). To get >100 leads per run we paginate. Issue 5 raised
# the practical cap from ~30 unique recipients to ~150-250.
_LIMIT_PER_PAGE = 100
_MAX_PAGES = 5

# Award-amount window, loosened in Issue 5. Lower bound 10K to surface
# small first-time SMB federal contractors; upper bound 2M to admit
# lower-middle-market firms whose contract sits below their major-
# carrier captive program.
_MIN_AWARD = 10_000
_MAX_AWARD = 2_000_000

# Government / public-sector winners — not insurance buyers in the
# agent-prospect sense. Filter at parse time.
_GOV_ENTITY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:CITY|COUNTY|STATE|TOWN|TOWNSHIP|VILLAGE) OF\b", re.IGNORECASE),
    re.compile(r"^(?:U\.?S\.?|UNITED STATES|US ARMY|US NAVY|US AIR FORCE)\b", re.IGNORECASE),
    re.compile(r"^DEPARTMENT OF\b", re.IGNORECASE),
    re.compile(r"\b(?:UNIVERSITY|COLLEGE)\b", re.IGNORECASE),
    re.compile(r"\bSCHOOL\s+DISTRICT\b", re.IGNORECASE),
    re.compile(r"\bPUBLIC\s+SCHOOLS?\b", re.IGNORECASE),
    re.compile(r"\b(?:AUTHORITY|COMMISSION|BOARD)\s*$", re.IGNORECASE),
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_gov_entity(name: str) -> bool:
    return any(p.search(name) for p in _GOV_ENTITY_PATTERNS)


def _build_body(*, effective_since: datetime, captured_at: datetime, page: int) -> dict:
    return {
        "filters": {
            # date_type is omitted intentionally — including it as
            # "action_date" returns 0 results from USAspending (API
            # quirk we verified directly). Default behavior matches
            # contracts in the window correctly.
            "time_period": [{
                "start_date": effective_since.date().isoformat(),
                "end_date": captured_at.date().isoformat(),
            }],
            "award_type_codes": ["A", "B", "C", "D"],  # contracts
            "award_amounts": [{
                "lower_bound": _MIN_AWARD,
                "upper_bound": _MAX_AWARD,
            }],
        },
        "fields": [
            "Award ID",
            "Recipient Name",
            "Award Amount",
            "Award Date",
            "Description",
            "awarding_agency_name",
            "naics_code",
            "naics_description",
            "Place of Performance State Code",
        ],
        # Sort omitted intentionally — USAspending's `Award Date` field
        # comes back as null for most contracts via this endpoint, and
        # `sort: "Award Date"` against an all-null field returns 0
        # rows. Default ordering is fine for our use case (we re-sort
        # in scoring by recency_decay against signal captured_at).
        "limit": _LIMIT_PER_PAGE,
        "page": page,
    }


def _fetch_one_page(body: dict) -> list[dict]:
    try:
        resp = requests.post(
            _USASPENDING_URL,
            json=body,
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        _log.exception("usaspending fetch failed (page=%s)", body.get("page"))
        return []
    results = data.get("results") or []
    if not isinstance(results, list):
        _log.warning("usaspending: non-list results page=%s", body.get("page"))
        return []
    return results


def fetch(*, since: datetime, limit: int | None = None) -> list[LeadCandidate]:
    captured_at = _utcnow()
    effective_since = max(
        since, captured_at - timedelta(days=_MAX_FILING_AGE_DAYS)
    )

    # Paginate. USAspending caps each response at 100 regardless of the
    # `limit` parameter; pulling _MAX_PAGES gives us up to ~500 raw
    # results, which after gov-entity filtering + dedup typically yields
    # ~150-250 unique recipients per run.
    all_results: list[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        body = _build_body(
            effective_since=effective_since,
            captured_at=captured_at,
            page=page,
        )
        page_results = _fetch_one_page(body)
        if not page_results:
            # Either error or empty — stop walking pages.
            break
        all_results.extend(page_results)
        if len(page_results) < _LIMIT_PER_PAGE:
            # Last page (fewer than full). No need to ask for more.
            break

    candidates: list[LeadCandidate] = []
    seen_names: set[str] = set()
    for row in all_results:
        name = (row.get("Recipient Name") or "").strip()
        if not name:
            continue
        if name in seen_names:
            # Same recipient may appear multiple times if they won
            # multiple contracts in the window. Keep one.
            continue
        if _is_gov_entity(name):
            continue
        seen_names.add(name)

        award_date = row.get("Award Date") or ""
        award_amount = row.get("Award Amount") or 0
        naics_desc = (row.get("naics_description") or "").strip()
        agency = (row.get("awarding_agency_name") or "").strip()
        state = (row.get("Place of Performance State Code") or "").strip()

        candidates.append(
            LeadCandidate(
                name=name,
                domain=None,
                initial_signal=Signal(
                    type=SignalType.FUNDING_RAISED,
                    source=SourceName.FUNDING,
                    captured_at=captured_at,
                    payload={
                        "filing_type": "Federal contract",
                        "filed_on": str(award_date)[:10],
                        "amount_usd": float(award_amount) if award_amount else None,
                        "title": f"{agency} contract: {naics_desc}".strip(": "),
                        "naics": naics_desc,
                        "agency": agency,
                        "state": state,
                    },
                ),
            )
        )

    if limit is not None:
        candidates = candidates[:limit]
    return candidates

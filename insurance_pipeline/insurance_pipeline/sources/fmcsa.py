"""FMCSA Motor Carrier Census source.

Queries the FMCSA Motor Carrier Census dataset on
data.transportation.gov (resource ``az4n-8mr2``) via Socrata's SoQL
API. Filters to **active US carriers** added in the lookback window.

Every active motor carrier in the US is federally mandated to carry
commercial auto insurance — this is the strongest insurance-buying
signal we can ingest, and the dataset is free + keyless.

Defensive constraints:
- Active only (``status_code='A'``) — skips revoked / inactive
  authorities that won't actually buy coverage.
- US only (``phy_country='US'``) — Canadian carriers registering with
  FMCSA for cross-border ops don't fit the US-agent niche.
- Power units ≤ 100 — heuristic SMB filter at source-emit time, saves
  the LLM enrichment call on obvious-enterprise fleets.

``add_date`` is a YYYYMMDD string in the dataset. Lexicographic
comparison works correctly under that encoding.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

from insurance_pipeline.models import (
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)

_log = logging.getLogger(__name__)

_FMCSA_CENSUS_API = "https://data.transportation.gov/resource/az4n-8mr2.json"
_MAX_FILING_AGE_DAYS = 60
_TIMEOUT_S = 60
# Per-request row cap. Socrata's hard ceiling is 50k; our nightly run
# only needs the trailing ~10-15k new authorities/month. 5000 keeps
# the response small and our enrichment step fast.
_LIMIT = 5000
# Heuristic SMB pre-filter at source emit time. The full SMB cap
# (250 employees) still applies in enrichment.purge_disqualified.
_MAX_POWER_UNITS = 100


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _format_yyyymmdd(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")


def _parse_yyyymmdd(s: str) -> datetime | None:
    s = (s or "").strip()
    if len(s) < 8 or not s[:8].isdigit():
        return None
    try:
        return datetime.strptime(s[:8], "%Y%m%d")
    except ValueError:
        return None


def _safe_int(v: object, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def fetch(*, since: datetime, limit: int | None = None) -> list[LeadCandidate]:
    captured_at = _utcnow()
    effective_since = max(
        since, captured_at - timedelta(days=_MAX_FILING_AGE_DAYS)
    )
    since_str = _format_yyyymmdd(effective_since)

    params = {
        "$where": (
            f"add_date>'{since_str}' AND status_code='A' "
            f"AND phy_country='US'"
        ),
        "$order": "add_date DESC",
        "$limit": _LIMIT,
    }

    try:
        resp = requests.get(_FMCSA_CENSUS_API, params=params, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        rows = resp.json()
    except Exception:
        _log.exception("fmcsa census fetch failed")
        return []

    if not isinstance(rows, list):
        _log.warning("fmcsa census returned non-list response")
        return []

    candidates: list[LeadCandidate] = []
    for row in rows:
        name = (row.get("legal_name") or "").strip()
        if not name:
            continue

        add_dt = _parse_yyyymmdd(row.get("add_date") or "")
        if add_dt is None or add_dt < effective_since:
            # Defensive: drop rows outside the requested window even if
            # Socrata's $where somehow returned them.
            continue

        power_units = _safe_int(row.get("power_units"))
        if power_units > _MAX_POWER_UNITS:
            continue

        usdot = str(row.get("dot_number") or "").strip()
        # USDOT-keyed dedup avoids collapsing two distinct carriers with
        # the same legal name (e.g. two "FX TRUCKING LLC" with different
        # USDOTs in different states). Fall back to name-based dedup if
        # the row is missing a USDOT (shouldn't happen but defensive).
        dedup_key = f"usdot:{usdot}" if usdot else None

        candidates.append(
            LeadCandidate(
                name=name,
                domain=None,
                dedup_key=dedup_key,
                initial_signal=Signal(
                    type=SignalType.NEW_MOTOR_CARRIER_AUTHORITY,
                    source=SourceName.FMCSA,
                    captured_at=captured_at,
                    payload={
                        "usdot": usdot,
                        "dba_name": (row.get("dba_name") or "").strip(),
                        "issue_date": add_dt.date().isoformat(),
                        "fleet_size_power_units": power_units,
                        "drivers": _safe_int(row.get("total_drivers")),
                        "carrier_operation": (
                            row.get("carrier_operation") or ""
                        ).strip(),
                        "city": (row.get("phy_city") or "").strip(),
                        "state": (row.get("phy_state") or "").strip(),
                        "officer_name": (
                            row.get("company_officer_1") or ""
                        ).strip(),
                    },
                ),
            )
        )

    if limit is not None:
        candidates = candidates[:limit]
    return candidates

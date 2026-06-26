"""Smoke tests for the FMCSA Motor Carrier Census source.

Network-mocked. Verifies SoQL parameter shape, the active-US-small
filter, the YYYYMMDD date parser, and defensive handling of malformed
rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

from insurance_pipeline.models import SignalType, SourceName
from insurance_pipeline.sources import fmcsa


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _row(
    *,
    name: str = "Pioneer Logistics LLC",
    add_date: str | None = None,
    state: str = "FL",
    power_units: int = 1,
    drivers: int = 1,
    dot: str = "12345678",
    officer: str = "Jane Doe",
) -> dict[str, Any]:
    if add_date is None:
        add_date = _now().strftime("%Y%m%d")
    return {
        "legal_name": name,
        "dba_name": "",
        "add_date": add_date,
        "status_code": "A",
        "phy_country": "US",
        "phy_state": state,
        "phy_city": "Miami",
        "power_units": str(power_units),
        "total_drivers": str(drivers),
        "dot_number": dot,
        "carrier_operation": "A",
        "company_officer_1": officer,
    }


def test_fetch_parses_carriers() -> None:
    rows = [
        _row(name="Pioneer Logistics LLC"),
        _row(name="Sunshine Trucking LLC", power_units=5, drivers=4),
    ]

    def fake_get(url: str, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=rows)
        return resp

    with patch.object(fmcsa.requests, "get", side_effect=fake_get):
        cands = fmcsa.fetch(since=_now() - timedelta(days=14))

    names = {c.name for c in cands}
    assert names == {"Pioneer Logistics LLC", "Sunshine Trucking LLC"}

    by_name = {c.name: c.initial_signal for c in cands}
    sig = by_name["Pioneer Logistics LLC"]
    assert sig.type == SignalType.NEW_MOTOR_CARRIER_AUTHORITY
    assert sig.source == SourceName.FMCSA
    assert sig.payload["usdot"] == "12345678"
    assert sig.payload["state"] == "FL"
    assert sig.payload["fleet_size_power_units"] == 1
    assert sig.payload["officer_name"] == "Jane Doe"


def test_fetch_filters_enterprise_fleets() -> None:
    # power_units > 100 is the source-level SMB pre-filter.
    rows = [
        _row(name="Big Trucks Inc", power_units=500),
        _row(name="Small Trucks LLC", power_units=3),
    ]

    def fake_get(url: str, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=rows)
        return resp

    with patch.object(fmcsa.requests, "get", side_effect=fake_get):
        cands = fmcsa.fetch(since=_now() - timedelta(days=14))

    names = {c.name for c in cands}
    assert names == {"Small Trucks LLC"}


def test_fetch_drops_rows_outside_window() -> None:
    very_old = (_now() - timedelta(days=400)).strftime("%Y%m%d")
    rows = [_row(name="Old Inc", add_date=very_old)]

    def fake_get(url: str, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=rows)
        return resp

    with patch.object(fmcsa.requests, "get", side_effect=fake_get):
        cands = fmcsa.fetch(since=_now() - timedelta(days=14))

    assert cands == []


def test_fetch_clamps_since_to_max_age() -> None:
    """A long-tail since should clamp to _MAX_FILING_AGE_DAYS."""
    captured: list[dict[str, object]] = []

    def fake_get(url: str, **kwargs: object) -> MagicMock:
        captured.append(dict(kwargs.get("params") or {}))
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=[])
        return resp

    with patch.object(fmcsa.requests, "get", side_effect=fake_get):
        fmcsa.fetch(since=datetime(2020, 1, 1))

    assert captured, "expected one HTTP call"
    where = str(captured[0]["$where"])
    # Extract date from "add_date>'YYYYMMDD' AND ..."
    import re
    m = re.search(r"add_date>'(\d{8})'", where)
    assert m, f"date not found in where clause: {where}"
    start = datetime.strptime(m.group(1), "%Y%m%d")
    age = (_now() - start).days
    assert (
        fmcsa._MAX_FILING_AGE_DAYS - 1
        <= age
        <= fmcsa._MAX_FILING_AGE_DAYS + 1
    )


def test_fetch_handles_network_failure() -> None:
    def fake_get(url: str, **kwargs: object) -> MagicMock:
        raise fmcsa.requests.ConnectionError("boom")

    with patch.object(fmcsa.requests, "get", side_effect=fake_get):
        assert fmcsa.fetch(since=_now() - timedelta(days=14)) == []


def test_fetch_handles_malformed_rows() -> None:
    rows = [
        {"legal_name": "", "add_date": "20260514"},  # blank name
        {"legal_name": "No Date Inc", "add_date": ""},  # blank date
        {"legal_name": "Bad Date Inc", "add_date": "not-a-date"},
        _row(name="Valid LLC"),  # one good row
    ]

    def fake_get(url: str, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=rows)
        return resp

    with patch.object(fmcsa.requests, "get", side_effect=fake_get):
        cands = fmcsa.fetch(since=_now() - timedelta(days=14))

    assert [c.name for c in cands] == ["Valid LLC"]


def test_limit_caps_total() -> None:
    rows = [_row(name=f"Carrier {i} LLC", dot=str(i)) for i in range(10)]

    def fake_get(url: str, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=rows)
        return resp

    with patch.object(fmcsa.requests, "get", side_effect=fake_get):
        cands = fmcsa.fetch(since=_now() - timedelta(days=14), limit=3)

    assert len(cands) == 3

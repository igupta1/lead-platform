"""Smoke tests for the USAspending federal-contract source.

Network-mocked. Verifies request body shape, the government-entity
filter, dedup of repeat recipients, defensive parse.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

from insurance_pipeline.models import SignalType, SourceName
from insurance_pipeline.sources import usaspending


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _result(name: str, **kw: Any) -> dict[str, Any]:
    return {
        "Recipient Name": name,
        "Award Amount": kw.get("amount", 50_000.0),
        "Award Date": kw.get("date", _now().date().isoformat()),
        "awarding_agency_name": kw.get("agency", "Department of Defense"),
        "naics_description": kw.get("naics", "ENGINEERING SERVICES"),
        "Place of Performance State Code": kw.get("state", "VA"),
    }


def test_is_gov_entity() -> None:
    for name in (
        "COUNTY OF SAN DIEGO",
        "CITY OF AUSTIN",
        "U.S. ARMY",
        "DEPARTMENT OF VETERANS AFFAIRS",
        "TEXAS A&M UNIVERSITY",
        "Acme School District",
        "Riverside Public Schools",
    ):
        assert usaspending._is_gov_entity(name), f"expected gov: {name!r}"

    for name in (
        "Pioneer Engineering LLC",
        "Acme Systems Inc.",
        "Bright Health Co",
    ):
        assert not usaspending._is_gov_entity(name), f"expected operating: {name!r}"


def test_fetch_extracts_contracts(monkeypatch) -> None:
    results = [
        _result("Pioneer Engineering LLC", amount=125_000),
        _result("COUNTY OF SAN DIEGO", amount=80_000),  # filtered
        _result("Acme Systems Inc.", amount=200_000),
        _result("Pioneer Engineering LLC", amount=50_000),  # dedup
    ]

    # First page returns the data, second page returns empty (signals
    # end of paging).
    page_responses = [results, []]
    call_count = {"i": 0}

    def fake_post(url: str, **kwargs: Any) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"results": page_responses[call_count["i"]] if call_count["i"] < len(page_responses) else []})
        call_count["i"] += 1
        return resp

    with patch.object(usaspending.requests, "post", side_effect=fake_post):
        cands = usaspending.fetch(since=_now() - timedelta(days=14))

    names = {c.name for c in cands}
    assert names == {"Pioneer Engineering LLC", "Acme Systems Inc."}

    by_name = {c.name: c.initial_signal for c in cands}
    sig = by_name["Pioneer Engineering LLC"]
    assert sig.type == SignalType.FUNDING_RAISED
    assert sig.source == SourceName.FUNDING
    assert sig.payload["filing_type"] == "Federal contract"
    assert sig.payload["amount_usd"] == 125_000.0
    assert "ENGINEERING" in sig.payload["title"]


def test_fetch_handles_http_failure(monkeypatch) -> None:
    def fake_post(url: str, **kwargs: Any) -> MagicMock:
        raise usaspending.requests.ConnectionError("boom")

    with patch.object(usaspending.requests, "post", side_effect=fake_post):
        assert usaspending.fetch(since=_now() - timedelta(days=14)) == []


def test_request_body_shape(monkeypatch) -> None:
    captured: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> MagicMock:
        captured.append(kwargs.get("json") or {})
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"results": []})
        return resp

    with patch.object(usaspending.requests, "post", side_effect=fake_post):
        usaspending.fetch(since=_now() - timedelta(days=14))

    assert captured, "expected POST"
    body = captured[0]
    filters = body["filters"]
    assert filters["award_type_codes"] == ["A", "B", "C", "D"]
    # Issue 5 widened the amount range from 25K-500K to 10K-2M.
    assert filters["award_amounts"][0]["lower_bound"] == 10_000
    assert filters["award_amounts"][0]["upper_bound"] == 2_000_000
    assert "time_period" in filters
    # No date_type in the time_period filter (its presence makes the
    # API return 0). No sort field (Award Date returns null and sort
    # on null returns 0). Both confirmed against the live API.
    assert "date_type" not in filters["time_period"][0]
    assert "sort" not in body
    assert body["limit"] == 100  # API silently caps higher values
    assert body["page"] == 1


def test_paginates_until_empty(monkeypatch) -> None:
    """Walks pages until USAspending returns fewer than _LIMIT_PER_PAGE
    rows (or empty). Confirms we don't stop after page 1."""
    page_1 = [_result(f"Co {i}", amount=50_000 + i) for i in range(100)]  # full page
    page_2 = [_result(f"Co2 {i}", amount=60_000 + i) for i in range(45)]  # partial → stop
    seen_pages: list[int] = []

    def fake_post(url: str, **kwargs: Any) -> MagicMock:
        body = kwargs.get("json") or {}
        page = body.get("page", 1)
        seen_pages.append(page)
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"results": page_1 if page == 1 else page_2})
        return resp

    with patch.object(usaspending.requests, "post", side_effect=fake_post):
        cands = usaspending.fetch(since=_now() - timedelta(days=14))

    # Should fetch page 1 (full), page 2 (partial → stop), and not page 3.
    assert seen_pages == [1, 2]
    assert len(cands) == 145  # 100 + 45, all unique names

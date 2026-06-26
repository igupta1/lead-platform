"""Smoke tests for the SEC EDGAR Form D source.

feedparser is mocked. Tests verify the company-name extraction, the
financial-entity filter, and defensive behavior on malformed entries.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from insurance_pipeline.models import SignalType, SourceName
from insurance_pipeline.sources import edgar_form_d


class _Entry:
    def __init__(
        self,
        title: str,
        *,
        term: str = "D",
        updated: str = "Fri, 15 May 2026 17:25:23 -0400",
        link: str = "https://example.com",
    ) -> None:
        self._d = {
            "title": title,
            "updated": updated,
            "published": updated,
            "link": link,
            "tags": [{"term": term, "scheme": "x"}],
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)


class _Feed:
    def __init__(self, entries: list[_Entry]):
        self.entries = entries


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_extract_company_name() -> None:
    assert edgar_form_d._extract_company_name(
        "D - Pioneer Robotics Inc. (0001234567) (Filer)"
    ) == "Pioneer Robotics Inc."
    assert edgar_form_d._extract_company_name(
        "D - Acme Health Co (0009999999) (Filer)"
    ) == "Acme Health Co"
    assert edgar_form_d._extract_company_name("malformed title") is None


def test_is_operating_company_filters_vc_funds() -> None:
    # Financial vehicles: drop
    for name in (
        "Acme Ventures III LP",
        "Pioneer Capital Partners",
        "Apex Holdings LLC",
        "Sequoia Fund 2026 LP",
        "Stellar Asset Management",
        "Hudson Family Office",
        "Smith Family Trust",
        # LP/LLP suffix variants seen in production noise
        "TPG AG Asset Based Credit Equity II, L.P.",
        "MidOcean Energy II-B, L.P.",
        "Stellar Co II, LP",
        # Bank holding companies
        "MC Bancshares, Inc./LA",
        "First National Bancorp",
        # GS / Big PE prefixed
        "GS Finance Corp.",
        "Blackstone Real Estate Income Fund II",
        "KKR Credit Fund III LP",
    ):
        assert not edgar_form_d._is_operating_company(name), (
            f"expected FILTERED: {name!r}"
        )

    # Operating companies: keep
    for name in (
        "Pioneer Robotics Inc.",
        "Acme Health Co",
        "Bright Solutions LLC",
        "Tier One Manufacturing Inc",
        "SKYX Platforms Corp.",
        "Prism Layer AI, Inc.",
    ):
        assert edgar_form_d._is_operating_company(name), f"expected KEPT: {name!r}"


# --- Issue 4 narrowing: real-estate SPV + vintage-year filters --------


def test_vintage_year_pattern_filters() -> None:
    """Year (1900-2199) immediately before legal suffix at end of name."""
    for name in (
        "Summit Ridge 2024 LLC",
        "Acme 1986 Inc",
        "Pioneer 2030 Corp",
        "Holdings 2024, LLC",
    ):
        assert not edgar_form_d._is_operating_company(name), (
            f"expected FILTERED (vintage-year): {name!r}"
        )


def test_vintage_year_doesnt_overmatch() -> None:
    """Deliberate non-match: year + word + suffix. Confirmed by review."""
    for name in (
        "Acme 2024 Holdings LLC",   # year mid-name → not caught (deliberate)
        "US 1031 Exchange Services Inc",  # year-like number in middle
        "3M Company",  # no year, no suffix issue
        "Acme Inc. (2024)",  # year after suffix
    ):
        # These should NOT trigger the vintage-year rule. Some may still
        # be filtered by other rules — we assert the vintage rule
        # specifically doesn't fire.
        assert not edgar_form_d._VINTAGE_YEAR_RE.search(name), (
            f"vintage-year regex over-matched: {name!r}"
        )


def test_street_spv_pattern_filters() -> None:
    """Single-property real-estate SPVs: '<street name> Blvd LLC' etc."""
    for name in (
        "JR Hyde Park Blvd LLC",
        "Marina Bay Avenue LLC",
        "Sunset Drive LLC",
        "5th Avenue Inc",
        "Madison Court LP",
    ):
        assert not edgar_form_d._is_operating_company(name), (
            f"expected FILTERED (street-SPV): {name!r}"
        )


def test_street_spv_doesnt_catch_real_companies() -> None:
    """Street-name pattern is anchored to suffix-immediately-after-
    street-type. A real company with a street word mid-name shouldn't
    trigger."""
    for name in (
        "Drive Logistics Inc",  # 'Drive' at start, not suffix
        "Park Place Catering",   # no LLC suffix at end
        "Avenue Capital LLC",    # 'Avenue' but followed by Capital
    ):
        # Vintage / financial filters may still drop some of these
        # (e.g. Avenue Capital LLC). We assert the STREET rule didn't
        # fire.
        assert not edgar_form_d._STREET_SPV_RE.search(name), (
            f"street-SPV regex over-matched: {name!r}"
        )


def test_fetch_extracts_form_d_operating_companies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [
        _Entry("D - Pioneer Robotics Inc. (0001234567) (Filer)"),
        _Entry("D - Acme Ventures III LP (0009999998) (Filer)"),  # filtered out
        _Entry("DEFA14A - Some Issuer (000111) (Filer)", term="DEFA14A"),  # filtered out
        _Entry("D - Tier One Manufacturing Inc (0008888888) (Filer)"),
    ]

    def fake_parse(url: str, **kwargs: object) -> _Feed:
        return _Feed(entries)

    monkeypatch.setattr(edgar_form_d.feedparser, "parse", fake_parse)

    cands = edgar_form_d.fetch(since=_now() - timedelta(days=14))
    names = {c.name for c in cands}
    assert names == {"Pioneer Robotics Inc.", "Tier One Manufacturing Inc"}

    by_name = {c.name: c.initial_signal for c in cands}
    sig = by_name["Pioneer Robotics Inc."]
    assert sig.type == SignalType.FUNDING_RAISED
    assert sig.source == SourceName.FUNDING
    assert sig.payload["filing_type"] == "Form D"


def test_fetch_skips_old_filings(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [
        _Entry(
            "D - Pioneer Robotics Inc. (0001234567) (Filer)",
            updated="Mon, 01 Jan 2020 00:00:00 -0400",
        ),
    ]

    def fake_parse(url: str, **kwargs: object) -> _Feed:
        return _Feed(entries)

    monkeypatch.setattr(edgar_form_d.feedparser, "parse", fake_parse)
    cands = edgar_form_d.fetch(since=_now() - timedelta(days=14))
    assert cands == []


def test_fetch_handles_feedparser_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_parse(url: str, **kwargs: object) -> _Feed:
        raise RuntimeError("network down")

    monkeypatch.setattr(edgar_form_d.feedparser, "parse", fake_parse)
    assert edgar_form_d.fetch(since=_now() - timedelta(days=14)) == []

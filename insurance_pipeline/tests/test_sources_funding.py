"""Smoke tests for the insurance pipeline's funding source.

Verifies the parse path, the title pre-filter, and the LLM-extraction
fallback. Doesn't hit the network — feedparser is mocked.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from insurance_pipeline.models import SignalType, SourceName
from insurance_pipeline.sources import funding


class _Entry:
    def __init__(self, title: str, link: str = "", published: str = ""):
        self._d = {"title": title, "link": link, "published": published}

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)


class _Feed:
    def __init__(self, entries: list[_Entry]):
        self.entries = entries


def test_is_funding_title_passes_real_funding() -> None:
    assert funding._is_funding_title("Acme raises $20M Series A")
    assert funding._is_funding_title("Beta secures $5M seed")
    assert funding._is_funding_title("Gamma closes Series B round")


def test_is_funding_title_filters_noise() -> None:
    assert not funding._is_funding_title("Acme sues competitor")
    assert not funding._is_funding_title("Beta hits $5B valuation")
    assert not funding._is_funding_title("XYZ Class Action Notice")
    assert not funding._is_funding_title("137 Ventures raises $700M for new funds")


def test_fetch_emits_funding_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [
        _Entry("Pioneer Logistics raises $12M Series A", link="https://example.com/p"),
        _Entry("Random class action notice"),  # filtered
    ]

    def fake_parse(url: str) -> _Feed:
        return _Feed(entries)

    def fake_llm(prompt: str, response_model: Any) -> Any:
        return response_model(
            company_name="Pioneer Logistics",
            is_real_buying_signal=True,
            is_vc_or_fund=False,
        )

    monkeypatch.setattr(funding.feedparser, "parse", fake_parse)
    monkeypatch.setattr(funding.llm, "call_openai", fake_llm)

    cands = funding.fetch(since=datetime(2020, 1, 1))
    names = {c.name for c in cands}
    assert "Pioneer Logistics" in names

    for c in cands:
        if c.name == "Pioneer Logistics":
            assert c.initial_signal.type == SignalType.FUNDING_RAISED
            assert c.initial_signal.source == SourceName.FUNDING


def test_fetch_falls_back_to_regex_when_llm_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [_Entry("Acme raises $20M Series A")]

    def fake_parse(url: str) -> _Feed:
        return _Feed(entries)

    def boom_llm(prompt: str, response_model: Any) -> Any:
        raise RuntimeError("openai down")

    monkeypatch.setattr(funding.feedparser, "parse", fake_parse)
    monkeypatch.setattr(funding.llm, "call_openai", boom_llm)

    cands = funding.fetch(since=datetime(2020, 1, 1))
    # Regex fallback extracts the company name from before the verb.
    assert any(c.name == "Acme" for c in cands)


def test_fetch_skips_vc_funds(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [_Entry("137 Ventures raises $700M for venture funds")]

    def fake_parse(url: str) -> _Feed:
        return _Feed(entries)

    def fake_llm(prompt: str, response_model: Any) -> Any:
        return response_model(
            company_name=None,
            is_real_buying_signal=True,
            is_vc_or_fund=True,
        )

    monkeypatch.setattr(funding.feedparser, "parse", fake_parse)
    monkeypatch.setattr(funding.llm, "call_openai", fake_llm)

    cands = funding.fetch(since=datetime(2020, 1, 1))
    assert cands == []

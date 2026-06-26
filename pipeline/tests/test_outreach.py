from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from msp_pipeline import outreach
from msp_pipeline.models import Lead, NicheName, Signal, SignalType, SourceName
from msp_pipeline.outreach import (
    Copy,
    _describe_headcount,
    _describe_location,
    _describe_signals,
    generate,
)


def _now() -> datetime:
    return datetime(2026, 5, 1, 12, 0, 0)


def _signal(
    *,
    type: SignalType,
    captured_at: datetime,
    source: SourceName = SourceName.JOBS,
    payload: dict[str, Any] | None = None,
) -> Signal:
    return Signal(
        type=type,
        source=source,
        captured_at=captured_at,
        payload=payload or {},
    )


def _lead(
    *,
    name: str = "Pioneer Legal LLP",
    industry: str | None = "legal_professional",
    headcount: int | None = 80,
    country: str | None = "US",
    signals: list[Signal] | None = None,
) -> Lead:
    return Lead(
        name=name,
        name_key=name.lower().split()[0],
        industry=industry,
        headcount=headcount,
        country=country,
        signals=signals or [],
    )


# --- Pure helpers ----------------------------------------------------------


@pytest.mark.parametrize(
    "headcount,expected",
    [(None, "unknown"), (120, "~120 employees"), (10, "~10 employees")],
)
def test_describe_headcount(headcount: int | None, expected: str) -> None:
    assert _describe_headcount(headcount) == expected


def test_describe_location_uses_latest_signal() -> None:
    older = _signal(
        type=SignalType.LOCATION_CAPTURED,
        source=SourceName.COMPUTED,
        captured_at=_now() - timedelta(days=10),
        payload={"city": "Austin", "state": "TX"},
    )
    newer = _signal(
        type=SignalType.LOCATION_CAPTURED,
        source=SourceName.COMPUTED,
        captured_at=_now(),
        payload={"city": "Boston", "state": "MA"},
    )
    lead = _lead(signals=[older, newer])
    assert _describe_location(lead) == "Boston, MA"


def test_describe_location_falls_back_to_country() -> None:
    lead = _lead(signals=[])
    assert _describe_location(lead) == "US"
    blank = _lead(country=None, signals=[])
    assert _describe_location(blank) == "unknown"


def test_describe_signals_excludes_markers() -> None:
    lead = _lead(
        signals=[
            _signal(type=SignalType.JOB_SECURITY, captured_at=_now()),
            _signal(
                type=SignalType.LOCATION_CAPTURED,
                source=SourceName.COMPUTED,
                captured_at=_now(),
                payload={"city": "Boston", "state": "MA"},
            ),
            _signal(
                type=SignalType.ENRICHMENT_RUN,
                source=SourceName.COMPUTED,
                captured_at=_now(),
            ),
        ]
    )
    out = _describe_signals(lead, now=_now())
    assert "job_posted_security" in out
    assert "location_captured" not in out
    assert "enrichment_run" not in out


def test_describe_signals_includes_payload_fields() -> None:
    lead = _lead(
        signals=[
            _signal(
                type=SignalType.JOB_SECURITY,
                captured_at=_now(),
                payload={
                    "title": "Senior Security Engineer",
                    "location": "Boston, MA",
                },
            )
        ]
    )
    out = _describe_signals(lead, now=_now())
    assert "title=" in out
    assert "Senior Security Engineer" in out
    assert "location=" in out
    assert "Boston, MA" in out


def test_describe_signals_renders_readable_agency_not_raw_code() -> None:
    # The breach payload stores a raw scraper code ("me_ag"); the prompt the
    # LLM sees must show the readable agency name so the insight can't leak it.
    lead = _lead(
        name="Chief River Nursery",
        signals=[
            _signal(
                type=SignalType.BREACH_DISCLOSED,
                source=SourceName.BREACHES,
                captured_at=_now(),
                payload={"agency": "me_ag", "reported_date": "2026-04-22"},
            )
        ],
    )
    out = _describe_signals(lead, niche=NicheName.MSSP, now=_now())
    assert "the Maine Attorney General" in out
    assert "me_ag" not in out


def test_generate_prompt_uses_readable_agency_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_call_openai(prompt: str, **kwargs: Any) -> Copy:
        captured["prompt"] = prompt
        return Copy(insight="x" * 30)

    monkeypatch.setattr(outreach.llm, "call_openai", fake_call_openai)
    lead = _lead(
        name="Chief River Nursery",
        signals=[
            _signal(
                type=SignalType.BREACH_DISCLOSED,
                source=SourceName.BREACHES,
                captured_at=_now(),
                payload={"agency": "me_ag", "reported_date": "2026-04-22"},
            )
        ],
    )
    generate(lead, NicheName.MSSP, 60.0)
    assert "the Maine Attorney General" in captured["prompt"]
    assert "me_ag" not in captured["prompt"]


def test_describe_signals_skips_missing_payload_fields() -> None:
    lead = _lead(
        signals=[_signal(type=SignalType.JOB_SECURITY, captured_at=_now(), payload={})]
    )
    out = _describe_signals(lead, now=_now())
    assert out == "- job_posted_security (today)"


def test_describe_signals_limits_and_orders() -> None:
    base = _now()
    sigs = []
    for i in range(10):
        sigs.append(
            _signal(
                type=SignalType.JOB_IT_SUPPORT,
                captured_at=base - timedelta(days=i),
                payload={"title": f"role-{i}"},
            )
        )
    lead = _lead(signals=sigs)
    out = _describe_signals(lead, limit=6, now=base)
    lines = out.splitlines()
    assert len(lines) == 6
    titles = [f"role-{i}" for i in range(6)]
    for expected, line in zip(titles, lines):
        assert expected in line


# --- generate() — single-sentence insight ----------------------------------


def test_generate_calls_openai_with_full_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_call_openai(prompt: str, **kwargs: Any) -> Copy:
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return Copy(insight="x" * 30)

    monkeypatch.setattr(outreach.llm, "call_openai", fake_call_openai)

    lead = _lead(
        name="Angel's Marketing and Advertising Agency",
        signals=[
            _signal(
                type=SignalType.JOB_SECURITY,
                captured_at=_now(),
                payload={"title": "Senior Security Engineer"},
            )
        ],
    )
    result = generate(lead, NicheName.MSSP, 72.0)
    assert isinstance(result, Copy)

    prompt = captured["prompt"]
    assert "Angel's Marketing and Advertising Agency" in prompt
    assert "legal_professional" in prompt
    assert "~80 employees" in prompt
    assert "managed security service provider" in prompt
    assert "72/100" in prompt
    assert "job_posted_security" in prompt
    assert "THIRD PERSON" in prompt

    assert captured["kwargs"]["response_model"] is Copy
    assert captured["kwargs"]["model"] == "gpt-4o-mini"


def test_generate_includes_value_prop_in_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_call_openai(prompt: str, **kwargs: Any) -> Copy:
        captured["prompt"] = prompt
        return Copy(insight="x" * 30)

    monkeypatch.setattr(outreach.llm, "call_openai", fake_call_openai)
    lead = _lead(signals=[_signal(type=SignalType.JOB_SECURITY, captured_at=_now())])
    lead.value_prop = "Family-owned auto dealership in Pennsylvania."

    generate(lead, NicheName.MSSP, 60.0)
    assert "Family-owned auto dealership in Pennsylvania." in captured["prompt"]


def test_generate_returns_copy_pydantic(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = Copy(insight="x" * 30)
    monkeypatch.setattr(outreach.llm, "call_openai", MagicMock(return_value=expected))
    lead = _lead(signals=[_signal(type=SignalType.JOB_SECURITY, captured_at=_now())])
    assert generate(lead, NicheName.IT_MSP, 50.0) is expected


def test_generate_per_niche_framing_differs(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []

    def capture(prompt: str, **kwargs: Any) -> Copy:
        prompts.append(prompt)
        return Copy(insight="x" * 30)

    monkeypatch.setattr(outreach.llm, "call_openai", capture)
    lead = _lead(signals=[_signal(type=SignalType.JOB_SECURITY, captured_at=_now())])

    for niche in NicheName:
        generate(lead, niche, 50.0)

    assert len({*prompts}) == 3
    assert "IT managed service provider" in prompts[0]
    assert "managed security service provider" in prompts[1]
    assert "cloud consultancy" in prompts[2]

"""Gemini free-tier throttle + 429 retry-delay parsing.

Free-tier gemini-2.5-flash-lite caps generate_content at ~20 req/min;
without pacing a scaled fetch bursts straight through it. These guard
the proactive throttle and the server-hint parser that make the
rate-limited path actually recover instead of dropping the lead.
"""

from __future__ import annotations

import cfo_pipeline.llm as llm


def test_retry_delay_parsed_from_429_message():
    class _Err(Exception):
        message = (
            "429 RESOURCE_EXHAUSTED. Quota exceeded ... Please retry in "
            "44.03324s. (see docs)"
        )

    assert llm._genai_retry_delay(_Err()) == 44.03324


def test_retry_delay_none_when_absent():
    class _Err(Exception):
        message = "503 UNAVAILABLE. high demand, try later"

    assert llm._genai_retry_delay(_Err()) is None
    # Plain string with no hint also yields None.
    assert llm._genai_retry_delay(Exception("boom")) is None


def test_throttle_noop_when_interval_zero(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(llm, "_GEMINI_MIN_INTERVAL_S", 0.0)
    llm._throttle_gemini()
    assert slept == []


def test_throttle_sleeps_to_maintain_interval(monkeypatch):
    slept: list[float] = []
    fake_now = [100.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: fake_now[0])
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(llm, "_GEMINI_MIN_INTERVAL_S", 4.5)
    monkeypatch.setattr(llm, "_gemini_quota_exhausted", False)
    # First call after a long idle — last_call far in the past, no sleep.
    monkeypatch.setattr(llm, "_gemini_last_call_ts", 0.0)
    llm._throttle_gemini()
    assert slept == []
    # Immediate second call — should sleep the full interval.
    llm._throttle_gemini()
    assert slept and abs(slept[0] - 4.5) < 1e-9


def test_quota_latch_short_circuits(monkeypatch):
    """Once latched, every Gemini call raises immediately — no API hit,
    no sleep — so a quota wall stops enrichment in seconds."""
    slept: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(llm, "_gemini_quota_exhausted", True)
    import pytest

    with pytest.raises(llm.GeminiQuotaExhausted):
        llm._throttle_gemini()
    assert slept == []  # short-circuit, never paced


def test_reset_quota_latch_clears_it():
    llm._gemini_quota_exhausted = True
    llm._reset_gemini_quota_latch()
    assert llm._gemini_quota_exhausted is False

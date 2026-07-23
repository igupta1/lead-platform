"""Nightly anomaly guard — flags silent source failures and sharp niche drops."""

from __future__ import annotations

from leadgen.monitoring import detect_anomalies


def test_first_run_has_nothing_to_compare():
    assert detect_anomalies(None, {"sources": {"jobs": 100}, "niches": {"cfo": 50}}) == []


def test_source_dropping_to_zero_is_flagged():
    prev = {"sources": {"jobs": 6000, "breaches": 100}, "niches": {}}
    curr = {"sources": {"jobs": 0, "breaches": 100}, "niches": {}}
    msgs = detect_anomalies(prev, curr)
    assert len(msgs) == 1
    assert "jobs" in msgs[0] and "0 candidates" in msgs[0]


def test_source_already_zero_is_not_flagged():
    # 0 -> 0 is not an anomaly (edgar_form_c can legitimately have no filings).
    prev = {"sources": {"edgar_form_c": 0}, "niches": {}}
    curr = {"sources": {"edgar_form_c": 0}, "niches": {}}
    assert detect_anomalies(prev, curr) == []


def test_sharp_niche_drop_is_flagged_but_small_change_is_not():
    prev = {"sources": {}, "niches": {"cfo": 400, "msp": 90}}
    curr = {"sources": {}, "niches": {"cfo": 150, "msp": 88}}  # cfo -62%, msp -2%
    msgs = detect_anomalies(prev, curr)
    assert len(msgs) == 1
    assert "cfo" in msgs[0]


def test_growth_is_never_flagged():
    prev = {"sources": {"jobs": 100}, "niches": {"cfo": 100}}
    curr = {"sources": {"jobs": 120}, "niches": {"cfo": 130}}
    assert detect_anomalies(prev, curr) == []

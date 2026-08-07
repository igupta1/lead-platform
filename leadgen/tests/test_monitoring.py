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


# --- Enrichment health ------------------------------------------------------
#
# A latched Gemini quota exits 0 and used to INFLATE niche counts (unenriched
# leads still projected), so neither the workflow's failure alert nor the
# count diff could see it. It gets its own check.


def test_quota_exhaustion_is_flagged():
    msgs = detect_anomalies(
        {"sources": {"jobs": 10}, "niches": {"cfo": 5}},
        {"sources": {"jobs": 10}, "niches": {"cfo": 5}, "gemini_quota_exhausted": True},
    )
    assert any("quota exhausted" in m for m in msgs)


def test_quota_exhaustion_is_flagged_on_the_first_ever_run():
    # no previous run to diff against, but this still has to alert
    msgs = detect_anomalies(None, {"gemini_quota_exhausted": True})
    assert any("quota exhausted" in m for m in msgs)


def test_healthy_run_is_silent():
    stats = {"sources": {"jobs": 10}, "niches": {"cfo": 5}}
    assert detect_anomalies(stats, stats) == []
    assert detect_anomalies(None, stats) == []


# --- insight floor (Layer 3: catastrophic-only) -----------------------------
#
# The floor exists for a systematic failure (dead/rotated API key, drained
# balance, provider outage, the field dropped from the record) — NOT for
# quality. These tests pin both halves: it fires at ~zero, and it stays silent
# across the whole range of normal-to-mediocre fill.


def test_empty_insight_is_flagged():
    msgs = detect_anomalies(
        None, {"insight_fill": {"cfo": {"total": 500, "filled": 0}}}
    )
    assert len(msgs) == 1
    assert "cfo" in msgs[0] and "0/500" in msgs[0]


def test_healthy_insight_fill_is_silent():
    # Observed production fill is 97-100%.
    assert detect_anomalies(
        None, {"insight_fill": {"cfo": {"total": 500, "filled": 490}}}
    ) == []


def test_model_variance_never_trips_the_floor():
    # Well below normal but nowhere near a systematic failure — must stay quiet,
    # or the alert gets muted and a real outage is missed.
    for filled in (500, 480, 400, 300, 200, 105):
        assert detect_anomalies(
            None, {"insight_fill": {"cfo": {"total": 500, "filled": filled}}}
        ) == [], f"floor tripped at {filled}/500"


def test_floor_fires_only_below_twenty_percent():
    assert detect_anomalies(
        None, {"insight_fill": {"cfo": {"total": 100, "filled": 20}}}
    ) == []
    msgs = detect_anomalies(
        None, {"insight_fill": {"cfo": {"total": 100, "filled": 19}}}
    )
    assert len(msgs) == 1


def test_empty_niche_is_not_a_fill_failure():
    # 0/0 is the count guard's business, not the floor's — no division by zero,
    # and no alert for a niche that simply published nothing.
    assert detect_anomalies(
        None, {"insight_fill": {"cfo": {"total": 0, "filled": 0}}}
    ) == []


def test_each_broken_niche_is_reported_separately():
    # A single broken niche must be distinguishable from a whole-feed failure.
    msgs = detect_anomalies(
        None,
        {
            "insight_fill": {
                "cfo": {"total": 500, "filled": 0},
                "accounting": {"total": 1200, "filled": 1180},
                "msp": {"total": 200, "filled": 2},
            }
        },
    )
    assert len(msgs) == 2
    assert any("cfo" in m for m in msgs) and any("msp" in m for m in msgs)
    assert not any("accounting" in m for m in msgs)


def test_missing_insight_fill_key_is_silent():
    # An older run_stats row (or a caller that doesn't pass it) must not alert.
    assert detect_anomalies(None, {"niches": {"cfo": 5}}) == []


def test_insight_floor_composes_with_the_count_diff():
    prev = {"sources": {"jobs": 6000}, "niches": {"cfo": 500}}
    curr = {
        "sources": {"jobs": 0},
        "niches": {"cfo": 500},
        "insight_fill": {"cfo": {"total": 500, "filled": 0}},
    }
    msgs = detect_anomalies(prev, curr)
    assert any("jobs" in m for m in msgs)
    assert any("insight" in m for m in msgs)

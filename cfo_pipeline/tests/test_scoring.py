"""Scoring tests for the tiered model.

The guarantee: category is a hard gate. Any fractional-CFO lead
outranks any finance-lead lead, which outranks any funding-only lead —
regardless of how many signals stack up. Within a tier, recency /
compounding signals / size fit reorder leads but never cross a band.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from cfo_pipeline import scoring
from cfo_pipeline.models import Lead, Signal, SignalType, SourceName


_NOW = datetime(2026, 5, 16, 12, 0, 0)

_HIRING = {
    SignalType.JOB_POSTED_FRACTIONAL_CFO,
    SignalType.JOB_POSTED_FINANCE_LEAD,
}

# Band edges (kept in sync with scoring.py).
_FRACTIONAL_FLOOR, _FRACTIONAL_CEIL = 80.0, 100.0
_FINANCE_FLOOR, _FINANCE_CEIL = 50.0, 74.0
_FUNDING_FLOOR, _FUNDING_CEIL = 5.0, 29.0


def _lead(*signals: Signal, **kwargs) -> Lead:
    return Lead(
        name=kwargs.get("name", "Test Co"),
        name_key=kwargs.get("name_key", "test co"),
        headcount=kwargs.get("headcount"),
        signals=list(signals),
    )


def _sig(t: SignalType, *, days_ago: int = 0, payload: dict | None = None) -> Signal:
    src = SourceName.JOBS if t in _HIRING else SourceName.FUNDING
    return Signal(
        type=t,
        source=src,
        captured_at=_NOW - timedelta(days=days_ago),
        payload=payload or {},
    )


def _score(*signals: Signal, **kwargs) -> float:
    return scoring.score(_lead(*signals, **kwargs), now=_NOW)


# --- Band containment ------------------------------------------------------


def test_fractional_lands_in_its_band():
    assert _FRACTIONAL_FLOOR <= _score(
        _sig(SignalType.JOB_POSTED_FRACTIONAL_CFO)
    ) <= _FRACTIONAL_CEIL


def test_finance_lands_in_its_band():
    assert _FINANCE_FLOOR <= _score(
        _sig(SignalType.JOB_POSTED_FINANCE_LEAD)
    ) <= _FINANCE_CEIL


def test_funding_lands_in_its_band():
    assert _FUNDING_FLOOR <= _score(
        _sig(SignalType.FUNDING_RAISED)
    ) <= _FUNDING_CEIL


# --- Strict tier ordering (the whole point) --------------------------------


def test_fractional_outranks_finance():
    assert _score(_sig(SignalType.JOB_POSTED_FRACTIONAL_CFO)) > _score(
        _sig(SignalType.JOB_POSTED_FINANCE_LEAD)
    )


def test_finance_outranks_funding():
    assert _score(_sig(SignalType.JOB_POSTED_FINANCE_LEAD)) > _score(
        _sig(SignalType.FUNDING_RAISED)
    )


def test_stacked_finance_never_outranks_single_fractional():
    """The bug the tiered model fixes: five below-CFO finance postings
    used to sum past 100 and bury a single, hotter fractional posting.
    Now the fractional lead wins even when it's nearly stale and the
    finance lead is fresh, multi-posting, and perfectly sized."""
    five_finance = _lead(
        *[
            _sig(
                SignalType.JOB_POSTED_FINANCE_LEAD,
                payload={"title": f"Controller {i}"},
            )
            for i in range(5)
        ],
        _sig(SignalType.FUNDING_RAISED),  # bullseye too
        headcount=25,  # size fit
    )
    single_stale_fractional = _lead(
        _sig(SignalType.JOB_POSTED_FRACTIONAL_CFO, days_ago=59)
    )
    assert scoring.score(single_stale_fractional, now=_NOW) > scoring.score(
        five_finance, now=_NOW
    )


def test_bullseye_lifts_within_band_but_stays_below_fractional():
    hiring_only = _score(_sig(SignalType.JOB_POSTED_FINANCE_LEAD))
    bullseye = _score(
        _sig(SignalType.JOB_POSTED_FINANCE_LEAD),
        _sig(SignalType.FUNDING_RAISED, days_ago=10),
    )
    assert bullseye > hiring_only
    assert bullseye <= _FINANCE_CEIL


# --- Within-tier recency ---------------------------------------------------


def test_fresh_outranks_stale_same_tier():
    fresh = _score(_sig(SignalType.JOB_POSTED_FINANCE_LEAD, days_ago=0))
    stale = _score(_sig(SignalType.JOB_POSTED_FINANCE_LEAD, days_ago=30))
    assert fresh > stale
    # A stale finance lead falls to the band floor but stays in-band.
    assert stale == _FINANCE_FLOOR


def test_fractional_recency_uses_60_day_window():
    """Fractional postings decay their recency bonus over 60 days, not
    30 — a 45-day-old fractional posting still carries a bonus."""
    frac_45d = _score(_sig(SignalType.JOB_POSTED_FRACTIONAL_CFO, days_ago=45))
    assert frac_45d > _FRACTIONAL_FLOOR  # still above the bare base


def test_recency_uses_event_date_not_capture_date():
    """A posting captured today but POSTED 30 days ago must score like a
    stale lead, not a fresh one."""
    date_posted = (_NOW - timedelta(days=30)).strftime("%Y-%m-%d")
    stale_event = _score(
        _sig(
            SignalType.JOB_POSTED_FINANCE_LEAD,
            days_ago=0,  # captured just now...
            payload={"title": "Controller", "date_posted": date_posted},
        )
    )
    fresh = _score(_sig(SignalType.JOB_POSTED_FINANCE_LEAD))
    assert stale_event < fresh
    assert stale_event == _FINANCE_FLOOR  # 30d event -> no recency bonus


def test_recency_uses_filed_on_for_funding():
    filed_on = (_NOW - timedelta(days=30)).strftime("%Y-%m-%d")
    stale = _score(
        _sig(
            SignalType.FUNDING_RAISED,
            days_ago=0,
            payload={"filing_type": "Form D", "filed_on": filed_on},
        )
    )
    assert stale == _FUNDING_FLOOR


def test_falls_back_to_captured_at_without_payload_date():
    aged = _score(
        _sig(
            SignalType.JOB_POSTED_FINANCE_LEAD,
            days_ago=30,
            payload={"title": "Controller", "date_posted": "last week"},
        )
    )
    assert aged == _FINANCE_FLOOR


# --- Within-tier compounding -----------------------------------------------


def test_multiple_distinct_roles_lift_score():
    one = _score(
        _sig(SignalType.JOB_POSTED_FINANCE_LEAD, payload={"title": "Controller"})
    )
    three = _score(
        _sig(SignalType.JOB_POSTED_FINANCE_LEAD, payload={"title": "Controller"}),
        _sig(SignalType.JOB_POSTED_FINANCE_LEAD, payload={"title": "VP Finance"}),
        _sig(SignalType.JOB_POSTED_FINANCE_LEAD, payload={"title": "FP&A Manager"}),
    )
    assert three > one


def test_reworded_same_role_does_not_double_count():
    """'VP Finance' and 'VP of Finance' are the same opening reposted —
    they must not earn the multiple-postings bonus."""
    reworded = _score(
        _sig(SignalType.JOB_POSTED_FINANCE_LEAD, payload={"title": "VP Finance"}),
        _sig(SignalType.JOB_POSTED_FINANCE_LEAD, payload={"title": "VP of Finance"}),
    )
    single = _score(
        _sig(SignalType.JOB_POSTED_FINANCE_LEAD, payload={"title": "VP Finance"})
    )
    assert reworded == single


def test_size_fit_lifts_score():
    with_fit = _score(
        _sig(SignalType.JOB_POSTED_FINANCE_LEAD), headcount=25
    )
    without_fit = _score(
        _sig(SignalType.JOB_POSTED_FINANCE_LEAD), headcount=300
    )
    assert with_fit > without_fit


# --- Degenerate cases ------------------------------------------------------


def test_non_scoring_markers_contribute_zero():
    lead = _lead(
        _sig(SignalType.ENRICHMENT_RUN),
        _sig(SignalType.LOCATION_CAPTURED),
    )
    assert scoring.score(lead, now=_NOW) == 0.0


def test_zero_signals_returns_zero():
    assert scoring.score(_lead(), now=_NOW) == 0.0

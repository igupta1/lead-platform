"""Scoring tests — weight ordering, decay, and combined-signal lift."""

from __future__ import annotations

from datetime import datetime, timedelta

from cfo_pipeline import scoring
from cfo_pipeline.models import Lead, Signal, SignalType, SourceName


_NOW = datetime(2026, 5, 16, 12, 0, 0)


def _lead(*signals: Signal, **kwargs) -> Lead:
    return Lead(
        name=kwargs.get("name", "Test Co"),
        name_key=kwargs.get("name_key", "test co"),
        signals=list(signals),
    )


def _sig(t: SignalType, *, days_ago: int = 0, payload: dict | None = None) -> Signal:
    src = (
        SourceName.JOBS if t == SignalType.JOB_POSTED_FINANCE_LEAD
        else SourceName.FUNDING
    )
    return Signal(
        type=t,
        source=src,
        captured_at=_NOW - timedelta(days=days_ago),
        payload=payload or {},
    )


def test_hiring_signal_outweighs_funding():
    """The gate signal is hiring. A fresh hiring signal alone must
    score higher than a fresh funding signal alone."""
    hiring = scoring.score(
        _lead(_sig(SignalType.JOB_POSTED_FINANCE_LEAD)), now=_NOW
    )
    funding = scoring.score(
        _lead(_sig(SignalType.FUNDING_RAISED)), now=_NOW
    )
    assert hiring > funding
    assert hiring == 60.0  # SIGNAL_WEIGHTS exact value, fresh -> decay=1
    assert funding == 25.0


def test_combined_signals_lift_score():
    """Hiring + funding should outscore hiring alone — both contribute."""
    hiring_only = scoring.score(
        _lead(_sig(SignalType.JOB_POSTED_FINANCE_LEAD)), now=_NOW
    )
    combined = scoring.score(
        _lead(
            _sig(SignalType.JOB_POSTED_FINANCE_LEAD),
            _sig(SignalType.FUNDING_RAISED, days_ago=15),
        ),
        now=_NOW,
    )
    assert combined > hiring_only


def test_score_clamped_to_100():
    """Five fresh hiring signals would total 300 weight; the score
    must clamp to 100."""
    sigs = [_sig(SignalType.JOB_POSTED_FINANCE_LEAD) for _ in range(5)]
    assert scoring.score(_lead(*sigs), now=_NOW) == 100.0


def test_decay_drops_score_for_old_signal():
    """A 30-day-old hiring signal should score ~half the fresh value."""
    fresh = scoring.score(
        _lead(_sig(SignalType.JOB_POSTED_FINANCE_LEAD)), now=_NOW
    )
    aged = scoring.score(
        _lead(_sig(SignalType.JOB_POSTED_FINANCE_LEAD, days_ago=30)), now=_NOW
    )
    assert aged == fresh * 0.5


def test_unknown_signal_type_contributes_zero():
    """Markers like ENRICHMENT_RUN / LOCATION_CAPTURED carry weight 0 —
    they shouldn't pull a lead into the visible page."""
    lead = _lead(
        _sig(SignalType.ENRICHMENT_RUN),
        _sig(SignalType.LOCATION_CAPTURED),
    )
    assert scoring.score(lead, now=_NOW) == 0.0


def test_zero_signals_returns_zero():
    assert scoring.score(_lead(), now=_NOW) == 0.0

"""Scoring tests — weight ordering, decay, and combined-signal lift."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

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


def _expected_decayed(weight: float, event_date_str: str) -> float:
    """Expected score for a single signal whose payload date parses to
    midnight of event_date_str (date-only payloads have no time)."""
    event_dt = datetime.strptime(event_date_str, "%Y-%m-%d")
    days = (_NOW - event_dt).total_seconds() / 86400.0
    return weight * 0.5 ** (days / scoring.HALF_LIFE_DAYS)


def test_decay_uses_event_date_not_capture_date():
    """A 30-day-old posting scraped today must score like a 30-day-old
    signal, not a fresh one — otherwise stale postings float to the
    top of the page every night they're re-scraped."""
    date_posted = (_NOW - timedelta(days=30)).strftime("%Y-%m-%d")
    stale_event = scoring.score(
        _lead(
            _sig(
                SignalType.JOB_POSTED_FINANCE_LEAD,
                days_ago=0,  # captured just now...
                payload={"title": "Controller", "date_posted": date_posted},
            )
        ),
        now=_NOW,
    )
    fresh = scoring.score(
        _lead(_sig(SignalType.JOB_POSTED_FINANCE_LEAD)), now=_NOW
    )
    assert stale_event == pytest.approx(_expected_decayed(60.0, date_posted))
    # Roughly half the fresh score — definitively not fresh.
    assert stale_event < fresh * 0.52


def test_decay_uses_filed_on_for_funding():
    filed_on = (_NOW - timedelta(days=30)).strftime("%Y-%m-%d")
    stale = scoring.score(
        _lead(
            _sig(
                SignalType.FUNDING_RAISED,
                days_ago=0,
                payload={"filing_type": "Form D", "filed_on": filed_on},
            )
        ),
        now=_NOW,
    )
    assert stale == pytest.approx(_expected_decayed(25.0, filed_on))


def test_decay_falls_back_to_captured_at_without_payload_date():
    """Empty payload (or unparseable date) keeps the old captured_at
    behavior — no signal silently becomes immortal."""
    aged = scoring.score(
        _lead(
            _sig(
                SignalType.JOB_POSTED_FINANCE_LEAD,
                days_ago=30,
                payload={"title": "Controller", "date_posted": "last week"},
            )
        ),
        now=_NOW,
    )
    fresh = scoring.score(
        _lead(_sig(SignalType.JOB_POSTED_FINANCE_LEAD)), now=_NOW
    )
    assert aged == fresh * 0.5


def test_fractional_cfo_signal_outranks_hiring_gate():
    """A company posting a Fractional / Interim CFO role is literally
    in-market — it must outrank the below-CFO hiring gate signal."""
    fractional = scoring.score(
        _lead(_sig(SignalType.JOB_POSTED_FRACTIONAL_CFO)), now=_NOW
    )
    hiring = scoring.score(
        _lead(_sig(SignalType.JOB_POSTED_FINANCE_LEAD)), now=_NOW
    )
    assert fractional > hiring
    assert fractional == 80.0

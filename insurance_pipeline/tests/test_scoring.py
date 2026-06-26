"""Scoring tests for the insurance pipeline."""

from __future__ import annotations

from datetime import datetime, timedelta  # noqa: F401 — timedelta used below
from typing import Any

from insurance_pipeline.models import Lead, Signal, SignalType, SourceName
from insurance_pipeline.scoring import SIGNAL_WEIGHTS, score


def _now() -> datetime:
    return datetime(2026, 5, 15, 12, 0, 0)


def _signal(
    *,
    type: SignalType,
    captured_at: datetime,
    source: SourceName = SourceName.FMCSA,
    payload: dict[str, Any] | None = None,
) -> Signal:
    return Signal(type=type, source=source, captured_at=captured_at, payload=payload or {})


def _lead(*signals: Signal) -> Lead:
    return Lead(name="Test Co", name_key="test", signals=list(signals))


def test_blank_lead_scores_zero() -> None:
    assert score(_lead(), now=_now()) == 0.0


def test_single_fresh_signal_full_weight() -> None:
    lead = _lead(_signal(type=SignalType.NEW_BUSINESS_FILED, captured_at=_now()))
    assert score(lead, now=_now()) == 45.0


def _fmcsa_signal(*, power_units: int = 0, drivers: int = 0, issue_offset_days: int = 0):
    issue_dt = _now() - timedelta(days=issue_offset_days)
    return _signal(
        type=SignalType.NEW_MOTOR_CARRIER_AUTHORITY,
        captured_at=_now(),
        payload={
            "fleet_size_power_units": power_units,
            "drivers": drivers,
            "issue_date": issue_dt.date().isoformat(),
        },
    )


def test_fmcsa_fresh_owner_operator_gets_recency_bonus() -> None:
    # 1 truck, issued today → 21.6 + 20 (fresh ≤7d) = 41.6
    lead = _lead(_fmcsa_signal(power_units=1, drivers=1, issue_offset_days=0))
    actual = score(lead, now=_now())
    assert 41 <= actual <= 42, f"expected ~41.6, got {actual}"


def test_fmcsa_old_owner_operator_loses_bonus() -> None:
    # 1 truck, issued 50 days ago → 21.6 + 0 (no bonus) = 21.6
    lead = _lead(_fmcsa_signal(power_units=1, drivers=1, issue_offset_days=50))
    actual = score(lead, now=_now())
    assert 21 <= actual <= 22, f"expected ~21.6, got {actual}"


def test_fmcsa_uses_drivers_when_power_units_is_zero() -> None:
    # power_units=0 (unreported) but drivers=5 → size=5, fresh → 28 + 20 = 48
    lead = _lead(_fmcsa_signal(power_units=0, drivers=5, issue_offset_days=0))
    actual = score(lead, now=_now())
    assert 47 <= actual <= 49


def test_fmcsa_fleet_saturates_at_100() -> None:
    # 100-truck fleet, fresh → 60 (cap on size) + 20 = 80
    # 100-truck fleet, fresh + drivers also high → still 80 (size cap)
    lead = _lead(_fmcsa_signal(power_units=100, drivers=200, issue_offset_days=0))
    actual = score(lead, now=_now())
    assert 79 <= actual <= 81


def test_fmcsa_unknown_size_unknown_age_falls_back() -> None:
    # No payload data → size=1 (max(0,0,1)) + 9999d age (no bonus) = 21.6
    lead = _lead(_signal(
        type=SignalType.NEW_MOTOR_CARRIER_AUTHORITY,
        captured_at=_now(),
        payload={},
    ))
    actual = score(lead, now=_now())
    assert 21 <= actual <= 22


def test_federal_contract_scales_by_award_amount() -> None:
    """Federal contracts → award-size-aware weight + recency bonus."""
    today_iso = _now().date().isoformat()
    # $25K, fresh → 25 + 2.5 + 20 = 47.5
    lead = _lead(_signal(
        type=SignalType.FUNDING_RAISED,
        captured_at=_now(),
        payload={"filing_type": "Federal contract", "amount_usd": 25_000, "filed_on": today_iso},
    ))
    assert 47 <= score(lead, now=_now()) <= 48

    # $250K, fresh → 50 + 20 = 70
    lead = _lead(_signal(
        type=SignalType.FUNDING_RAISED,
        captured_at=_now(),
        payload={"filing_type": "Federal contract", "amount_usd": 250_000, "filed_on": today_iso},
    ))
    assert 69 <= score(lead, now=_now()) <= 71

    # $500K, fresh → 60 (cap) + 20 = 80
    lead = _lead(_signal(
        type=SignalType.FUNDING_RAISED,
        captured_at=_now(),
        payload={"filing_type": "Federal contract", "amount_usd": 500_000, "filed_on": today_iso},
    ))
    assert 79 <= score(lead, now=_now()) <= 81


def test_federal_contract_recency_bonus_decays() -> None:
    """Old contracts lose the recency bonus."""
    old_iso = (_now() - timedelta(days=50)).date().isoformat()
    lead = _lead(_signal(
        type=SignalType.FUNDING_RAISED,
        captured_at=_now(),
        payload={"filing_type": "Federal contract", "amount_usd": 250_000, "filed_on": old_iso},
    ))
    # $250K, 50d old → 50 + 0 = 50
    assert 49 <= score(lead, now=_now()) <= 51


def test_form_d_funding_uses_demoted_flat_weight() -> None:
    """Issue 4 demoted FUNDING_RAISED 25→8. Form D / RSS funding
    signals (no amount) fall through to the flat weight; the buyer for
    Series A D&O is a specialist broker, not an independent agency."""
    lead = _lead(_signal(
        type=SignalType.FUNDING_RAISED,
        captured_at=_now(),
        payload={"filing_type": "Form D"},
    ))
    assert score(lead, now=_now()) == 8.0


def test_funding_signal_without_payload_uses_flat() -> None:
    lead = _lead(_signal(
        type=SignalType.FUNDING_RAISED,
        captured_at=_now(),
        payload={"feed_title": "Acme raises $20M Series A"},
    ))
    assert score(lead, now=_now()) == 8.0


def test_recency_decay_halves_at_one_half_life() -> None:
    fresh = _lead(_signal(type=SignalType.NEW_BUSINESS_FILED, captured_at=_now()))
    stale = _lead(
        _signal(
            type=SignalType.NEW_BUSINESS_FILED,
            captured_at=_now() - timedelta(days=30),
        )
    )
    f = score(fresh, now=_now())
    s = score(stale, now=_now())
    assert abs(s - f / 2) < 0.01 * f


def test_score_clamps_to_100() -> None:
    # All weighted signal types at once → sums past 100, must clamp.
    lead = _lead(*[
        _signal(type=t, captured_at=_now())
        for t in SIGNAL_WEIGHTS
    ])
    assert sum(SIGNAL_WEIGHTS.values()) > 100, (
        "sanity: weights should sum past 100 so the clamp matters"
    )
    assert score(lead, now=_now()) == 100.0


def test_non_scoring_signals_ignored() -> None:
    lead = _lead(
        _signal(
            type=SignalType.LOCATION_CAPTURED,
            captured_at=_now(),
            source=SourceName.COMPUTED,
            payload={"city": "Miami", "state": "FL"},
        ),
        _signal(
            type=SignalType.ENRICHMENT_RUN,
            captured_at=_now(),
            source=SourceName.COMPUTED,
        ),
    )
    assert score(lead, now=_now()) == 0.0

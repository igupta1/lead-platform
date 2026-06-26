from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from msp_pipeline import db
from msp_pipeline.models import (
    Lead,
    LeadCandidate,
    NicheName,
    Signal,
    SignalType,
    SourceName,
)
from msp_pipeline.scoring import score


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


def _lead(*signals: Signal) -> Lead:
    return Lead(name="Test Co", name_key="test", signals=list(signals))


def test_score_returns_three_niches() -> None:
    result = score(_lead(), now=_now())
    assert set(result.keys()) == set(NicheName)
    for v in result.values():
        assert v is None or 0.0 <= v <= 100.0


def test_score_none_for_blank_lead() -> None:
    # A lead with no qualifying signal scores None (not 0.0) in every niche,
    # so it's dropped from every dashboard rather than sorted to the bottom.
    result = score(_lead(), now=_now())
    assert result == {n: None for n in NicheName}


def test_single_fresh_signal_scores_only_its_niche() -> None:
    # A breach qualifies MSSP only — it must NOT leak into IT MSP or Cloud.
    lead = _lead(_signal(type=SignalType.BREACH_DISCLOSED, captured_at=_now()))
    result = score(lead, now=_now())
    assert result[NicheName.MSSP] == 45.0
    assert result[NicheName.IT_MSP] is None
    assert result[NicheName.CLOUD] is None


def test_recency_decay_halves_at_one_half_life() -> None:
    fresh_lead = _lead(_signal(type=SignalType.JOB_SECURITY, captured_at=_now()))
    stale_lead = _lead(
        _signal(type=SignalType.JOB_SECURITY, captured_at=_now() - timedelta(days=30))
    )
    fresh = score(fresh_lead, now=_now())[NicheName.MSSP]
    stale = score(stale_lead, now=_now())[NicheName.MSSP]
    assert abs(stale - fresh / 2) < 0.01 * fresh


def test_recency_decay_floors_at_zero_for_future_dates() -> None:
    future = _now() + timedelta(days=10)
    lead = _lead(_signal(type=SignalType.BREACH_DISCLOSED, captured_at=future))
    result = score(lead, now=_now())
    assert result[NicheName.MSSP] == 45.0


def test_score_clamps_to_100() -> None:
    now = _now()
    # IT MSP: 40 + 35 + 35 = 110 -> clamps.
    it_lead = _lead(
        _signal(type=SignalType.JOB_IT_LEADERSHIP, captured_at=now),
        _signal(type=SignalType.JOB_IT_SUPPORT, captured_at=now),
        _signal(type=SignalType.FUNDING_RAISED, captured_at=now),
    )
    assert score(it_lead, now=now)[NicheName.IT_MSP] == 100.0
    # MSSP: 45 + 40 + 30 (CISO exec) = 115 -> clamps.
    mssp_lead = _lead(
        _signal(type=SignalType.BREACH_DISCLOSED, captured_at=now),
        _signal(type=SignalType.JOB_SECURITY, captured_at=now),
        _signal(type=SignalType.EXEC_HIRED, captured_at=now, payload={"title": "CISO"}),
    )
    assert score(mssp_lead, now=now)[NicheName.MSSP] == 100.0
    # Cloud: 40 * 3 = 120 -> clamps.
    cloud_lead = _lead(
        _signal(type=SignalType.JOB_CLOUD_DEVOPS, captured_at=now, payload={"title": "a"}),
        _signal(type=SignalType.JOB_CLOUD_DEVOPS, captured_at=now, payload={"title": "b"}),
        _signal(type=SignalType.JOB_CLOUD_DEVOPS, captured_at=now, payload={"title": "c"}),
    )
    assert score(cloud_lead, now=now)[NicheName.CLOUD] == 100.0


def test_exec_hire_routes_to_niche_by_title() -> None:
    now = _now()
    ciso = _lead(_signal(type=SignalType.EXEC_HIRED, captured_at=now, payload={"title": "CISO"}))
    cio = _lead(_signal(type=SignalType.EXEC_HIRED, captured_at=now, payload={"title": "VP of IT"}))
    devops = _lead(
        _signal(type=SignalType.EXEC_HIRED, captured_at=now, payload={"title": "VP of DevOps"})
    )
    assert score(ciso, now=now)[NicheName.MSSP] == 30.0
    assert score(ciso, now=now)[NicheName.IT_MSP] is None
    assert score(cio, now=now)[NicheName.IT_MSP] == 35.0
    assert score(cio, now=now)[NicheName.MSSP] is None
    assert score(devops, now=now)[NicheName.CLOUD] == 30.0
    assert score(devops, now=now)[NicheName.IT_MSP] is None


def test_breach_recency_decays_from_disclosure_date() -> None:
    # Recency uses the breach's reported_date, not capture time. A breach
    # reported one half-life ago scores half weight even if scraped today.
    disclosed = (_now() - timedelta(days=30)).strftime("%Y-%m-%d")
    lead = _lead(
        _signal(
            type=SignalType.BREACH_DISCLOSED,
            captured_at=_now(),
            payload={"agency": "ca_ag", "reported_date": disclosed},
        )
    )
    result = score(lead, now=_now())
    assert abs(result[NicheName.MSSP] - 22.5) < 0.5


def test_unknown_signal_types_ignored() -> None:
    lead = _lead(
        _signal(
            type=SignalType.LOCATION_CAPTURED,
            captured_at=_now(),
            source=SourceName.COMPUTED,
            payload={"city": "Boston", "state": "MA"},
        ),
        _signal(
            type=SignalType.ENRICHMENT_RUN,
            captured_at=_now(),
            source=SourceName.COMPUTED,
        ),
    )
    result = score(lead, now=_now())
    assert result == {n: None for n in NicheName}


def _candidate(name: str) -> LeadCandidate:
    return LeadCandidate(
        name=name,
        initial_signal=_signal(
            type=SignalType.JOB_SECURITY,
            captured_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ),
    )


def test_score_is_pure_no_db_writes(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "leads.db")
    inserted = db.upsert_lead(conn, _candidate("Pure Co"))
    assert inserted.id is not None

    score(inserted)

    refreshed = db.get_lead(conn, lead_id=inserted.id)
    assert refreshed is not None
    assert refreshed.it_msp_score is None
    assert refreshed.mssp_score is None
    assert refreshed.cloud_score is None

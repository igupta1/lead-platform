from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from msp_pipeline import apollo, daily_run, db, enrichment
from msp_pipeline.models import (
    Lead,
    LeadCandidate,
    NicheName,
    Signal,
    SignalType,
    SourceName,
)
from msp_pipeline.outreach import Copy


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _signal(
    type: SignalType,
    captured_at: datetime,
    *,
    source: SourceName = SourceName.JOBS,
    payload: dict[str, Any] | None = None,
) -> Signal:
    return Signal(
        type=type,
        source=source,
        captured_at=captured_at,
        payload=payload or {},
    )


def _candidate(
    name: str,
    sig_type: SignalType = SignalType.JOB_SECURITY,
    source: SourceName = SourceName.JOBS,
) -> LeadCandidate:
    return LeadCandidate(
        name=name,
        initial_signal=_signal(sig_type, _now(), source=source),
    )


def test_main_dry_run_makes_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "leads.db"
    out_path = tmp_path / "leads.json"

    fetch_mock = MagicMock(return_value=[_candidate("Test Co")])
    monkeypatch.setattr(daily_run.jobs, "fetch", fetch_mock)
    monkeypatch.setattr(daily_run.funding, "fetch", fetch_mock)
    monkeypatch.setattr(daily_run.breaches, "fetch", fetch_mock)

    enrich_mock = MagicMock()
    generate_mock = MagicMock()
    monkeypatch.setattr("msp_pipeline.enrichment.enrich", enrich_mock)
    monkeypatch.setattr("msp_pipeline.outreach.generate", generate_mock)

    rc = daily_run.main(
        [
            "--dry-run",
            "--db-path",
            str(db_path),
            "--output-path",
            str(out_path),
        ]
    )
    assert rc == 0
    assert not db_path.exists()
    assert not out_path.exists()
    enrich_mock.assert_not_called()
    generate_mock.assert_not_called()
    assert fetch_mock.call_count == 3


def test_main_end_to_end_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "leads.db"
    out_path = tmp_path / "leads.json"

    monkeypatch.setattr(
        daily_run.jobs,
        "fetch",
        MagicMock(return_value=[_candidate("Acme Co", SignalType.JOB_SECURITY)]),
    )
    monkeypatch.setattr(
        daily_run.funding,
        "fetch",
        MagicMock(
            return_value=[
                _candidate("Beta Inc", SignalType.FUNDING_RAISED, SourceName.FUNDING)
            ]
        ),
    )
    monkeypatch.setattr(
        daily_run.breaches,
        "fetch",
        MagicMock(
            return_value=[
                _candidate("Gamma LLC", SignalType.BREACH_DISCLOSED, SourceName.BREACHES)
            ]
        ),
    )

    monkeypatch.setattr(
        "msp_pipeline.enrichment.lookup_company",
        MagicMock(
            return_value=enrichment._Lookup(
                headcount=100, city="Boston", state="MA", country="US"
            )
        ),
    )
    monkeypatch.setattr(
        "msp_pipeline.enrichment.classify_industry",
        MagicMock(return_value=enrichment.Industry.FINTECH),
    )
    monkeypatch.setattr(
        "msp_pipeline.outreach.generate",
        MagicMock(return_value=Copy(insight="x" * 30)),
    )

    rc = daily_run.main(
        ["--db-path", str(db_path), "--output-path", str(out_path)]
    )
    assert rc == 0
    assert db_path.exists()
    assert out_path.exists()

    payload = json.loads(out_path.read_text())
    assert "generated_at" in payload
    assert set(payload["niches"].keys()) == {"it_msp", "mssp", "cloud"}

    # Each lead appears only in the niches its signal qualifies for:
    # Acme (security) -> MSSP, Beta (funding) -> IT MSP + Cloud,
    # Gamma (breach) -> MSSP.
    names_by_niche = {
        niche: {lead["name"] for lead in leads}
        for niche, leads in payload["niches"].items()
    }
    assert names_by_niche["it_msp"] == {"Beta Inc"}
    assert names_by_niche["mssp"] == {"Acme Co", "Gamma LLC"}
    assert names_by_niche["cloud"] == {"Beta Inc"}

    for leads in payload["niches"].values():
        for lead in leads:
            assert set(lead.keys()) >= {
                "name",
                "industry",
                "headcount",
                "country",
                "city",
                "state",
                "score",
                "insight",
                "signals",
            }
            assert lead["score"] is not None
            assert lead["country"] == "US"
            assert lead["city"] == "Boston"
            assert lead["state"] == "MA"


def test_per_source_failure_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "leads.db"
    out_path = tmp_path / "leads.json"

    monkeypatch.setattr(
        daily_run.jobs,
        "fetch",
        MagicMock(side_effect=RuntimeError("jobs down")),
    )
    monkeypatch.setattr(
        daily_run.funding,
        "fetch",
        MagicMock(
            return_value=[
                _candidate("Funding Co", SignalType.FUNDING_RAISED, SourceName.FUNDING)
            ]
        ),
    )
    monkeypatch.setattr(
        daily_run.breaches,
        "fetch",
        MagicMock(
            return_value=[
                _candidate("Breach Co", SignalType.BREACH_DISCLOSED, SourceName.BREACHES)
            ]
        ),
    )
    monkeypatch.setattr(
        "msp_pipeline.enrichment.lookup_company",
        MagicMock(
            return_value=enrichment._Lookup(
                headcount=80, city=None, state=None, country="US"
            )
        ),
    )
    monkeypatch.setattr(
        "msp_pipeline.enrichment.classify_industry",
        MagicMock(return_value=enrichment.Industry.OTHER),
    )
    monkeypatch.setattr(
        "msp_pipeline.outreach.generate",
        MagicMock(return_value=Copy(insight="x" * 30)),
    )

    rc = daily_run.main(
        ["--db-path", str(db_path), "--output-path", str(out_path)]
    )
    assert rc == 0
    payload = json.loads(out_path.read_text())
    # jobs source failed; funding (Funding Co) and breaches (Breach Co)
    # survived. Funding qualifies IT MSP; the breach qualifies MSSP only.
    assert {lead["name"] for lead in payload["niches"]["it_msp"]} == {"Funding Co"}
    assert {lead["name"] for lead in payload["niches"]["mssp"]} == {"Breach Co"}


def test_full_run_rescores_preexisting_untouched_leads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lead already in the DB with a stale (legacy) score must be re-scored
    on a full run even when it gets no new signal — otherwise it stays pinned
    to a niche its signals no longer fit. Here a breach-only lead carries a
    bogus it_msp_score; after the run it must be gone from IT MSP (None) but
    present in MSSP."""
    db_path = tmp_path / "leads.db"
    out_path = tmp_path / "leads.json"

    conn = db.init_db(db_path)
    lead = db.upsert_lead(
        conn,
        _candidate("Stale Breach Co", SignalType.BREACH_DISCLOSED, SourceName.BREACHES),
    )
    assert lead.id is not None
    db.update_lead(
        conn, lead.id,
        industry="other", headcount=80, country="US",
        it_msp_score=87.0,  # legacy leak: breach should never score IT MSP
        mssp_score=87.0,
    )
    conn.close()

    # No source returns anything, so the pre-existing lead is never "touched".
    empty = MagicMock(return_value=[])
    monkeypatch.setattr(daily_run.jobs, "fetch", empty)
    monkeypatch.setattr(daily_run.funding, "fetch", empty)
    monkeypatch.setattr(daily_run.breaches, "fetch", empty)
    monkeypatch.setattr(
        "msp_pipeline.outreach.generate",
        MagicMock(return_value=Copy(insight="x" * 30)),
    )

    rc = daily_run.main(["--db-path", str(db_path), "--output-path", str(out_path)])
    assert rc == 0
    payload = json.loads(out_path.read_text())
    assert "Stale Breach Co" not in {ld["name"] for ld in payload["niches"]["it_msp"]}
    assert "Stale Breach Co" in {ld["name"] for ld in payload["niches"]["mssp"]}


def test_copy_regen_threshold_gates_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(tmp_path / "leads.db")

    # All primary signals now score ≥35 fresh; use a stale (100-day-old)
    # signal so recency decay drops the score below the 20 threshold.
    stale_when = _now() - timedelta(days=100)
    cold = db.upsert_lead(conn, _candidate("Cold Co", SignalType.EXEC_HIRED))
    assert cold.id is not None
    # Replace the candidate's fresh signal with a stale one.
    conn.execute(
        "UPDATE leads SET signals = ? WHERE id = ?",
        (
            json.dumps([
                {
                    "type": SignalType.EXEC_HIRED.value,
                    "source": SourceName.JOBS.value,
                    "captured_at": stale_when.isoformat(),
                    "payload": {},
                }
            ]),
            cold.id,
        ),
    )
    conn.commit()
    db.update_lead(conn, cold.id, industry="other", headcount=80, country="US")

    hot = db.upsert_lead(
        conn, _candidate("Hot Co", SignalType.BREACH_DISCLOSED, SourceName.BREACHES)
    )
    assert hot.id is not None
    db.append_signal(conn, hot.id, _signal(SignalType.JOB_SECURITY, _now()))
    db.update_lead(conn, hot.id, industry="healthcare", headcount=80, country="US")

    generate_mock = MagicMock(
        return_value=Copy(insight="x" * 30)
    )
    monkeypatch.setattr("msp_pipeline.outreach.generate", generate_mock)

    rescored, copy_calls = daily_run._rescore_and_regen_copy(
        conn, [cold.id, hot.id], model="gpt-4o-mini"
    )
    assert len(rescored) == 2
    # Hot Co (breach + security) qualifies MSSP only and crosses there; its
    # signals don't fit IT MSP or Cloud. Cold Co's stale exec hire stays
    # below threshold in IT MSP. So exactly Hot Co gets copy generated.
    assert copy_calls >= 1
    called_names = {call.args[0].name for call in generate_mock.call_args_list}
    assert called_names == {"Hot Co"}

    generate_mock.reset_mock()
    rescored2, copy_calls2 = daily_run._rescore_and_regen_copy(
        conn, [cold.id, hot.id], model="gpt-4o-mini"
    )
    assert copy_calls2 == 0


def test_insight_has_raw_agency_code() -> None:
    f = daily_run._insight_has_raw_agency_code
    assert f("Disclosed a breach to the agency 'me_ag' recently.")
    assert f("Breach reported via CA_AG last month")  # case-insensitive
    assert not f("Disclosed a breach to the Maine Attorney General.")
    assert not f("")
    assert not f(None)


def test_regen_refreshes_insight_leaking_raw_agency_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored insight that still leaks a raw agency code is regenerated even
    when the niche score barely moved (so neither the threshold-crossing nor
    the delta rule would fire on its own)."""
    conn = db.init_db(tmp_path / "leads.db")
    lead = db.upsert_lead(
        conn,
        _candidate("Chief River Nursery", SignalType.BREACH_DISCLOSED, SourceName.BREACHES),
    )
    assert lead.id is not None
    reported = (_now() - timedelta(days=2)).strftime("%Y-%m-%d")
    conn.execute(
        "UPDATE leads SET signals = ? WHERE id = ?",
        (
            json.dumps([
                {
                    "type": SignalType.BREACH_DISCLOSED.value,
                    "source": SourceName.BREACHES.value,
                    "captured_at": _now().isoformat(),
                    "payload": {"agency": "me_ag", "reported_date": reported},
                }
            ]),
            lead.id,
        ),
    )
    conn.commit()
    # Pre-store a leaked insight and a matching score so only the stale-code
    # rule can trigger regeneration (recent breach scores ~45 in MSSP).
    db.update_lead(
        conn,
        lead.id,
        industry="other",
        headcount=80,
        country="US",
        mssp_score=45.0,
        mssp_insight="Disclosed a breach to the agency 'me_ag' recently.",
    )

    generate_mock = MagicMock(
        return_value=Copy(insight="Disclosed a breach to the Maine Attorney General.")
    )
    monkeypatch.setattr("msp_pipeline.outreach.generate", generate_mock)

    daily_run._rescore_and_regen_copy(conn, [lead.id], model="gpt-4o-mini")

    regen_niches = {
        call.args[1] for call in generate_mock.call_args_list if call.args[0].id == lead.id
    }
    assert NicheName.MSSP in regen_niches
    after = db.get_lead(conn, lead_id=lead.id)
    assert after is not None
    assert "me_ag" not in (after.mssp_insight or "")
    assert after.mssp_insight == "Disclosed a breach to the Maine Attorney General."


def test_json_output_shape(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "leads.db")

    a = db.upsert_lead(conn, _candidate("Alpha"))
    b = db.upsert_lead(conn, _candidate("Beta"))
    c = db.upsert_lead(conn, _candidate("Gamma"))
    assert a.id is not None and b.id is not None and c.id is not None

    db.update_lead(conn, a.id, it_msp_score=80.0)
    db.update_lead(conn, b.id, it_msp_score=30.0)

    output = daily_run._build_output(conn)
    assert "generated_at" in output
    assert set(output["niches"].keys()) == {"it_msp", "mssp", "cloud"}

    # Only leads with a non-NULL niche score appear. Alpha + Beta have an
    # it_msp_score; Gamma (NULL) is dropped. No lead has an mssp/cloud score.
    it_leads = output["niches"]["it_msp"]
    assert [lead["name"] for lead in it_leads] == ["Alpha", "Beta"]
    assert it_leads[0]["score"] == 80.0
    assert it_leads[1]["score"] == 30.0
    assert output["niches"]["mssp"] == []
    assert output["niches"]["cloud"] == []


def test_lead_to_json_signals_filtered_and_capped() -> None:
    base = _now()
    sigs: list[Signal] = []
    for i in range(10):
        sigs.append(
            _signal(
                SignalType.JOB_IT_SUPPORT,
                base - timedelta(days=i),
                payload={"title": f"role-{i}"},
            )
        )
    sigs.append(
        _signal(
            SignalType.LOCATION_CAPTURED,
            base,
            source=SourceName.COMPUTED,
            payload={"city": "Boston", "state": "MA"},
        )
    )
    sigs.append(
        _signal(
            SignalType.ENRICHMENT_RUN,
            base,
            source=SourceName.COMPUTED,
        )
    )

    lead = Lead(name="Test", name_key="test", signals=sigs)
    out = daily_run._lead_to_json(lead, NicheName.IT_MSP, now=base)
    assert len(out["signals"]) == 6
    types = {s["type"] for s in out["signals"]}
    assert "location_captured" not in types
    assert "enrichment_run" not in types


def test_lead_to_json_pulls_city_state_from_signal() -> None:
    base = _now()
    lead_with_loc = Lead(
        name="Loc Co",
        name_key="loc",
        signals=[
            _signal(
                SignalType.LOCATION_CAPTURED,
                base,
                source=SourceName.COMPUTED,
                payload={"city": "Austin", "state": "TX"},
            )
        ],
    )
    out = daily_run._lead_to_json(lead_with_loc, NicheName.IT_MSP, now=base)
    assert out["city"] == "Austin"
    assert out["state"] == "TX"

    lead_no_loc = Lead(name="Bare Co", name_key="bare", signals=[])
    out2 = daily_run._lead_to_json(lead_no_loc, NicheName.IT_MSP, now=base)
    assert out2["city"] is None
    assert out2["state"] is None


# --- --upload --------------------------------------------------------------


def _setup_minimal_pipeline_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch sources / enrichment / outreach so main() runs to the upload
    step without making any real network calls."""
    monkeypatch.setattr(
        daily_run.jobs,
        "fetch",
        MagicMock(return_value=[_candidate("Acme Co")]),
    )
    monkeypatch.setattr(
        daily_run.funding, "fetch", MagicMock(return_value=[])
    )
    monkeypatch.setattr(
        daily_run.breaches, "fetch", MagicMock(return_value=[])
    )
    monkeypatch.setattr(
        "msp_pipeline.enrichment.lookup_company",
        MagicMock(
            return_value=enrichment._Lookup(
                headcount=80, city=None, state=None, country="US"
            )
        ),
    )
    monkeypatch.setattr(
        "msp_pipeline.enrichment.classify_industry",
        MagicMock(return_value=enrichment.Industry.OTHER),
    )
    monkeypatch.setattr(
        "msp_pipeline.outreach.generate",
        MagicMock(return_value=Copy(insight="x" * 30)),
    )


def test_upload_called_when_flag_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_minimal_pipeline_mocks(monkeypatch)

    monkeypatch.setenv("LEADS_UPLOAD_URL", "https://example.com/api/upload-leads")
    monkeypatch.setenv("LEADS_UPLOAD_API_KEY", "secret-token")

    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json = MagicMock(return_value={"ok": True, "url": "..."})
    post_mock = MagicMock(return_value=fake_response)
    monkeypatch.setattr(daily_run.requests, "post", post_mock)

    rc = daily_run.main(
        [
            "--upload",
            "--db-path",
            str(tmp_path / "leads.db"),
            "--output-path",
            str(tmp_path / "leads.json"),
        ]
    )
    assert rc == 0
    post_mock.assert_called_once()
    call = post_mock.call_args
    assert call.args[0] == "https://example.com/api/upload-leads"
    assert call.kwargs["headers"] == {"Authorization": "Bearer secret-token"}
    assert "niches" in call.kwargs["json"]
    assert set(call.kwargs["json"]["niches"].keys()) == {"it_msp", "mssp", "cloud"}


def test_upload_skipped_without_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_minimal_pipeline_mocks(monkeypatch)

    post_mock = MagicMock()
    monkeypatch.setattr(daily_run.requests, "post", post_mock)

    rc = daily_run.main(
        [
            "--db-path",
            str(tmp_path / "leads.db"),
            "--output-path",
            str(tmp_path / "leads.json"),
        ]
    )
    assert rc == 0
    post_mock.assert_not_called()


def test_rescore_only_skips_fetch_and_enrich(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "leads.db"
    out_path = tmp_path / "leads.json"

    conn = db.init_db(db_path)
    lead = db.upsert_lead(conn, _candidate("Rescore Co"))
    assert lead.id is not None
    db.update_lead(conn, lead.id, industry="other", headcount=80, country="US")
    conn.close()

    fetch_mock = MagicMock()
    enrich_mock = MagicMock()
    generate_mock = MagicMock()
    monkeypatch.setattr(daily_run.jobs, "fetch", fetch_mock)
    monkeypatch.setattr(daily_run.funding, "fetch", fetch_mock)
    monkeypatch.setattr(daily_run.breaches, "fetch", fetch_mock)
    monkeypatch.setattr("msp_pipeline.enrichment.enrich", enrich_mock)
    monkeypatch.setattr("msp_pipeline.outreach.generate", generate_mock)

    rc = daily_run.main(
        [
            "--rescore-only",
            "--db-path",
            str(db_path),
            "--output-path",
            str(out_path),
        ]
    )
    assert rc == 0
    fetch_mock.assert_not_called()
    enrich_mock.assert_not_called()
    assert out_path.exists()


def test_rescore_only_dedups_existing_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "leads.db"
    out_path = tmp_path / "leads.json"

    conn = db.init_db(db_path)
    lead = db.upsert_lead(
        conn, _candidate("Spammed Co", SignalType.JOB_IT_SUPPORT)
    )
    assert lead.id is not None
    db.update_lead(conn, lead.id, industry="other", headcount=80, country="US")
    now = _now()
    raw_signals = [
        {
            "type": SignalType.JOB_IT_SUPPORT.value,
            "source": SourceName.JOBS.value,
            "captured_at": now.isoformat(),
            "payload": {"url": "https://example.com/dup"},
        }
    ] * 6
    conn.execute(
        "UPDATE leads SET signals = ? WHERE id = ?",
        (json.dumps(raw_signals), lead.id),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        "msp_pipeline.outreach.generate",
        MagicMock(return_value=Copy(insight="x" * 30)),
    )

    rc = daily_run.main(
        [
            "--rescore-only",
            "--db-path",
            str(db_path),
            "--output-path",
            str(out_path),
        ]
    )
    assert rc == 0

    conn2 = db.init_db(db_path)
    after = db.get_lead(conn2, lead_id=lead.id)
    assert after is not None
    job_sigs = [s for s in after.signals if s.type == SignalType.JOB_IT_SUPPORT]
    assert len(job_sigs) == 1


def test_rescore_clears_stale_copy_when_score_drops_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(tmp_path / "leads.db")
    # Use a stale signal so IT MSP recency-decayed score drops below 20.
    stale_when = _now() - timedelta(days=100)
    lead = db.upsert_lead(conn, _candidate("Falling Co", SignalType.EXEC_HIRED))
    assert lead.id is not None
    conn.execute(
        "UPDATE leads SET signals = ? WHERE id = ?",
        (
            json.dumps([
                {
                    "type": SignalType.EXEC_HIRED.value,
                    "source": SourceName.JOBS.value,
                    "captured_at": stale_when.isoformat(),
                    "payload": {},
                }
            ]),
            lead.id,
        ),
    )
    conn.commit()
    # Pre-seed: score above threshold + insight already populated.
    db.update_lead(
        conn,
        lead.id,
        it_msp_score=80.0,
        it_msp_insight="stale insight",
    )
    refreshed = db.get_lead(conn, lead_id=lead.id)
    assert refreshed is not None

    generate_mock = MagicMock()
    monkeypatch.setattr("msp_pipeline.outreach.generate", generate_mock)

    daily_run._rescore_and_regen_copy(
        conn, [lead.id], model="gpt-4o-mini"
    )

    after = db.get_lead(conn, lead_id=lead.id)
    assert after is not None
    assert after.it_msp_insight is None
    generate_mock.assert_not_called()


# --- Apollo integration ----------------------------------------------------


def _seed_lead(
    conn: Any,
    name: str,
    *,
    it_msp_score: float | None = None,
    mssp_score: float | None = None,
    cloud_score: float | None = None,
    industry: str = "other",
    headcount: int = 80,
) -> int:
    lead = db.upsert_lead(conn, _candidate(name))
    assert lead.id is not None
    db.update_lead(
        conn,
        lead.id,
        industry=industry,
        headcount=headcount,
        country="US",
        it_msp_score=it_msp_score,
        mssp_score=mssp_score,
        cloud_score=cloud_score,
    )
    return lead.id


def test_apollo_top_n_skips_when_unconfigured(
    tmp_path: Path,
) -> None:
    """When APOLLO_API_KEY is unset (the default thanks to conftest), the
    apollo stage no-ops and returns an empty set."""
    conn = db.init_db(tmp_path / "leads.db")
    _seed_lead(conn, "Top Co", it_msp_score=90.0)
    result = daily_run._apollo_enrich_top_n(conn, n=30)
    assert result == set()


def test_apollo_top_n_only_runs_on_top_n_union(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With n=2, each niche contributes its top-2 by score. Companies
    outside every niche's top-2 should never be Apollo-queried, even if
    they exist in the DB."""
    monkeypatch.setenv("APOLLO_API_KEY", "test-key")
    conn = db.init_db(tmp_path / "leads.db")
    # Two clear winners (different per niche), one loser.
    _seed_lead(conn, "Alpha", it_msp_score=90.0, mssp_score=10.0, cloud_score=10.0)
    _seed_lead(conn, "Beta", it_msp_score=80.0, mssp_score=10.0, cloud_score=10.0)
    c = _seed_lead(conn, "Gamma", it_msp_score=10.0, mssp_score=10.0, cloud_score=10.0)
    # Boost Gamma in MSSP only, so it sneaks into the union.
    db.update_lead(conn, c, mssp_score=85.0)
    # Loser — never in any top-2.
    _seed_lead(conn, "Delta", it_msp_score=5.0, mssp_score=5.0, cloud_score=5.0)

    seen: list[str] = []

    def fake_find_dm(name: str, domain: str | None) -> apollo.Result:
        seen.append(name)
        return apollo.Result(
            org_found=True,
            dm_found=True,
            dm_name=f"DM at {name}",
            dm_title="CTO",
            dm_email=f"dm@{name.lower()}.com",
        )

    monkeypatch.setattr(daily_run.apollo, "find_decision_maker", fake_find_dm)

    enriched = daily_run._apollo_enrich_top_n(conn, n=2)
    assert "Delta" not in seen
    assert {"Alpha", "Beta", "Gamma"} <= set(seen)
    # Each company called exactly once even when in multiple niches' top-2.
    assert len(seen) == len(set(seen))
    assert {db.get_lead(conn, lead_id=lid).name for lid in enriched} == {
        "Alpha", "Beta", "Gamma",
    }


def test_apollo_skips_already_marked_leads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APOLLO_API_KEY", "test-key")
    conn = db.init_db(tmp_path / "leads.db")
    lid = _seed_lead(conn, "Already Apollo'd", it_msp_score=90.0)
    db.append_signal(
        conn, lid,
        Signal(
            type=SignalType.APOLLO_ENRICHED,
            source=SourceName.APOLLO,
            captured_at=_now(),
            payload={"dm_found": True},
        ),
    )

    find_mock = MagicMock()
    monkeypatch.setattr(daily_run.apollo, "find_decision_maker", find_mock)

    result = daily_run._apollo_enrich_top_n(conn, n=30)
    find_mock.assert_not_called()
    assert result == set()


def test_apollo_writes_dm_fields_and_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APOLLO_API_KEY", "test-key")
    conn = db.init_db(tmp_path / "leads.db")
    lid = _seed_lead(conn, "Skio", it_msp_score=90.0)

    monkeypatch.setattr(
        daily_run.apollo,
        "find_decision_maker",
        MagicMock(
            return_value=apollo.Result(
                org_found=True,
                dm_found=True,
                dm_name="Andrew Chen",
                dm_title="Chief Technology Officer",
                dm_email="andrew@skio.com",
                dm_linkedin_url="http://www.linkedin.com/in/andrewmnchen",
                apollo_person_id="p_andrew",
                headcount=30,
            )
        ),
    )

    enriched = daily_run._apollo_enrich_top_n(conn, n=30)
    assert enriched == {lid}

    after = db.get_lead(conn, lead_id=lid)
    assert after is not None
    assert after.dm_name == "Andrew Chen"
    assert after.dm_email == "andrew@skio.com"
    assert after.dm_linkedin_url == "http://www.linkedin.com/in/andrewmnchen"
    assert after.headcount == 30
    markers = [s for s in after.signals if s.type == SignalType.APOLLO_ENRICHED]
    assert len(markers) == 1
    assert markers[0].payload["dm_found"] is True
    assert markers[0].payload["apollo_person_id"] == "p_andrew"


def test_apollo_no_marker_when_org_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Org not in Apollo → don't mark, so we retry next night."""
    monkeypatch.setenv("APOLLO_API_KEY", "test-key")
    conn = db.init_db(tmp_path / "leads.db")
    lid = _seed_lead(conn, "Mystery Co", it_msp_score=90.0)

    monkeypatch.setattr(
        daily_run.apollo,
        "find_decision_maker",
        MagicMock(return_value=apollo.Result(org_found=False)),
    )

    enriched = daily_run._apollo_enrich_top_n(conn, n=30)
    assert enriched == set()
    after = db.get_lead(conn, lead_id=lid)
    assert after is not None
    assert not any(s.type == SignalType.APOLLO_ENRICHED for s in after.signals)


def test_apollo_marks_when_org_found_but_no_dm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Org in Apollo but no DM-titled person → mark, so we don't retry forever."""
    monkeypatch.setenv("APOLLO_API_KEY", "test-key")
    conn = db.init_db(tmp_path / "leads.db")
    lid = _seed_lead(conn, "TinyShop", it_msp_score=80.0)

    monkeypatch.setattr(
        daily_run.apollo,
        "find_decision_maker",
        MagicMock(
            return_value=apollo.Result(org_found=True, dm_found=False, headcount=8)
        ),
    )

    enriched = daily_run._apollo_enrich_top_n(conn, n=30)
    assert enriched == set()  # No DM data → no force-regen needed
    after = db.get_lead(conn, lead_id=lid)
    assert after is not None
    markers = [s for s in after.signals if s.type == SignalType.APOLLO_ENRICHED]
    assert len(markers) == 1
    assert markers[0].payload["dm_found"] is False


def test_apollo_deletes_when_headcount_over_smb_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APOLLO_API_KEY", "test-key")
    conn = db.init_db(tmp_path / "leads.db")
    lid = _seed_lead(conn, "Big Sneaker", it_msp_score=70.0, headcount=80)

    monkeypatch.setattr(
        daily_run.apollo,
        "find_decision_maker",
        MagicMock(
            return_value=apollo.Result(
                org_found=True, dm_found=True,
                dm_name="Some VP", headcount=750,
            )
        ),
    )

    daily_run._apollo_enrich_top_n(conn, n=30)
    assert db.get_lead(conn, lead_id=lid) is None


def test_upload_failure_returns_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_minimal_pipeline_mocks(monkeypatch)

    monkeypatch.setenv("LEADS_UPLOAD_URL", "https://example.com/api/upload-leads")
    monkeypatch.setenv("LEADS_UPLOAD_API_KEY", "secret-token")

    import requests as real_requests

    monkeypatch.setattr(
        daily_run.requests,
        "post",
        MagicMock(side_effect=real_requests.RequestException("boom")),
    )

    rc = daily_run.main(
        [
            "--upload",
            "--db-path",
            str(tmp_path / "leads.db"),
            "--output-path",
            str(tmp_path / "leads.json"),
        ]
    )
    assert rc == 1

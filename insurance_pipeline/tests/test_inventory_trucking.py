"""The trucking sub-inventory is a subset of the full insurance inventory —
only leads carrying an FMCSA new-carrier authority signal belong in it."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from insurance_pipeline import daily_run, db
from insurance_pipeline.models import (
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fmcsa_candidate(name: str) -> LeadCandidate:
    return LeadCandidate(
        name=name,
        initial_signal=Signal(
            type=SignalType.NEW_MOTOR_CARRIER_AUTHORITY,
            source=SourceName.FMCSA,
            captured_at=_now(),
            payload={"usdot": name, "city": "Dallas", "state": "TX"},
        ),
    )


def _funding_candidate(name: str) -> LeadCandidate:
    return LeadCandidate(
        name=name,
        initial_signal=Signal(
            type=SignalType.FUNDING_RAISED,
            source=SourceName.FUNDING,
            captured_at=_now(),
            payload={"amount": 5_000_000},
        ),
    )


def _conn_with_mixed_leads(tmp_path: Path):
    conn = db.init_db(tmp_path / "leads.db")
    db.upsert_lead(conn, _fmcsa_candidate("Alpha Trucking LLC"))
    db.upsert_lead(conn, _fmcsa_candidate("Beta Freight Inc"))
    db.upsert_lead(conn, _funding_candidate("Gamma SaaS Co"))
    return conn


def test_is_trucking_lead_discriminates(tmp_path: Path) -> None:
    conn = _conn_with_mixed_leads(tmp_path)
    by_name = {lead.name: lead for lead in db.iter_leads(conn)}
    assert daily_run._is_trucking_lead(by_name["Alpha Trucking LLC"]) is True
    assert daily_run._is_trucking_lead(by_name["Gamma SaaS Co"]) is False


def test_full_inventory_includes_every_lead(tmp_path: Path) -> None:
    conn = _conn_with_mixed_leads(tmp_path)
    names = {lead["name"] for lead in daily_run._build_output(conn)["leads"]}
    assert names == {"Alpha Trucking LLC", "Beta Freight Inc", "Gamma SaaS Co"}


def test_trucking_inventory_is_fmcsa_only(tmp_path: Path) -> None:
    conn = _conn_with_mixed_leads(tmp_path)
    trucking = daily_run._build_output(
        conn, predicate=daily_run._is_trucking_lead
    )
    names = {lead["name"] for lead in trucking["leads"]}
    assert names == {"Alpha Trucking LLC", "Beta Freight Inc"}
    assert "Gamma SaaS Co" not in names


def test_pc_inventory_is_growth_leads_no_trucking(tmp_path: Path) -> None:
    conn = _conn_with_mixed_leads(tmp_path)
    pc = daily_run._build_output(conn, predicate=daily_run._is_pc_lead)
    names = {lead["name"] for lead in pc["leads"]}
    assert names == {"Gamma SaaS Co"}                    # the funding lead only
    assert "Alpha Trucking LLC" not in names             # trucking excluded from P&C
    # trucking and P&C partition the scoring leads with no overlap
    for lead in daily_run.db.iter_leads(conn):
        assert not (daily_run._is_trucking_lead(lead) and daily_run._is_pc_lead(lead))


def test_emit_inventories_writes_both_files(tmp_path: Path) -> None:
    conn = _conn_with_mixed_leads(tmp_path)

    class _Args:
        output_path = tmp_path / "leads.json"
        trucking_output_path = tmp_path / "trucking-leads.json"
        pc_output_path = tmp_path / "pc-leads.json"
        upload = False

    rc = daily_run._emit_inventories(conn, _Args())
    assert rc == 0
    assert _Args.output_path.exists()
    assert _Args.trucking_output_path.exists()
    assert _Args.pc_output_path.exists()

    import json

    full = json.loads(_Args.output_path.read_text())
    trucking = json.loads(_Args.trucking_output_path.read_text())
    pc = json.loads(_Args.pc_output_path.read_text())
    assert len(full["leads"]) == 3
    assert len(trucking["leads"]) == 2
    assert len(pc["leads"]) == 1                          # the funding lead
    # trucking + P&C partition the scoring leads
    assert len(trucking["leads"]) + len(pc["leads"]) == len(full["leads"])

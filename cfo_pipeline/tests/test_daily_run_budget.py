"""Enrichment budget governor + backlog drain (daily_run).

The budget caps Gemini web lookups per run; overflow leads must stay
alive in the DB and rank into the next run's backlog, hiring-signal
leads first.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from cfo_pipeline import daily_run, db, enrichment, llm
from cfo_pipeline.models import LeadCandidate, Signal, SignalType, SourceName


@pytest.fixture()
def conn(tmp_path: Path):
    return db.init_db(tmp_path / "leads.db")


def _candidate(
    name: str, sig_type: SignalType = SignalType.JOB_POSTED_FINANCE_LEAD
) -> LeadCandidate:
    if sig_type == SignalType.FUNDING_RAISED:
        source = SourceName.EDGAR_FORM_D
        payload = {"filing_type": "Form D", "filed_on": "2026-07-01", "link": ""}
    else:
        source = SourceName.JOBS
        payload = {
            "title": "Controller",
            "url": "x",
            "date_posted": "2026-07-01",
            "site": "indeed",
        }
    return LeadCandidate(
        name=name,
        domain=None,
        initial_signal=Signal(
            type=sig_type,
            source=source,
            captured_at=datetime(2026, 7, 1, 12, 0, 0),
            payload=payload,
        ),
    )


def test_enrich_budget_defers_overflow(conn, monkeypatch):
    ids = []
    for name in ("Alpha Co", "Beta Co", "Gamma Co"):
        lead = db.upsert_lead(conn, _candidate(name))
        assert lead is not None
        ids.append(lead.id)

    calls: list[str] = []

    def fake_enrich(c, lead, *, force=False):
        calls.append(lead.name)
        return True

    monkeypatch.setattr(enrichment, "enrich", fake_enrich)

    kept, spent, deferred = daily_run._enrich_all(conn, ids, force=False, budget=2)
    assert spent == 2
    assert deferred == 1
    assert len(calls) == 2
    # Deferred leads stay alive downstream — they're not dropped.
    assert sorted(kept) == sorted(ids)


def test_enrich_stops_on_quota_exhaustion(conn, monkeypatch):
    """When Gemini's daily quota latches mid-run, enrichment stops and
    every remaining lead defers — no hanging on a wall we can't clear."""
    ids = []
    for name in ("Alpha Co", "Beta Co", "Gamma Co", "Delta Co"):
        lead = db.upsert_lead(conn, _candidate(name))
        assert lead is not None
        ids.append(lead.id)

    calls: list[str] = []

    def fake_enrich(c, lead, *, force=False):
        calls.append(lead.name)
        if len(calls) == 2:  # second lead hits the quota wall
            raise llm.GeminiQuotaExhausted("gemini quota exhausted")
        return True

    monkeypatch.setattr(enrichment, "enrich", fake_enrich)

    kept, spent, deferred = daily_run._enrich_all(conn, ids, force=False, budget=None)
    assert len(calls) == 2  # stopped after the wall — didn't try leads 3 & 4
    assert spent == 1  # only the first lead's lookup succeeded
    assert deferred == 3  # the wall lead + the two never attempted
    # Nothing is dropped — every lead survives to retry next run.
    assert set(kept) == set(ids)


def test_enrich_budget_none_means_unlimited(conn, monkeypatch):
    ids = []
    for name in ("Alpha Co", "Beta Co", "Gamma Co"):
        lead = db.upsert_lead(conn, _candidate(name))
        assert lead is not None
        ids.append(lead.id)

    monkeypatch.setattr(enrichment, "enrich", lambda c, lead, *, force=False: True)

    kept, spent, deferred = daily_run._enrich_all(conn, ids, force=False, budget=None)
    assert spent == 3
    assert deferred == 0
    assert sorted(kept) == sorted(ids)


def test_worklist_prioritizes_by_signal_tier(conn):
    fund = db.upsert_lead(conn, _candidate("Fund Only Co", SignalType.FUNDING_RAISED))
    hire = db.upsert_lead(conn, _candidate("Hire Co"))
    fractional = db.upsert_lead(
        conn, _candidate("Fractional Co", SignalType.JOB_POSTED_FRACTIONAL_CFO)
    )
    assert fund is not None and hire is not None and fractional is not None

    worklist = daily_run._enrichment_worklist(conn, force=False)
    assert set(worklist) == {fund.id, hire.id, fractional.id}
    # Tier order: fractional-CFO posting, then below-CFO hire, then
    # funding-only.
    assert worklist.index(fractional.id) < worklist.index(hire.id)
    assert worklist.index(hire.id) < worklist.index(fund.id)


def test_worklist_skips_already_enriched(conn, monkeypatch):
    hire = db.upsert_lead(conn, _candidate("Hire Co"))
    assert hire is not None
    # A lead the skip-check says is done drops out of the worklist.
    monkeypatch.setattr(
        daily_run.enrichment, "_should_skip", lambda lead, force: not force
    )
    assert daily_run._enrichment_worklist(conn, force=False) == []
    # force=True re-includes everything (re-enrich path).
    assert daily_run._enrichment_worklist(conn, force=True) == [hire.id]


# --- Funding-only output gate ------------------------------------------------


def _rendered(name, *, domain=None, types=("funding_raised",), offering=None):
    signals = []
    for t in types:
        payload = {}
        if t == "funding_raised":
            payload = {"filing_type": "Form D", "offering_amount": offering}
        signals.append({"type": t, "payload": payload})
    return {"name": name, "domain": domain, "score": 25.0, "signals": signals}


def test_gate_keeps_hiring_leads_unconditionally():
    leads = [
        _rendered("Hire Co", types=("job_posted_finance_lead",)),
        _rendered("Fractional Co", types=("job_posted_fractional_cfo",)),
    ]
    assert daily_run._gate_funding_only(leads) == leads


def test_gate_drops_domainless_and_small_funding_only():
    leads = [
        _rendered("No Domain Co", domain=None, offering=5_000_000),
        _rendered("Tiny Raise Co", domain="tiny.com", offering=100_000),
        _rendered("Real Raise Co", domain="real.com", offering=2_000_000),
        _rendered("Legacy Co", domain="legacy.com", offering=None),  # pre-mining signal
    ]
    kept = [l["name"] for l in daily_run._gate_funding_only(leads)]
    assert kept == ["Real Raise Co", "Legacy Co"]


def test_gate_caps_funding_only_cards(monkeypatch):
    monkeypatch.setattr(daily_run, "FUNDING_ONLY_MAX_CARDS", 2)
    leads = [
        _rendered(f"Fund Co {i}", domain=f"fund{i}.com", offering=1_000_000)
        for i in range(4)
    ] + [_rendered("Hire Co", types=("job_posted_finance_lead",))]
    kept = [l["name"] for l in daily_run._gate_funding_only(leads)]
    # First two funding-only survive (list arrives score-sorted);
    # hiring leads are never capped.
    assert kept == ["Fund Co 0", "Fund Co 1", "Hire Co"]

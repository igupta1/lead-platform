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


def _form_c_candidate(
    name: str, *, domain: str = "issuer.com", headcount: int = 20,
    filed_on: str = "2026-07-01",
) -> LeadCandidate:
    return LeadCandidate(
        name=name,
        domain=domain,
        headcount=headcount,
        initial_signal=Signal(
            type=SignalType.FUNDING_RAISED,
            source=SourceName.EDGAR_FORM_C,
            captured_at=datetime(2026, 7, 1, 12, 0, 0),
            payload={
                "filing_type": "Form C",
                "filed_on": filed_on,
                "biz_location": "Austin",
                "biz_state": "TX",
            },
        ),
    )


def _form_d_candidate(
    name: str, offering: float | None, *, filed_on: str = "2026-07-01",
) -> LeadCandidate:
    return LeadCandidate(
        name=name,
        domain="raiser.com",
        initial_signal=Signal(
            type=SignalType.FUNDING_RAISED,
            source=SourceName.EDGAR_FORM_D,
            captured_at=datetime(2026, 7, 1, 12, 0, 0),
            payload={
                "filing_type": "Form D",
                "filed_on": filed_on,
                "offering_amount": offering,
                "link": "",
            },
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


# --- LLM-usage optimizations -------------------------------------------------


def test_form_c_funding_only_needs_no_gemini(conn):
    fc = db.upsert_lead(conn, _form_c_candidate("Crowdfund Co"))
    fd = db.upsert_lead(conn, _candidate("FormD Co", SignalType.FUNDING_RAISED))
    hire = db.upsert_lead(conn, _candidate("Hire Co"))
    assert fc and fd and hire
    fc = db.get_lead(conn, lead_id=fc.id)
    fd = db.get_lead(conn, lead_id=fd.id)
    hire = db.get_lead(conn, lead_id=hire.id)
    assert fc and fd and hire
    # Form C funding-only: light path, no Gemini -> doesn't count vs budget.
    assert enrichment.needs_gemini_lookup(fc, force=False) is False
    # Form D needs Gemini to resolve a domain; hiring leads always do.
    assert enrichment.needs_gemini_lookup(fd, force=False) is True
    assert enrichment.needs_gemini_lookup(hire, force=False) is True


def test_form_c_light_path_skips_gemini(conn, monkeypatch):
    fc = db.upsert_lead(conn, _form_c_candidate("Crowdfund Co"))
    assert fc is not None
    lead = db.get_lead(conn, lead_id=fc.id)
    assert lead is not None

    def boom(*a, **k):
        raise AssertionError("lookup_company (Gemini) must not run on the light path")

    monkeypatch.setattr(enrichment, "lookup_company", boom)
    monkeypatch.setattr(
        enrichment, "classify_niche",
        lambda lead, value_prop=None: "b2b_saas",
    )

    assert enrichment.enrich(conn, lead, force=False) is True
    updated = db.get_lead(conn, lead_id=fc.id)
    assert updated is not None
    assert updated.niche == "b2b_saas"
    assert updated.industry == "software_saas"  # derived parent
    assert updated.country == "US"
    assert any(s.type == SignalType.LOCATION_CAPTURED for s in updated.signals)
    assert any(s.type == SignalType.ENRICHMENT_RUN for s in updated.signals)


def test_worklist_drops_small_form_d_and_caps_funding(conn, monkeypatch):
    monkeypatch.setattr(daily_run, "FUNDING_ONLY_ENRICH_CAP", 2)
    small = db.upsert_lead(conn, _form_d_candidate("Tiny Raise", 100_000))
    big1 = db.upsert_lead(conn, _form_d_candidate("Big 1", 3_000_000, filed_on="2026-07-04"))
    big2 = db.upsert_lead(conn, _form_d_candidate("Big 2", 3_000_000, filed_on="2026-07-03"))
    big3 = db.upsert_lead(conn, _form_d_candidate("Big 3", 3_000_000, filed_on="2026-07-01"))
    assert small and big1 and big2 and big3

    wl = daily_run._enrichment_worklist(conn, force=False)
    # Below-floor raise never enters the worklist.
    assert small.id not in wl
    # Cap=2, freshest-first: big3 (oldest) is capped out.
    assert set(wl) == {big1.id, big2.id}


def test_worklist_hiring_never_capped(conn, monkeypatch):
    """The funding cap must not touch the fractional / finance tiers."""
    monkeypatch.setattr(daily_run, "FUNDING_ONLY_ENRICH_CAP", 0)
    hire = db.upsert_lead(conn, _candidate("Hire Co"))
    frac = db.upsert_lead(
        conn, _candidate("Frac Co", SignalType.JOB_POSTED_FRACTIONAL_CFO)
    )
    fund = db.upsert_lead(conn, _form_d_candidate("Fund Co", 3_000_000))
    assert hire and frac and fund
    wl = daily_run._enrichment_worklist(conn, force=False)
    assert hire.id in wl and frac.id in wl
    assert fund.id not in wl  # cap=0 drops all funding-only


def test_form_c_light_path_not_counted_against_budget(conn, monkeypatch):
    """A Form C funding-only lead enriches even when the Gemini budget
    is exhausted — the light path costs no grounded-search call."""
    hire = db.upsert_lead(conn, _candidate("Hire Co"))
    fc = db.upsert_lead(conn, _form_c_candidate("Crowdfund Co"))
    assert hire and fc

    calls: list[str] = []
    monkeypatch.setattr(
        enrichment, "enrich",
        lambda c, lead, *, force=False: (calls.append(lead.name) or True),
    )

    kept, spent, deferred = daily_run._enrich_all(
        conn, [hire.id, fc.id], force=False, budget=0
    )
    assert calls == ["Crowdfund Co"]  # only the light-path lead ran
    assert spent == 0
    assert deferred == 1  # the hiring lead deferred (budget exhausted)
    assert set(kept) == {hire.id, fc.id}  # nothing dropped


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

"""Parallel enrichment: the plan/apply split and run.enrich_all's concurrent
orchestration (workers, budget cap, time-budget backstop, serial DB apply).

Run:  leadgen/.venv/bin/python -m pytest leadgen/tests/test_enrich_parallel.py -q
"""

from __future__ import annotations

import time

from leadgen import db, enrichment, run
from leadgen.models import LeadCandidate, SignalType

from tests.conftest import make_signal


def _seed(conn, name):
    return db.upsert_lead(conn, LeadCandidate(
        name=name, initial_signal=make_signal(SignalType.JOB_FINANCE_LEAD)))


def _get(conn, lead_id):
    return next(lead for lead in db.iter_leads(conn) if lead.id == lead_id)


# --------------------------------------------------------------------------
# plan_enrichment + apply_enrichment compose to the same as enrich()
# --------------------------------------------------------------------------

def test_plan_then_apply_updates_lead(conn, monkeypatch):
    lead = _seed(conn, "Acme Widgets")
    monkeypatch.setattr(enrichment, "lookup_company", lambda ld: enrichment._Lookup(
        headcount=20, city="Denver", state="CO", country="US", domain="acme.com",
        insight="makes widgets"))
    monkeypatch.setattr(enrichment, "classify_niche", lambda ld, insight=None: "manufacturing")

    plan = enrichment.plan_enrichment(lead)
    assert plan.action == "update" and plan.updates["niche"] == "manufacturing"
    assert enrichment.apply_enrichment(conn, lead, plan) is True

    got = _get(conn, lead.id)
    assert got.niche == "manufacturing"
    assert got.domain == "acme.com"
    assert got.enriched_at is not None


def test_plan_delete_is_pure_apply_deletes(conn, monkeypatch):
    lead = _seed(conn, "Acme Widgets")
    # non-US country -> a delete plan, computed with NO db access
    monkeypatch.setattr(enrichment, "lookup_company", lambda ld: enrichment._Lookup(
        country="GB", insight="a british firm"))
    monkeypatch.setattr(enrichment, "classify_niche", lambda ld, insight=None: "manufacturing")

    plan = enrichment.plan_enrichment(lead)
    assert plan.action == "delete" and "non_us" in plan.reason
    assert enrichment.apply_enrichment(conn, lead, plan) is False
    assert all(existing.id != lead.id for existing in db.iter_leads(conn))   # gone


# --------------------------------------------------------------------------
# run.enrich_all — concurrent plan, serial apply
# --------------------------------------------------------------------------

def _patch_planapply(monkeypatch):
    planned, applied = [], []

    def fake_plan(lead, *, force=False):
        planned.append(lead.id)
        return enrichment.EnrichPlan("update", updates={"niche": "x"}, reason="enriched")

    def fake_apply(conn, lead, plan):
        applied.append(lead.id)
        return True

    monkeypatch.setattr(enrichment, "plan_enrichment", fake_plan)
    monkeypatch.setattr(enrichment, "apply_enrichment", fake_apply)
    return planned, applied


def test_enrich_all_parallel_plans_all_and_applies_serially(conn, monkeypatch):
    ids = [_seed(conn, f"Co{i}").id for i in range(10)]
    planned, applied = _patch_planapply(monkeypatch)

    n = run.enrich_all(conn, budget=None, force=False, workers=4)

    assert n == 10
    assert sorted(planned) == sorted(ids)       # every candidate planned (concurrently)
    assert sorted(applied) == sorted(ids)       # every candidate applied (serially)


def test_enrich_all_serial_path_still_works(conn, monkeypatch):
    ids = [_seed(conn, f"Co{i}").id for i in range(5)]
    planned, applied = _patch_planapply(monkeypatch)

    n = run.enrich_all(conn, budget=None, force=False, workers=1)   # serial via enrich()

    assert n == 5
    assert sorted(applied) == sorted(ids)


def test_enrich_all_respects_count_budget(conn, monkeypatch):
    [_seed(conn, f"Co{i}") for i in range(10)]
    planned, applied = _patch_planapply(monkeypatch)

    n = run.enrich_all(conn, budget=3, force=False, workers=4)

    assert n == 3
    assert len(planned) == 3 and len(applied) == 3


def test_enrich_all_time_budget_stops_before_work(conn, monkeypatch):
    [_seed(conn, f"Co{i}") for i in range(10)]
    planned, applied = _patch_planapply(monkeypatch)

    n = run.enrich_all(conn, budget=None, force=False, workers=4,
                       deadline=time.monotonic() - 1)   # already past

    assert n == 0 and planned == [] and applied == []


def test_enrich_all_real_plan_apply_under_threads(conn, monkeypatch):
    """Real plan_enrichment runs in worker threads (network mocked); real
    apply_enrichment writes to the one sqlite conn serially on the main thread."""
    ids = [_seed(conn, f"Co{i}").id for i in range(8)]
    monkeypatch.setattr(enrichment, "lookup_company", lambda ld: enrichment._Lookup(
        headcount=15, country="US", domain=f"{ld.name.lower()}.com", insight="x"))
    monkeypatch.setattr(enrichment, "classify_niche", lambda ld, insight=None: "manufacturing")

    n = run.enrich_all(conn, budget=None, force=False, workers=4)

    assert n == 8
    for lead_id in ids:
        got = _get(conn, lead_id)
        assert got.niche == "manufacturing" and got.enriched_at is not None


def test_reenrich_forces_already_enriched_leads_on_the_parallel_path(conn, monkeypatch):
    """--reenrich was a silent no-op with workers > 1: _plan_chunk hardcoded
    force=False, so plan_enrichment re-checked needs_enrichment and returned
    "skip" for every already-enriched lead. Only the serial path honored it."""
    from datetime import datetime, timezone

    from leadgen import db, run
    from leadgen.models import LeadCandidate

    from tests.conftest import make_signal

    lead = db.upsert_lead(conn, LeadCandidate(
        name="Already Enriched Co", domain="already.com",
        initial_signal=make_signal(SignalType.JOB_FINANCE_LEAD, url="https://j/ae")))
    db.update_lead(conn, lead.id,
                   enriched_at=datetime.now(timezone.utc).replace(tzinfo=None))

    planned: list[bool] = []

    def _fake_plan(lead, *, force=False):
        planned.append(force)
        return enrichment.EnrichPlan("skip")

    monkeypatch.setattr(enrichment, "plan_enrichment", _fake_plan)
    run.enrich_all(conn, budget=None, force=True, workers=4)

    assert planned, "an already-enriched lead must still be planned under --reenrich"
    assert all(planned), "force=True must reach plan_enrichment"

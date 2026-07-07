"""Lead inventory builder — the queryable /api/leads feed.

Covers the pure labellers (stable id, 4-way signal_type, freshness,
plain-words) and the _build_inventory qualification (require domain +
state, include stale, exclude dead)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cfo_pipeline import daily_run, db, taxonomy
from cfo_pipeline.models import LeadCandidate, Signal, SignalType, SourceName

_NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def _recent(days: int) -> str:
    return (_NOW - timedelta(days=days)).strftime("%Y-%m-%d")


@pytest.fixture()
def conn(tmp_path: Path):
    return db.init_db(tmp_path / "leads.db")


def _sig(t: SignalType, *, days_ago: int = 0, payload: dict | None = None) -> Signal:
    src = (
        SourceName.JOBS
        if t in (SignalType.JOB_POSTED_FINANCE_LEAD, SignalType.JOB_POSTED_FRACTIONAL_CFO)
        else SourceName.EDGAR_FORM_D
    )
    return Signal(
        type=t, source=src,
        captured_at=_NOW - timedelta(days=days_ago),
        payload=payload or {},
    )


# --- pure labellers --------------------------------------------------------


def test_stable_id_is_domain_based_and_normalized():
    a = daily_run._stable_id("Acme.com", "acme")
    assert a == daily_run._stable_id("acme.com ", "acme")   # normalized
    assert a != daily_run._stable_id("other.com", "acme")   # domain drives it


def test_signal_type_four_way():
    F, H, U = (
        SignalType.JOB_POSTED_FRACTIONAL_CFO,
        SignalType.JOB_POSTED_FINANCE_LEAD,
        SignalType.FUNDING_RAISED,
    )
    st = daily_run._lead_signal_type
    assert st([_sig(F)]) == "cfo_wanted"
    assert st([_sig(H), _sig(U)]) == "double_signal"
    assert st([_sig(U)]) == "funding_only"
    assert st([_sig(H)]) == "hiring_only"
    assert st([_sig(F), _sig(U)]) == "cfo_wanted"  # cfo-wanted always wins


def test_freshness_boundaries():
    assert daily_run._freshness_label(0) == "fresh"
    assert daily_run._freshness_label(30) == "fresh"
    assert daily_run._freshness_label(31) == "stale"
    assert daily_run._freshness_label(60) == "stale"
    assert daily_run._freshness_label(61) == "dead"


def test_plain_words_per_type():
    now = _NOW
    frac = _sig(SignalType.JOB_POSTED_FRACTIONAL_CFO,
                payload={"title": "Fractional CFO", "date_posted": _recent(5)})
    assert "fractional" in daily_run._plain_words(frac, now).lower()

    fin = _sig(SignalType.JOB_POSTED_FINANCE_LEAD,
               payload={"title": "Controller", "date_posted": _recent(2)})
    assert "controller" in daily_run._plain_words(fin, now).lower()

    fd = _sig(SignalType.FUNDING_RAISED,
              payload={"filing_type": "Form D", "offering_amount": 2_500_000,
                       "filed_on": _recent(4)})
    pw = daily_run._plain_words(fd, now)
    assert "raise" in pw.lower() and "$2.5M" in pw

    fc = _sig(SignalType.FUNDING_RAISED,
              payload={"filing_type": "Form C", "filed_on": _recent(3)})
    assert "reg cf" in daily_run._plain_words(fc, now).lower()


def test_date_confidence_softens_board_copy():
    now = _NOW
    # fractionaljobs.io -> low confidence, no specific date in the copy
    board = _sig(SignalType.JOB_POSTED_FRACTIONAL_CFO,
                 payload={"title": "Chief Financial Officer", "site": "fractionaljobs",
                          "date_posted": _recent(15)})
    assert daily_run._date_confidence(board) == "low"
    pw = daily_run._plain_words(board, now)
    assert "fractional" in pw.lower() and "ago" not in pw.lower()
    assert daily_run._inventory_signal(board, now)["date_confidence"] == "low"

    # WWR / job board -> high confidence, keeps the specific date
    wwr = _sig(SignalType.JOB_POSTED_FRACTIONAL_CFO,
               payload={"title": "Fractional CFO", "site": "weworkremotely",
                        "date_posted": _recent(5)})
    assert daily_run._date_confidence(wwr) == "high"
    assert "ago" in daily_run._plain_words(wwr, now).lower()


# --- _build_inventory qualification ---------------------------------------


def _make(conn, name, *, domain, state=None, city=None, niche=None, days_ago=3,
          funding=False, value_prop=None):
    if funding:
        sig = _sig(SignalType.FUNDING_RAISED, payload={
            "filing_type": "Form D", "offering_amount": 3_000_000,
            "filed_on": _recent(days_ago), "link": ""})
    else:
        sig = _sig(SignalType.JOB_POSTED_FINANCE_LEAD, payload={
            "title": "Controller", "date_posted": _recent(days_ago),
            "url": "x", "site": "indeed"})
    cand = LeadCandidate(name=name, domain=domain, initial_signal=sig)
    lead = db.upsert_lead(conn, cand)
    assert lead is not None and lead.id is not None
    if state or city:
        db.append_signal(
            conn, lead.id,
            Signal(type=SignalType.LOCATION_CAPTURED, source=SourceName.COMPUTED,
                   captured_at=_NOW, payload={"city": city, "state": state}),
        )
    updates = {}
    if niche:
        updates.update(niche=niche, industry=taxonomy.parent_of(niche))
    if value_prop:
        updates["value_prop"] = value_prop
    if updates:
        db.update_lead(conn, lead.id, **updates)
    return lead


def test_build_inventory_qualification(conn):
    _make(conn, "Fresh Co", domain="fresh.com", state="CA", city="Oakland",
          niche="b2b_saas", days_ago=5, value_prop="B2B SaaS for gyms")  # fresh
    _make(conn, "Stale Co", domain="stale.com", state="TX", city="Austin",
          niche="restaurant", days_ago=45)                     # stale (31-60) -> included
    _make(conn, "Dead Co", domain="dead.com", state="NY", days_ago=70)   # >60 -> excluded
    _make(conn, "NoDomain Hire", domain=None, state="FL", days_ago=4)    # hiring, no domain -> INCLUDED
    _make(conn, "NoDomain Fund", domain=None, state="WA", days_ago=4, funding=True)  # funding, no domain -> excluded
    _make(conn, "NoState Co", domain="nostate.com")                      # no location -> excluded

    inv = daily_run._build_inventory(conn)
    by_name = {l["company"]: l for l in inv["leads"]}

    # domain relaxed for hiring leads, still required for funding-only
    assert set(by_name) == {"Fresh Co", "Stale Co", "NoDomain Hire"}
    assert by_name["NoDomain Hire"]["domain"] is None

    assert by_name["Fresh Co"]["freshness"] == "fresh"
    assert by_name["Stale Co"]["freshness"] == "stale"

    fresh = by_name["Fresh Co"]
    assert fresh["signal_type"] == "hiring_only"
    assert fresh["industry"] == "software_saas"  # derived from niche
    assert fresh["niche"] == "b2b_saas"
    assert fresh["value_prop"] == "B2B SaaS for gyms"  # surfaced for the review card
    assert fresh["state"] == "CA" and fresh["city"] == "Oakland"
    # id is domain-derived (name_key arg is ignored when a domain exists).
    assert fresh["id"] == daily_run._stable_id("fresh.com", "ignored")
    assert fresh["signals"][0]["type"] == "finance_posting"
    assert "date" in fresh["signals"][0] and "plain_words_description" in fresh["signals"][0]

    # Taxonomy travels with the inventory (for /api/niches).
    assert inv["taxonomy"]["software_saas"]

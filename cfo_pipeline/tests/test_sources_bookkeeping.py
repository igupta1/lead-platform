"""Bookkeeping source: junior classifier, candidate emission, inventory output.

The CFO path is unchanged (its suite still passes); these cover the new junior
capture in isolation, with an injected scrape so no network is needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cfo_pipeline import bookkeeping_run
from cfo_pipeline.models import SignalType
from cfo_pipeline.sources import bookkeeping


def _recent() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()


def _scrape_returning(rows):
    def _scrape(since, queries, max_age_days):
        return rows
    return _scrape


# --- classifier ------------------------------------------------------------

def test_classifier_accepts_junior_rejects_senior() -> None:
    for t in ["Bookkeeper", "Staff Accountant", "Accounts Payable Specialist",
              "Payroll Specialist", "Accounting Clerk", "Accountant"]:
        assert bookkeeping.is_bookkeeping_title(t) is True, t
    for t in ["Senior Accountant", "Controller", "VP Finance",
              "Chief Financial Officer", "Fractional CFO", "Tax Accountant"]:
        assert bookkeeping.is_bookkeeping_title(t) is False, t


# --- fetch (classification + exclusions) -----------------------------------

def _row(title, company, location="Austin, TX", date_posted=None):
    return {
        "title": title, "company": company, "job_url": "http://x",
        "date_posted": date_posted or _recent(), "site": "indeed", "location": location,
    }


def test_fetch_emits_junior_candidates_with_role_tier() -> None:
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    rows = [_row("Bookkeeper", "Sunrise Dental")]
    cands = bookkeeping.fetch(since=since, scrape=_scrape_returning(rows))
    assert len(cands) == 1
    c = cands[0]
    assert c.name == "Sunrise Dental"
    assert c.initial_signal.type == SignalType.JOB_POSTED_BOOKKEEPING
    payload = c.initial_signal.payload
    assert payload["role_tier"] == "junior"
    assert payload["city"] == "Austin" and payload["state"] == "TX"


def test_fetch_excludes_cfo_and_lead_and_junk() -> None:
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    rows = [
        _row("Controller", "Acme Co"),               # CFO finance-lead tier
        _row("Fractional CFO", "Beta Co"),           # in-market CFO
        _row("Chief Financial Officer", "Gamma Co"),  # CFO disqualifier
        _row("Bookkeeper", "Staffing Partners LLC"),  # recruiter name
        _row("Bookkeeper", "Grand Hyatt Hotel"),      # hotel name
        _row("Bookkeeper", "Riverside Dental"),       # <-- the only keeper
    ]
    cands = bookkeeping.fetch(since=since, scrape=_scrape_returning(rows))
    names = {c.name for c in cands}
    assert names == {"Riverside Dental"}


# --- inventory output ------------------------------------------------------

def test_build_output_dedups_by_company_keeping_freshest() -> None:
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    older = (datetime.now(timezone.utc) - timedelta(days=20)).date().isoformat()
    newer = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    rows = [
        _row("Bookkeeper", "Peak Dental", date_posted=older),
        _row("Staff Accountant", "Peak Dental", date_posted=newer),  # same co, fresher
        _row("Payroll Specialist", "Vista Clinic", date_posted=newer),
    ]
    cands = bookkeeping.fetch(since=since, scrape=_scrape_returning(rows))
    out = bookkeeping_run.build_output(cands)
    leads = out["leads"]
    assert len(leads) == 2                                  # Peak Dental deduped to one
    peak = next(l for l in leads if l["name"] == "Peak Dental")
    assert peak["role_tier"] == "junior"
    assert peak["signals"][0]["payload"]["title"] == "Staff Accountant"   # the fresher one
    assert peak["signals"][0]["type"] == "job_posted_bookkeeping"

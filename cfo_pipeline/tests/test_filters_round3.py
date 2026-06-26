"""3rd-review fix-pass coverage.

One test per requirement (req #1-#10), focused on the structural
filter additions. Captured-fixture style — no network."""

from __future__ import annotations

from datetime import datetime, timedelta

from cfo_pipeline import db
from cfo_pipeline.models import (
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)
from cfo_pipeline.sources import edgar_form_d, jobs


# --- Req #1: signal dedup -------------------------------------------------


def _job_candidate(name: str, title: str, *, site: str, url: str = "x") -> LeadCandidate:
    return LeadCandidate(
        name=name,
        domain=None,
        initial_signal=Signal(
            type=SignalType.JOB_POSTED_FINANCE_LEAD,
            source=SourceName.JOBS,
            captured_at=datetime(2026, 5, 17),
            payload={"title": title, "url": url, "site": site, "date_posted": "2026-05-17"},
        ),
    )


def test_same_role_across_boards_dedups_to_one_signal(tmp_path):
    conn = db.init_db(tmp_path / "leads.db")
    # Same posting on three boards — should be one signal after dedup.
    db.upsert_lead(conn, _job_candidate("Acme", "Senior Controller (10660JFXV)", site="indeed", url="a"))
    db.upsert_lead(conn, _job_candidate("Acme", "Senior Controller", site="linkedin", url="b"))
    db.upsert_lead(conn, _job_candidate("Acme", "  senior  controller!  ", site="google", url="c"))
    lead = db.get_lead(conn, name_key="acme")
    assert lead is not None
    assert len(lead.signals) == 1


def test_different_titles_dont_dedup(tmp_path):
    conn = db.init_db(tmp_path / "leads.db")
    db.upsert_lead(conn, _job_candidate("Acme", "Senior Controller", site="indeed"))
    db.upsert_lead(conn, _job_candidate("Acme", "VP Finance", site="indeed"))
    lead = db.get_lead(conn, name_key="acme")
    assert lead is not None
    assert len(lead.signals) == 2


# --- Req #4: Form D name-pattern exclusions ------------------------------


def test_form_d_excludes_real_estate_keywords():
    names = [
        "Cupressus Apartments LLC",
        "Reno City Center Owner LLC",  # not in regex list but caught by 'Center' if added; for now just confirm Apartments
        "Avalon Crossing LLC",
        "Stone Ridge Properties LLC",
        "Multifamily Growth Fund",  # 'Multifamily' AND 'Fund'
        "Centerville Industrial Trust",
    ]
    for n in names:
        # Some of these are caught by multiple regexes; only assert the overall verdict.
        assert not edgar_form_d._is_operating_company(n), n


def test_form_d_excludes_investment_vehicles():
    names = [
        "HG SPV1 LLC",
        "Pathway Access Vehicle LLC",
        "MCR Macon Investco LLC",
        "BWM Private Equity II LLC",
        "Man Systematic Global Core Equities",
        "Goldman Sachs Private Credit Corp",
        "Lightstone Direct I LLC",
        "Acme Multi-Strategy LLC",
    ]
    for n in names:
        assert not edgar_form_d._is_operating_company(n), n


def test_form_d_excludes_roman_and_trailing_digit_spv():
    names = [
        "EMT XI LLC",
        "Acretrader 278 LLC",
        "Acme XVI",
        "Pioneer Fund III",
    ]
    for n in names:
        assert not edgar_form_d._is_operating_company(n), n


def test_form_d_keeps_real_operating_companies():
    """Sanity — the expanded filter must NOT nuke a startup."""
    operating = [
        "Genesis AI",
        "Stripe Inc",
        "Acme Robotics Inc",
        "Pioneer Software Inc",  # 'Pioneer' alone shouldn't trigger anything
        "Hadley Designs",
        "Tugboat Solutions Inc.",
    ]
    for n in operating:
        assert edgar_form_d._is_operating_company(n), n


# --- Req #5: recruiter detection -----------------------------------------


def test_recruiter_search_variants():
    """The new "Search Group / Partners / Masters" patterns."""
    recruiters = [
        "NorthPoint Search Group",
        "Search Masters Inc",
        "Pyxis Search Partners",
        "Acme Executive Search",
        "BigCo Talent Acquisition",
    ]
    for n in recruiters:
        assert jobs._is_recruiter_name(n), n


def test_search_substring_doesnt_misfire():
    """'Search' as a substring in a non-recruiter context must pass."""
    assert not jobs._is_recruiter_name("SearchGPT Labs")
    assert not jobs._is_recruiter_name("Research Triangle Inc")


# --- Req #6: auto-dealer exclusion ---------------------------------------


def test_auto_dealer_brand_names():
    dealers = [
        "Route 128 Honda",
        "Luxury Auto Mall of Sioux Falls",
        "Boston BMW",
        "Capital City Motors",
        "Tampa Toyota",
        "CARWARRIORS LLC",
    ]
    for n in dealers:
        assert jobs._is_auto_dealer_name(n), n


def test_automotive_title_excluded():
    assert jobs._is_automotive_title("Automotive Finance Manager")
    assert jobs._is_automotive_title("Automotive Assistant Controller")
    assert not jobs._is_automotive_title("Finance Manager")


# --- Req #10: posting-age cutoff ----------------------------------------


def test_too_old_posting_excluded():
    now = datetime(2026, 5, 17)
    # 35 days old → too old
    assert jobs._is_too_old("2026-04-12", now)
    # 25 days old → keep
    assert not jobs._is_too_old("2026-04-22", now)
    # Unknown date → keep (don't punish missing data)
    assert not jobs._is_too_old(None, now)
    assert not jobs._is_too_old("nan", now)


# --- Req #9: brand_key normalization -------------------------------------


def test_brand_key_strips_operational_suffixes():
    assert db.brand_key("Estately Operations LLC") == "estately"
    assert db.brand_key("Acme Holdings Inc") == "acme"
    assert db.brand_key("Pioneer Group LLC") == "pioneer"


def test_brand_key_matches_brand_name():
    """The whole point: 'Estately Operations LLC' must produce the
    same brand_key as 'Estately' so the bullseye merge fires."""
    assert db.brand_key("Estately Operations LLC") == db.brand_key("Estately")


def test_brand_key_doesnt_overstrip():
    # brand_key strips suffixes only, not prefixes — "Operations" at the
    # START of a name is retained. (We don't want to conflate
    # "Operations Research Inc" with "Research Inc".)
    assert db.brand_key("Operations Research Group") == "operations research"
    # 'Acme' alone is its own brand key.
    assert db.brand_key("Acme") == "acme"

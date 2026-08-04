"""Parser-helper tests for the jobs source.

``classify`` is already covered in ``test_sources_jobs.py``; this file guards
the posting-normalization helpers instead: location splitting, Indeed
headcount-band parsing, and posted-date parsing / age gating. All pure, no
network.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from leadgen import filters
from leadgen.sources import jobs


# --------------------------------------------------------------------------
# _split_location — "City, ST" -> (city, state); remote/unparseable -> None
# --------------------------------------------------------------------------


def test_split_location_city_state():
    assert jobs._split_location("San Francisco, CA") == ("San Francisco", "CA")


def test_split_location_with_zip():
    assert jobs._split_location("Austin, TX 78701") == ("Austin", "TX")


def test_split_location_city_only():
    assert jobs._split_location("New York") == ("New York", None)


def test_split_location_remote_and_empty():
    assert jobs._split_location("Remote") == (None, None)
    assert jobs._split_location("Remote, US") == (None, None)
    assert jobs._split_location("") == (None, None)


def test_split_location_non_state_token():
    # A second token that isn't a US state abbr yields no state.
    city, state = jobs._split_location("Toronto, Ontario")
    assert city == "Toronto"
    assert state is None


# --------------------------------------------------------------------------
# _parse_headcount_label — Indeed size bands -> integer upper bound
# --------------------------------------------------------------------------


def test_parse_headcount_band():
    assert jobs._parse_headcount_label("11 to 50") == 50
    assert jobs._parse_headcount_label("201 to 500") == 500


def test_parse_headcount_plus():
    assert jobs._parse_headcount_label("10,001+") == 10001


def test_parse_headcount_plain_int():
    assert jobs._parse_headcount_label("50") == 50


def test_parse_headcount_unknown():
    assert jobs._parse_headcount_label("Unknown") is None
    assert jobs._parse_headcount_label("") is None
    assert jobs._parse_headcount_label(None) is None
    assert jobs._parse_headcount_label("n/a") is None


# --------------------------------------------------------------------------
# _parse_posted_date — JobSpy (YYYY-MM-DD) + Adzuna (ISO-8601)
# --------------------------------------------------------------------------


def test_parse_posted_date_jobspy():
    assert jobs._parse_posted_date("2026-07-15") == datetime(2026, 7, 15)


def test_parse_posted_date_iso8601():
    # Adzuna sends full ISO-8601; the parser normalizes to the calendar date
    # (the leading YYYY-MM-DD branch matches first), dropping the time.
    assert jobs._parse_posted_date("2026-07-15T09:30:00Z") == datetime(2026, 7, 15)


def test_parse_posted_date_unparseable():
    assert jobs._parse_posted_date("") is None
    assert jobs._parse_posted_date(None) is None
    assert jobs._parse_posted_date("nan") is None
    assert jobs._parse_posted_date("not-a-date") is None


# --------------------------------------------------------------------------
# _is_too_old — the candidate-level posting-age gate
# --------------------------------------------------------------------------


def test_is_too_old_gate():
    now = datetime(2026, 7, 21)
    fresh = (now - timedelta(days=5)).date().isoformat()
    stale = (now - timedelta(days=45)).date().isoformat()
    assert jobs._is_too_old(fresh, now, max_days=30) is False
    assert jobs._is_too_old(stale, now, max_days=30) is True
    # Fractional postings use the wider 60-day window.
    assert jobs._is_too_old(stale, now, max_days=60) is False


def test_is_too_old_unknown_date_kept():
    # Unknown age -> keep (let downstream recency scoring decay it).
    assert jobs._is_too_old("", datetime(2026, 7, 21)) is False


# --------------------------------------------------------------------------
# _is_generic_stub_name — reject a lone generic corporate word
# --------------------------------------------------------------------------


def test_generic_stub_name_rejects_lone_generic_word():
    # A truncated/junk company field that is just one generic term.
    assert filters._is_generic_stub_name("Enterprises") is True
    assert filters._is_generic_stub_name("solutions") is True
    assert filters._is_generic_stub_name("  Group  ") is True
    assert filters._is_generic_stub_name("Holdings, LLC") is True  # one real token


def test_generic_stub_name_keeps_real_companies():
    # Multi-token names are never stubs, even when they end in a generic word.
    assert filters._is_generic_stub_name("Acme Enterprises") is False
    assert filters._is_generic_stub_name("Palantir") is False
    assert filters._is_generic_stub_name("Stripe") is False
    assert filters._is_generic_stub_name("Redwood Holdings") is False


@pytest.mark.parametrize("name", [
    "A great organization!",              # rank ~9 of published accounting
    "Confidential Startup SaaS Company",
    "Smart Apply Test Company",           # reached rank 1 of published cloud
    "Undisclosed Employer",
    "Our Client",
    "Company Name",
])
def test_descriptions_and_test_records_are_untargetable(name):
    from leadgen.filters import is_untargetable_name
    assert is_untargetable_name(name) is True


@pytest.mark.parametrize("name", [
    "Heven AeroTech", "Portland Pet Food Company", "Integrity Realty Group, LLC",
    "Test Equipment Depot",        # 'test' not followed by company/org
    "Confidence Interval Labs",    # 'confiden...' but not the word
    "Outtake", "Akina, Inc.",
])
def test_real_companies_survive_the_placeholder_gate(name):
    from leadgen.filters import is_untargetable_name
    assert is_untargetable_name(name) is False


# --------------------------------------------------------------------------
# strip_ats_artifact — repair a job board's trailing label, never reject
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw,clean", [
    ("Canopy Careers", "Canopy"),
    ("Carisk Partners Careers", "Carisk Partners"),
    ("GT Independence Careers", "GT Independence"),
    ("Salamander Palm Beach Employer", "Salamander Palm Beach"),
    ("Acme Hiring", "Acme"),
    ("Acme Jobs  ", "Acme"),
])
def test_strip_ats_artifact_repairs_trailing_board_label(raw, clean):
    assert filters.strip_ats_artifact(raw) == clean


@pytest.mark.parametrize("name", [
    "Employer Solutions Group",   # the same word mid-name is ordinary
    "Careers Inc",                # only the label -> left for the stub gate
    "Jobs",
    "Canopy",
    "Steve Madden",
])
def test_strip_ats_artifact_leaves_real_names_alone(name):
    assert filters.strip_ats_artifact(name) == name


def test_repaired_name_still_faces_the_untargetable_gates():
    # Repair first, then judge: the label is not a licence to publish junk.
    from leadgen.filters import is_untargetable_name, strip_ats_artifact
    assert is_untargetable_name(strip_ats_artifact("Undisclosed Employer")) is True

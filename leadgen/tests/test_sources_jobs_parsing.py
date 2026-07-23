"""Parser-helper tests for the jobs source.

``classify`` is already covered in ``test_sources_jobs.py``; this file guards
the posting-normalization helpers instead: location splitting, Indeed
headcount-band parsing, and posted-date parsing / age gating. All pure, no
network.
"""

from __future__ import annotations

from datetime import datetime, timedelta

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

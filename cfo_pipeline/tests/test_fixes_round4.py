"""4th-review fix-pass coverage.

One block per requirement; pure unit coverage for the Apollo helpers
and the empty-shell filter. The bullseye / dedup integration paths
are covered by the round-3 tests."""

from __future__ import annotations

import pytest

from cfo_pipeline import apollo
from cfo_pipeline.daily_run import _is_empty_shell


# --- Req 4a: doubled-name + generic-email guard --------------------------


def test_is_doubled_name():
    assert apollo._is_doubled_name("Paces Paces")
    assert apollo._is_doubled_name("Acme Acme")
    assert apollo._is_doubled_name("ACME acme")  # case-insensitive
    # Real names — don't flag.
    assert not apollo._is_doubled_name("James McWalter")
    assert not apollo._is_doubled_name("Jane Doe")
    # Single-token name — unusual but not a doubling pattern.
    assert not apollo._is_doubled_name("Madonna")
    # Three-token: token[0] vs token[-1].
    assert apollo._is_doubled_name("Paces Inc Paces")
    assert not apollo._is_doubled_name("John P Smith")


def test_is_generic_email():
    generic = [
        "intercom@paces.com",
        "info@acme.com",
        "support@example.com",
        "hello@startup.io",
        "noreply@bigco.com",
        "careers@hiring.com",
        "hr@workforce.com",
    ]
    for e in generic:
        assert apollo._is_generic_email(e), e
    personal = [
        "james@paces.com",
        "j.mcwalter@paces.com",
        "dimitrios.iliopoulos@athos.com",
    ]
    for e in personal:
        assert not apollo._is_generic_email(e), e
    # Edge cases
    assert not apollo._is_generic_email(None)
    assert not apollo._is_generic_email("")


# --- Req 4b: operator-title gate -----------------------------------------


def test_pick_best_rejects_vp_function():
    """Athos Therapeutics shipped a 'VP Analytical Chemistry' as the
    DM. The post-pick title-validation must reject this even if no
    other people are returned."""
    people = [
        {"name": "Phithi Nguyen", "title": "VP Analytical Chemistry", "seniority": "vp"},
    ]
    assert apollo._pick_best(people) is None


def test_pick_best_keeps_ceo():
    people = [
        {"name": "Dimitrios Iliopoulos", "title": "CEO and Founder", "seniority": "c_suite"},
    ]
    chosen = apollo._pick_best(people)
    assert chosen is not None and chosen["name"] == "Dimitrios Iliopoulos"


def test_pick_best_picks_operator_over_vp():
    people = [
        {"name": "Phithi Nguyen", "title": "VP Analytical Chemistry", "seniority": "vp"},
        {"name": "Dimitrios Iliopoulos", "title": "CEO and Founder", "seniority": "c_suite"},
    ]
    chosen = apollo._pick_best(people)
    assert chosen is not None and chosen["name"] == "Dimitrios Iliopoulos"


def test_pick_best_accepts_managing_partner():
    people = [
        {"name": "Jane Doe", "title": "Managing Partner", "seniority": "partner"},
    ]
    chosen = apollo._pick_best(people)
    assert chosen is not None


def test_pick_best_rejects_director_of_engineering():
    """A 'Director of Engineering' is not a fractional-CFO buyer."""
    people = [
        {"name": "Bob Smith", "title": "Director of Engineering", "seniority": "director"},
    ]
    assert apollo._pick_best(people) is None


# --- Req 4c: empty-shell output filter -----------------------------------


def _shell(**fields):
    base = {
        "domain": None,
        "dm_name": None,
        "headcount": None,
        "city": None,
        "state": None,
    }
    base.update(fields)
    return base


def test_empty_shell_lead_filtered():
    assert _is_empty_shell(_shell())


def test_lead_with_dm_kept():
    assert not _is_empty_shell(_shell(dm_name="James McWalter"))


def test_lead_with_just_state_kept():
    assert not _is_empty_shell(_shell(state="DE"))


def test_lead_with_just_domain_kept():
    assert not _is_empty_shell(_shell(domain="acme.com"))


def test_lead_with_just_headcount_kept():
    assert not _is_empty_shell(_shell(headcount=12))

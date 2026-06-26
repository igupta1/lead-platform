"""Tests for the contact_strength badge resolution and the
Apollo-skip heuristic added in Issue 2."""

from __future__ import annotations

from insurance_pipeline.daily_run import _contact_strength, _names_match


def test_verified_when_name_plus_email() -> None:
    assert _contact_strength(
        "Acme Inc", "Jane Doe", "jane@acme.com", None
    ) == "verified"


def test_verified_when_name_plus_linkedin() -> None:
    assert _contact_strength(
        "Acme Inc", "Jane Doe", None, "http://linkedin.com/in/jane"
    ) == "verified"


def test_partial_when_name_only() -> None:
    assert _contact_strength("Acme Inc", "Jane Doe", None, None) == "partial"


def test_cold_when_no_dm_name() -> None:
    assert _contact_strength("Acme Inc", None, None, None) == "cold"
    assert _contact_strength("Acme Inc", "", None, None) == "cold"


def test_cold_when_dm_name_duplicates_company_name() -> None:
    """Owner-operator filed under their own personal name → DM name
    just echoes the lead title. Badge resolves to Cold."""
    assert _contact_strength(
        "JOSEPH CERRONE", "JOSEPH CERRONE", None, None
    ) == "cold"
    # Even if Apollo somehow returned a contact channel for a
    # name-duplicate lead, still cold (the panel would just echo
    # the company name).
    assert _contact_strength(
        "GEDEON K MAFU", "Gedeon K Mafu", "fake@example.com", None
    ) == "cold"


def test_names_match_ignores_case_and_punctuation() -> None:
    assert _names_match("Acme, Inc.", "ACME INC")
    assert _names_match("JOSEPH CERRONE", "Joseph Cerrone")
    assert not _names_match("Acme Inc", "Acme Corp")


def test_names_match_handles_nulls() -> None:
    assert not _names_match(None, "Joseph")
    assert not _names_match("Joseph", None)
    assert not _names_match(None, None)

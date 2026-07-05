"""Fractional job boards — slug parsing, date extraction, finance filter.

No live HTTP: exercises the pure helpers.
"""

from __future__ import annotations

from datetime import datetime

from cfo_pipeline.sources import fractional_boards as fb


def test_split_slug_extracts_role_and_company():
    assert fb._fj_split_slug("controller-at-anderson-lock-safe") == (
        "Controller", "Anderson Lock Safe",
    )
    assert fb._fj_split_slug("chief-finance-officer-at-the-healthy-back-institute") == (
        "Chief Finance Officer", "The Healthy Back Institute",
    )


def test_split_slug_skips_anonymized_companies():
    assert fb._fj_split_slug("controller-at-a-salesforce-based-saas-tool") is None
    assert fb._fj_split_slug("controller-at-an-it-msp-rollup") is None
    assert fb._fj_split_slug("cfo-at-a-public-sector-tech-marketplace") is None


def test_split_slug_handles_missing_at():
    assert fb._fj_split_slug("just-a-role-slug") is None


def test_finance_slug_filter():
    assert fb._FJ_FINANCE_SLUG_RE.search("cfo-at-vowchedme")
    assert fb._FJ_FINANCE_SLUG_RE.search("controller-at-ambio")
    assert fb._FJ_FINANCE_SLUG_RE.search("chief-accounting-officer-at-softledger")
    assert fb._FJ_FINANCE_SLUG_RE.search("head-of-finance-at-acme")
    # Non-finance fractional roles are excluded:
    assert not fb._FJ_FINANCE_SLUG_RE.search("cmo-at-acme")
    assert not fb._FJ_FINANCE_SLUG_RE.search("account-executive-at-acme")


def test_page_date_extraction():
    html = "<p>Published: Mon Jun 22 2026 19:29:30 GMT+0000 (Coordinated ...)</p>"
    assert fb._fj_page_date(html) == datetime(2026, 6, 22)
    assert fb._fj_page_date("<p>no date here</p>") is None

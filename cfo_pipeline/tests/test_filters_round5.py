"""Round-5 filters: finance-vertical operators + null-headcount
foreign-subsidiary proxy + expanded megacorp brand denylist."""

from __future__ import annotations

from datetime import datetime

from cfo_pipeline import enrichment
from cfo_pipeline.models import Lead, Signal, SignalType, SourceName


def _lead(name: str, *, headcount: int | None = None) -> Lead:
    return Lead(
        name=name, name_key=name.lower(), headcount=headcount,
        signals=[Signal(
            type=SignalType.JOB_POSTED_FINANCE_LEAD, source=SourceName.JOBS,
            captured_at=datetime(2026, 7, 1), payload={"title": "Controller"},
        )],
    )


def test_finance_vertical_operators_excluded():
    for n in ("Solvay Bank", "North American Savings Bank", "Salt Creek Capital",
              "DayMark Wealth Partners", "Capital Blue Cross", "Acme Insurance Group",
              "Blackstone Private Equity", "Larson Financial", "Evergreen Credit Union"):
        assert enrichment._is_finance_vertical(n), n


def test_fintech_products_preserved():
    for n in ("Stripe", "Plaid", "Affirm", "Ramp", "Mercury", "Brex", "DataBank"):
        assert not enrichment._is_finance_vertical(n), n


def test_nonfinancial_banks_preserved():
    for n in ("Second Harvest Food Bank", "Red Cross Blood Bank", "The Milk Bank"):
        assert not enrichment._is_finance_vertical(n), n


def test_foreign_subsidiary_names():
    for n in ("Canon U.S.A., Inc.", "Würth Industry USA", "JCB North America",
              "KARL STORZ Endoscopy - America", "UZIN UTZ North America"):
        assert enrichment._is_foreign_subsidiary_name(n), n
    # "American" (not "America") must not match:
    assert not enrichment._is_foreign_subsidiary_name("Pan American Life Group")


def test_subsidiary_proxy_gated_on_null_headcount():
    # unsized subsidiary-named lead -> excluded
    assert enrichment._disqualification_reason(
        _lead("UZIN UTZ North America", headcount=None)
    ) == "likely_oversized_subsidiary"
    # same suffix but we DID size it small -> kept
    assert enrichment._disqualification_reason(
        _lead("Tiny Widgets USA", headcount=20)
    ) is None


def test_megacorp_brand_denylist():
    for n in ("Canon Solutions America", "Würth Industry North America",
              "SXSW", "Hengli Group", "JCB North America", "Karl Storz US"):
        assert enrichment._is_megacorp_subsidiary(n), n


def test_finance_vertical_via_full_disqualifier():
    assert enrichment._disqualification_reason(_lead("Solvay Bank")) == "finance_vertical"
    assert enrichment._disqualification_reason(_lead("Salt Creek Capital")) == "finance_vertical"

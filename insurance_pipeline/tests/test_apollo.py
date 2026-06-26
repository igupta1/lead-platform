"""Apollo scoring tests for the insurance pipeline.

The insurance Apollo module heavily prefers Owner / CFO / COO over IT
roles — the opposite of msp_pipeline's bias. These tests pin that
behavior so a future refactor can't silently flip it back.
"""

from __future__ import annotations

from typing import Any

import pytest

from insurance_pipeline.apollo import (
    _pick_best,
    _score_person,
    find_decision_maker,
    is_configured,
)


def _person(title: str, seniority: str = "") -> dict[str, Any]:
    return {"title": title, "seniority": seniority}


def test_is_configured_false_when_unset() -> None:
    assert is_configured() is False


def test_find_decision_maker_raises_when_unconfigured() -> None:
    with pytest.raises(RuntimeError, match="APOLLO_API_KEY"):
        find_decision_maker("Acme", "acme.com")


def test_owner_beats_cio_at_small_business() -> None:
    # At an FL SunBiz lead with both a CIO and an Owner, the Owner is
    # the insurance buyer. (Opposite of msp_pipeline.)
    owner = _person("Owner", "owner")
    cio = _person("Chief Information Officer", "c_suite")
    assert _score_person(owner) >= _score_person(cio)


def test_cfo_beats_coo_at_same_seniority() -> None:
    cfo = _person("Chief Financial Officer", "c_suite")
    coo = _person("Chief Operating Officer", "c_suite")
    assert _score_person(cfo) > _score_person(coo)


def test_cfo_beats_hr_director() -> None:
    cfo = _person("Chief Financial Officer", "c_suite")
    hr = _person("HR Director", "director")
    assert _score_person(cfo) > _score_person(hr)


def test_safety_director_recognized_for_fmcsa_leads() -> None:
    # Mid-size trucking carriers commonly list a Safety Director —
    # they're the insurance/compliance interface and a legitimate DM.
    safety = _person("Safety Director", "director")
    assert _score_person(safety) > 0


def test_sales_disqualified() -> None:
    sales = _person("VP of Sales", "vp")
    assert _score_person(sales) < 0


def test_pick_best_returns_owner_at_small_biz() -> None:
    people = [
        _person("IT Manager", "manager"),
        _person("Office Manager", "manager"),
        _person("Owner", "owner"),
        _person("VP of Sales", "vp"),  # disqualified
    ]
    chosen = _pick_best(people)
    assert chosen is not None
    assert chosen["title"] == "Owner"


def test_pick_best_returns_cfo_at_mid_size() -> None:
    people = [
        _person("HR Director", "director"),
        _person("Office Manager", "manager"),
        _person("Chief Financial Officer", "c_suite"),
        _person("VP of Sales", "vp"),
    ]
    chosen = _pick_best(people)
    assert chosen is not None
    assert chosen["title"] == "Chief Financial Officer"


def test_pick_best_empty_returns_none() -> None:
    assert _pick_best([]) is None

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from msp_pipeline import apollo
from msp_pipeline.apollo import (
    Result,
    _pick_best,
    _score_person,
    find_decision_maker,
    is_configured,
)


# --- is_configured ---------------------------------------------------------


def test_is_configured_false_when_unset() -> None:
    assert is_configured() is False


def test_is_configured_true_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APOLLO_API_KEY", "k")
    assert is_configured() is True


def test_find_decision_maker_raises_when_unconfigured() -> None:
    with pytest.raises(RuntimeError, match="APOLLO_API_KEY"):
        find_decision_maker("Acme", "acme.com")


# --- _score_person ---------------------------------------------------------


def _person(title: str, seniority: str = "") -> dict[str, Any]:
    return {"title": title, "seniority": seniority}


def test_it_csuite_beats_non_it_csuite() -> None:
    cto = _person("Chief Technology Officer", "c_suite")
    ceo = _person("Chief Executive Officer", "c_suite")
    assert _score_person(cto) > _score_person(ceo)


def test_csuite_beats_director_at_same_it_focus() -> None:
    cio = _person("Chief Information Officer", "c_suite")
    it_director = _person("Director of Information Technology", "director")
    assert _score_person(cio) > _score_person(it_director)


def test_director_of_it_beats_director_of_ops() -> None:
    it_dir = _person("Director of IT", "director")
    ops_dir = _person("Director of Operations", "director")
    assert _score_person(it_dir) > _score_person(ops_dir)


def test_pick_best_returns_highest_score() -> None:
    people = [
        _person("CEO", "c_suite"),
        _person("Office Manager", "manager"),
        _person("VP of Information Technology", "vp"),  # IT focus + VP
    ]
    chosen = _pick_best(people)
    assert chosen is not None
    assert chosen["title"] == "VP of Information Technology"


def test_pick_best_empty_returns_none() -> None:
    assert _pick_best([]) is None


# --- find_decision_maker (HTTP-mocked) -------------------------------------


def _mock_post(monkeypatch: pytest.MonkeyPatch, route_responses: dict[str, dict[str, Any]]) -> MagicMock:
    """Patch requests.post so each call to a known apollo path returns the
    canned JSON body. ``route_responses`` keys are URL substrings."""
    post_mock = MagicMock()

    def fake_post(url: str, **kwargs: Any) -> MagicMock:
        for needle, body in route_responses.items():
            if needle in url:
                resp = MagicMock()
                resp.status_code = 200
                resp.raise_for_status = MagicMock()
                resp.json = MagicMock(return_value=body)
                return resp
        # Unknown route — surface as 404 to fail loudly in tests
        resp = MagicMock()
        resp.status_code = 404
        resp.raise_for_status = MagicMock(
            side_effect=requests.HTTPError(f"unexpected route: {url}")
        )
        return resp

    post_mock.side_effect = fake_post
    monkeypatch.setattr(apollo.requests, "post", post_mock)
    return post_mock


def test_find_dm_returns_empty_when_org_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APOLLO_API_KEY", "k")
    _mock_post(
        monkeypatch,
        {
            "organizations/enrich": {"organization": None},
            "mixed_companies/search": {"organizations": []},
        },
    )
    result = find_decision_maker("Mystery Co", "mystery.com")
    assert result == Result()
    assert result.org_found is False


def test_find_dm_org_found_no_people(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APOLLO_API_KEY", "k")
    _mock_post(
        monkeypatch,
        {
            "organizations/enrich": {
                "organization": {
                    "id": "org_abc",
                    "estimated_num_employees": 80,
                }
            },
            "mixed_people/api_search": {"people": []},
        },
    )
    result = find_decision_maker("Tiny Co", "tiny.com")
    assert result.org_found is True
    assert result.dm_found is False
    assert result.headcount == 80


def test_find_dm_full_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APOLLO_API_KEY", "k")
    _mock_post(
        monkeypatch,
        {
            "organizations/enrich": {
                "organization": {
                    "id": "org_skio",
                    "estimated_num_employees": 30,
                }
            },
            "mixed_people/api_search": {
                "people": [
                    {
                        "id": "person_andrew",
                        "first_name": "Andrew",
                        "title": "Chief Technology Officer",
                        "seniority": "c_suite",
                    },
                    {
                        "id": "person_ceo",
                        "first_name": "Sara",
                        "title": "Chief Executive Officer",
                        "seniority": "c_suite",
                    },
                ]
            },
            "people/match": {
                "person": {
                    "id": "person_andrew",
                    "name": "Andrew Chen",
                    "title": "Chief Technology Officer",
                    "email": "andrew@skio.com",
                    "email_status": "verified",
                    "linkedin_url": "http://www.linkedin.com/in/andrewmnchen",
                }
            },
        },
    )
    result = find_decision_maker("Skio", "skio.com")
    assert result.org_found is True
    assert result.dm_found is True
    assert result.dm_name == "Andrew Chen"
    assert result.dm_title == "Chief Technology Officer"
    assert result.dm_email == "andrew@skio.com"
    assert result.dm_linkedin_url == "http://www.linkedin.com/in/andrewmnchen"
    assert result.apollo_person_id == "person_andrew"
    assert result.headcount == 30


def test_find_dm_falls_back_to_name_search_when_no_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APOLLO_API_KEY", "k")
    post_mock = _mock_post(
        monkeypatch,
        {
            "mixed_companies/search": {
                "organizations": [{"id": "org_xyz"}]
            },
            "mixed_people/api_search": {"people": []},
        },
    )
    result = find_decision_maker("No Domain Inc", None)
    assert result.org_found is True

    # Verify enrich-by-domain was NOT called when domain is None
    called_urls = [c.args[0] for c in post_mock.call_args_list]
    assert not any("organizations/enrich" in u for u in called_urls)
    assert any("mixed_companies/search" in u for u in called_urls)


def test_find_dm_recovers_when_enrich_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the domain-based enrich call errors, we should still fall back
    to name-based search rather than bailing."""
    monkeypatch.setenv("APOLLO_API_KEY", "k")

    def fake_post(url: str, **kwargs: Any) -> Any:
        if "organizations/enrich" in url:
            raise requests.ConnectionError("network glitch")
        if "mixed_companies/search" in url:
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(
                return_value={"organizations": [{"id": "org_fallback"}]}
            )
            return resp
        if "mixed_people/api_search" in url:
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"people": []})
            return resp
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(apollo.requests, "post", MagicMock(side_effect=fake_post))
    result = find_decision_maker("Glitchy Co", "glitch.com")
    assert result.org_found is True
    assert result.dm_found is False


def test_find_dm_match_failure_keeps_org_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If api_search picks a candidate but people/match fails (network or
    quota), we still return org_found=True so the marker logic can choose
    whether to retry — but dm_found stays False."""
    monkeypatch.setenv("APOLLO_API_KEY", "k")

    def fake_post(url: str, **kwargs: Any) -> Any:
        if "organizations/enrich" in url:
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(
                return_value={"organization": {"id": "org_x"}}
            )
            return resp
        if "mixed_people/api_search" in url:
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(
                return_value={
                    "people": [{"id": "person_x", "title": "CTO", "seniority": "c_suite"}]
                }
            )
            return resp
        if "people/match" in url:
            raise requests.HTTPError("429 over quota")
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(apollo.requests, "post", MagicMock(side_effect=fake_post))
    result = find_decision_maker("Quota Co", "quota.com")
    assert result.org_found is True
    assert result.dm_found is False
    assert result.apollo_person_id == "person_x"

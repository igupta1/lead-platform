"""Recruiter pipeline: function classifier, ATS parsers, aggregation, output."""

from __future__ import annotations

from datetime import date

from recruiter_pipeline import daily_run
from recruiter_pipeline.aggregate import aggregate, is_heavy, normalize_title
from recruiter_pipeline.functions import classify_function
from recruiter_pipeline.sources import ats
from recruiter_pipeline.sources.ats import Role

TODAY = date(2026, 7, 11)


# --- function classifier ---------------------------------------------------

def test_classify_function() -> None:
    assert classify_function("Staff Accountant") == "finance"
    assert classify_function("SOC Analyst") == "security"
    assert classify_function("Senior Software Engineer") == "engineering"
    assert classify_function("Account Executive") == "sales"
    assert classify_function("Data Scientist") == "data"
    assert classify_function("Help Desk Technician") == "it"
    assert classify_function("Underwater Basket Weaver") == "other"


# --- ATS parsers -----------------------------------------------------------

def test_parse_greenhouse() -> None:
    data = {"jobs": [
        {"title": "Software Engineer", "location": {"name": "Austin, TX"},
         "updated_at": "2026-07-01T00:00:00Z", "departments": [{"name": "Eng"}]},
    ]}
    roles = ats._parse_greenhouse(data)
    assert roles[0].title == "Software Engineer" and roles[0].location == "Austin, TX"


def test_parse_lever_epoch() -> None:
    roles = ats._parse_lever([
        {"text": "Account Executive", "categories": {"location": "Denver, CO", "team": "Sales"},
         "createdAt": 1_780_000_000_000},
    ])
    assert roles[0].title == "Account Executive"
    assert roles[0].location == "Denver, CO"
    assert roles[0].updated_at and len(roles[0].updated_at) == 10   # ISO date


def test_fetch_board_injected_http() -> None:
    def fake_http(url: str):
        assert "greenhouse" in url
        return {"jobs": [{"title": "SRE", "location": {"name": "Remote"}, "updated_at": None}]}
    roles = ats.fetch_board("greenhouse", "acme", http_get_json=fake_http)
    assert len(roles) == 1 and roles[0].title == "SRE"


def test_fetch_board_swallows_errors() -> None:
    def boom(url: str):
        raise RuntimeError("network down")
    assert ats.fetch_board("lever", "acme", http_get_json=boom) == []


# --- aggregation -----------------------------------------------------------

def test_normalize_title_collapses_seniority() -> None:
    assert normalize_title("Sr. Software Engineer II (Remote)") == "software engineer"
    assert normalize_title("Staff Software Engineer") == "software engineer"


def test_aggregate_counts_unique_roles_and_functions() -> None:
    roles = [
        Role("Senior Software Engineer", "Austin, TX", "2026-07-01", "Eng"),
        Role("Software Engineer II", "Austin, TX", "2026-07-02", "Eng"),   # dup of above -> 1 unique
        Role("Account Executive", "Austin, TX", "2026-07-03", "Sales"),
        Role("Staff Accountant", "Austin, TX", "2026-07-04", "Finance"),
    ]
    ch = aggregate("Acme", "greenhouse", "acme", roles, today=TODAY)
    assert ch.unique_role_count == 3                       # eng (deduped), sales, finance
    assert ch.functions.get("engineering") == 1
    assert ch.city == "Austin" and ch.state == "TX"
    assert is_heavy(ch) is True


def test_aggregate_drops_out_of_window_roles() -> None:
    roles = [
        Role("Software Engineer", "Austin, TX", "2026-05-01", "Eng"),   # >30d old
        Role("Account Executive", "Austin, TX", "2026-07-05", "Sales"),
    ]
    ch = aggregate("Acme", "greenhouse", "acme", roles, today=TODAY)
    assert ch.unique_role_count == 1
    assert is_heavy(ch) is False


def test_undated_roles_are_kept() -> None:
    roles = [Role(f"Role {i}", "Austin, TX", None, None) for i in range(3)]
    ch = aggregate("Acme", "greenhouse", "acme", roles, today=TODAY)
    assert ch.unique_role_count == 3 and is_heavy(ch)


# --- output ----------------------------------------------------------------

def test_collect_and_build_output_filters_heavy() -> None:
    boards = {
        ("greenhouse", "heavyco"): {"jobs": [
            {"title": "Software Engineer", "location": {"name": "Austin, TX"}, "updated_at": "2026-07-01"},
            {"title": "Account Executive", "location": {"name": "Austin, TX"}, "updated_at": "2026-07-02"},
            {"title": "Staff Accountant", "location": {"name": "Austin, TX"}, "updated_at": "2026-07-03"},
        ]},
        ("greenhouse", "quietco"): {"jobs": [
            {"title": "Office Manager", "location": {"name": "Reno, NV"}, "updated_at": "2026-07-01"},
        ]},
    }

    def fake_http(url: str):
        for (prov, slug), payload in boards.items():
            if slug in url:
                return payload
        return {"jobs": []}

    seeds = [
        {"provider": "greenhouse", "slug": "heavyco", "company": "Heavy Co"},
        {"provider": "greenhouse", "slug": "quietco", "company": "Quiet Co"},
    ]
    hirings = daily_run.collect(seeds, today=TODAY, http_get_json=fake_http)
    out = daily_run.build_output(hirings)
    names = {l["name"] for l in out["leads"]}
    assert names == {"Heavy Co"}                          # Quiet Co (1 role) excluded
    lead = out["leads"][0]
    assert lead["unique_role_count"] == 3
    assert lead["city"] == "Austin" and lead["state"] == "TX"
    assert lead["signals"][0]["type"] == "hiring_volume"

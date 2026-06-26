import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from msp_pipeline.models import (
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)
from msp_pipeline.sources import jobs as jobs_module

_FIXTURES = Path(__file__).parent / "fixtures" / "jobs"


def _make_candidate(name: str) -> LeadCandidate:
    return LeadCandidate(
        name=name,
        initial_signal=Signal(
            type=SignalType.JOB_IT_SUPPORT,
            source=SourceName.JOBS,
            captured_at=datetime.now(timezone.utc).replace(tzinfo=None),
            payload={},
        ),
    )


def test_is_recruiter_name_matches_staffing_patterns() -> None:
    assert jobs_module._is_recruiter_name("Liberty Personnel Services, Inc.")
    assert jobs_module._is_recruiter_name("Diati Staffing")
    assert jobs_module._is_recruiter_name("Eleven Recruiting")
    assert jobs_module._is_recruiter_name("Ringside Talent")
    assert jobs_module._is_recruiter_name("Motion Recruitment")
    # Real companies that aren't staffing firms.
    assert not jobs_module._is_recruiter_name("Acme Corp")
    assert not jobs_module._is_recruiter_name("Software Solutions LLC")
    assert not jobs_module._is_recruiter_name("Berger & Williams, LLP")


def test_classify_job_title() -> None:
    cases: list[tuple[str, SignalType | None]] = [
        ("Help Desk Technician", SignalType.JOB_IT_SUPPORT),
        ("Director of IT", SignalType.JOB_IT_LEADERSHIP),
        ("CISO", SignalType.EXEC_HIRED),
        ("Chief Information Security Officer", SignalType.EXEC_HIRED),
        ("Senior Security Engineer", SignalType.JOB_SECURITY),
        ("DevOps Engineer", SignalType.JOB_CLOUD_DEVOPS),
        ("Cloud Engineer (AWS)", SignalType.JOB_CLOUD_DEVOPS),
        ("VP of IT", SignalType.EXEC_HIRED),
        ("VP of Sales", None),
        ("Plumber", None),
    ]
    for title, expected in cases:
        assert jobs_module._classify_job_title(title) == expected, f"failed for {title!r}"


def test_jobs_jobspy_parses_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("msp_pipeline.sources.jobs._JOB_QUERIES", ("any",))
    fixture = json.loads((_FIXTURES / "jobspy.json").read_text())
    df = pd.DataFrame(fixture)

    since = datetime(2020, 1, 1)
    with patch.object(jobs_module.jobspy, "scrape_jobs", return_value=df):
        candidates = jobs_module._fetch_from_jobspy(since)

    by_name = {c.name: c.initial_signal.type for c in candidates}
    assert by_name == {
        "Acme Manufacturing": SignalType.JOB_IT_SUPPORT,
        "Beta Healthcare": SignalType.EXEC_HIRED,
        "Gamma Logistics": SignalType.JOB_CLOUD_DEVOPS,
    }


def test_jobs_adzuna_parses_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("msp_pipeline.sources.jobs._JOB_QUERIES", ("any",))
    monkeypatch.setenv("ADZUNA_APP_ID", "test")
    monkeypatch.setenv("ADZUNA_APP_KEY", "test")
    fixture = json.loads((_FIXTURES / "adzuna.json").read_text())

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = fixture

    since = datetime(2020, 1, 1)
    with patch.object(jobs_module.requests, "get", return_value=response):
        candidates = jobs_module._fetch_from_adzuna(since)

    by_name = {c.name: c.initial_signal.type for c in candidates}
    assert by_name == {
        "Delta Corp": SignalType.JOB_IT_SUPPORT,
        "Echo Systems": SignalType.JOB_CLOUD_DEVOPS,
    }


def test_jobs_adzuna_skips_when_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    candidates = jobs_module._fetch_from_adzuna(datetime(2020, 1, 1))
    assert candidates == []


def test_jobs_hn_parses_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("msp_pipeline.sources.jobs._JOB_QUERIES", ("any",))
    fixture = json.loads((_FIXTURES / "hn_algolia.json").read_text())

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = fixture

    since = datetime(2020, 1, 1)
    with patch.object(jobs_module.requests, "get", return_value=response):
        candidates = jobs_module._fetch_from_hn(since)

    by_name = {c.name: c.initial_signal.type for c in candidates}
    assert by_name == {
        "Hotel Inc": SignalType.EXEC_HIRED,
        "India Cybersecurity": SignalType.JOB_SECURITY,
    }


def test_jobs_fetch_aggregates_and_continues_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADZUNA_APP_ID", "test")
    monkeypatch.setenv("ADZUNA_APP_KEY", "test")

    a, b, c = _make_candidate("Acme A"), _make_candidate("Beta B"), _make_candidate("Gamma C")

    with patch.object(jobs_module, "_fetch_from_jobspy", return_value=[a]), \
         patch.object(jobs_module, "_fetch_from_adzuna", return_value=[b]), \
         patch.object(jobs_module, "_fetch_from_hn", return_value=[c]):
        names = {x.name for x in jobs_module.fetch(since=datetime(2020, 1, 1))}
    assert names == {"Acme A", "Beta B", "Gamma C"}

    def boom(_since: datetime) -> list[LeadCandidate]:
        raise RuntimeError("boom")

    with patch.object(jobs_module, "_fetch_from_jobspy", side_effect=boom), \
         patch.object(jobs_module, "_fetch_from_adzuna", return_value=[b]), \
         patch.object(jobs_module, "_fetch_from_hn", return_value=[c]):
        names = {x.name for x in jobs_module.fetch(since=datetime(2020, 1, 1))}
    assert names == {"Beta B", "Gamma C"}

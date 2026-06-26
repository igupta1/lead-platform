from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from msp_pipeline.models import (
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)
from msp_pipeline.sources import breaches as breaches_module

_FIXTURES = Path(__file__).parent / "fixtures" / "breaches"


def _make_candidate(name: str) -> LeadCandidate:
    return LeadCandidate(
        name=name,
        initial_signal=Signal(
            type=SignalType.BREACH_DISCLOSED,
            source=SourceName.BREACHES,
            captured_at=datetime.now(timezone.utc).replace(tzinfo=None),
            payload={},
        ),
    )


def _mock_response(content: bytes) -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.content = content
    return response


def test_breaches_ca_ag_parses_fixture() -> None:
    html = (_FIXTURES / "ca_ag.html").read_bytes()
    with patch.object(
        breaches_module.requests, "get", return_value=_mock_response(html)
    ):
        candidates = breaches_module._fetch_from_ca_ag(datetime(2020, 1, 1))

    names = {c.name for c in candidates}
    assert names == {"Victor Financial", "Whiskey Tech Inc"}
    assert all(c.initial_signal.type == SignalType.BREACH_DISCLOSED for c in candidates)


def test_breaches_me_ag_parses_fixture() -> None:
    html = (_FIXTURES / "me_ag.html").read_bytes()
    with patch.object(
        breaches_module.requests, "get", return_value=_mock_response(html)
    ):
        candidates = breaches_module._fetch_from_me_ag(datetime(2020, 1, 1))

    names = {c.name for c in candidates}
    assert names == {"X-ray Insurance", "Yankee Banking"}


def test_breaches_wa_ag_parses_fixture() -> None:
    html = (_FIXTURES / "wa_ag.html").read_bytes()
    with patch.object(
        breaches_module.requests, "get", return_value=_mock_response(html)
    ):
        candidates = breaches_module._fetch_from_wa_ag(datetime(2020, 1, 1))

    names = {c.name for c in candidates}
    assert names == {"Zulu Logistics", "Alpha Health Network"}


def test_breaches_fetch_aggregates_and_continues_on_failure() -> None:
    a = _make_candidate("CA Co")
    b = _make_candidate("ME Co")
    c = _make_candidate("WA Co")

    with patch.object(breaches_module, "_fetch_from_ca_ag", return_value=[a]), \
         patch.object(breaches_module, "_fetch_from_me_ag", return_value=[b]), \
         patch.object(breaches_module, "_fetch_from_wa_ag", return_value=[c]):
        names = {x.name for x in breaches_module.fetch(since=datetime(2020, 1, 1))}
    assert names == {"CA Co", "ME Co", "WA Co"}

    def boom(_since: datetime) -> list[LeadCandidate]:
        raise RuntimeError("boom")

    with patch.object(breaches_module, "_fetch_from_ca_ag", side_effect=boom), \
         patch.object(breaches_module, "_fetch_from_me_ag", return_value=[b]), \
         patch.object(breaches_module, "_fetch_from_wa_ag", return_value=[c]):
        names = {x.name for x in breaches_module.fetch(since=datetime(2020, 1, 1))}
    assert names == {"ME Co", "WA Co"}


def test_agency_display_name_maps_codes_and_falls_back() -> None:
    f = breaches_module.agency_display_name
    assert f("me_ag") == "the Maine Attorney General"
    assert f("ca_ag") == "the California Attorney General"
    assert f("wa_ag") == "the Washington State Attorney General"
    # Case-insensitive; unknown / missing codes degrade to a generic regulator.
    assert f("ME_AG") == "the Maine Attorney General"
    assert f("xx_ag") == "a state regulator"
    assert f("") == "a state regulator"
    assert f(None) == "a state regulator"


def test_clean_breach_entity_extracts_company_behind_agent() -> None:
    clean = breaches_module._clean_breach_entity
    # Reporting agent "on behalf of" the breached company -> keep the company.
    assert (
        clean("Cornick, Garber & Sandler, LLP on behalf of Voxx International Corporation")
        == "Voxx International Corporation"
    )
    assert clean("Smith Law Group o/b/o Acme Widgets, Inc.") == "Acme Widgets, Inc."
    # No identifiable company behind the agent -> drop the row.
    assert clean("Some Breach Response Firm on behalf of its clients") is None
    assert clean("Law Firm LLP on behalf of multiple individuals") is None
    # Ordinary entity is returned unchanged.
    assert clean("Sandhills Medical Foundation") == "Sandhills Medical Foundation"

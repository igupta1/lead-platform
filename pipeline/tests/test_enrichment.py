from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from msp_pipeline import db, enrichment
from msp_pipeline.enrichment import (
    Industry,
    _IndustryOut,
    _Lookup,
    _round_to_10,
    classify_industry,
    compute_band,
    enrich,
    lookup_company,
)
from msp_pipeline.models import (
    Lead,
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _signal(
    *,
    type: SignalType = SignalType.JOB_IT_SUPPORT,
    source: SourceName = SourceName.JOBS,
    captured_at: datetime | None = None,
    payload: dict[str, Any] | None = None,
) -> Signal:
    return Signal(
        type=type,
        source=source,
        captured_at=captured_at or _now(),
        payload=payload or {},
    )


def _candidate(name: str) -> LeadCandidate:
    return LeadCandidate(name=name, initial_signal=_signal())


def _lookup_response(
    *,
    headcount: str = "120",
    city: str = "Boston",
    state: str = "MA",
    country: str = "US",
    domain: str = "acme.com",
    is_it_vendor: str = "no",
    is_recruiter: str = "no",
    dm_name: str = "Sarah Johnson",
    dm_title: str = "Director of IT",
    value_prop: str = "Sells industrial widgets to manufacturing plants.",
) -> str:
    return (
        f"HEADCOUNT: {headcount}\n"
        f"CITY: {city}\n"
        f"STATE: {state}\n"
        f"COUNTRY: {country}\n"
        f"DOMAIN: {domain}\n"
        f"IS_IT_VENDOR: {is_it_vendor}\n"
        f"IS_RECRUITER: {is_recruiter}\n"
        f"DM_NAME: {dm_name}\n"
        f"DM_TITLE: {dm_title}\n"
        f"VALUE_PROP: {value_prop}\n"
    )


# --- Pure helpers ----------------------------------------------------------


@pytest.mark.parametrize(
    "headcount,expected",
    [
        (None, None),
        (5, "1-10"),
        (10, "11-50"),
        (50, "51-200"),
        (4999, "1001-5000"),
        (5000, "5000+"),
        (100000, "5000+"),
    ],
)
def test_compute_band(headcount: int | None, expected: str | None) -> None:
    assert compute_band(headcount) == expected


@pytest.mark.parametrize(
    "n,expected",
    [(0, 0), (4, 0), (7, 10), (23, 20), (1247, 1250), (5000, 5000)],
)
def test_round_to_10(n: int, expected: int) -> None:
    assert _round_to_10(n) == expected


# --- lookup_company --------------------------------------------------------


def test_lookup_company_parses_full_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        enrichment.llm,
        "call_gemini",
        MagicMock(return_value=_lookup_response(headcount="1247")),
    )
    lead = Lead(name="Acme Inc", name_key="acme", signals=[_signal()])
    result = lookup_company(lead)
    assert result == _Lookup(
        headcount=1250,
        city="Boston",
        state="MA",
        country="US",
        domain="acme.com",
        is_it_vendor=False,
        dm_name="Sarah Johnson",
        dm_title="Director of IT",
        value_prop="Sells industrial widgets to manufacturing plants.",
    )


def test_lookup_company_handles_unknowns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        enrichment.llm,
        "call_gemini",
        MagicMock(
            return_value=_lookup_response(
                headcount="unknown",
                city="unknown",
                state="unknown",
                country="unknown",
                domain="unknown",
                is_it_vendor="unknown",
                dm_name="unknown",
                dm_title="unknown",
                value_prop="unknown",
            )
        ),
    )
    lead = Lead(name="Mystery Co", name_key="mystery", signals=[_signal()])
    result = lookup_company(lead)
    assert result == _Lookup(
        headcount=None,
        city=None,
        state=None,
        country=None,
        domain=None,
        is_it_vendor=False,
        dm_name=None,
        dm_title=None,
        value_prop=None,
    )


# --- classify_industry -----------------------------------------------------


def test_classify_industry_returns_enum(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_call_openai(prompt: str, **kwargs: Any) -> _IndustryOut:
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return _IndustryOut(industry=Industry.FINTECH)

    monkeypatch.setattr(enrichment.llm, "call_openai", fake_call_openai)
    lead = Lead(
        name="Stripe",
        name_key="stripe",
        signals=[_signal(type=SignalType.JOB_SECURITY)],
    )
    result = classify_industry(lead)
    assert result is Industry.FINTECH
    assert "Stripe" in captured["prompt"]
    assert "job_posted_security" in captured["prompt"]
    assert captured["kwargs"]["response_model"] is _IndustryOut


# --- enrich() orchestrator -------------------------------------------------


def _insert_lead(conn: Any, name: str) -> Lead:
    lead = db.upsert_lead(conn, _candidate(name))
    assert lead.id is not None
    return lead


def test_enrich_skips_when_industry_set_and_no_new_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(tmp_path / "leads.db")
    lead = _insert_lead(conn, "Already Enriched Co")
    assert lead.id is not None
    db.update_lead(conn, lead.id, industry="fintech", country="US")
    db.append_signal(
        conn,
        lead.id,
        _signal(type=SignalType.ENRICHMENT_RUN, source=SourceName.COMPUTED),
    )

    refreshed = db.get_lead(conn, lead_id=lead.id)
    assert refreshed is not None

    gemini_mock = MagicMock()
    openai_mock = MagicMock()
    monkeypatch.setattr(enrichment.llm, "call_gemini", gemini_mock)
    monkeypatch.setattr(enrichment.llm, "call_openai", openai_mock)

    assert enrich(conn, refreshed) is True
    gemini_mock.assert_not_called()
    openai_mock.assert_not_called()


def test_enrich_re_runs_when_new_signal_after_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(tmp_path / "leads.db")
    lead = _insert_lead(conn, "Stale Enrich Co")
    assert lead.id is not None
    db.update_lead(conn, lead.id, industry="fintech", country="US")
    base = _now()
    db.append_signal(
        conn,
        lead.id,
        _signal(
            type=SignalType.ENRICHMENT_RUN,
            source=SourceName.COMPUTED,
            captured_at=base,
        ),
    )
    db.append_signal(
        conn,
        lead.id,
        _signal(
            type=SignalType.FUNDING_RAISED,
            source=SourceName.FUNDING,
            captured_at=base + timedelta(days=1),
        ),
    )

    refreshed = db.get_lead(conn, lead_id=lead.id)
    assert refreshed is not None

    monkeypatch.setattr(
        enrichment.llm,
        "call_gemini",
        MagicMock(return_value=_lookup_response(headcount="80")),
    )
    monkeypatch.setattr(
        enrichment.llm,
        "call_openai",
        MagicMock(return_value=_IndustryOut(industry=Industry.SOFTWARE_SAAS)),
    )

    assert enrich(conn, refreshed) is True

    after = db.get_lead(conn, lead_id=lead.id)
    assert after is not None
    assert after.industry == "software_saas"
    assert after.headcount == 80
    enrichment_runs = [s for s in after.signals if s.type == SignalType.ENRICHMENT_RUN]
    assert len(enrichment_runs) == 2


def test_enrich_writes_us_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(tmp_path / "leads.db")
    lead = _insert_lead(conn, "New US Co")

    monkeypatch.setattr(
        enrichment.llm,
        "call_gemini",
        MagicMock(return_value=_lookup_response(headcount="120")),
    )
    monkeypatch.setattr(
        enrichment.llm,
        "call_openai",
        MagicMock(return_value=_IndustryOut(industry=Industry.FINTECH)),
    )

    assert enrich(conn, lead) is True
    assert lead.id is not None

    after = db.get_lead(conn, lead_id=lead.id)
    assert after is not None
    assert after.industry == "fintech"
    assert after.headcount == 120
    assert after.country == "US"
    assert after.updated_at is not None
    assert lead.updated_at is not None
    assert after.updated_at >= lead.updated_at

    location_signals = [
        s for s in after.signals if s.type == SignalType.LOCATION_CAPTURED
    ]
    assert len(location_signals) == 1
    assert location_signals[0].source == SourceName.COMPUTED
    assert location_signals[0].payload == {"city": "Boston", "state": "MA"}

    enrichment_runs = [
        s for s in after.signals if s.type == SignalType.ENRICHMENT_RUN
    ]
    assert len(enrichment_runs) == 1
    assert enrichment_runs[0].source == SourceName.COMPUTED


def test_enrich_deletes_non_us_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(tmp_path / "leads.db")
    lead = _insert_lead(conn, "London Co")
    assert lead.id is not None

    openai_mock = MagicMock()
    monkeypatch.setattr(
        enrichment.llm,
        "call_gemini",
        MagicMock(return_value=_lookup_response(country="GB")),
    )
    monkeypatch.setattr(enrichment.llm, "call_openai", openai_mock)

    assert enrich(conn, lead) is False
    assert db.get_lead(conn, lead_id=lead.id) is None
    openai_mock.assert_not_called()


def test_enrich_skips_location_signal_when_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(tmp_path / "leads.db")
    lead = _insert_lead(conn, "No Loc Co")
    assert lead.id is not None

    monkeypatch.setattr(
        enrichment.llm,
        "call_gemini",
        MagicMock(
            return_value=_lookup_response(
                headcount="50", city="unknown", state="unknown", country="US"
            )
        ),
    )
    monkeypatch.setattr(
        enrichment.llm,
        "call_openai",
        MagicMock(return_value=_IndustryOut(industry=Industry.OTHER)),
    )

    assert enrich(conn, lead) is True
    after = db.get_lead(conn, lead_id=lead.id)
    assert after is not None
    location_signals = [
        s for s in after.signals if s.type == SignalType.LOCATION_CAPTURED
    ]
    assert location_signals == []


def test_enrich_persists_headcount_rounding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(tmp_path / "leads.db")
    lead = _insert_lead(conn, "Rounding Co")
    assert lead.id is not None

    monkeypatch.setattr(
        enrichment.llm,
        "call_gemini",
        MagicMock(return_value=_lookup_response(headcount="247")),
    )
    monkeypatch.setattr(
        enrichment.llm,
        "call_openai",
        MagicMock(return_value=_IndustryOut(industry=Industry.SOFTWARE_SAAS)),
    )

    enrich(conn, lead)
    after = db.get_lead(conn, lead_id=lead.id)
    assert after is not None
    assert after.headcount == 250


def test_enrich_deletes_oversized_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(tmp_path / "leads.db")
    lead = _insert_lead(conn, "Big Co")
    assert lead.id is not None

    openai_mock = MagicMock()
    monkeypatch.setattr(
        enrichment.llm,
        "call_gemini",
        MagicMock(return_value=_lookup_response(headcount="300", country="US")),
    )
    monkeypatch.setattr(enrichment.llm, "call_openai", openai_mock)

    assert enrich(conn, lead) is False
    assert db.get_lead(conn, lead_id=lead.id) is None
    openai_mock.assert_not_called()


def test_enrich_deletes_it_vendor_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT services / staffing / consulting firms are competitors, not
    targets. The is_it_vendor flag from the Gemini lookup deletes them."""
    conn = db.init_db(tmp_path / "leads.db")
    lead = _insert_lead(conn, "Sky Systems Inc")
    assert lead.id is not None

    openai_mock = MagicMock()
    monkeypatch.setattr(
        enrichment.llm,
        "call_gemini",
        MagicMock(
            return_value=_lookup_response(
                headcount="150", country="US", is_it_vendor="yes"
            )
        ),
    )
    monkeypatch.setattr(enrichment.llm, "call_openai", openai_mock)

    assert enrich(conn, lead) is False
    assert db.get_lead(conn, lead_id=lead.id) is None
    openai_mock.assert_not_called()


@pytest.mark.parametrize(
    "name",
    [
        "Toyota North America",
        "Panasonic North America",
        "Capital One",
        "Raytheon Technologies",
        "Best Western International",
        "ManpowerGroup",
        "Planned Parenthood of Greater New York",
        "Automobile Club of Southern California",
        "Dalio Family Office",
        # Large orgs that slipped the SMB cap with NULL / unverified headcount.
        "PennyMac Loan Services",
        "PennyMac Financial Services, Inc.",
        "GM Financial",
        "General Motors Financial Company",
        "Miami Marlins",
    ],
)
def test_disqualifies_enterprise_name(name: str) -> None:
    lead = Lead(name=name, name_key="x", signals=[])
    assert enrichment._disqualification_reason(lead) == "blocked_enterprise_name"


@pytest.mark.parametrize(
    "name",
    [
        "James Search Group",
        "The Everest Search Group",
        "Grayson Search Partners",
        "Jobot",
        "Tailored Management",
        "Diati Staffing",
        "Acme Executive Search",
    ],
)
def test_disqualifies_recruiter_name(name: str) -> None:
    lead = Lead(name=name, name_key="x", signals=[])
    assert enrichment._disqualification_reason(lead) == "recruiter_name"


@pytest.mark.parametrize(
    "name",
    [
        "Winona County",
        "Jacksonville Transportation Authority",
        "Chaffey Joint Union High School District",
        "Las Lomitas Elementary School District",
        "Springfield Township",
        "Metropolitan Transit Authority",
        "Clark County Sheriff",
    ],
)
def test_disqualifies_government_entity(name: str) -> None:
    lead = Lead(name=name, name_key="x", signals=[])
    assert enrichment._disqualification_reason(lead) == "government_entity"


def test_does_not_disqualify_normal_smb_names() -> None:
    # Includes near-misses for the gov filter: a business that merely contains
    # "County" mid-name, and a private water company without "District".
    for name in [
        "Capital City Auto",
        "Sandhills Medical Foundation",
        "Pioneer Legal LLP",
        "Orange County Choppers",
        "Dutchess County Bank",
    ]:
        lead = Lead(name=name, name_key="x", signals=[])
        assert enrichment._disqualification_reason(lead) is None


# --- recruiter detection by domain / email ---------------------------------


def test_is_recruiter_domain_matches_brand() -> None:
    assert enrichment._is_recruiter_domain("mackenziestuart.com")
    assert enrichment._is_recruiter_domain("careers.mackenziestuart.com")
    assert enrichment._is_recruiter_domain("jobot.com")
    # Ordinary SMB domains and blanks are left alone.
    assert not enrichment._is_recruiter_domain("chiefrivernursery.com")
    assert not enrichment._is_recruiter_domain("")
    assert not enrichment._is_recruiter_domain(None)


def test_disqualifies_recruiter_by_email_domain() -> None:
    # The displayed name ("MACKENZIE") gives no recruiter signal, but the
    # contact email's domain reveals the staffing brand (Mackenzie Stuart).
    lead = Lead(
        name="MACKENZIE",
        name_key="mackenzie",
        dm_email="david.stone@mackenziestuart.com",
        signals=[],
    )
    assert enrichment._disqualification_reason(lead) == "recruiter_name"


def test_keeps_smb_with_ordinary_email_domain() -> None:
    lead = Lead(
        name="Chief River Nursery",
        name_key="chief",
        domain="chiefrivernursery.com",
        dm_email="owner@chiefrivernursery.com",
        signals=[],
    )
    assert enrichment._disqualification_reason(lead) is None


# --- bogus funding-headline purge ------------------------------------------


def _funding_signal(feed_title: str) -> Signal:
    return _signal(
        type=SignalType.FUNDING_RAISED,
        source=SourceName.FUNDING,
        payload={"feed_title": feed_title},
    )


@pytest.mark.parametrize(
    "feed_title",
    [
        "Dominari Securities Raises $200,000,000.00 in World's Largest IPO",
        "Kodiak AI raises $100M at a steep discount, sending its stock tumbling 37%",
    ],
)
def test_disqualifies_bogus_funding_only_lead(feed_title: str) -> None:
    lead = Lead(name="Bogus Co", name_key="bogus", signals=[_funding_signal(feed_title)])
    assert enrichment._disqualification_reason(lead) == "bogus_funding_signal"


def test_keeps_real_funding_lead() -> None:
    lead = Lead(
        name="Real Co",
        name_key="real",
        signals=[_funding_signal("Real Co raises $25M Series B")],
    )
    assert enrichment._disqualification_reason(lead) is None


def test_keeps_lead_with_bogus_funding_but_other_qualifying_signal() -> None:
    # A genuine security posting keeps the lead even though a junk funding
    # headline rode in on the same company — drop the headline, not the lead.
    lead = Lead(
        name="Mixed Co",
        name_key="mixed",
        signals=[
            _funding_signal("Mixed Co stock tumbling 50% after earnings"),
            _signal(
                type=SignalType.JOB_SECURITY,
                source=SourceName.JOBS,
                payload={"title": "Security Engineer"},
            ),
        ],
    )
    assert enrichment._disqualification_reason(lead) is None


def test_keeps_lead_with_unreadable_funding_headline() -> None:
    # A blank headline can't be judged bogus; keep the lead conservatively.
    lead = Lead(name="Blank Co", name_key="blank", signals=[_funding_signal("")])
    assert enrichment._disqualification_reason(lead) is None


def test_enrich_deletes_recruiter_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A company Gemini flags as a staffing / recruiting firm is deleted —
    the real hiring company is hidden behind the recruiter."""
    conn = db.init_db(tmp_path / "leads.db")
    lead = _insert_lead(conn, "Mystery Recruiter Co")
    assert lead.id is not None

    openai_mock = MagicMock()
    monkeypatch.setattr(
        enrichment.llm,
        "call_gemini",
        MagicMock(
            return_value=_lookup_response(
                headcount="40", country="US", is_recruiter="yes"
            )
        ),
    )
    monkeypatch.setattr(enrichment.llm, "call_openai", openai_mock)

    assert enrich(conn, lead) is False
    assert db.get_lead(conn, lead_id=lead.id) is None
    openai_mock.assert_not_called()


def test_insurance_is_a_valid_industry() -> None:
    assert Industry("insurance") is Industry.INSURANCE


def test_enrich_writes_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(tmp_path / "leads.db")
    lead = _insert_lead(conn, "Domain Co")
    assert lead.id is not None

    monkeypatch.setattr(
        enrichment.llm,
        "call_gemini",
        MagicMock(
            return_value=_lookup_response(
                headcount="80", country="US", domain="domain-co.com"
            )
        ),
    )
    monkeypatch.setattr(
        enrichment.llm,
        "call_openai",
        MagicMock(return_value=_IndustryOut(industry=Industry.OTHER)),
    )

    assert enrich(conn, lead) is True
    after = db.get_lead(conn, lead_id=lead.id)
    assert after is not None
    assert after.domain == "domain-co.com"


def test_enrich_writes_dm_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(tmp_path / "leads.db")
    lead = _insert_lead(conn, "DM Co")
    assert lead.id is not None

    monkeypatch.setattr(
        enrichment.llm,
        "call_gemini",
        MagicMock(
            return_value=_lookup_response(
                headcount="80",
                country="US",
                dm_name="Jane Smith",
                dm_title="VP of IT",
            )
        ),
    )
    monkeypatch.setattr(
        enrichment.llm,
        "call_openai",
        MagicMock(return_value=_IndustryOut(industry=Industry.OTHER)),
    )

    assert enrich(conn, lead) is True
    after = db.get_lead(conn, lead_id=lead.id)
    assert after is not None
    assert after.dm_name == "Jane Smith"
    assert after.dm_title == "VP of IT"


def test_enrich_writes_value_prop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(tmp_path / "leads.db")
    lead = _insert_lead(conn, "VP Co")
    assert lead.id is not None

    monkeypatch.setattr(
        enrichment.llm,
        "call_gemini",
        MagicMock(
            return_value=_lookup_response(
                value_prop="Pediatric clinic with 3 locations in Spokane.",
            )
        ),
    )
    monkeypatch.setattr(
        enrichment.llm,
        "call_openai",
        MagicMock(return_value=_IndustryOut(industry=Industry.HEALTHCARE)),
    )

    assert enrich(conn, lead) is True
    after = db.get_lead(conn, lead_id=lead.id)
    assert after is not None
    assert after.value_prop == "Pediatric clinic with 3 locations in Spokane."


def test_enrich_preserves_apollo_dm_fields_when_marker_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once Apollo has enriched a lead, Gemini re-enrichment must NOT
    overwrite the DM data — Apollo's records are higher quality."""
    from msp_pipeline.models import SourceName

    conn = db.init_db(tmp_path / "leads.db")
    lead = _insert_lead(conn, "Apollo'd Co")
    assert lead.id is not None
    db.update_lead(
        conn,
        lead.id,
        dm_name="Real Apollo Person",
        dm_title="CTO",
        dm_email="real@apollod-co.com",
        headcount=80,
        domain="apollod-co.com",
    )
    db.append_signal(
        conn,
        lead.id,
        Signal(
            type=SignalType.APOLLO_ENRICHED,
            source=SourceName.APOLLO,
            captured_at=_now(),
            payload={"dm_found": True},
        ),
    )

    refreshed = db.get_lead(conn, lead_id=lead.id)
    assert refreshed is not None

    monkeypatch.setattr(
        enrichment.llm,
        "call_gemini",
        MagicMock(
            return_value=_lookup_response(
                headcount="500",  # different from Apollo's 80
                dm_name="Wrong Gemini Guess",
                dm_title="Office Manager",
                domain="different.com",
                value_prop="Software firm specializing in real estate analytics.",
            )
        ),
    )
    monkeypatch.setattr(
        enrichment.llm,
        "call_openai",
        MagicMock(return_value=_IndustryOut(industry=Industry.SOFTWARE_SAAS)),
    )

    assert enrich(conn, refreshed, force=True) is True
    after = db.get_lead(conn, lead_id=lead.id)
    assert after is not None
    # Apollo-sourced fields untouched
    assert after.dm_name == "Real Apollo Person"
    assert after.dm_title == "CTO"
    assert after.dm_email == "real@apollod-co.com"
    assert after.headcount == 80
    assert after.domain == "apollod-co.com"
    # Gemini-only fields updated
    assert after.industry == "software_saas"
    assert after.value_prop == "Software firm specializing in real estate analytics."


def test_enrich_uses_apollo_headcount_for_smb_cap_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini may guess wrong on headcount; if Apollo has set a smaller
    real value, don't delete the lead based on Gemini's guess."""
    from msp_pipeline.models import SourceName

    conn = db.init_db(tmp_path / "leads.db")
    lead = _insert_lead(conn, "Right Sized Co")
    assert lead.id is not None
    db.update_lead(conn, lead.id, headcount=80)
    db.append_signal(
        conn,
        lead.id,
        Signal(
            type=SignalType.APOLLO_ENRICHED,
            source=SourceName.APOLLO,
            captured_at=_now(),
            payload={"dm_found": True},
        ),
    )
    refreshed = db.get_lead(conn, lead_id=lead.id)
    assert refreshed is not None

    monkeypatch.setattr(
        enrichment.llm,
        "call_gemini",
        MagicMock(return_value=_lookup_response(headcount="500")),  # wrong
    )
    monkeypatch.setattr(
        enrichment.llm,
        "call_openai",
        MagicMock(return_value=_IndustryOut(industry=Industry.OTHER)),
    )

    assert enrich(conn, refreshed, force=True) is True
    after = db.get_lead(conn, lead_id=lead.id)
    assert after is not None
    assert after.headcount == 80  # Apollo's value preserved


def test_enrich_keeps_lead_at_exactly_250(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(tmp_path / "leads.db")
    lead = _insert_lead(conn, "Edge Co")
    assert lead.id is not None

    monkeypatch.setattr(
        enrichment.llm,
        "call_gemini",
        MagicMock(return_value=_lookup_response(headcount="250")),
    )
    monkeypatch.setattr(
        enrichment.llm,
        "call_openai",
        MagicMock(return_value=_IndustryOut(industry=Industry.OTHER)),
    )

    assert enrich(conn, lead) is True
    after = db.get_lead(conn, lead_id=lead.id)
    assert after is not None
    assert after.headcount == 250


def test_enrich_keeps_lead_with_unknown_headcount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(tmp_path / "leads.db")
    lead = _insert_lead(conn, "Mystery Co")
    assert lead.id is not None

    monkeypatch.setattr(
        enrichment.llm,
        "call_gemini",
        MagicMock(
            return_value=_lookup_response(
                headcount="unknown", city="unknown", state="unknown", country="US"
            )
        ),
    )
    monkeypatch.setattr(
        enrichment.llm,
        "call_openai",
        MagicMock(return_value=_IndustryOut(industry=Industry.OTHER)),
    )

    assert enrich(conn, lead) is True
    after = db.get_lead(conn, lead_id=lead.id)
    assert after is not None
    assert after.headcount is None


def test_enrich_force_re_enriches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.init_db(tmp_path / "leads.db")
    lead = _insert_lead(conn, "Force Co")
    assert lead.id is not None
    db.update_lead(conn, lead.id, industry="fintech", country="US")
    db.append_signal(
        conn,
        lead.id,
        _signal(type=SignalType.ENRICHMENT_RUN, source=SourceName.COMPUTED),
    )

    refreshed = db.get_lead(conn, lead_id=lead.id)
    assert refreshed is not None

    gemini_mock = MagicMock(return_value=_lookup_response(headcount="100"))
    openai_mock = MagicMock(return_value=_IndustryOut(industry=Industry.HEALTHCARE))
    monkeypatch.setattr(enrichment.llm, "call_gemini", gemini_mock)
    monkeypatch.setattr(enrichment.llm, "call_openai", openai_mock)

    assert enrich(conn, refreshed, force=True) is True
    gemini_mock.assert_called_once()
    openai_mock.assert_called_once()

    after = db.get_lead(conn, lead_id=lead.id)
    assert after is not None
    assert after.industry == "healthcare"
    assert after.headcount == 100

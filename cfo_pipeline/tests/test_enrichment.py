"""Enrichment pure-code filter tests (no LLM calls).

Exercises ``_disqualification_reason`` against the lead shapes that
should be purged — CFO competitors, megacorps, oversized headcount,
form-D-noise patterns. The Gemini lookup path is not exercised here
(network-bound).
"""

from __future__ import annotations

from datetime import datetime

from cfo_pipeline import enrichment
from cfo_pipeline.models import Lead, Signal, SignalType, SourceName


def _bare_lead(name: str, **kwargs) -> Lead:
    return Lead(
        id=1,
        name=name,
        name_key=name.lower(),
        **kwargs,
    )


def test_cfo_competitor_names_disqualified():
    names = [
        "Acme Fractional CFO",
        "Outsourced CFO Group",
        "CFO Services Inc",
        "Smith CPAs",
        "Acme Bookkeeping Services",
        "Wealth Advisors LLC",
        "Smith Tax Services",
    ]
    for n in names:
        reason = enrichment._disqualification_reason(_bare_lead(n))
        assert reason == "cfo_competitor_name", (n, reason)


def test_oversized_headcount_disqualified():
    lead = _bare_lead("Big Co", headcount=120)
    assert enrichment._disqualification_reason(lead) == "oversized=120"


def test_at_cap_passes():
    # SMB cap is 75 — exactly 75 should pass (it's the cap, not over).
    lead = _bare_lead("Acme Robotics", headcount=75)
    assert enrichment._disqualification_reason(lead) is None


def test_megacorp_subsidiary_disqualified():
    lead = _bare_lead("Microsoft Cloud Services")
    assert enrichment._disqualification_reason(lead) == "megacorp_subsidiary"


def test_megacorp_brand_names_disqualified():
    """Consumer-brand surfaces that aren't caught by Gemini's
    has_full_time_cfo lookup or Apollo's headcount cap. The Tinder
    case from the first uncapped run motivated this list."""
    brands = [
        "Tinder",
        "Hinge",
        "Instagram",
        "WhatsApp",
        "LinkedIn",
        "GitHub",
        "Twitch",
        "Hulu",
        "Whole Foods",
        "Slack",
    ]
    for b in brands:
        assert enrichment._disqualification_reason(_bare_lead(b)) == "megacorp_subsidiary", b


def test_brand_match_is_case_insensitive_and_trimmed():
    assert enrichment._is_megacorp_subsidiary("  tinder  ")
    assert enrichment._is_megacorp_subsidiary("TINDER")


def test_unrelated_name_not_misflagged_as_brand():
    """The brand list is exact-match — substrings must not trigger."""
    assert not enrichment._is_megacorp_subsidiary("Slacker Records")
    assert not enrichment._is_megacorp_subsidiary("Tinder Box Pizza")
    assert not enrichment._is_megacorp_subsidiary("Hinge Health")  # actual standalone co


def test_form_d_noise_only_disqualifies_form_d_leads():
    """The vintage-year regex must not nuke an operating company that
    happens to have a year in its name unless the lead actually has a
    Form D signal."""
    captured = datetime(2026, 5, 16, 12, 0, 0)

    # Has Form D signal AND name matches vintage-year pattern -> drop.
    lead_form_d = _bare_lead(
        "Summit Ridge 2024 LLC",
        signals=[
            Signal(
                type=SignalType.FUNDING_RAISED,
                source=SourceName.EDGAR_FORM_D,
                captured_at=captured,
                payload={"filing_type": "Form D"},
            )
        ],
    )
    assert enrichment._disqualification_reason(lead_form_d) == "form_d_noise_pattern"

    # Same name but no Form D signal (came from jobs) -> not Form-D-noise
    # disqualifier. (LP suffix would still hit financial_vehicle, so use
    # a name that's vintage-year only.)
    lead_no_form_d = _bare_lead(
        "Summit Ridge 2024 LLC",
        signals=[
            Signal(
                type=SignalType.JOB_POSTED_FINANCE_LEAD,
                source=SourceName.JOBS,
                captured_at=captured,
                payload={"title": "Controller"},
            )
        ],
    )
    # financial-vehicle regex still drops LP-suffix names, but
    # Summit Ridge 2024 LLC has "LLC" not "LP", so the financial-
    # vehicle path doesn't fire. Confirm that the form-D-noise path
    # specifically doesn't fire either.
    reason = enrichment._disqualification_reason(lead_no_form_d)
    assert reason != "form_d_noise_pattern"


def test_blocked_government_names():
    assert enrichment._disqualification_reason(
        _bare_lead("State of California")
    ) == "blocked_name_pattern"
    assert enrichment._disqualification_reason(
        _bare_lead("Department of Energy")
    ) == "blocked_name_pattern"


def test_blocked_university_names():
    assert enrichment._disqualification_reason(
        _bare_lead("Stanford University")
    ) == "blocked_name_pattern"


def test_blocked_megacorp_domains():
    assert enrichment._disqualification_reason(
        _bare_lead("Random Subsidiary", domain="microsoft.com")
    ) == "blocked_domain=microsoft.com"


def test_zero_headcount_disqualified():
    lead = _bare_lead("Ghost Co", headcount=0)
    assert enrichment._disqualification_reason(lead) == "zero_headcount"


def test_clean_lead_passes():
    lead = _bare_lead("Acme Robotics", domain="acme.com", headcount=25)
    assert enrichment._disqualification_reason(lead) is None

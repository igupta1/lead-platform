"""Enrichment purge logic — the pure-code disqualifier ladder that decides who
stays in the buyer inventory. This is the correctness-critical filter, so every
branch is exercised AND a set of real SMBs is checked to trip none of them."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from leadgen import db
from leadgen.enrichment import (
    _disqualification_reason,
    _insight_is_service_provider,
    _is_cfo_competitor,
)
from leadgen.models import Lead, Signal, SignalType, SourceName


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _lead(name: str, *, domain: str | None = None, headcount: int | None = None,
          funding: bool = False) -> Lead:
    signals = []
    if funding:
        signals.append(Signal(
            type=SignalType.FUNDING_FORM_D, source=SourceName.EDGAR_FORM_D,
            captured_at=_now(), event_date=_now(),
            evidence_text="Form D filing", source_url="https://sec.gov/x",
        ))
    return Lead(name=name, name_key=db.name_key(name), domain=domain,
                headcount=headcount, signals=signals)


# --- competitor checks (name + insight backstop) ---------------------------


def test_cfo_competitor_name_catches_vcfo_brand():
    assert _is_cfo_competitor("vcfo") is True
    assert _is_cfo_competitor("v-CFO Partners") is True
    assert _is_cfo_competitor("Acme Bookkeeping Services LLC") is True
    assert _is_cfo_competitor("Redwood Robotics") is False


def test_insight_backstop_flags_service_providers():
    assert _insight_is_service_provider(
        "Provides outsourced finance, including fractional CFO services."
    ) is True
    assert _insight_is_service_provider("A bookkeeping services firm for SMBs.") is True
    assert _insight_is_service_provider("Operates a family dental practice in Ohio.") is False
    assert _insight_is_service_provider(None) is False


# --- the disqualifier ladder: one representative company per branch --------
# First-match-wins, so each case is crafted to trigger exactly its branch.

_DISQUALIFY_CASES = [
    (_lead("Acme Inc", domain="google.com"), "blocked_domain"),
    (_lead("Acme Inc", domain="foo.edu"), "blocked_domain"),
    (_lead("Redstone Ventures"), "blocked_name_pattern"),
    (_lead("Summit Bookkeeping Services LLC"), "cfo_competitor_name"),
    (_lead("Acme Bancorp"), "financial_vehicle"),
    (_lead("Deloitte"), "megacorp_subsidiary"),
    (_lead("American Red Cross"), "oversized_ngo"),
    (_lead("Aerotek"), "recruiter_name_pattern"),
    (_lead("Toyota of Downtown"), "auto_dealer_name_pattern"),
    (_lead("Marriott Hotels"), "hotel_name_pattern"),
    (_lead("Springfield Police Department"), "public_sector_pattern"),
    (_lead("Widgetworks (WDGT)"), "public_company_ticker"),
    (_lead("First National Bank"), "finance_vertical"),
    (_lead("Riverside 278 LLC", funding=True), "form_d_noise_pattern"),
    (_lead("Bigco Widgets", headcount=500), "oversized"),
    (_lead("Ghost Co", headcount=0), "zero_headcount"),
    (_lead("Widgetco North America"), "likely_oversized_subsidiary"),
]


@pytest.mark.parametrize("lead, expected", _DISQUALIFY_CASES)
def test_disqualification_reason_fires_per_branch(lead, expected):
    reason = _disqualification_reason(lead)
    assert reason is not None and expected in reason, \
        f"{lead.name!r}: expected reason containing {expected!r}, got {reason!r}"


_CLEAN_SMBS = [
    _lead("Redwood Robotics", domain="redwoodrobotics.com", headcount=40),
    _lead("Bright Dental Care", headcount=25),
    _lead("Summit Widgets"),  # unknown headcount alone is NOT disqualifying
]


@pytest.mark.parametrize("lead", _CLEAN_SMBS)
def test_real_smbs_are_not_disqualified(lead):
    reason = _disqualification_reason(lead)
    assert reason is None, f"{lead.name!r} wrongly disqualified: {reason!r}"

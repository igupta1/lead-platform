"""Enrichment purge logic — the pure-code disqualifier ladder that decides who
stays in the buyer inventory. This is the correctness-critical filter, so every
branch is exercised AND a set of real SMBs is checked to trip none of them."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from leadgen import db, llm
from leadgen.enrichment import (
    _disqualification_reason,
    _insight_is_service_provider,
    _is_cfo_competitor,
    lookup_company,
    needs_enrichment,
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


# --- Size bands -------------------------------------------------------------
#
# An exact employee count is undiscoverable for most small private companies
# (~60% came back NULL), and a NULL size silently bypassed every size cap.
# The band is the fallback the caps actually test.


@pytest.mark.parametrize("raw,expected", [
    ("11-50", "11-50"),
    ("11-50 employees", "11-50"),
    (" 51-200 ", "51-200"),
    ("51 - 200", "51-200"),
    ("1000+", "1000+"),
    ("1,000+", "1000+"),
    ("201–1000", "201-1000"),      # en dash from a grounded answer
    ('"1-10"', "1-10"),
    # unmappable answers are unknown, never a guess -- a wrong band would
    # silently widen the cap
    ("about 40 people", None),
    ("medium sized", None),
    ("unknown", None),
    (None, None),
])
def test_parse_band(raw, expected):
    from leadgen.enrichment import _parse_band
    assert _parse_band(raw) == expected


@pytest.mark.parametrize("headcount,expected", [
    (1, "1-10"), (10, "1-10"), (11, "11-50"), (50, "11-50"),
    (200, "51-200"), (999, "201-1000"), (50_000, "1000+"), (None, None),
])
def test_band_for_exact_headcount(headcount, expected):
    from leadgen.models import band_for
    assert band_for(headcount) == expected


def test_effective_size_prefers_exact_then_band():
    from leadgen.models import effective_size
    exact = Lead(name="A", name_key="a", headcount=37, headcount_band="11-50")
    assert effective_size(exact) == 37          # exact wins when it fits the band
    banded = Lead(name="B", name_key="b", headcount_band="51-200")
    assert effective_size(banded) == 200        # band's UPPER bound (conservative)
    unknown = Lead(name="C", name_key="c")
    assert effective_size(unknown) is None      # genuinely unsized
    unmappable = Lead(name="D", name_key="d", headcount=37, headcount_band="dozens")
    assert effective_size(unmappable) == 37     # no usable band -> the count stands


@pytest.mark.parametrize("name, headcount, band", [
    # Real offenders found in the published inventory on 2026-08-02: the
    # lookup returned a small exact count next to a band that says enterprise.
    ("Texas Oncology", 10, "1000+"),
    ("Springhill Medical Health Systems", 50, "1000+"),
    ("Shipium", 90, "201-1000"),
    # The mirror case: a count far ABOVE its band.
    ("Inverted Co", 800, "1-10"),
])
def test_contradicting_headcount_and_band_reads_as_unknown(name, headcount, band):
    from leadgen.models import effective_size
    lead = Lead(name=name, name_key=name.lower(), headcount=headcount, headcount_band=band)
    assert effective_size(lead) is None


def test_contradicting_size_is_held_back_but_not_deleted():
    """The bug this closes: headcount 10 / band "1000+" used to size as 10 and
    publish an enterprise into a gift built for small companies. It must now
    read as unsized — held out of the inventory by the fail-closed projection,
    but NOT purged, since phase 2 deletes with no audit trail and the evidence
    that would justify deleting is the evidence we just called unreliable."""
    leaked = Lead(name="Texas Oncology", name_key="texasoncology",
                  headcount=10, headcount_band="1000+")
    from leadgen.models import effective_size
    assert effective_size(leaked) is None        # never published
    assert _disqualification_reason(leaked) is None   # never destroyed
    assert needs_enrichment(leaked) is True      # re-asked until it resolves


def test_oversized_reason_reports_the_deciding_size():
    """A band-sized lead used to log a misleading "oversized=50" (the raw
    headcount) while the band was what tripped the cap."""
    big = Lead(name="Big Co", name_key="bigco", headcount_band="201-1000")
    assert _disqualification_reason(big) == "oversized=1000"


def test_lookup_drops_a_headcount_that_contradicts_its_band(monkeypatch):
    """Stop generating the contradiction at the source: keep the band (the
    answer the model gives reliably), drop the exact count."""
    raw = (
        "DOMAIN: example.com\nHEADCOUNT: 10\nHEADCOUNT_BAND: 1000+\n"
        "CITY: Dallas\nSTATE: TX\nCOUNTRY: US\nINSIGHT: x\n"
    )
    monkeypatch.setattr(llm, "call_gemini", lambda *a, **k: raw)
    got = lookup_company(Lead(name="Texas Oncology", name_key="texasoncology"))
    assert got.headcount is None
    assert got.headcount_band == "1000+"


def test_lookup_keeps_a_headcount_that_agrees_with_its_band(monkeypatch):
    raw = (
        "DOMAIN: example.com\nHEADCOUNT: 40\nHEADCOUNT_BAND: 11-50\n"
        "CITY: Dallas\nSTATE: TX\nCOUNTRY: US\nINSIGHT: x\n"
    )
    monkeypatch.setattr(llm, "call_gemini", lambda *a, **k: raw)
    got = lookup_company(Lead(name="Small Co", name_key="smallco"))
    assert got.headcount == 40
    assert got.headcount_band == "11-50"


def test_enriched_but_unsized_lead_is_retried():
    """Fail-closed publishing holds unsized leads back, so an enriched lead
    that never got a size would be stranded forever without this retry."""
    stamped = _now()
    unsized = Lead(name="A", name_key="a", enriched_at=stamped)
    assert needs_enrichment(unsized) is True
    sized = Lead(name="B", name_key="b", enriched_at=stamped, headcount_band="11-50")
    assert needs_enrichment(sized) is False


def test_band_alone_trips_the_oversized_purge():
    """The whole point: no exact headcount, but a band that clears the SMB cap
    now disqualifies. Previously a NULL headcount sailed straight through."""
    big = Lead(name="Big Co", name_key="bigco", headcount_band="1000+")
    reason = _disqualification_reason(big)
    assert reason is not None and reason.startswith("oversized")

    small = Lead(name="Small Co", name_key="smallco", headcount_band="11-50")
    assert _disqualification_reason(small) is None

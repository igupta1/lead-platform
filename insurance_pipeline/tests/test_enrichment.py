"""Enrichment-disqualification tests for the insurance pipeline.

These tests exercise the pure-code disqualification path. The Gemini
lookup and Apollo enrichment paths have integration-level tests that
require live keys (run manually).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from insurance_pipeline import db, enrichment
from insurance_pipeline.models import (
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)


def _candidate(name: str) -> LeadCandidate:
    return LeadCandidate(
        name=name,
        initial_signal=Signal(
            type=SignalType.NEW_BUSINESS_FILED,
            source=SourceName.SOS_FL,
            captured_at=datetime.now(timezone.utc).replace(tzinfo=None),
            payload={"state": "FL", "filing_type": "LLC"},
        ),
    )


def test_is_insurance_vendor_recognizes_carriers_and_brokers() -> None:
    matches = [
        "Acme Insurance Agency",
        "Acme Insurance Brokers, LLC",
        "Acme Insurance Services Inc.",
        "Acme Insurance Company",
        "Acme Mutual Insurance Co",
        "Acme Casualty Group",
        "Acme Indemnity Corp",
        "Acme Reinsurance Holdings",
        "Acme MGA",
        "Acme Managing General Agent",
        "Acme Wholesale Insurance",
        "Lloyd's Underwriters",
        "Acme TPA",
        "Acme Third Party Administrator",
        "Acme Claims Adjusters",
        "Acme Adjusting Services",
        "Acme Independent Adjusters",
    ]
    for name in matches:
        assert enrichment._is_insurance_vendor(name), f"expected match: {name!r}"


def test_is_insurance_vendor_ignores_non_insurance() -> None:
    for name in (
        "Pioneer Logistics LLC",
        "Acme Manufacturing",
        "Berger & Williams, LLP",
        "Acme IT Services",
        "Cloud Solutions Group",
        "Acme Property Management",
    ):
        assert not enrichment._is_insurance_vendor(name)


def test_disqualification_reason_for_gov_domain() -> None:
    from insurance_pipeline.models import Lead

    lead = Lead(name="State DMV", name_key="state_dmv", domain="state.ca.gov")
    assert enrichment._disqualification_reason(lead) is not None


def test_disqualification_reason_oversized() -> None:
    from insurance_pipeline.models import Lead

    lead = Lead(name="Mega Corp", name_key="mega", headcount=5000)
    reason = enrichment._disqualification_reason(lead)
    assert reason is not None and reason.startswith("oversized")


def test_disqualification_reason_insurance_vendor() -> None:
    from insurance_pipeline.models import Lead

    lead = Lead(name="Acme Insurance Agency", name_key="acme")
    assert enrichment._disqualification_reason(lead) == "insurance_vendor_name"


def test_disqualification_reason_enterprise_subsidiary() -> None:
    """Subsidiaries of large multinationals whose Gemini lookup returned
    no headcount, so the SMB-cap check couldn't fire."""
    from insurance_pipeline.models import Lead

    # Caught by domain apex-match. Names that ALSO trip the older
    # megacorp prefix filter (Siemens, Honeywell, etc.) are excluded
    # from this list since they get a different reason string — they're
    # still disqualified, just by a different rule.
    for name, domain in (
        ("Canon U.S.A., Inc.", "usa.canon.com"),
        ("Quest Diagnostics TB LLC", "questdiagnostics.com"),
        ("CoreLogic Solutions, LLC", "corelogic.com"),
    ):
        lead = Lead(name=name, name_key=name.lower(), domain=domain)
        assert enrichment._disqualification_reason(lead) == "enterprise_subsidiary", (
            f"expected enterprise_subsidiary for {name!r} / {domain!r}"
        )

    # Caught by name-pattern (no domain or unrelated domain). Bosch
    # Mitsubishi etc. aren't in the megacorp prefix list so this
    # filter is what catches them.
    for name in (
        "Canon U.S.A., Inc.",
        "Bosch USA Inc",
        "Mitsubishi North America Corp",
        "Acme Worldwide",
    ):
        lead = Lead(name=name, name_key=name.lower())  # no domain
        assert enrichment._disqualification_reason(lead) == "enterprise_subsidiary", (
            f"expected enterprise_subsidiary for {name!r}"
        )

    # Negative: real SMBs that mention "USA" or "America" mid-name shouldn't trigger
    for name, domain in (
        ("Pioneer Engineering LLC", "pioneer-eng.com"),
        ("Acme Holdings", "acme.com"),
        ("USA Trucking Co", None),  # 'USA' at start, not as suffix
        ("American Pipe Co", None),
    ):
        lead = Lead(name=name, name_key=name.lower(), domain=domain)
        reason = enrichment._disqualification_reason(lead)
        assert reason != "enterprise_subsidiary", (
            f"unexpected enterprise_subsidiary for {name!r}: got {reason!r}"
        )


def test_disqualification_reason_form_d_noise_pattern() -> None:
    """Vintage-year + street-SPV regexes from the EDGAR source apply
    retroactively to leads already in the DB. Gated to leads carrying
    a Form D signal so the patterns can't nuke a real trucking company
    whose name contains a year or a street word."""
    from insurance_pipeline.models import Lead

    form_d_sig = Signal(
        type=SignalType.FUNDING_RAISED,
        source=SourceName.FUNDING,
        captured_at=datetime.now(timezone.utc).replace(tzinfo=None),
        payload={"filing_type": "Form D"},
    )

    for name in (
        "Summit Ridge 2024 LLC",
        "JR Hyde Park Blvd LLC",
        "Marina Bay Avenue LLC",
        "Holdings 2024, LLC",
    ):
        lead = Lead(name=name, name_key=name.lower(), signals=[form_d_sig])
        assert enrichment._disqualification_reason(lead) == "form_d_noise_pattern", (
            f"expected form_d_noise_pattern for {name!r}"
        )

    # Same name patterns without a Form D signal: NOT purged. A motor
    # carrier called "JR Hyde Park Blvd LLC" stays in the DB.
    fmcsa_sig = Signal(
        type=SignalType.NEW_MOTOR_CARRIER_AUTHORITY,
        source=SourceName.FMCSA,
        captured_at=datetime.now(timezone.utc).replace(tzinfo=None),
        payload={},
    )
    lead = Lead(
        name="JR Hyde Park Blvd LLC",
        name_key="jr_hyde",
        signals=[fmcsa_sig],
    )
    assert enrichment._disqualification_reason(lead) is None


def test_disqualification_reason_megacorp_subsidiary() -> None:
    from insurance_pipeline.models import Lead

    for name in (
        "ATT MOBILITY LLC",
        "AT&T Wireless Inc.",
        "VERIZON CONNECT NWF INC",
        "Johnson Controls Fire Protection LP",
        "AECOM USA Inc",
        "CDM Federal Programs Corporation",
        "Tetra Tech NUS",
        "Compass PTS JV LLC",
        "Honeywell International Inc",
        "Microsoft Federal Sales LLC",
    ):
        lead = Lead(name=name, name_key=name.lower())
        assert enrichment._disqualification_reason(lead) == "megacorp_subsidiary", (
            f"expected megacorp_subsidiary for {name!r}"
        )


def test_megacorp_filter_only_matches_at_prefix() -> None:
    """Prefix-anchored — megacorp names buried mid-string don't trigger.
    The prefix match accepts the tradeoff that a real SMB named with a
    megacorp prefix (e.g. 'Boeing Avenue Diner LLC') would get caught;
    in practice companies don't name themselves after streets/locations
    that match major brands, so the false-positive risk is small and
    well below the noise that megacorp subsidiaries create."""
    for name in (
        "Smith & Verizon Plumbing Inc",  # 'Verizon' is in the middle, not prefix
        "Acme Honeywell Repair Service",  # mid-string
        "Pioneer Comcast Cabinetry LLC",  # mid-string
    ):
        assert not enrichment._is_megacorp_subsidiary(name), (
            f"expected NOT megacorp: {name!r}"
        )


def test_purge_disqualified_deletes_vendor_rows(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "leads.db")
    keeper = db.upsert_lead(conn, _candidate("Pioneer Logistics LLC"))
    vendor = db.upsert_lead(conn, _candidate("Acme Insurance Brokers"))
    assert keeper.id and vendor.id

    purged = enrichment.purge_disqualified(conn)
    assert purged == 1
    assert db.get_lead(conn, lead_id=keeper.id) is not None
    assert db.get_lead(conn, lead_id=vendor.id) is None

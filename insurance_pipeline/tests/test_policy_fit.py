"""Tests for policy_fit — the deterministic insight + premium estimator
that replaced LLM-generated insights."""

from __future__ import annotations

from datetime import datetime, timezone

from insurance_pipeline.models import Signal, SignalType, SourceName
from insurance_pipeline.policy_fit import estimate_policy_fit, trigger_type


def _sig(*, type: SignalType, payload: dict) -> Signal:
    return Signal(
        type=type,
        source=SourceName.FMCSA,
        captured_at=datetime.now(timezone.utc).replace(tzinfo=None),
        payload=payload,
    )


# --- FMCSA ----------------------------------------------------------


def test_fmcsa_owner_operator_one_truck_one_driver() -> None:
    sig = _sig(
        type=SignalType.NEW_MOTOR_CARRIER_AUTHORITY,
        payload={"fleet_size_power_units": 1, "drivers": 1},
    )
    fit = estimate_policy_fit(sig)
    assert fit is not None
    assert fit["coverages"] == ["Commercial Auto", "Workers Comp"]
    assert fit["tagline"] == "Commercial Auto + Workers Comp"
    # Premium dollars dropped — coverages-only display.
    assert "est_annual_premium_usd" not in fit


def test_fmcsa_zero_drivers_drops_wc() -> None:
    sig = _sig(
        type=SignalType.NEW_MOTOR_CARRIER_AUTHORITY,
        payload={"fleet_size_power_units": 1, "drivers": 0},
    )
    fit = estimate_policy_fit(sig)
    assert fit["coverages"] == ["Commercial Auto"]
    assert "Workers Comp" not in fit["coverages"]
    assert fit["tagline"] == "Commercial Auto"


# --- Federal contracts ---------------------------------------------


def test_federal_contract_construction_uses_construction_coverages() -> None:
    sig = _sig(
        type=SignalType.FUNDING_RAISED,
        payload={
            "filing_type": "Federal contract",
            "amount_usd": 200_000,
            "naics": "COMMERCIAL AND INSTITUTIONAL BUILDING CONSTRUCTION",
        },
    )
    fit = estimate_policy_fit(sig)
    assert "GL" in fit["coverages"]
    assert "Builders Risk" in fit["coverages"]
    assert "est_annual_premium_usd" not in fit


def test_federal_contract_engineering_uses_e_and_o() -> None:
    sig = _sig(
        type=SignalType.FUNDING_RAISED,
        payload={
            "filing_type": "Federal contract",
            "amount_usd": 400_000,
            "naics": "ENGINEERING SERVICES",
        },
    )
    fit = estimate_policy_fit(sig)
    assert any("E&O" in c or "Professional Liability" in c for c in fit["coverages"])


def test_federal_contract_hazardous_uses_pollution_liability() -> None:
    sig = _sig(
        type=SignalType.FUNDING_RAISED,
        payload={
            "filing_type": "Federal contract",
            "amount_usd": 442_000,
            "naics": "HAZARDOUS WASTE TREATMENT AND DISPOSAL",
        },
    )
    fit = estimate_policy_fit(sig)
    assert "Pollution Liability" in fit["coverages"]


def test_federal_contract_unknown_naics_default_fallback() -> None:
    sig = _sig(
        type=SignalType.FUNDING_RAISED,
        payload={
            "filing_type": "Federal contract",
            "amount_usd": 100_000,
            "naics": "SOMETHING ELSE ENTIRELY",
        },
    )
    fit = estimate_policy_fit(sig)
    assert fit["coverages"] == ["Cyber", "E&O", "GL"]


# --- Form D / RSS funding ------------------------------------------


def test_form_d_funding_qualitative_tagline() -> None:
    sig = _sig(
        type=SignalType.FUNDING_RAISED,
        payload={"filing_type": "Form D"},
    )
    fit = estimate_policy_fit(sig)
    assert "D&O" in fit["coverages"]
    assert "post-funding" in fit["tagline"]
    assert "est_annual_premium_usd" not in fit


# --- Trigger type classifier --------------------------------------


def test_trigger_type_classifier() -> None:
    fmcsa = _sig(type=SignalType.NEW_MOTOR_CARRIER_AUTHORITY, payload={})
    assert trigger_type(fmcsa) == "motor_carrier"

    fed = _sig(
        type=SignalType.FUNDING_RAISED,
        payload={"filing_type": "Federal contract"},
    )
    assert trigger_type(fed) == "federal_contract"

    formd = _sig(
        type=SignalType.FUNDING_RAISED,
        payload={"filing_type": "Form D"},
    )
    assert trigger_type(formd) == "funding_event"

    sos = _sig(type=SignalType.NEW_BUSINESS_FILED, payload={})
    assert trigger_type(sos) == "new_entity"

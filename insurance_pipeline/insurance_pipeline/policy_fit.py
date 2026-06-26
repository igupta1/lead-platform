"""Deterministic policy-fit hint per signal.

Returns the recommended P&C product lines an agent would pitch given
the signal. v1 included rough premium-dollar estimates; the prior
review pulled those — the error band on individual leads (2-10×
either direction) was wide enough that any experienced commercial-
lines agent could spot the miss, and seeing one wrong number erodes
trust in every other field on the card. Lines-of-business tags
alone are defensible and do the actual triage work.

Examples:
  "Commercial Auto + Workers Comp"   ← FMCSA, 5 trucks 5 drivers
  "Commercial Auto"                  ← FMCSA, 1 truck owner-op
  "Pollution Liability + GL"         ← Federal contract, hazardous waste NAICS
  "Professional Liability (E&O) + GL" ← Federal contract, engineering NAICS
  "D&O + EPLI · post-funding policy" ← Form D / TechCrunch
  "GL + WC starter pack"             ← New business filing
"""

from __future__ import annotations

from typing import Any

from insurance_pipeline.models import Signal, SignalType


# NAICS-keyword → (coverage list, premium rate as fraction of contract).
# Order matters: first match wins. More specific verticals near the top.
_NAICS_COVERAGE_TABLE: tuple[tuple[tuple[str, ...], list[str], float], ...] = (
    (("HAZARDOUS", "REMEDIATION", "WASTE TREAT"),
     ["Pollution Liability", "GL", "Workers Comp"], 0.018),
    (("ARCHITECT", "ENGINEER"),
     ["Professional Liability (E&O)", "GL"], 0.012),
    (("CONSTRUCTION", "BUILDING CONSTRUCTION", "PREFABRICATED METAL"),
     ["GL", "Builders Risk", "Workers Comp"], 0.016),
    (("LABORATORY", "TESTING"),
     ["Professional Liability", "Cyber", "GL"], 0.012),
    (("ANALYTICAL LABORATORY INSTRUMENT",),
     ["Product Liability", "Cyber", "GL"], 0.014),
    (("SOFTWARE", "COMPUTING", "INTERNET", "DATA PROCESSING", "WEB"),
     ["Cyber", "E&O", "D&O"], 0.012),
    (("SECURITY SYSTEMS", "GUARD"),
     ["GL", "E&O", "Workers Comp"], 0.015),
    (("HEALTHCARE", "MEDICAL", "CLINIC"),
     ["Professional Liability (Malpractice)", "Cyber"], 0.018),
    (("FITNESS", "RECREATIONAL"),
     ["GL", "Professional Liability"], 0.012),
    (("CONSULTING", "MANAGEMENT", "ADMINISTRATIVE"),
     ["E&O", "D&O", "GL"], 0.010),
    (("WATER SUPPLY", "IRRIGATION"),
     ["GL", "Pollution Liability", "Property"], 0.014),
    (("MANUFACTURING", "FABRICATED METAL"),
     ["Product Liability", "GL", "Workers Comp"], 0.014),
    (("TRANSLATION", "INTERPRETATION", "PROFESSIONAL TRAINING"),
     ["E&O", "GL"], 0.008),
    (("DELIVERY", "MESSENGER", "LOCAL DELIVERY"),
     ["Commercial Auto", "GL", "Workers Comp"], 0.018),
)


def _coverages_for_naics(naics: str) -> tuple[list[str], float]:
    """Return (coverages, premium rate as fraction of contract)."""
    naics_upper = (naics or "").upper()
    for keywords, coverages, rate in _NAICS_COVERAGE_TABLE:
        if any(kw in naics_upper for kw in keywords):
            return coverages, rate
    # Default fallback for federal contractors with unknown NAICS.
    return ["Cyber", "E&O", "GL"], 0.012


def _safe_int(v: Any) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def _fmcsa_fit(sig: Signal) -> dict[str, Any]:
    drivers = _safe_int(sig.payload.get("drivers"))
    coverages = ["Commercial Auto"]
    if drivers > 0:
        coverages.append("Workers Comp")
    tagline = " + ".join(coverages)
    return {
        "coverages": coverages,
        "tagline": tagline,
    }


def _federal_contract_fit(sig: Signal) -> dict[str, Any]:
    naics = sig.payload.get("naics") or ""
    coverages, _rate = _coverages_for_naics(naics)
    # Show the top two coverage products for brevity in the tagline;
    # full list stays in `coverages` for any future filter/sort use.
    tagline = " + ".join(coverages[:2])
    return {
        "coverages": coverages,
        "tagline": tagline,
    }


def _equity_funding_fit(sig: Signal) -> dict[str, Any]:
    return {
        "coverages": ["D&O", "EPLI", "Cyber"],
        "tagline": "D&O + EPLI · post-funding policy",
    }


def _new_entity_fit(sig: Signal) -> dict[str, Any]:
    return {
        "coverages": ["GL", "Workers Comp", "Property"],
        "tagline": "GL + WC starter pack",
    }


def estimate_policy_fit(sig: Signal) -> dict[str, Any] | None:
    """Return policy-fit hint for an agent triaging this signal.

    Returns dict with:
      - coverages: list[str] of recommended P&C product lines
      - tagline: short agent-facing string (≤80 chars)
    Or None if the signal type doesn't carry enough info.
    """
    if sig.type == SignalType.NEW_MOTOR_CARRIER_AUTHORITY:
        return _fmcsa_fit(sig)
    if sig.type == SignalType.FUNDING_RAISED:
        if sig.payload.get("filing_type") == "Federal contract":
            return _federal_contract_fit(sig)
        return _equity_funding_fit(sig)
    if sig.type == SignalType.NEW_BUSINESS_FILED:
        return _new_entity_fit(sig)
    return None


def trigger_type(sig: Signal) -> str:
    """Coarse classifier for UI filtering. Same enum used in LeadFilters."""
    if sig.type == SignalType.NEW_MOTOR_CARRIER_AUTHORITY:
        return "motor_carrier"
    if sig.type == SignalType.FUNDING_RAISED:
        ft = sig.payload.get("filing_type")
        if ft == "Federal contract":
            return "federal_contract"
        return "funding_event"
    if sig.type == SignalType.NEW_BUSINESS_FILED:
        return "new_entity"
    return "other"

"""Insurance pipeline data models.

Single-niche pipeline: no NicheName enum, no per-niche score columns.
Each lead has one ``score`` and one ``insight``. Mirror of
``msp_pipeline.models`` in shape but not in content — every enum is
insurance-specific.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SignalType(str, Enum):
    # Tier 1 — live sources at v1 ship.
    NEW_MOTOR_CARRIER_AUTHORITY = "new_motor_carrier_authority"
    NEW_BUSINESS_FILED = "new_business_filed"

    # Tier 2 — defined in the enum now so scoring weights can reference
    # them ahead of the source landing. Pipeline ignores them until the
    # corresponding source emits.
    OSHA_INSPECTION_RECORDED = "osha_inspection_recorded"
    BUILDING_PERMIT_ISSUED = "building_permit_issued"
    FUNDING_RAISED = "funding_raised"

    # Bookkeeping.
    LOCATION_CAPTURED = "location_captured"
    ENRICHMENT_RUN = "enrichment_run"
    APOLLO_ENRICHED = "apollo_enriched"


class SourceName(str, Enum):
    FMCSA = "fmcsa"
    SOS_FL = "sos_fl"
    SOS_CO = "sos_co"
    SOS_WA = "sos_wa"
    OSHA = "osha"
    BUILDING_PERMITS = "building_permits"
    FUNDING = "funding"
    COMPUTED = "computed"
    APOLLO = "apollo"


class Signal(BaseModel):
    type: SignalType
    source: SourceName
    captured_at: datetime
    payload: dict[str, Any]


class Lead(BaseModel):
    id: int | None = None
    name: str
    name_key: str
    domain: str | None = None
    industry: str | None = None
    headcount: int | None = None
    country: str | None = None
    dm_name: str | None = None
    dm_title: str | None = None
    dm_email: str | None = None
    dm_linkedin_url: str | None = None
    value_prop: str | None = None
    signals: list[Signal] = Field(default_factory=list)
    score: float | None = None
    insight: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class LeadCandidate(BaseModel):
    name: str
    domain: str | None = None
    initial_signal: Signal
    # When set, upsert dedups on this key instead of fuzzy-matching the
    # name. FMCSA passes "usdot:<n>" so two distinct carriers with the
    # same legal name don't collapse to one row (e.g. two "FX TRUCKING
    # LLC" with different USDOTs in different states).
    dedup_key: str | None = None

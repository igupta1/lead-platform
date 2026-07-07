"""CFO pipeline data models.

Single-niche pipeline: no NicheName enum, one ``score`` and one
``insight`` per lead. Same Pydantic shape as ``insurance_pipeline.models``
but every enum is fractional-CFO-specific.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SignalType(str, Enum):
    # Hottest signal — the company posted a Fractional / Interim /
    # Part-time CFO role. They are literally in-market for the exact
    # service being sold; outranks every other signal.
    JOB_POSTED_FRACTIONAL_CFO = "job_posted_fractional_cfo"

    # Primary buying signal — company is hiring finance leadership
    # one rung below CFO (Controller, VP / Head / Director of
    # Finance, Accounting / Finance Manager).
    JOB_POSTED_FINANCE_LEAD = "job_posted_finance_lead"

    # Secondary urgency signals (Form D + TechCrunch / PRNewswire RSS
    # collapsed into one type per spec).
    FUNDING_RAISED = "funding_raised"

    # Hard-exclude marker. Written to the disqualified table, NOT to
    # leads.signals — kept here so callers can reference the type when
    # constructing disqualifier payloads.
    CFO_ROLE_OPEN = "cfo_role_open"

    # Bookkeeping.
    LOCATION_CAPTURED = "location_captured"
    ENRICHMENT_RUN = "enrichment_run"
    APOLLO_ENRICHED = "apollo_enriched"


class SourceName(str, Enum):
    JOBS = "jobs"
    FUNDING = "funding"
    EDGAR_FORM_D = "edgar_form_d"
    EDGAR_FORM_C = "edgar_form_c"
    FRACTIONAL_BOARD = "fractional_board"
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
    industry: str | None = None       # coarse parent, derived from niche
    niche: str | None = None          # granular child (see taxonomy.py)
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
    headcount: int | None = None  # Used when source carries headcount (Indeed company_num_employees)
    initial_signal: Signal


class Disqualifier(BaseModel):
    """A signal that a company should be permanently excluded from
    the CFO dashboard. Today the only producer is the jobs source
    (full-time CFO posting), but the shape allows future producers."""

    name: str
    reason: str
    source: SourceName
    payload: dict[str, Any] = Field(default_factory=dict)

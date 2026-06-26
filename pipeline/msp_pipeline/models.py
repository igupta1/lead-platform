from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class NicheName(str, Enum):
    IT_MSP = "it_msp"
    MSSP = "mssp"
    CLOUD = "cloud"


class SignalType(str, Enum):
    JOB_IT_SUPPORT = "job_posted_it_support"
    JOB_IT_LEADERSHIP = "job_posted_it_leadership"
    JOB_SECURITY = "job_posted_security"
    JOB_CLOUD_DEVOPS = "job_posted_cloud_devops"
    EXEC_HIRED = "exec_hired"
    FUNDING_RAISED = "funding_raised"
    BREACH_DISCLOSED = "breach_disclosed"
    LOCATION_CAPTURED = "location_captured"
    ENRICHMENT_RUN = "enrichment_run"
    APOLLO_ENRICHED = "apollo_enriched"


class SourceName(str, Enum):
    JOBS = "jobs"
    FUNDING = "funding"
    BREACHES = "breaches"
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
    it_msp_score: float | None = None
    mssp_score: float | None = None
    cloud_score: float | None = None
    it_msp_insight: str | None = None
    mssp_insight: str | None = None
    cloud_insight: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class LeadCandidate(BaseModel):
    name: str
    domain: str | None = None
    initial_signal: Signal

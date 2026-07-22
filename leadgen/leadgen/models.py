"""leadgen data models — the shared contract for the whole platform.

One package, one company store, five niches. A *niche is a query* over the
company store, not a pipeline: every source writes into the same ``Lead``
(one record per company, every signal attached), and each niche is a
``scores`` entry computed by :mod:`leadgen.scoring`.

Signal contract (enforced here): every stored signal carries verbatim
``evidence_text`` and a ``source_url``. No evidence -> the Signal cannot be
constructed, so it is never stored. There are no title-absence proxies and no
``exec_hired`` guessing — only signals a company actually emitted in public.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class SignalType(str, Enum):
    # --- Funding (SEC only — no RSS/headline proxies) --------------------
    # SEC Form D — a private-securities offering (a raise). Feeds
    # accounting, cfo, and cloud.
    FUNDING_FORM_D = "funding_form_d"
    # SEC Reg-CF Form C — crowdfunding raise; arrives pre-enriched with
    # domain / headcount / revenue. Feeds accounting.
    FUNDING_FORM_C = "funding_form_c"

    # --- Job posts: explicit-intent + finance hires ----------------------
    # Hottest CFO signal — the company posted a Fractional / Interim /
    # Part-time CFO role. Literally in-market for the service being sold.
    JOB_FRACTIONAL_CFO = "job_fractional_cfo"
    # Finance leadership one rung below CFO (Controller, VP / Head /
    # Director of Finance, Accounting / Finance Manager, FP&A, Sr
    # Accountant). Feeds cfo AND accounting.
    JOB_FINANCE_LEAD = "job_finance_lead"
    # Junior IC finance hire (bookkeeper, staff / junior accountant, AP /
    # AR clerk, payroll / billing). Feeds accounting.
    JOB_JUNIOR_FINANCE = "job_junior_finance"

    # --- Job posts: IT / security / cloud --------------------------------
    # Help desk / IT support / desktop / sysadmin. Feeds msp.
    JOB_IT_SUPPORT = "job_it_support"
    # IT leadership (IT Director / Manager, Head of IT / Technology). Feeds
    # msp (kept per product decision — "IT/helpdesk" includes leadership).
    JOB_IT_LEADERSHIP = "job_it_leadership"
    # Security / infosec / SOC / CISO. Feeds mssp.
    JOB_SECURITY = "job_security"
    # DevOps / SRE / cloud / platform engineering. Feeds cloud.
    JOB_CLOUD_DEVOPS = "job_cloud_devops"

    # --- Breach ----------------------------------------------------------
    # A publicly-disclosed data breach (HHS OCR, state AGs). Carries a
    # record count. Marked non-exclusive (public list). Feeds mssp.
    BREACH_DISCLOSED = "breach_disclosed"

    # --- Disqualifier marker (NOT a stored signal) -----------------------
    # Written to the ``disqualified`` table, never to ``Lead.signals`` — a
    # negative gate (an open full-time CFO posting means the company has
    # graduated past the fractional stage). Kept here so callers can name
    # the reason when constructing a Disqualifier.
    CFO_ROLE_OPEN = "cfo_role_open"


class SourceName(str, Enum):
    JOBS = "jobs"
    FRACTIONAL_BOARD = "fractional_board"
    EDGAR_FORM_D = "edgar_form_d"
    EDGAR_FORM_C = "edgar_form_c"
    BREACHES = "breaches"
    COMPUTED = "computed"


# Signal types that must never appear in Lead.signals (disqualifier-only).
_NON_STORABLE: frozenset[SignalType] = frozenset({SignalType.CFO_ROLE_OPEN})


class Signal(BaseModel):
    """One public buying signal emitted by a company.

    ``evidence_text`` (verbatim) and ``source_url`` are required and must be
    non-empty — the platform stores nothing it can't show and link. Source
    specific extras (job title, offering amount, record count, industry
    group, …) live in ``payload`` for display.
    """

    type: SignalType
    source: SourceName
    captured_at: datetime
    # When the underlying event happened (posting date, filing date, breach
    # date). Recency is scored off this; falls back to captured_at if unknown.
    event_date: datetime | None = None
    evidence_text: str
    source_url: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def _storable(cls, v: SignalType) -> SignalType:
        if v in _NON_STORABLE:
            raise ValueError(f"{v.value} is a disqualifier marker, not a storable Signal")
        return v

    @field_validator("evidence_text", "source_url")
    @classmethod
    def _non_empty(cls, v: str, info: Any) -> str:
        if v is None or not str(v).strip():
            raise ValueError(f"Signal.{info.field_name} is required (no evidence -> not stored)")
        return str(v).strip()


class Lead(BaseModel):
    """One company record, deduped across every source.

    Named ``Lead`` for continuity with the website / outreach contract, but
    it is one company with *all* its signals attached. Scored once per niche
    (``scores``); it appears in each niche inventory it qualifies for.
    """

    id: int | None = None
    name: str
    name_key: str
    domain: str | None = None
    industry: str | None = None       # coarse parent (see taxonomy.py)
    niche: str | None = None          # granular taxonomy child (outreach fit)
    headcount: int | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    signals: list[Signal] = Field(default_factory=list)
    # niche -> score. A company can qualify for several niches at once.
    scores: dict[str, float] = Field(default_factory=dict)
    insight: str | None = None
    enriched_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class LeadCandidate(BaseModel):
    """What a source emits: a company name + one signal, plus whatever
    identity/geo/size the source already knows. The store dedupes it into a
    Lead and attaches the signal."""

    name: str
    domain: str | None = None
    headcount: int | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    initial_signal: Signal


class Disqualifier(BaseModel):
    """A company that must be permanently excluded from every niche. The
    only producer today is the jobs source (an open full-time CFO posting),
    but the shape allows future producers."""

    name: str
    reason: str
    source: SourceName
    payload: dict[str, Any] = Field(default_factory=dict)

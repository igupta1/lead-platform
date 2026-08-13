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
    # Same explicit intent one rung DOWN: a fractional / interim controller
    # (or accounting manager, head of accounting). Split out of
    # JOB_FRACTIONAL_CFO because the buyer is different — an outsourced
    # ACCOUNTING firm sells a fractional controller, a fractional-CFO firm
    # sells strategy — and a niche can only select on the signal type. Feeds
    # accounting as its lead-first signal.
    JOB_FRACTIONAL_CONTROLLER = "job_fractional_controller"
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


class SourceName(str, Enum):
    JOBS = "jobs"
    FRACTIONAL_BOARD = "fractional_board"
    EDGAR_FORM_D = "edgar_form_d"
    EDGAR_FORM_C = "edgar_form_c"
    BREACHES = "breaches"
    COMPUTED = "computed"


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

    @field_validator("evidence_text", "source_url")
    @classmethod
    def _non_empty(cls, v: str, info: Any) -> str:
        if v is None or not str(v).strip():
            raise ValueError(f"Signal.{info.field_name} is required (no evidence -> not stored)")
        return str(v).strip()


# --- Company size ---------------------------------------------------------
#
# An exact employee count is not reliably discoverable for a small private
# company: the grounded lookup resolves the website but not the size roughly
# half the time, which used to leave ``headcount`` NULL and silently bypass
# every size gate. A coarse BAND is both what a model can answer reliably and
# all the size caps actually need, so it is the fallback whenever the exact
# number is unknown. Ordered smallest-first; the value is the band's inclusive
# upper bound.
HEADCOUNT_BANDS: dict[str, int] = {
    "1-10": 10,
    "11-50": 50,
    "51-200": 200,
    "201-1000": 1000,
    # Open-ended top band. Any real SMB cap sits far below this, so it always
    # reads as "too big" without pretending to know the real number.
    "1000+": 1_000_000,
}


def band_max(band: str | None) -> int | None:
    """The inclusive upper bound of a headcount band, or None when the band is
    missing / unrecognized (treated as unknown, never as small)."""
    if not band:
        return None
    return HEADCOUNT_BANDS.get(band.strip())


def band_for(headcount: int | None) -> str | None:
    """The band an exact employee count falls into. Used to backfill a band for
    the leads that already carry a real number."""
    if headcount is None:
        return None
    for name, upper in HEADCOUNT_BANDS.items():
        if headcount <= upper:
            return name
    return "1000+"


def effective_size(lead: "Lead") -> int | None:
    """The company size the caps should test: the exact count when known and
    self-consistent, otherwise the band's upper bound, otherwise None
    (genuinely unknown).

    Using the band's UPPER bound is the conservative direction — an unsized
    "51-200" company is treated as 200, so it fails a 100-person cap rather
    than sneaking under it.

    The two numbers are separate lookup answers, so they can contradict each
    other (observed: headcount 10 alongside band "1000+"). A contradiction is
    reported as UNKNOWN rather than resolved to either number, because we
    genuinely do not know which one is wrong — and "unknown" is the one answer
    that is both safe and recoverable:

    * `project_niche` fails closed on an unsized lead, so a contradictory
      enterprise is held out of the inventory immediately.
    * `_disqualification_reason` only purges on a size it can trust, so the
      lead is NOT deleted on evidence we have just called unreliable. Phase 2
      of `purge_disqualified` deletes outright and writes no `disqualified`
      row, so resolving a contradiction to "too big" would destroy the lead
      with no audit trail and no way back.
    * `needs_enrichment` retries anything unsized, so the next run re-asks and
      the contradiction resolves itself into a real size.

    Picking the larger number instead only looks more decisive: it trades a
    recoverable hold for an irreversible delete, on the one input we have
    already decided we cannot trust."""
    upper = band_max(lead.headcount_band)
    if lead.headcount is None:
        return upper
    if upper is None or band_for(lead.headcount) == lead.headcount_band:
        # Agreement (or no usable band): the exact count is the precise answer.
        return lead.headcount
    return None


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
    # Coarse size band (see HEADCOUNT_BANDS). Set whenever the lookup can size
    # the company at all; the exact ``headcount`` above is a bonus, not a
    # requirement. Read both through ``effective_size``.
    headcount_band: str | None = None
    # How many enrichment passes have tried and failed to size this company.
    # An unsized lead is held back from every publish and re-asked, so without
    # a counter the un-sizable ones bill a lookup a night forever. Reset to 0
    # the moment a size lands. See MAX_SIZE_ATTEMPTS in enrichment.py.
    size_attempts: int = 0
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
    """A company that must be permanently excluded from every niche. Produced
    by the enrichment layer (CFO-services competitor, recruiting firm, auto
    dealer per the web lookup); the shape allows future producers."""

    name: str
    reason: str
    source: SourceName
    payload: dict[str, Any] = Field(default_factory=dict)

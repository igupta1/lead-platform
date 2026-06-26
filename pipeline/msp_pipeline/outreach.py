"""Per-niche insight copy generation.

One OpenAI structured call per niche per lead. Returns a ``Copy`` Pydantic
with one field: ``insight`` — a single third-person sentence summarizing
the buying-moment, shown above the signal list on each card. Pure — does
not write to the DB. The ``daily_run.py`` orchestrator handles persistence
and re-generation gating.

Outreach email bodies were removed from the lead magnet; the file is kept
under the ``outreach`` name so existing imports / call sites stay stable,
but it now generates only the insight blurb.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from msp_pipeline import llm, scoring
from msp_pipeline.models import Lead, NicheName, Signal, SignalType
from msp_pipeline.sources import breaches


class Copy(BaseModel):
    insight: str = Field(min_length=10, max_length=160)


_NICHE_FRAMING: dict[NicheName, str] = {
    NicheName.IT_MSP: (
        "You are summarizing leads for a salesperson at an IT managed "
        "service provider (MSP) selling help desk, sysadmin, and general "
        "IT support to growing SMBs (10-250 employees)."
    ),
    NicheName.MSSP: (
        "You are summarizing leads for a salesperson at a managed security "
        "service provider (MSSP) selling SOC monitoring, vulnerability "
        "management, and compliance support to regulated SMBs (10-250 "
        "employees)."
    ),
    NicheName.CLOUD: (
        "You are summarizing leads for a salesperson at a cloud "
        "consultancy that helps SMBs (10-250 employees) plan, migrate, "
        "and optimize their AWS / GCP / Azure footprint."
    ),
}


_PROMPT_TEMPLATE = """\
{framing}

Lead profile:
- Company: {name}
- What they do: {value_prop}
- Industry: {industry}
- Headcount: {headcount}
- Location: {location}
- Heat score for this niche: {score:.0f}/100

Recent signals (most recent first):
{signals}

Write ONE sentence (<= 140 chars), THIRD PERSON, naming the specific
buying moment that makes this lead a fit (the actual job title, the
funding amount/round, the agency that received the breach disclosure,
etc.). Use the company name or pronouns like "they" / "the company" —
NEVER "you" or "your". This text is shown to a salesperson scanning a
list of leads, not to the lead being pitched.

Avoid generic phrasing like "growing companies" or "highlighting the
need for IT support". Lead with the concrete signal.

Refer to timing naturally ("recently", or by the reported date shown in
the signal) — do NOT restate raw day counts or write phrases like
"0 days ago" / "just 0 days ago". Do NOT repeat sensational or
promotional headline wording verbatim (e.g. "World's Largest IPO") and
do NOT frame a negative event (a stock decline, a layoff) as growth;
describe the funding factually — round and amount only when they look
plausible.
"""


def generate(
    lead: Lead,
    niche: NicheName,
    score: float,
    *,
    model: str = "gpt-4o-mini",
) -> Copy:
    prompt = _PROMPT_TEMPLATE.format(
        framing=_NICHE_FRAMING[niche],
        name=lead.name,
        value_prop=lead.value_prop or "unknown",
        industry=lead.industry or "unknown",
        headcount=_describe_headcount(lead.headcount),
        location=_describe_location(lead),
        score=score,
        signals=_describe_signals(lead, niche=niche),
    )
    return llm.call_openai(prompt, response_model=Copy, model=model)


# --- Private helpers -------------------------------------------------------


_SCORING_SIGNAL_TYPES = frozenset(
    {
        SignalType.JOB_IT_SUPPORT,
        SignalType.JOB_IT_LEADERSHIP,
        SignalType.JOB_SECURITY,
        SignalType.JOB_CLOUD_DEVOPS,
        SignalType.EXEC_HIRED,
        SignalType.FUNDING_RAISED,
        SignalType.BREACH_DISCLOSED,
    }
)

_SIGNAL_PAYLOAD_FIELDS: dict[SignalType, tuple[str, ...]] = {
    SignalType.JOB_IT_SUPPORT: ("title", "location"),
    SignalType.JOB_IT_LEADERSHIP: ("title", "location"),
    SignalType.JOB_SECURITY: ("title", "location"),
    SignalType.JOB_CLOUD_DEVOPS: ("title", "location"),
    SignalType.EXEC_HIRED: ("title", "location"),
    SignalType.FUNDING_RAISED: ("feed_title", "title", "amount_usd", "round"),
    SignalType.BREACH_DISCLOSED: ("agency", "reported_date"),
}


def _describe_headcount(n: int | None) -> str:
    if n is None:
        return "unknown"
    return f"~{n} employees"


def _describe_location(lead: Lead) -> str:
    locs = sorted(
        (s for s in lead.signals if s.type == SignalType.LOCATION_CAPTURED),
        key=lambda s: s.captured_at,
        reverse=True,
    )
    if not locs:
        return lead.country or "unknown"
    payload = locs[0].payload
    city = payload.get("city")
    state = payload.get("state")
    if city and state:
        return f"{city}, {state}"
    if state:
        return state
    if city:
        return city
    return lead.country or "unknown"


def _format_signal(sig: Signal, days_ago: int) -> str:
    fields = _SIGNAL_PAYLOAD_FIELDS.get(sig.type, ())
    parts: list[str] = []
    for field in fields:
        val = sig.payload.get(field)
        if not val:
            continue
        # The breach payload stores a raw agency code ("me_ag"); render the
        # readable name so the LLM names "the Maine Attorney General" rather
        # than leaking the scraper code into the prospect-facing insight.
        if sig.type == SignalType.BREACH_DISCLOSED and field == "agency":
            val = breaches.agency_display_name(str(val))
        parts.append(f"{field}={val!r}")
    detail = f" - {'; '.join(parts)}" if parts else ""
    when = "today" if days_ago == 0 else f"{days_ago}d ago"
    return f"- {sig.type.value} ({when}){detail}"


def _describe_signals(
    lead: Lead,
    *,
    niche: NicheName | None = None,
    limit: int = 6,
    now: datetime | None = None,
) -> str:
    # When a niche is given, describe only the signals that fit it, so the
    # per-niche insight can't reference an off-niche signal (e.g. a breach in
    # a Cloud insight). Falls back to all scoring signals when none given.
    if niche is not None:
        relevant = [s for s in lead.signals if scoring.signal_matches_niche(s, niche)]
    else:
        relevant = [s for s in lead.signals if s.type in _SCORING_SIGNAL_TYPES]
    relevant.sort(key=scoring.effective_date, reverse=True)
    if not relevant:
        return "(no recent signals on file)"
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    return "\n".join(
        _format_signal(s, max(0, (now - scoring.effective_date(s)).days))
        for s in relevant[:limit]
    )

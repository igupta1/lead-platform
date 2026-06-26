"""Insight copy generation for the insurance pipeline.

One OpenAI structured call per lead → ``Copy.insight`` (single
third-person sentence shown above the signal list on each card). Pure;
``daily_run`` handles persistence + regeneration gating.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from insurance_pipeline import llm
from insurance_pipeline.models import Lead, Signal, SignalType


class Copy(BaseModel):
    insight: str = Field(min_length=10, max_length=160)


_FRAMING = (
    "You are summarizing leads for a salesperson at an independent "
    "insurance agency that sells commercial lines (general "
    "liability, workers compensation, commercial auto, property, "
    "D&O / EPLI, group benefits) to SMBs (10-250 employees) and "
    "newly-formed entities. The agent prospects on buying triggers — "
    "new motor-carrier authorities, new business registrations, "
    "federal contracts, fresh private offerings, building permits."
)


# Rotation of style directives to prevent the formulaic "X recently
# obtained Y" template that every card reads if we don't force
# variety. The lead id is hashed into this list so the same lead
# always gets the same style on regen but different leads get
# different styles across the page.
_INSIGHT_STYLES: tuple[str, ...] = (
    "Open with the most striking number or detail from the signal — "
    "the fleet size, the contract amount, the NAICS, the date. Lead "
    "with the number, then the company, then the implication.",

    "Lead with the specific commercial-lines coverage this triggers "
    "(e.g. 'Commercial auto + WC for...', 'D&O exposure as...'). "
    "Name the coverage type in the first three words.",

    "Frame the urgency or timing window — why call this week vs next "
    "month. Reference how fresh the signal is or what window the "
    "buying decision typically sits in.",

    "Highlight what's distinctive about this company's vertical or "
    "geography. Construction subs in [state]. Owner-operators with "
    "[N] drivers. Healthcare orgs in [city].",

    "Open with the decision maker (their title or role), then what "
    "the company just did, then the policy implication.",

    "Start with a verb — 'Adds [N] trucks to...', 'Won [contract] "
    "for...', 'Filed [form] in [state]...'. Action-first.",
)


_PROMPT_TEMPLATE = """\
{framing}

STYLE FOR THIS LEAD: {style}

Lead profile:
- Company: {name}
- What they do: {value_prop}
- Industry: {industry}
- Headcount: {headcount}
- Location: {location}
- Heat score: {score:.0f}/100

Recent signals (most recent first):
{signals}

Write ONE sentence (<= 140 chars), THIRD PERSON, naming the specific
buying moment that makes this lead a fit — the actual filing type, the
fleet size and authority date, the contract amount, etc. Use the
company name or pronouns like "they" / "the company" — NEVER "you" or
"your". This text is shown to a salesperson scanning a list of leads.

Hard rules — the LLM grading this rejects insights that violate any:
1. Do NOT start the sentence with the company name.
2. Do NOT use these phrases: "recently obtained", "recently received",
   "just obtained", "just received", "effective [date]",
   "has filed for", "indicating a need for".
3. Do NOT use generic filler: "growing companies", "highlighting the
   need", "presents an opportunity", "may indicate".
4. Do NOT use marketing-fluff hedge language: "positioned for",
   "poised for", "may require", "well-positioned", "in a prime
   position", "primed for", "potential need for", "potential
   insurance needs", "imminent need". These are content-free.
5. Output ENGLISH ONLY. No non-English characters or words anywhere
   in the sentence.
6. Do NOT echo "0 days ago" or "0d ago" — the signal context already
   uses "today" / "yesterday" for fresh signals; mirror that
   phrasing.
7. DO lead with a concrete detail from the signal payload.
8. DO follow the STYLE directive above for this specific lead.
"""


def generate(
    lead: Lead,
    score: float,
    *,
    model: str = "gpt-4o-mini",
) -> Copy:
    # Deterministic style index: same lead → same style on regen; but
    # different leads → different styles across the page. Hashing on
    # lead.id when present, name as a stable fallback for unpersisted
    # leads in tests.
    seed = lead.id if lead.id is not None else hash(lead.name)
    style = _INSIGHT_STYLES[seed % len(_INSIGHT_STYLES)]

    prompt = _PROMPT_TEMPLATE.format(
        framing=_FRAMING,
        style=style,
        name=lead.name,
        value_prop=lead.value_prop or "unknown",
        industry=lead.industry or "unknown",
        headcount=_describe_headcount(lead.headcount),
        location=_describe_location(lead),
        score=score,
        signals=_describe_signals(lead),
    )
    return llm.call_openai(prompt, response_model=Copy, model=model)


_SCORING_SIGNAL_TYPES = frozenset(
    {
        SignalType.NEW_MOTOR_CARRIER_AUTHORITY,
        SignalType.NEW_BUSINESS_FILED,
        SignalType.OSHA_INSPECTION_RECORDED,
        SignalType.BUILDING_PERMIT_ISSUED,
        SignalType.FUNDING_RAISED,
    }
)

_SIGNAL_PAYLOAD_FIELDS: dict[SignalType, tuple[str, ...]] = {
    SignalType.NEW_MOTOR_CARRIER_AUTHORITY: (
        "usdot", "fleet_size_power_units", "drivers", "issue_date",
    ),
    SignalType.NEW_BUSINESS_FILED: ("state", "filing_type", "filed_on"),
    SignalType.OSHA_INSPECTION_RECORDED: ("naics", "citations", "penalty"),
    SignalType.BUILDING_PERMIT_ISSUED: ("job_type", "estimated_cost"),
    SignalType.FUNDING_RAISED: ("title", "amount_usd", "round"),
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


def _humanize_days_ago(days_ago: int) -> str:
    """Render days-ago as natural language. The LLM otherwise echoes
    '0 days ago' / '1 days ago' literally in the insight."""
    if days_ago <= 0:
        return "today"
    if days_ago == 1:
        return "yesterday"
    return f"{days_ago}d ago"


def _format_signal(sig: Signal, days_ago: int) -> str:
    fields = _SIGNAL_PAYLOAD_FIELDS.get(sig.type, ())
    parts: list[str] = []
    for field in fields:
        val = sig.payload.get(field)
        if val:
            parts.append(f"{field}={val!r}")
    detail = f" - {'; '.join(parts)}" if parts else ""
    return f"- {sig.type.value} ({_humanize_days_ago(days_ago)}){detail}"


def _describe_signals(
    lead: Lead, *, limit: int = 6, now: datetime | None = None
) -> str:
    relevant = [s for s in lead.signals if s.type in _SCORING_SIGNAL_TYPES]
    relevant.sort(key=lambda s: s.captured_at, reverse=True)
    if not relevant:
        return "(no recent signals on file)"
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    return "\n".join(
        _format_signal(s, max(0, (now - s.captured_at).days))
        for s in relevant[:limit]
    )

"""Insight copy generation for the CFO pipeline.

One OpenAI structured call per lead → ``Copy.insight`` (single
third-person sentence shown above the signal list on each card).
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from cfo_pipeline import llm
from cfo_pipeline.models import Lead, Signal, SignalType


class Copy(BaseModel):
    insight: str = Field(min_length=10, max_length=160)


_FRAMING = (
    "You are summarizing leads for a fractional CFO. The product: "
    "part-time CFO services for sub-75-employee US companies. The "
    "buyer is the founder / CEO."
)


# Style rotation kept for variety, but every style is now signal-
# scoped: a hiring-flavored style on a Form-D-only lead caused the
# RenX-style hallucination ("transition from CFO" with no CFO in
# payload). The runner picks a style consistent with what the lead
# actually has — see _pick_style.
_HIRING_STYLES: tuple[str, ...] = (
    "Open with the EXACT posting title from the signal payload. Then "
    "the implication: a Controller hire signals they've outgrown "
    "founder-as-CFO but aren't big enough for a $300K CFO comp.",

    "Action-first verb opener: 'Hiring [exact title]', 'Filling [exact "
    "title]'. Then the gap a fractional CFO closes.",

    "Frame the urgency window — finance-lead postings typically bind "
    "to a candidate in 6-10 weeks. Why this week beats next month.",
)
_FUNDING_STYLES: tuple[str, ...] = (
    "Lead with the filing or round itself (Form D filed today, seed "
    "closed). Then the reporting / board-prep load a fractional CFO "
    "absorbs. Do NOT speculate about hiring they haven't disclosed.",

    "Open with the date detail (filed today / yesterday / Xd ago) "
    "then the investor-side load: new board, new reporting cadence, "
    "first audit cycle.",

    "Highlight what's distinctive about the stage or industry from "
    "the signal payload. Do NOT add roles, hires, or transitions the "
    "payload doesn't reference.",
)
_COMBINED_STYLES: tuple[str, ...] = (
    "Bridge the two signals: 'Filed [filing] AND hiring [exact "
    "title]'. The co-occurrence is the strongest fractional-CFO "
    "trigger on the page.",

    "Open with the hiring title, end with the funding-side urgency "
    "(or vice versa). Tie both to the same buying moment.",
)


_PROMPT_TEMPLATE = """\
{framing}

==========================================================
GROUND TRUTH — the ONLY information about this lead.
Do not introduce facts that are not in this block.
==========================================================

Company:    {name}
What they do: {value_prop}
Industry:   {industry}
Headcount:  {headcount}
Location:   {location}
Heat score: {score:.0f}/100

Signals present on this lead (newest first):
{signals}

Signal-type guidance:
{signal_guidance}

STYLE FOR THIS LEAD: {style}

==========================================================

Write ONE sentence (<= 140 chars), THIRD PERSON, naming the SPECIFIC
buying moment that's already in the signal payload above. Shown to
a fractional CFO scanning leads.

Hard rules — the LLM grading this rejects insights that violate any:

OPENING (the most common failure mode):
1. Do NOT open with "The company", "They", "The firm", a pronoun, or
   the lead's name. Open with a SPECIFIC detail from the signal
   payload above — the posting title in quotes, the filing type, the
   date, the round.

HALLUCINATION GUARD (the second-most-common failure mode):
2. Do NOT mention any role title (CFO, Chief Financial Officer,
   Controller, VP Finance, Accounting Manager, etc.) UNLESS that
   exact title appears in the signal payload above. Specifically
   forbidden: "transition from CFO" / "step up from Controller" /
   "planning to hire a Controller" when those roles are not in the
   signals list.
3. Do NOT cross signal types. If the only signal is FUNDING_RAISED,
   do NOT mention hiring, postings, or finance-lead titles. If the
   only signal is JOB_POSTED_FINANCE_LEAD, do NOT mention funding,
   Form D, or investor-side context.
4. Do NOT speculate about future hiring, future filings, or future
   moves the company has not made. Stay on what the signals actually
   say.

PHRASING:
5. Do NOT use: "recently posted", "recently filed", "just filed",
   "effective [date]", "indicating a need for", "in need of a CFO".
6. Do NOT use filler: "growing companies", "presents an opportunity",
   "may indicate", "ideal candidate".
7. Do NOT use hedge language: "positioned for", "poised for", "may
   require", "well-positioned", "primed for", "potential need for".
8. Output ENGLISH ONLY.
9. Do NOT echo "0 days ago" or "0d ago" — use "today" / "yesterday".

POSITIVE:
10. DO follow the STYLE directive above.
11. DO use concrete payload values: the EXACT posting title in
    quotes, the filing type ("Form D"), the published date phrasing.
"""


def _pick_style(lead: Lead, seed: int) -> str:
    """Pick a style consistent with the signal types actually present
    on the lead. Stops the LLM from being primed with a hiring-flavored
    style when the only signal is funding (the RenX failure mode)."""
    has_hire = any(s.type == SignalType.JOB_POSTED_FINANCE_LEAD for s in lead.signals)
    has_fund = any(s.type == SignalType.FUNDING_RAISED for s in lead.signals)
    if has_hire and has_fund:
        bucket = _COMBINED_STYLES
    elif has_hire:
        bucket = _HIRING_STYLES
    elif has_fund:
        bucket = _FUNDING_STYLES
    else:
        bucket = _HIRING_STYLES  # fallback — shouldn't happen for scored leads
    return bucket[seed % len(bucket)]


def _signal_guidance(lead: Lead) -> str:
    """Explicit per-signal-set instruction injected into the prompt.
    The previous version relied on a single shared rule list; the
    LLM still leaked title mentions onto funding-only leads because
    the framing block primed it with role-title vocabulary.

    Surfacing the present-signal-set as a labeled directive prevents
    cross-contamination."""
    has_hire = any(s.type == SignalType.JOB_POSTED_FINANCE_LEAD for s in lead.signals)
    has_fund = any(s.type == SignalType.FUNDING_RAISED for s in lead.signals)
    if has_hire and has_fund:
        return (
            "BOTH a hiring signal AND a funding signal are present. "
            "Reference the EXACT posting title from the payload AND the "
            "filing type. Tie them together as the buying moment."
        )
    if has_hire:
        return (
            "ONLY a hiring signal is present. Reference the EXACT "
            "posting title (in quotes). Do NOT mention funding, Form D, "
            "Series A, board prep, or investor reporting — none of "
            "those are in the payload for this lead."
        )
    if has_fund:
        return (
            "ONLY a funding signal is present. Reference the filing "
            "type (e.g. Form D) and the date. Do NOT mention any role "
            "title (Controller, VP Finance, CFO, Accounting Manager, "
            "etc.) — no hiring signal exists for this lead. Frame the "
            "value as board prep / investor reporting / fresh-cash "
            "discipline, not hiring."
        )
    return "Signal context unclear. Stick to what's literally in the payload."


def generate(
    lead: Lead,
    score: float,
    *,
    model: str = "gpt-4o-mini",
) -> Copy:
    seed = lead.id if lead.id is not None else hash(lead.name)
    style = _pick_style(lead, seed)

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
        signal_guidance=_signal_guidance(lead),
    )
    return llm.call_openai(prompt, response_model=Copy, model=model)


_SCORING_SIGNAL_TYPES = frozenset(
    {
        SignalType.JOB_POSTED_FINANCE_LEAD,
        SignalType.FUNDING_RAISED,
    }
)

_SIGNAL_PAYLOAD_FIELDS: dict[SignalType, tuple[str, ...]] = {
    SignalType.JOB_POSTED_FINANCE_LEAD: ("title", "date_posted", "site"),
    SignalType.FUNDING_RAISED: ("filing_type", "filed_on", "feed_title"),
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


# --- Trigger classifier (used by daily_run for the UI filter chip) -------


def trigger_type(sig: Signal) -> str:
    """Coarse classifier for UI filtering. Mirrors insurance's
    ``policy_fit.trigger_type`` so the LeadsPage trigger dropdown
    works the same way."""
    if sig.type == SignalType.JOB_POSTED_FINANCE_LEAD:
        return "finance_hire"
    if sig.type == SignalType.FUNDING_RAISED:
        ft = sig.payload.get("filing_type")
        if ft == "Form D":
            return "form_d"
        return "funding_event"
    return "other"

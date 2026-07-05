"""Single-niche scorer for the CFO pipeline.

Tiered, not additive. The category of a lead's *strongest* signal sets
a non-overlapping band, and within-tier quality (recency, compounding
signals, size fit) only reorders leads inside that band. A fractional-
CFO lead can therefore never be outranked by a finance-lead lead, and a
finance-lead lead never by a funding-only lead — no matter how many
signals pile up.

    Fractional-CFO posting  → 80..100   (in-market: shopping for the service)
    Finance-lead hire       → 50..74    (the gate: needs finance leadership)
    Funding-only            →  5..29    (implicit need, weak)

This replaces the old ``clamp(Σ weight × decay)`` model, where five
below-CFO finance postings summed past 100 and buried a single, hotter
Fractional-CFO posting at ~80. No LLM — pure deterministic compute,
cheap to run across the whole table every night.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from cfo_pipeline.models import Lead, Signal, SignalType

SCORE_MIN = 0.0
SCORE_MAX = 100.0

# Tier bases — the strongest signal present sets the band. Bases +
# per-tier headroom are chosen so the bands never overlap, which is
# what makes the category a hard gate rather than a soft weighting.
_TIER_BASE_FRACTIONAL = 80.0   # band 80..100
_TIER_BASE_FINANCE = 50.0      # band 50..74
_TIER_BASE_FUNDING = 5.0       # band  5..29

_HEADROOM_FRACTIONAL = 20.0    # 80..100
_HEADROOM_FINANCE = 24.0       # 50..74  (ceiling 74 < fractional floor 80)
_HEADROOM_FUNDING = 24.0       #  5..29  (ceiling 29 < finance floor 50)

# Within-tier bonuses (summed, then capped at the tier's headroom).
# Recency is additive here — NOT a multiplier on the base — so even a
# month-old fractional posting (80) still tops a red-hot finance hire
# (≤74). "Fractional on top" holds regardless of age within the feed.
_RECENCY_MAX = 12.0
_RECENCY_WINDOW_DAYS: dict[SignalType, float] = {
    SignalType.JOB_POSTED_FRACTIONAL_CFO: 60.0,  # matches the wider scrape window
    SignalType.JOB_POSTED_FINANCE_LEAD: 30.0,
    SignalType.FUNDING_RAISED: 30.0,
}
_DEFAULT_RECENCY_WINDOW_DAYS = 30.0

_BULLSEYE_BONUS = 6.0          # hiring tier that ALSO has a funding signal
_EXTRA_ROLE_BONUS = 2.0        # per additional distinct finance posting
_EXTRA_ROLE_CAP = 6.0
_SIZE_FIT_BONUS = 4.0          # headcount in the fractional sweet spot
_SIZE_FIT_MIN = 10
_SIZE_FIT_MAX = 50
_REVENUE_BONUS = 4.0           # Form D/C shows the company has real revenue

# Form D revenueRange bands that mean "no real revenue yet".
_NO_REVENUE_RANGES: frozenset[str] = frozenset(
    {"", "no revenues", "decline to disclose", "not applicable"}
)


_HIRING_SIGNAL_TYPES: frozenset[SignalType] = frozenset(
    {
        SignalType.JOB_POSTED_FRACTIONAL_CFO,
        SignalType.JOB_POSTED_FINANCE_LEAD,
    }
)

_JOB_SIGNAL_TYPES = _HIRING_SIGNAL_TYPES  # alias kept for payload_event_date


def payload_event_date(sig: Signal) -> datetime | None:
    """The date the EVENT happened (job posted, Form D filed) — not
    when the pipeline captured it. Job boards return postings up to two
    months old; decaying on captured_at would score a stale posting as
    brand-new and float it to the top of the page. Returns None when
    the payload carries no parseable date (caller falls back to
    captured_at)."""
    p = sig.payload or {}
    candidates: list[str] = []
    if sig.type in _JOB_SIGNAL_TYPES:
        v = p.get("date_posted")
        if v:
            candidates.append(str(v))
    elif sig.type == SignalType.FUNDING_RAISED:
        v = p.get("filed_on") or p.get("published")
        if v:
            candidates.append(str(v))
    for raw in candidates:
        raw = raw.strip()
        if not raw:
            continue
        # YYYY-MM-DD or ISO timestamp prefix
        try:
            return datetime.fromisoformat(raw[:19].replace("Z", ""))
        except ValueError:
            pass
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            continue
    return None


def _signal_age_days(sig: Signal, now: datetime) -> float:
    base = payload_event_date(sig) or sig.captured_at
    return max(0.0, (now - base).total_seconds() / 86400.0)


def _recency_bonus(sig: Signal, now: datetime) -> float:
    """Linear from _RECENCY_MAX (posted today) to 0 (at the signal
    type's window edge). Older-than-window contributes nothing but does
    not go negative."""
    window = _RECENCY_WINDOW_DAYS.get(sig.type, _DEFAULT_RECENCY_WINDOW_DAYS)
    frac = max(0.0, 1.0 - _signal_age_days(sig, now) / window)
    return _RECENCY_MAX * frac


_TITLE_NORM_RE = re.compile(r"[^a-z0-9]+")
_TITLE_STOPWORDS = frozenset({"of", "the", "and", "a", "an", "for", "to"})


def _norm_title(title: str | None) -> str:
    """Collapse a posting title to a coarse key so "VP Finance" and
    "VP of Finance" count as the SAME role for the multiple-postings
    bonus (we want distinct openings, not reworded reposts)."""
    if not title:
        return ""
    toks = [t for t in _TITLE_NORM_RE.sub(" ", title.lower()).split()
            if t not in _TITLE_STOPWORDS]
    return " ".join(toks)


def _distinct_hiring_titles(lead: Lead) -> int:
    keys = {
        _norm_title(s.payload.get("title"))
        for s in lead.signals
        if s.type in _HIRING_SIGNAL_TYPES and (s.payload or {}).get("title")
    }
    keys.discard("")
    return len(keys)


def _has_real_revenue(lead: Lead) -> bool:
    """True when a funding signal (Form D revenueRange or Form C
    revenue amount) shows the company has real revenue — a stronger
    operating profile than a pre-revenue shell."""
    for s in lead.signals:
        if s.type != SignalType.FUNDING_RAISED:
            continue
        p = s.payload or {}
        amt = p.get("revenue_amount")
        if isinstance(amt, (int, float)) and amt > 0:
            return True
        rng = str(p.get("revenue_range") or "").strip().lower()
        if rng and rng not in _NO_REVENUE_RANGES:
            return True
    return False


def score(lead: Lead, *, now: datetime | None = None) -> float:
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)

    types = {s.type for s in lead.signals}
    has_fractional = SignalType.JOB_POSTED_FRACTIONAL_CFO in types
    has_finance = SignalType.JOB_POSTED_FINANCE_LEAD in types
    has_funding = SignalType.FUNDING_RAISED in types

    # Tier by strongest signal present.
    if has_fractional:
        base, headroom, tier_type = (
            _TIER_BASE_FRACTIONAL, _HEADROOM_FRACTIONAL,
            SignalType.JOB_POSTED_FRACTIONAL_CFO,
        )
    elif has_finance:
        base, headroom, tier_type = (
            _TIER_BASE_FINANCE, _HEADROOM_FINANCE,
            SignalType.JOB_POSTED_FINANCE_LEAD,
        )
    elif has_funding:
        base, headroom, tier_type = (
            _TIER_BASE_FUNDING, _HEADROOM_FUNDING, SignalType.FUNDING_RAISED,
        )
    else:
        return SCORE_MIN  # only bookkeeping markers (enrichment / location)

    # Recency from the freshest signal OF THE TIER TYPE — a lead's rank
    # inside its band tracks how recently it expressed the tier signal.
    tier_recency = max(
        (_recency_bonus(s, now) for s in lead.signals if s.type == tier_type),
        default=0.0,
    )
    bonus = tier_recency

    # Bullseye: a hiring lead that ALSO raised. Real urgency, but it
    # stays inside the hiring band — a fractional posting still wins.
    if tier_type in _HIRING_SIGNAL_TYPES and has_funding:
        bonus += _BULLSEYE_BONUS

    # Multiple distinct finance openings = finance-org strain a
    # fractional CFO is hired to fix.
    extra_roles = max(0, _distinct_hiring_titles(lead) - 1)
    bonus += min(extra_roles * _EXTRA_ROLE_BONUS, _EXTRA_ROLE_CAP)

    # Headcount sweet spot for the fractional model.
    if lead.headcount is not None and _SIZE_FIT_MIN <= lead.headcount <= _SIZE_FIT_MAX:
        bonus += _SIZE_FIT_BONUS

    # Real revenue (Form D/C) beats a pre-revenue shell.
    if _has_real_revenue(lead):
        bonus += _REVENUE_BONUS

    return max(SCORE_MIN, min(SCORE_MAX, base + min(bonus, headroom)))

"""Single-niche scorer for the CFO pipeline.

Score = clamp(sum(signal_weight × recency_decay), 0, 100). No LLM,
pure deterministic, 30-day half-life carried over from the other
pipelines.

The buying model is gate-and-amplify:
- ``JOB_POSTED_FRACTIONAL_CFO`` is the in-market signal (highest
  weight). The company posted a Fractional / Interim / Part-time CFO
  role — they are literally shopping for the service being sold.
- ``JOB_POSTED_FINANCE_LEAD`` is the gate signal (high weight). The
  company is hiring finance leadership *below* CFO, which is the
  exact moment a fractional CFO is the right fit.
- ``FUNDING_RAISED`` is the urgency amplifier — meaningful only on
  top of (or near) a hiring signal. Companies with funding but no
  hiring signal are weaker prospects: their need is implicit, not
  expressed, so they don't outrank companies who are actively looking.

Compounding works through the decayed sum: fractional-CFO-posting
leads score ~80, a company with hire+funding signals ~85, hiring-only
~60, funding-only ~25.
"""

from __future__ import annotations

from datetime import datetime, timezone

from cfo_pipeline.models import Lead, Signal, SignalType

HALF_LIFE_DAYS = 30.0
SCORE_MIN = 0.0
SCORE_MAX = 100.0

SIGNAL_WEIGHTS: dict[SignalType, float] = {
    SignalType.JOB_POSTED_FRACTIONAL_CFO: 80.0,
    SignalType.JOB_POSTED_FINANCE_LEAD: 60.0,
    SignalType.FUNDING_RAISED: 25.0,
}


_JOB_SIGNAL_TYPES: frozenset[SignalType] = frozenset(
    {
        SignalType.JOB_POSTED_FRACTIONAL_CFO,
        SignalType.JOB_POSTED_FINANCE_LEAD,
    }
)


def _signal_weight(sig: Signal) -> float:
    return float(SIGNAL_WEIGHTS.get(sig.type, 0))


def payload_event_date(sig: Signal) -> datetime | None:
    """The date the EVENT happened (job posted, Form D filed) — not
    when the pipeline captured it. Job boards return postings up to a
    month old; decaying on captured_at would score a stale posting as
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


def _decay(base_dt: datetime, now: datetime) -> float:
    days = max(0.0, (now - base_dt).total_seconds() / 86400.0)
    return 0.5 ** (days / HALF_LIFE_DAYS)


def score(lead: Lead, *, now: datetime | None = None) -> float:
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    total = 0.0
    for sig in lead.signals:
        w = _signal_weight(sig)
        if w == 0:
            continue
        event_dt = payload_event_date(sig) or sig.captured_at
        total += w * _decay(event_dt, now)
    return max(SCORE_MIN, min(SCORE_MAX, total))

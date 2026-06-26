"""Single-niche scorer for the CFO pipeline.

Score = clamp(sum(signal_weight × recency_decay), 0, 100). No LLM,
pure deterministic, 30-day half-life carried over from the other
pipelines.

The buying model is gate-and-amplify:
- ``JOB_POSTED_FINANCE_LEAD`` is the gate signal (high weight). The
  company is hiring finance leadership *below* CFO, which is the
  exact moment a fractional CFO is the right fit.
- ``FUNDING_RAISED`` is the urgency amplifier — meaningful only on
  top of (or near) a hiring signal. Companies with funding but no
  hiring signal are weaker prospects: their need is implicit, not
  expressed, so they don't outrank companies who are actively looking.

Compounding works through the decayed sum: a company with both
signals scores ~85, hiring-only scores ~60, funding-only scores ~25.
"""

from __future__ import annotations

from datetime import datetime, timezone

from cfo_pipeline.models import Lead, Signal, SignalType

HALF_LIFE_DAYS = 30.0
SCORE_MIN = 0.0
SCORE_MAX = 100.0

SIGNAL_WEIGHTS: dict[SignalType, float] = {
    SignalType.JOB_POSTED_FINANCE_LEAD: 60.0,
    SignalType.FUNDING_RAISED: 25.0,
}


def _signal_weight(sig: Signal) -> float:
    return float(SIGNAL_WEIGHTS.get(sig.type, 0))


def _decay(captured_at: datetime, now: datetime) -> float:
    days = max(0.0, (now - captured_at).total_seconds() / 86400.0)
    return 0.5 ** (days / HALF_LIFE_DAYS)


def score(lead: Lead, *, now: datetime | None = None) -> float:
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    total = 0.0
    for sig in lead.signals:
        w = _signal_weight(sig)
        if w == 0:
            continue
        total += w * _decay(sig.captured_at, now)
    return max(SCORE_MIN, min(SCORE_MAX, total))

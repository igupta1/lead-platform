"""Niche-parameterized scorer.

Tiered, not additive, and applied once per niche. For a given niche the
*strongest* tier a company has a signal in sets a non-overlapping base band;
recency of its most-recent qualifying signal ("more recent = better") fills
the headroom within that band; a small bonus rewards a richer signal stack.
No fresh/aged split, no still-open re-check — just recency.

A company is scored for every niche it qualifies for (``score_all``), so one
deduped record can appear in several niche inventories.
"""

from __future__ import annotations

from datetime import datetime, timezone

from leadgen.models import Lead
from leadgen.niches import NICHES
from leadgen.niches.base import (
    EXTRA_TYPE_BONUS,
    EXTRA_TYPE_BONUS_CAP,
    RECENCY_SPAN,
    RECENCY_WINDOW_DAYS,
    NicheConfig,
)

SCORE_MIN = 1.0
SCORE_MAX = 100.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _age_days(when: datetime | None, *, now: datetime) -> float:
    if when is None:
        return RECENCY_WINDOW_DAYS  # unknown date -> zero recency headroom
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return max(0.0, (now - when).total_seconds() / 86400.0)


def _recency_factor(age_days: float) -> float:
    """Linear decay over the window: fresh -> 1.0, older-than-window -> 0.0."""
    return max(0.0, 1.0 - age_days / RECENCY_WINDOW_DAYS)


def score_lead_for_niche(
    lead: Lead, niche: NicheConfig, *, now: datetime | None = None
) -> float | None:
    """Score one company for one niche, or None if it doesn't qualify (no
    signal in any of the niche's tiers, or over the size cap)."""
    now = now or _utcnow()

    qualifying = [s for s in lead.signals if s.type in niche.all_types]
    if not qualifying:
        return None

    # Size cap is signal-aware: a breach lead is uncapped, a security-role lead
    # caps at the niche's size_cap. Use the loosest cap among qualifying signals.
    cap = niche.cap_for(s.type for s in qualifying)
    if lead.headcount is not None and lead.headcount >= cap:
        return None

    # Band from the strongest tier the company has any signal in.
    best_tier = min(
        (niche.tier_index(s.type) for s in qualifying),
        default=None,
    )
    assert best_tier is not None
    base = niche.tier_base(best_tier)

    # Recency from the most-recent qualifying signal (any tier).
    freshest_age = min(
        _age_days(s.event_date or s.captured_at, now=now) for s in qualifying
    )
    recency = RECENCY_SPAN * _recency_factor(freshest_age)

    # Richer stack bonus (kept small so it can't jump a band).
    distinct_types = len({s.type for s in qualifying})
    bonus = min((distinct_types - 1) * EXTRA_TYPE_BONUS, EXTRA_TYPE_BONUS_CAP)

    return max(SCORE_MIN, min(SCORE_MAX, base + recency + bonus))


def score_all(lead: Lead, *, now: datetime | None = None) -> dict[str, float]:
    """Score a company across every niche. Returns {niche_key: score} for the
    niches it qualifies for (empty if it qualifies for none)."""
    now = now or _utcnow()
    scores: dict[str, float] = {}
    for key, niche in NICHES.items():
        s = score_lead_for_niche(lead, niche, now=now)
        if s is not None:
            scores[key] = round(s, 1)
    return scores

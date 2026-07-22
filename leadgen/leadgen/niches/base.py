"""Niche configuration — a niche is a *query* over the shared company store.

Each niche declares which signal types qualify a company and how they band
(strongest tier first). It owns no fetching and no DB: :mod:`leadgen.scoring`
reads these configs to compute one score per niche for every company, and
:mod:`leadgen.run` projects each niche's inventory out of the same store.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from leadgen.models import SignalType

# Per-tier score base, strongest tier first. Gaps (~27) keep bands from
# overlapping so a stronger signal always outranks a weaker one regardless of
# recency. Supports up to four tiers; niches use as many as they declare.
TIER_BASES: tuple[float, ...] = (82.0, 55.0, 28.0, 12.0)

# Recency headroom added within a band (< the 27-pt gap, so no overlap) and
# the linear-decay window used to turn a signal's age into that headroom.
RECENCY_SPAN: float = 16.0
RECENCY_WINDOW_DAYS: float = 60.0

# Small reward for a richer signal stack (a company with funding AND a hire is
# a better magnet). Kept small so it can't jump a band.
EXTRA_TYPE_BONUS: float = 2.0
EXTRA_TYPE_BONUS_CAP: float = 6.0

# Uniform size cap: the magnets are SMBs under 100 employees.
DEFAULT_SIZE_CAP: int = 100


@dataclass(frozen=True)
class NicheConfig:
    key: str
    label: str
    # Strongest tier first. Each tier is the set of signal types that band
    # together; the strongest tier a company has a signal in sets its base.
    tiers: tuple[tuple[SignalType, ...], ...]
    output_filename: str
    size_cap: int = DEFAULT_SIZE_CAP
    # Per-signal size-cap overrides (e.g. a breach is valid at any size, so
    # mssp uncaps breach_disclosed). A lead's applicable cap is the LOOSEST cap
    # among its qualifying signal types.
    signal_size_caps: Mapping[SignalType, int] = field(default_factory=dict)

    @property
    def all_types(self) -> frozenset[SignalType]:
        return frozenset(t for tier in self.tiers for t in tier)

    def cap_for(self, sig_types: Iterable[SignalType]) -> int:
        return max(
            (self.signal_size_caps.get(t, self.size_cap) for t in sig_types),
            default=self.size_cap,
        )

    def tier_index(self, sig_type: SignalType) -> int | None:
        for i, tier in enumerate(self.tiers):
            if sig_type in tier:
                return i
        return None

    def tier_base(self, tier_index: int) -> float:
        return TIER_BASES[min(tier_index, len(TIER_BASES) - 1)]

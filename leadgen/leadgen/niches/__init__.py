"""The five niches, as a registry keyed by niche key.

A niche is a query over the shared company store (see ``base.NicheConfig``).
``ORDER`` is the canonical display/emit order.
"""

from leadgen.niches.accounting import CONFIG as ACCOUNTING
from leadgen.niches.base import NicheConfig
from leadgen.niches.cfo import CONFIG as CFO
from leadgen.niches.cloud import CONFIG as CLOUD
from leadgen.niches.mssp import CONFIG as MSSP
from leadgen.niches.msp import CONFIG as MSP

ORDER: tuple[NicheConfig, ...] = (ACCOUNTING, CFO, MSSP, MSP, CLOUD)
NICHES: dict[str, NicheConfig] = {c.key: c for c in ORDER}

__all__ = ["NICHES", "ORDER", "NicheConfig"]

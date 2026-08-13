"""Accounting niche — the CONTROLLER rung.

Buyer: an outsourced accounting firm. It sells a finance function: the close,
the reporting, a controller. So the gift is companies hiring at that rung —
and, best of all, companies explicitly shopping for a FRACTIONAL controller,
which is the same in-market signal the CFO niche gets from a fractional-CFO
posting.

The junior rung (bookkeeper, AP/AR, payroll) moved to ``bookkeeping``: those
1,179 companies are a different sale to a different buyer, and only 1.7% of
them also carry a controller-level signal, so the two niches barely overlap.

Overlap with ``cfo`` is real and unavoidable: both draw on JOB_FINANCE_LEAD,
because a fractional-controller-only inventory is too thin to gift from. cfo
LEADS with a fractional-CFO posting and this one leads with a fractional
controller, so the gifts still differ even where the pool is shared.
"""

from leadgen.models import SignalType
from leadgen.niches.base import NicheConfig

CONFIG = NicheConfig(
    key="accounting",
    label="Accounting",
    tiers=(
        (SignalType.JOB_FRACTIONAL_CONTROLLER,),
        (SignalType.JOB_FINANCE_LEAD,),
    ),
    output_filename="accounting-leads.json",
    # A hotel/club's finance lead reports to a management company or
    # REIT, not to a fractional-CFO or bookkeeping buyer.
    excluded_company_niches=frozenset({"hotel_lodging"}),
)

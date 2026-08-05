"""Accounting / Bookkeeping niche.

Signals (spec): Form D/C funding · junior finance hire, plus the finance-lead
hire tier (per product decision — accounting shares finance-lead with CFO).
Tier order: a company actively hiring finance help is more in-market than one
that merely raised.
"""

from leadgen.models import SignalType
from leadgen.niches.base import NicheConfig

CONFIG = NicheConfig(
    key="accounting",
    label="Accounting / Bookkeeping",
    tiers=(
        (SignalType.JOB_FINANCE_LEAD, SignalType.JOB_JUNIOR_FINANCE),
    ),
    output_filename="accounting-leads.json",
    # A hotel/club's finance lead reports to a management company or
    # REIT, not to a fractional-CFO or bookkeeping buyer.
    excluded_company_niches=frozenset({"hotel_lodging"}),
)

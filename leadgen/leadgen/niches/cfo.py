"""Fractional CFO niche.

Signals (spec + decisions): fractional/interim CFO job post (explicit intent,
hottest) · finance-lead hire · Form D funding. Form C and RSS funding are
deliberately excluded — CFO funding is Form D only.
"""

from leadgen.models import SignalType
from leadgen.niches.base import NicheConfig

CONFIG = NicheConfig(
    key="cfo",
    label="Fractional CFO",
    tiers=(
        (SignalType.JOB_FRACTIONAL_CFO,),
        (SignalType.JOB_FINANCE_LEAD,),
        (SignalType.FUNDING_FORM_D,),
    ),
    output_filename="cfo-leads.json",
)

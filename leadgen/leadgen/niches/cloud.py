"""Cloud / DevOps niche.

Signals (spec, minus surge-hiring which was dropped): DevOps/SRE roles
(in-market) · Form D funding. The eng-hiring-surge signal was intentionally
cut — too hard to implement reliably.
"""

from leadgen.models import SignalType
from leadgen.niches.base import NicheConfig

CONFIG = NicheConfig(
    key="cloud",
    label="Cloud / DevOps",
    tiers=(
        (SignalType.JOB_CLOUD_DEVOPS,),
        (SignalType.FUNDING_FORM_D,),
    ),
    output_filename="cloud-leads.json",
    size_cap=250,
)

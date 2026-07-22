"""MSP niche.

Signals (spec + decision): IT/helpdesk roles, including IT leadership. Single
tier — the two IT signal types band together and recency ranks within.
"""

from leadgen.models import SignalType
from leadgen.niches.base import NicheConfig

CONFIG = NicheConfig(
    key="msp",
    label="MSP",
    tiers=(
        (SignalType.JOB_IT_SUPPORT, SignalType.JOB_IT_LEADERSHIP),
    ),
    output_filename="msp-leads.json",
    size_cap=250,
)

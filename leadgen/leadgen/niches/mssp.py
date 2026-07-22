"""MSSP niche.

Signals (spec): breach · security roles. A disclosed breach is the urgent,
in-market signal and outranks a security-hire posting.
"""

from leadgen.models import SignalType
from leadgen.niches.base import NicheConfig

CONFIG = NicheConfig(
    key="mssp",
    label="MSSP",
    tiers=(
        (SignalType.BREACH_DISCLOSED,),
        (SignalType.JOB_SECURITY,),
    ),
    output_filename="mssp-leads.json",
    size_cap=250,
    # A breach is a valid signal at any company size (breached orgs are often
    # large hospitals / systems), so breaches are effectively uncapped;
    # security-role leads still cap at 250.
    signal_size_caps={SignalType.BREACH_DISCLOSED: 100_000},
)

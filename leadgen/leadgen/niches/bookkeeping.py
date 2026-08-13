"""Bookkeeping niche — the JUNIOR rung.

Buyer: a bookkeeping or outsourced-books firm. The gift is companies posting a
bookkeeper, staff accountant, AP/AR clerk, payroll or billing role — a business
standing at the hire-versus-outsource decision, which is the whole of that
firm's sales trigger.

Split out of ``accounting`` because the two rungs are different sales to
different buyers, and the leads barely overlap: of the companies carrying
either signal, only 1.7% carry both.

There is deliberately NO lead-first signal here, and no way to add one. A
fractional CFO is a product a company advertises for; outsourced bookkeeping is
not — a business that decides to hand its books to a firm simply stops posting.
Measured on live inventory, only 3.5% of these titles carry any outsourcing
word at all, and 90% of those say "part-time", which describes an employee
rather than a purchase. The hire-versus-outsource moment is the strongest
signal that exists here, and this niche is built on it.
"""

from leadgen.models import SignalType
from leadgen.niches.base import NicheConfig

CONFIG = NicheConfig(
    key="bookkeeping",
    label="Bookkeeping",
    tiers=(
        (SignalType.JOB_JUNIOR_FINANCE,),
    ),
    output_filename="bookkeeping-leads.json",
    excluded_company_niches=frozenset({"hotel_lodging"}),
)

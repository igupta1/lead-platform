"""Trigger classification for the CFO pipeline.

Insight / outreach-copy generation was removed (3rd-review req #8):
the LLM kept leaking the same autopilot phrases ("$300K CFO comp",
"6-10 weeks", "outgrown founder-as-CFO") across companies of wildly
different stages — the pattern was obvious to readers and damaged
credibility. The JSON output keeps an ``insight: null`` field for
schema stability; the React card collapses null cleanly.

What remains is the coarse trigger classifier used by ``daily_run``
for the UI filter chip.
"""

from __future__ import annotations

from cfo_pipeline.models import Signal, SignalType


def trigger_type(sig: Signal) -> str:
    """Coarse classifier for UI filtering. Mirrors insurance's
    ``policy_fit.trigger_type`` so the LeadsPage trigger dropdown
    works the same way."""
    if sig.type == SignalType.JOB_POSTED_FRACTIONAL_CFO:
        return "fractional_cfo_sought"
    if sig.type == SignalType.JOB_POSTED_FINANCE_LEAD:
        return "finance_hire"
    if sig.type == SignalType.FUNDING_RAISED:
        ft = sig.payload.get("filing_type")
        if ft == "Form D":
            return "form_d"
        return "funding_event"
    return "other"

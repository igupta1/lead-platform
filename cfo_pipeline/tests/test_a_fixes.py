"""System-A fixes surfaced by the live-feed card audit:
  A2 — large-NGO null-headcount denylist (CARE / care.org class)

(A1 — domain/value_prop mismatch — moved to a non-destructive System-B review
flag: string-matching a name to a domain has too many false positives
[acronyms, branded domains] to safely null data upstream.)
"""

from __future__ import annotations

from datetime import datetime

from cfo_pipeline import enrichment
from cfo_pipeline.models import Lead, Signal, SignalType, SourceName


def _lead(name: str, *, headcount: int | None = None) -> Lead:
    return Lead(
        name=name, name_key=name.lower(), headcount=headcount,
        signals=[Signal(
            type=SignalType.JOB_POSTED_FINANCE_LEAD, source=SourceName.JOBS,
            captured_at=datetime(2026, 7, 1), payload={"title": "Controller"},
        )],
    )


# --------------------------------------------------------------------------
# A2 — large-NGO denylist
# --------------------------------------------------------------------------

def test_large_ngos_dropped():
    for n in ("CARE", "care", "Catholic Charities", "American Red Cross",
              "United Way", "Feeding America", "Goodwill", "UNICEF"):
        assert enrichment._is_large_ngo(n), n
        assert enrichment._disqualification_reason(_lead(n)) == "oversized_ngo"


def test_local_chapters_and_small_nonprofits_survive():
    # exact-match only: a local chapter is a legitimate fractional-CFO client
    for n in ("Catholic Charities of Denver", "Goodwill of Central Texas",
              "YMCA of Greater Boston", "Second Harvest Food Bank",
              "Boulder Community Foundation"):
        assert not enrichment._is_large_ngo(n), n
        assert enrichment._disqualification_reason(_lead(n)) != "oversized_ngo"

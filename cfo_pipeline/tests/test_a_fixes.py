"""System-A fixes surfaced by the live-feed card audit:
  A1 — mis-resolved domain guard (Poaster Technologies -> warp.co)
  A2 — large-NGO null-headcount denylist (CARE / care.org class)
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
# A1 — domain / value_prop match guard
# --------------------------------------------------------------------------

def test_domain_mismatch_is_rejected():
    # the actual bug: a Form D filer resolved to an unrelated brand's site
    assert not enrichment._domain_matches_name("Poaster Technologies Inc.", "warp.co")
    assert not enrichment._domain_matches_name("Acme Plumbing", "salesforce.com")


def test_legitimate_domains_are_kept():
    for name, dom in [
        ("Content Raven, Inc.", "contentraven.com"),
        ("Legion Health, Inc.", "legionhealth.com"),
        ("Plastic Labs Inc.", "plasticlabs.ai"),
        ("Aspero Medical", "asperomedical.com"),
        ("Iq Sig", "iqsig.com"),
        ("International Business Machines", "ibm.com"),   # acronym
        ("American Broadcasting Company", "abc.com"),      # acronym w/ generic word
    ]:
        assert enrichment._domain_matches_name(name, dom), (name, dom)


def test_no_domain_or_unjudgeable_name_is_kept():
    assert enrichment._domain_matches_name("Anything", None)
    assert enrichment._domain_matches_name("Anything", "")
    # an all-generic name can't be judged -> keep rather than falsely drop
    assert enrichment._domain_matches_name("The Company Group", "whatever.com")


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

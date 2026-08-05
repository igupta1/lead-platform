"""Niche-parameterized scoring: tier bands, recency, size cap, multi-niche."""

from datetime import datetime, timezone

from leadgen.models import Lead, SignalType
from leadgen.niches import NICHES
from leadgen.scoring import score_all, score_lead_for_niche

from tests.conftest import make_signal


def _lead(*sigs, headcount=None):
    return Lead(name="X", name_key="x", headcount=headcount, signals=list(sigs))


NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def test_stronger_tier_outranks_weaker_regardless_of_recency():
    # fractional CFO (tier0) but stale vs finance-lead (tier1) but fresh, in cfo
    frac_stale = _lead(make_signal(SignalType.JOB_FRACTIONAL_CFO, days_ago=59))
    lead_fresh = _lead(make_signal(SignalType.JOB_FINANCE_LEAD, days_ago=0))
    cfo = NICHES["cfo"]
    assert score_lead_for_niche(frac_stale, cfo, now=NOW) > score_lead_for_niche(lead_fresh, cfo, now=NOW)


def test_recency_ranks_within_a_tier():
    fresh = _lead(make_signal(SignalType.JOB_IT_SUPPORT, days_ago=0))
    old = _lead(make_signal(SignalType.JOB_IT_SUPPORT, days_ago=59))
    msp = NICHES["msp"]
    assert score_lead_for_niche(fresh, msp, now=NOW) > score_lead_for_niche(old, msp, now=NOW)


def test_size_caps_are_per_niche():
    # accounting / cfo cap at 100
    acc = NICHES["accounting"]
    assert score_lead_for_niche(_lead(make_signal(SignalType.JOB_FINANCE_LEAD), headcount=100), acc, now=NOW) is None
    assert score_lead_for_niche(_lead(make_signal(SignalType.JOB_FINANCE_LEAD), headcount=99), acc, now=NOW) is not None
    # msp / mssp / cloud cap at 250
    mssp = NICHES["mssp"]
    assert score_lead_for_niche(_lead(make_signal(SignalType.JOB_SECURITY), headcount=250), mssp, now=NOW) is None
    assert score_lead_for_niche(_lead(make_signal(SignalType.JOB_SECURITY), headcount=249), mssp, now=NOW) is not None


def test_breach_is_uncapped_in_mssp():
    # a breach lead qualifies even at a large headcount (breached orgs are big)
    big_breach = _lead(make_signal(SignalType.BREACH_DISCLOSED), headcount=5000)
    assert score_lead_for_niche(big_breach, NICHES["mssp"], now=NOW) is not None


def test_non_qualifying_signal_returns_none():
    # a pure breach doesn't qualify for cfo
    lead = _lead(make_signal(SignalType.BREACH_DISCLOSED))
    assert score_lead_for_niche(lead, NICHES["cfo"], now=NOW) is None


def test_one_company_scores_multiple_niches():
    lead = _lead(
        make_signal(SignalType.JOB_CLOUD_DEVOPS),
        make_signal(SignalType.JOB_FINANCE_LEAD),
    )
    scores = score_all(lead, now=NOW)
    # finance-lead -> accounting (tier0) + cfo (tier0); devops -> cloud (tier0)
    assert set(scores) == {"accounting", "cfo", "cloud"}


def test_breach_outranks_security_in_mssp():
    breach = _lead(make_signal(SignalType.BREACH_DISCLOSED))
    sec = _lead(make_signal(SignalType.JOB_SECURITY))
    mssp = NICHES["mssp"]
    assert score_lead_for_niche(breach, mssp, now=NOW) > score_lead_for_niche(sec, mssp, now=NOW)


def test_richer_stack_gets_a_small_bonus():
    one = _lead(make_signal(SignalType.JOB_FINANCE_LEAD, days_ago=0))
    two = _lead(
        make_signal(SignalType.JOB_FINANCE_LEAD, days_ago=0),
        make_signal(SignalType.JOB_JUNIOR_FINANCE, days_ago=0),
    )
    acc = NICHES["accounting"]
    assert score_lead_for_niche(two, acc, now=NOW) > score_lead_for_niche(one, acc, now=NOW)


def test_undated_signal_earns_no_recency():
    # event_date=None falls back to captured_at (scrape time) in the model, so
    # counting it as recency scored missing data as maximally fresh.
    dated = _lead(make_signal(SignalType.JOB_IT_SUPPORT, days_ago=0))
    undated = _lead(make_signal(SignalType.JOB_IT_SUPPORT, days_ago=0))
    undated.signals[0].event_date = None
    msp = NICHES["msp"]
    assert score_lead_for_niche(undated, msp, now=NOW) < score_lead_for_niche(dated, msp, now=NOW)


def test_undated_does_not_beat_a_genuinely_old_dated_signal():
    undated = _lead(make_signal(SignalType.JOB_IT_SUPPORT, days_ago=0))
    undated.signals[0].event_date = None
    old = _lead(make_signal(SignalType.JOB_IT_SUPPORT, days_ago=59))
    msp = NICHES["msp"]
    assert score_lead_for_niche(undated, msp, now=NOW) <= score_lead_for_niche(old, msp, now=NOW)


def test_hotels_excluded_from_cfo_and_accounting_but_not_msp():
    hotel = Lead(name="Surf & Sand Laguna Beach", name_key="surf",
                 niche="hotel_lodging",
                 signals=[make_signal(SignalType.JOB_FINANCE_LEAD),
                          make_signal(SignalType.JOB_IT_SUPPORT)])
    assert score_lead_for_niche(hotel, NICHES["cfo"], now=NOW) is None
    assert score_lead_for_niche(hotel, NICHES["accounting"], now=NOW) is None
    # ...but a hotel is a perfectly good MSP buyer.
    assert score_lead_for_niche(hotel, NICHES["msp"], now=NOW) is not None

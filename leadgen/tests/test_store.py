"""The shared company store: global dedup, identity backfill, disqualifier
gate, signal-level dedup."""

from leadgen import db
from leadgen.models import Disqualifier, LeadCandidate, SignalType, SourceName


def test_cross_source_dedup_one_company_all_signals(conn, make_sig):
    db.upsert_lead(conn, LeadCandidate(
        name="Acme Robotics Inc", state="CA",
        initial_signal=make_sig(SignalType.FUNDING_FORM_D, url="https://sec.gov/1")))
    db.upsert_lead(conn, LeadCandidate(
        name="Acme Robotics",
        initial_signal=make_sig(SignalType.JOB_FINANCE_LEAD, url="https://jobs/1")))

    leads = list(db.iter_leads(conn))
    assert len(leads) == 1
    types = {s.type for s in leads[0].signals}
    assert types == {SignalType.FUNDING_FORM_D, SignalType.JOB_FINANCE_LEAD}


def test_identity_backfill_never_overwrites(conn, make_sig):
    db.upsert_lead(conn, LeadCandidate(
        name="Beta LLC", state="NY", domain="beta.com",
        initial_signal=make_sig(SignalType.FUNDING_FORM_D, url="https://sec.gov/2")))
    # second source knows a different domain + a city; state already set
    db.upsert_lead(conn, LeadCandidate(
        name="Beta", state="TX", domain="other.com", city="Austin",
        initial_signal=make_sig(SignalType.JOB_IT_SUPPORT, url="https://jobs/2")))
    lead = list(db.iter_leads(conn))[0]
    assert lead.domain == "beta.com"   # not overwritten
    assert lead.state == "NY"          # not overwritten
    assert lead.city == "Austin"       # backfilled (was None)


def test_disqualifier_blocks_and_sweeps(conn, make_sig):
    db.upsert_lead(conn, LeadCandidate(
        name="Gamma Inc",
        initial_signal=make_sig(SignalType.FUNDING_FORM_D, url="https://sec.gov/3")))
    key = db.mark_disqualified(conn, Disqualifier(
        name="Gamma Inc", reason="open_full_time_cfo_posting", source=SourceName.JOBS))
    db.delete_lead_by_name_key(conn, key)
    assert list(db.iter_leads(conn)) == []
    # a later candidate with the same name is refused
    assert db.upsert_lead(conn, LeadCandidate(
        name="Gamma Inc",
        initial_signal=make_sig(SignalType.JOB_FINANCE_LEAD, url="https://jobs/3"))) is None


def test_job_signal_dedup_by_title_across_boards(conn, make_sig):
    db.upsert_lead(conn, LeadCandidate(
        name="Delta Co",
        initial_signal=make_sig(SignalType.JOB_FINANCE_LEAD,
                                url="https://indeed/x", evidence="Controller")))
    # same title, different board URL -> collapses to one signal
    lead = db.upsert_lead(conn, LeadCandidate(
        name="Delta Co",
        initial_signal=make_sig(SignalType.JOB_FINANCE_LEAD,
                                url="https://linkedin/y", evidence="Controller")))
    assert len([s for s in lead.signals if s.type is SignalType.JOB_FINANCE_LEAD]) == 1


def test_funding_signal_dedup_by_url(conn, make_sig):
    db.upsert_lead(conn, LeadCandidate(
        name="Epsilon Inc",
        initial_signal=make_sig(SignalType.FUNDING_FORM_D, url="https://sec.gov/same")))
    lead = db.upsert_lead(conn, LeadCandidate(
        name="Epsilon Inc",
        initial_signal=make_sig(SignalType.FUNDING_FORM_D, url="https://sec.gov/same")))
    assert len(lead.signals) == 1


def test_merge_by_domain_collapses_spv_name_variants(conn, make_sig):
    # SEC SPV/tranche name variants that name-key dedup misses, but share a
    # domain once enriched.
    db.upsert_lead(conn, LeadCandidate(
        name="Lifesitenews 07Cfc", domain="lifesitenews.com",
        initial_signal=make_sig(SignalType.FUNDING_FORM_D, url="https://sec.gov/a")))
    db.upsert_lead(conn, LeadCandidate(
        name="Lifesitenews Bef8C", domain="lifesitenews.com", state="VA",
        initial_signal=make_sig(SignalType.JOB_FINANCE_LEAD, url="https://jobs/b")))
    assert len(list(db.iter_leads(conn))) == 2

    merged = db.merge_by_domain(conn)
    assert merged == 1
    leads = list(db.iter_leads(conn))
    assert len(leads) == 1
    # both signals survived on the single record, and state backfilled
    assert {s.type for s in leads[0].signals} == {SignalType.FUNDING_FORM_D, SignalType.JOB_FINANCE_LEAD}
    assert leads[0].state == "VA"


def test_merge_by_domain_leaves_distinct_domains(conn, make_sig):
    db.upsert_lead(conn, LeadCandidate(name="Alpha", domain="alpha.com",
        initial_signal=make_sig(SignalType.JOB_SECURITY, url="https://j/1")))
    db.upsert_lead(conn, LeadCandidate(name="Beta", domain="beta.com",
        initial_signal=make_sig(SignalType.JOB_SECURITY, url="https://j/2")))
    assert db.merge_by_domain(conn) == 0
    assert len(list(db.iter_leads(conn))) == 2


def test_set_scores_roundtrip(conn, make_sig):
    lead = db.upsert_lead(conn, LeadCandidate(
        name="Zeta Inc",
        initial_signal=make_sig(SignalType.JOB_SECURITY, url="https://jobs/z")))
    db.set_scores(conn, lead.id, {"mssp": 82.0})
    assert db.get_lead(conn, lead_id=lead.id).scores == {"mssp": 82.0}

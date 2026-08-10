"""The shared company store: global dedup, identity backfill, disqualifier
gate, signal-level dedup."""

from datetime import datetime, timedelta

from leadgen import db
from leadgen.models import Disqualifier, LeadCandidate, Signal, SignalType, SourceName
from tests.conftest import _now as _now_ref


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
        name="Gamma Inc", reason="cfo_competitor_per_llm", source=SourceName.COMPUTED))
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


def test_same_url_dedups_when_derived_title_changed(conn, make_sig):
    # The live regression: a title-format change ("Controller" ->
    # "Fractional Controller") re-split 73 CFO postings, because the title key
    # alone cannot see that it is the same posting. Same URL -> one signal.
    db.upsert_lead(conn, LeadCandidate(
        name="Zeta Co",
        initial_signal=make_sig(SignalType.JOB_FRACTIONAL_CFO, url="https://fj/same",
                                evidence="Controller", days_ago=13)))
    lead = db.upsert_lead(conn, LeadCandidate(
        name="Zeta Co",
        initial_signal=make_sig(SignalType.JOB_FRACTIONAL_CFO, url="https://fj/same",
                                evidence="Fractional Controller", days_ago=4)))
    assert len(lead.signals) == 1


def test_duplicate_keeps_the_earliest_event_date(conn, make_sig):
    # A re-scrape reading a bumped date on the same posting is not a new
    # event: keep the original date, or the lead's recency score and the
    # gift copy both claim a three-week-old posting is days old.
    db.upsert_lead(conn, LeadCandidate(
        name="Eta Co",
        initial_signal=make_sig(SignalType.JOB_FINANCE_LEAD, url="https://fj/e",
                                evidence="Controller", days_ago=13)))
    lead = db.upsert_lead(conn, LeadCandidate(
        name="Eta Co",
        initial_signal=make_sig(SignalType.JOB_FINANCE_LEAD, url="https://fj/e",
                                evidence="Fractional Controller", days_ago=4)))
    assert len(lead.signals) == 1
    assert (_now_ref() - lead.signals[0].event_date).days == 13


def test_multi_board_repost_still_collapses_by_title(conn, make_sig):
    # The URL key must not reopen what the title key exists to close: one role
    # on two boards is two URLs and one event.
    db.upsert_lead(conn, LeadCandidate(
        name="Theta Co",
        initial_signal=make_sig(SignalType.JOB_FINANCE_LEAD, url="https://indeed/a",
                                evidence="Controller")))
    lead = db.upsert_lead(conn, LeadCandidate(
        name="Theta Co",
        initial_signal=make_sig(SignalType.JOB_FINANCE_LEAD, url="https://linkedin/b",
                                evidence="Controller")))
    assert len(lead.signals) == 1


def test_dedup_pass_backfills_split_signals_to_earliest_date(conn, make_sig):
    # The nightly pass is what cleans history: two rows already in the store
    # collapse to one carrying the ORIGINAL posting date.
    db.upsert_lead(conn, LeadCandidate(
        name="Iota Co",
        initial_signal=make_sig(SignalType.JOB_FRACTIONAL_CFO, url="https://fj/i",
                                evidence="Chief Financial Officer", days_ago=13)))
    # Force the split the old key produced, bypassing the (now fixed) gate.
    import json as _json
    row = conn.execute("SELECT id, signals FROM leads").fetchone()
    sigs = _json.loads(row["signals"])
    later = dict(sigs[0])
    later["evidence_text"] = "Fractional Chief Financial Officer"
    later["payload"] = {"title": "Fractional Chief Financial Officer"}
    later["event_date"] = (_now_ref() - timedelta(days=4)).isoformat()
    conn.execute("UPDATE leads SET signals = ? WHERE id = ?",
                 (_json.dumps([sigs[0], later]), row["id"]))
    conn.commit()

    assert db.dedup_signals_pass(conn) == 1
    lead = list(db.iter_leads(conn))[0]
    assert len(lead.signals) == 1
    assert (_now_ref() - lead.signals[0].event_date).days == 13


def test_set_scores_roundtrip(conn, make_sig):
    lead = db.upsert_lead(conn, LeadCandidate(
        name="Zeta Inc",
        initial_signal=make_sig(SignalType.JOB_SECURITY, url="https://jobs/z")))
    db.set_scores(conn, lead.id, {"mssp": 82.0})
    assert db.get_lead(conn, lead_id=lead.id).scores == {"mssp": 82.0}


def test_prune_stale_drops_old_keeps_fresh(conn, make_sig):
    db.upsert_lead(conn, LeadCandidate(
        name="Fresh Co",
        initial_signal=make_sig(SignalType.JOB_IT_SUPPORT, url="https://jobs/f", days_ago=5)))
    db.upsert_lead(conn, LeadCandidate(
        name="Stale Co",
        initial_signal=make_sig(SignalType.JOB_IT_SUPPORT, url="https://jobs/s", days_ago=120)))
    assert len(list(db.iter_leads(conn))) == 2

    pruned = db.prune_stale(conn, max_age_days=90)
    assert pruned == 1
    assert {ld.name for ld in db.iter_leads(conn)} == {"Fresh Co"}


def test_prune_stale_keeps_lead_with_one_fresh_signal(conn, make_sig):
    # Newest signal wins: an old signal + a fresh one -> kept.
    lead = db.upsert_lead(conn, LeadCandidate(
        name="Mixed Co",
        initial_signal=make_sig(SignalType.FUNDING_FORM_D, url="https://sec/1", days_ago=200)))
    db.append_signal(conn, lead.id, make_sig(SignalType.JOB_FINANCE_LEAD, url="https://jobs/m", days_ago=3))
    assert db.prune_stale(conn, max_age_days=90) == 0
    assert len(list(db.iter_leads(conn))) == 1


def test_run_stats_roundtrip_returns_most_recent(conn):
    assert db.last_run_stats(conn) is None
    db.record_run_stats(conn, {"sources": {"jobs": 100}, "niches": {"cfo": 40}})
    db.record_run_stats(conn, {"sources": {"jobs": 90}, "niches": {"cfo": 38}})
    assert db.last_run_stats(conn) == {"sources": {"jobs": 90}, "niches": {"cfo": 38}}


def _candidate(company: str, title: str, url: str) -> LeadCandidate:
    """A job-post candidate keyed to one posting URL, so a second upsert with a
    different title is a DUPLICATE of the same event, not a new one."""
    return LeadCandidate(
        name=company,
        initial_signal=Signal(
            type=SignalType.JOB_FRACTIONAL_CFO, source=SourceName.FRACTIONAL_BOARD,
            captured_at=datetime(2026, 8, 9), event_date=datetime(2026, 8, 1),
            evidence_text=title, source_url=url,
            payload={"title": title, "url": url, "site": "fractionaljobs"},
        ),
    )


# --- a duplicate re-scrape may upgrade the title, never downgrade it --------

def test_duplicate_upgrades_a_bare_title_to_the_fractional_one(conn):
    """`fractional_boards._evidence_title` puts "Fractional" back onto a bare
    board listing, but the first-stored version used to win outright — freezing
    78 of 103 fractional-CFO postings holding a plain "Chief Financial Officer"
    under a subject line promising a fractional role."""
    url = "https://www.fractionaljobs.io/jobs/cfo-at-acme"
    bare = _candidate("Acme", "Chief Financial Officer", url)
    rich = _candidate("Acme", "Fractional Chief Financial Officer", url)
    db.upsert_lead(conn, bare)
    db.upsert_lead(conn, rich)
    lead = next(iter(db.iter_leads(conn)))
    assert len(lead.signals) == 1                       # still one event
    assert lead.signals[0].evidence_text == "Fractional Chief Financial Officer"


def test_a_bare_rescrape_never_strips_an_existing_qualifier(conn):
    url = "https://www.fractionaljobs.io/jobs/cfo-at-beta"
    db.upsert_lead(conn, _candidate("Beta", "Interim Chief Financial Officer", url))
    db.upsert_lead(conn, _candidate("Beta", "Chief Financial Officer", url))
    lead = next(iter(db.iter_leads(conn)))
    assert lead.signals[0].evidence_text == "Interim Chief Financial Officer"

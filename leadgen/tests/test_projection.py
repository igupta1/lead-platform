"""run.py projection: per-niche output shape, geo filter, and 'never pad
below the count'."""

from leadgen import db, run, scoring
from leadgen.models import LeadCandidate, SignalType
from leadgen.niches import NICHES

from tests.conftest import make_signal


def _seed(conn, name, sig, *, state=None):
    lead = db.upsert_lead(conn, LeadCandidate(
        name=name, state=state, domain=f"{name.lower().replace(' ','')}.com",
        initial_signal=sig))
    return lead


def _rescore(conn):
    for lead in list(db.iter_leads(conn)):
        db.set_scores(conn, lead.id, scoring.score_all(lead))


def test_projection_record_shape(conn):
    _seed(conn, "Acme Inc", make_signal(SignalType.JOB_FINANCE_LEAD,
          url="https://jobs/acme", evidence="Controller wanted"), state="CA")
    _rescore(conn)
    payload = run.project_niche(conn, NICHES["accounting"], state=None, limit=None)
    assert payload["niche"] == "accounting"
    assert payload["count"] == 1
    rec = payload["leads"][0]
    for key in ("name", "domain", "signal_type", "evidence_text", "source_url"):
        assert rec[key], f"{key} missing"
    assert rec["signal_type"] == "job_finance_lead"
    assert rec["source_url"] == "https://jobs/acme"
    assert rec["signals"] and rec["signals"][0]["evidence_text"] == "Controller wanted"


def test_geo_filter(conn):
    _seed(conn, "CA Co", make_signal(SignalType.JOB_SECURITY, url="https://j/1"), state="CA")
    _seed(conn, "TX Co", make_signal(SignalType.JOB_SECURITY, url="https://j/2"), state="TX")
    _rescore(conn)
    ca = run.project_niche(conn, NICHES["mssp"], state="CA", limit=None)
    assert [r["name"] for r in ca["leads"]] == ["CA Co"]


def test_never_pads_below_count(conn):
    _seed(conn, "Only One", make_signal(SignalType.JOB_IT_SUPPORT, url="https://j/3"))
    _rescore(conn)
    payload = run.project_niche(conn, NICHES["msp"], state=None, limit=10)
    assert payload["count"] == 1  # asked for 10, only 1 qualifies -> returns 1


def test_primary_signal_is_strongest_tier(conn):
    # a company with fractional-CFO (tier0) + form_d (tier2) headlines the CFO
    # card with the fractional post, not the funding
    lead = _seed(conn, "Dual Inc", make_signal(SignalType.JOB_FRACTIONAL_CFO,
                 url="https://jobs/frac", evidence="Fractional CFO"))
    db.append_signal(conn, lead.id, make_signal(SignalType.FUNDING_FORM_D, url="https://sec/d"))
    _rescore(conn)
    payload = run.project_niche(conn, NICHES["cfo"], state=None, limit=None)
    assert payload["leads"][0]["signal_type"] == "job_fractional_cfo"

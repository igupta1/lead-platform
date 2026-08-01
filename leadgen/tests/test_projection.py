"""run.py projection: per-niche output shape, geo filter, and 'never pad
below the count'."""

from datetime import datetime, timezone

from leadgen import db, run, scoring
from leadgen.models import LeadCandidate, SignalType
from leadgen.niches import NICHES

from tests.conftest import make_signal


def _seed(conn, name, sig, *, state=None, enriched=True, band="11-50"):
    """Seed a lead. Projection fails closed on unenriched / unsized leads, so
    the default is an enriched, sized lead — the tests that exercise those
    gates opt out explicitly."""
    lead = db.upsert_lead(conn, LeadCandidate(
        name=name, state=state, domain=f"{name.lower().replace(' ','')}.com",
        initial_signal=sig))
    updates = {}
    if enriched:
        updates["enriched_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
    if band is not None:
        updates["headcount_band"] = band
    if updates:
        db.update_lead(conn, lead.id, **updates)
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


# --- Fail-closed publish gates ---------------------------------------------
#
# An unenriched lead has no domain / vertical / insight; an unsized one has
# never been tested against the size cap. Both used to publish anyway, which
# is how a 43%-junk inventory and enterprise gifts reached outreach. They are
# held back now, and ship the night they can be enriched / sized.


def test_unenriched_lead_is_not_published(conn):
    _seed(conn, "Raw Co", make_signal(SignalType.JOB_FINANCE_LEAD, url="https://j/raw"),
          enriched=False)
    _rescore(conn)
    payload = run.project_niche(conn, NICHES["accounting"], state=None, limit=None)
    assert payload["count"] == 0


def test_unsized_lead_is_not_published(conn):
    _seed(conn, "Unsized Co", make_signal(SignalType.JOB_FINANCE_LEAD, url="https://j/uns"),
          band=None)
    _rescore(conn)
    payload = run.project_niche(conn, NICHES["accounting"], state=None, limit=None)
    assert payload["count"] == 0


def test_band_only_lead_is_published(conn):
    """No exact headcount, but a band under the cap -> ships. This is the
    ~60% of small private companies whose exact size isn't discoverable."""
    _seed(conn, "Banded Co", make_signal(SignalType.JOB_FINANCE_LEAD, url="https://j/band"),
          band="11-50")
    _rescore(conn)
    payload = run.project_niche(conn, NICHES["accounting"], state=None, limit=None)
    assert [r["name"] for r in payload["leads"]] == ["Banded Co"]


def test_band_over_cap_is_scored_out(conn):
    """A "201-1000" band exceeds the 100-person niche cap on its upper bound,
    so the lead never scores and never reaches the inventory."""
    _seed(conn, "Big Co", make_signal(SignalType.JOB_FINANCE_LEAD, url="https://j/big"),
          band="201-1000")
    _rescore(conn)
    payload = run.project_niche(conn, NICHES["accounting"], state=None, limit=None)
    assert payload["count"] == 0

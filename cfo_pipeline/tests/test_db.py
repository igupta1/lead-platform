"""DB tests — disqualifier table semantics + upsert refusal."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from cfo_pipeline import db
from cfo_pipeline.models import (
    Disqualifier,
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)


@pytest.fixture()
def conn(tmp_path: Path):
    return db.init_db(tmp_path / "leads.db")


def _candidate(name: str, *, sig_type: SignalType = SignalType.JOB_POSTED_FINANCE_LEAD) -> LeadCandidate:
    return LeadCandidate(
        name=name,
        domain=None,
        initial_signal=Signal(
            type=sig_type,
            source=SourceName.JOBS,
            captured_at=datetime(2026, 5, 16, 12, 0, 0),
            payload={"title": "Controller", "url": "x", "date_posted": "2026-05-16", "site": "indeed"},
        ),
    )


def test_upsert_then_lookup(conn):
    lead = db.upsert_lead(conn, _candidate("Acme Robotics Inc"))
    assert lead is not None
    assert lead.id is not None
    assert lead.name == "Acme Robotics Inc"
    assert lead.name_key == "acme robotics"  # legal suffix stripped
    assert len(lead.signals) == 1


def test_disqualified_blocks_upsert(conn):
    # Mark "Globex" disqualified, then attempt an upsert.
    db.mark_disqualified(
        conn,
        Disqualifier(
            name="Globex Corp",
            reason="open_full_time_cfo_posting",
            source=SourceName.JOBS,
            payload={"title": "Chief Financial Officer"},
        ),
    )
    result = db.upsert_lead(conn, _candidate("Globex Corp"))
    assert result is None
    # And no row was created.
    assert db.get_lead(conn, name_key="globex") is None


def test_disqualified_uses_normalized_key(conn):
    """A disqualifier for 'Globex Corporation' should block an upsert
    for 'Globex Inc' (same name_key after legal-suffix stripping)."""
    db.mark_disqualified(
        conn,
        Disqualifier(
            name="Globex Corporation",
            reason="open_full_time_cfo_posting",
            source=SourceName.JOBS,
            payload={},
        ),
    )
    assert db.upsert_lead(conn, _candidate("Globex, Inc.")) is None
    assert db.upsert_lead(conn, _candidate("Globex LLC")) is None


def test_delete_lead_by_name_key_sweeps_existing(conn):
    """Used in daily_run's _record_disqualifiers stage: a CFO posting
    on day N should evict a Form D lead inserted on day N-1."""
    lead = db.upsert_lead(conn, _candidate("Initech LLC"))
    assert lead is not None
    removed = db.delete_lead_by_name_key(conn, lead.name_key)
    assert removed == 1
    assert db.get_lead(conn, name_key=lead.name_key) is None


def test_is_disqualified_checks_normalized_key(conn):
    db.mark_disqualified(
        conn,
        Disqualifier(
            name="Hooli Inc",
            reason="open_full_time_cfo_posting",
            source=SourceName.JOBS,
            payload={},
        ),
    )
    assert db.is_disqualified(conn, "Hooli Inc")
    assert db.is_disqualified(conn, "Hooli, LLC")  # same name_key
    assert not db.is_disqualified(conn, "Hooli Industries")


def test_mark_disqualified_upserts(conn):
    """A second mark with a different reason should overwrite, not
    duplicate (PK is name_key)."""
    db.mark_disqualified(
        conn,
        Disqualifier(name="Acme", reason="r1", source=SourceName.JOBS, payload={}),
    )
    db.mark_disqualified(
        conn,
        Disqualifier(name="Acme", reason="r2", source=SourceName.JOBS, payload={}),
    )
    assert db.disqualified_count(conn) == 1
    entries = list(db.iter_disqualified(conn))
    assert entries[0][2] == "r2"


def test_candidate_headcount_persisted_on_insert(conn):
    cand = LeadCandidate(
        name="Sizely Co",
        domain=None,
        headcount=25,
        initial_signal=Signal(
            type=SignalType.JOB_POSTED_FINANCE_LEAD,
            source=SourceName.JOBS,
            captured_at=datetime(2026, 5, 16),
            payload={},
        ),
    )
    lead = db.upsert_lead(conn, cand)
    assert lead is not None
    assert lead.headcount == 25

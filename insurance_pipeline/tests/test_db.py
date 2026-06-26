"""Smoke tests for the insurance pipeline's DB layer."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from insurance_pipeline import db
from insurance_pipeline.models import (
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)


def _candidate(name: str, payload: dict[str, str] | None = None) -> LeadCandidate:
    return LeadCandidate(
        name=name,
        initial_signal=Signal(
            type=SignalType.NEW_BUSINESS_FILED,
            source=SourceName.SOS_FL,
            captured_at=datetime.now(timezone.utc).replace(tzinfo=None),
            payload=payload or {"state": "FL", "filing_type": "LLC"},
        ),
    )


def test_init_db_creates_schema(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "leads.db")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    # Single-niche schema: one `score`, one `insight`, no per-niche columns.
    assert "score" in cols
    assert "insight" in cols
    assert "it_msp_score" not in cols
    assert "mssp_score" not in cols
    assert "cloud_score" not in cols


def test_init_db_idempotent(tmp_path: Path) -> None:
    p = tmp_path / "leads.db"
    db.init_db(p)
    db.init_db(p)


def test_init_db_migrates_legacy_db(tmp_path: Path) -> None:
    """A pre-existing DB missing _MIGRATIONS columns must open cleanly.
    Same pattern as msp_pipeline's regression test — protects against
    CREATE INDEX running before ALTER TABLE."""
    p = tmp_path / "legacy.db"
    legacy_ddl = """
    CREATE TABLE IF NOT EXISTS leads (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        name_key    TEXT NOT NULL UNIQUE,
        signals     TEXT NOT NULL DEFAULT '[]',
        score       REAL,
        insight     TEXT,
        created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """
    pre = sqlite3.connect(p)
    with pre:
        pre.execute(legacy_ddl)
    pre.close()
    # Must not raise.
    db.init_db(p)


def test_upsert_inserts_then_merges_fuzzy(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "leads.db")

    a = db.upsert_lead(conn, _candidate("Acme Logistics LLC"))
    assert a.id is not None
    assert len(a.signals) == 1

    # Different payload → not deduped, second signal appended.
    b = db.upsert_lead(
        conn,
        _candidate("Acme Logistics LLC", payload={"state": "FL", "filing_type": "Corp"}),
    )
    assert b.id == a.id
    assert len(b.signals) == 2

    # Same name, slight variation → fuzzy match to same row.
    c = db.upsert_lead(
        conn,
        _candidate("Acme Logistics", payload={"state": "FL", "filing_type": "Corp"}),
    )
    # Same payload as `b` → dedup keeps signals at 2.
    assert c.id == a.id
    assert len(c.signals) == 2


def test_upsert_dedup_key_isolates_same_name_distinct_records(tmp_path: Path) -> None:
    """Two candidates with identical legal names but different external
    identifiers (e.g. two FMCSA carriers named 'FX TRUCKING LLC' with
    different USDOTs) stay as separate leads when `dedup_key` is set.
    Without dedup_key the fuzzy-name fallback would collapse them."""
    conn = db.init_db(tmp_path / "leads.db")

    def _fmcsa(name: str, usdot: str) -> LeadCandidate:
        return LeadCandidate(
            name=name,
            dedup_key=f"usdot:{usdot}",
            initial_signal=Signal(
                type=SignalType.NEW_MOTOR_CARRIER_AUTHORITY,
                source=SourceName.FMCSA,
                captured_at=datetime.now(timezone.utc).replace(tzinfo=None),
                payload={"usdot": usdot},
            ),
        )

    a = db.upsert_lead(conn, _fmcsa("FX TRUCKING LLC", "4582506"))
    b = db.upsert_lead(conn, _fmcsa("FX TRUCKING LLC", "4582521"))
    assert a.id != b.id, "same-name candidates with distinct USDOTs must stay separate"

    # Same USDOT a second time → routes to the same lead row. (The
    # signal-level dedup in _append_signal_row collapses identical
    # payloads, so we don't assert signal count here.)
    c = db.upsert_lead(conn, _fmcsa("FX TRUCKING LLC", "4582506"))
    assert c.id == a.id


def test_iter_leads_orders_by_score_desc_nulls_last(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "leads.db")
    a = db.upsert_lead(conn, _candidate("Aaa LLC"))
    b = db.upsert_lead(conn, _candidate("Bbb LLC"))
    c = db.upsert_lead(conn, _candidate("Ccc LLC"))
    assert a.id and b.id and c.id

    db.update_lead(conn, a.id, score=50.0)
    db.update_lead(conn, b.id, score=80.0)
    # c stays NULL

    ordered = list(db.iter_leads(conn))
    assert [lead.name for lead in ordered] == ["Bbb LLC", "Aaa LLC", "Ccc LLC"]

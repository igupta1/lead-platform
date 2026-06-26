import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from msp_pipeline.db import (
    _name_key,
    append_signal,
    dedup_signals_pass,
    delete_lead,
    get_lead,
    init_db,
    iter_leads,
    merge_duplicates,
    update_lead,
    upsert_lead,
)
from msp_pipeline.models import (
    LeadCandidate,
    NicheName,
    Signal,
    SignalType,
    SourceName,
)


def _candidate(
    name: str,
    *,
    signal_type: SignalType = SignalType.JOB_IT_SUPPORT,
    source: SourceName = SourceName.JOBS,
    payload: dict[str, Any] | None = None,
) -> LeadCandidate:
    return LeadCandidate(
        name=name,
        initial_signal=Signal(
            type=signal_type,
            source=source,
            captured_at=datetime.now(timezone.utc).replace(tzinfo=None),
            payload=payload or {},
        ),
    )


def _signal() -> Signal:
    return Signal(
        type=SignalType.JOB_IT_SUPPORT,
        source=SourceName.JOBS,
        captured_at=datetime.now(timezone.utc).replace(tzinfo=None),
        payload={},
    )


def test_init_db_idempotent(tmp_path: Path) -> None:
    p = tmp_path / "leads.db"
    init_db(p)
    init_db(p)


def test_init_db_migrates_legacy_db_missing_columns(tmp_path: Path) -> None:
    """A pre-existing leads.db that doesn't yet have the columns from
    _MIGRATIONS must open cleanly. If indexes that reference migrated
    columns ever run before the ALTER TABLEs land, CREATE INDEX hits
    'no such column' — this test catches that ordering regression."""
    import sqlite3

    p = tmp_path / "legacy.db"
    legacy_ddl = """
    CREATE TABLE IF NOT EXISTS leads (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL,
        name_key        TEXT NOT NULL UNIQUE,
        signals         TEXT NOT NULL DEFAULT '[]',
        it_msp_score    REAL,
        mssp_score      REAL,
        cloud_score     REAL,
        created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """
    pre = sqlite3.connect(p)
    with pre:
        pre.execute(legacy_ddl)
    pre.close()

    # Must not raise.
    conn = init_db(p)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    # dm_* columns are in _MIGRATIONS (added post-original-schema); they
    # should land on a legacy DB after init_db runs.
    assert "dm_name" in cols
    assert "dm_email" in cols
    assert "value_prop" in cols


def test_upsert_inserts_new_then_merges_fuzzy(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")

    a = upsert_lead(conn, _candidate("Acme Inc", payload={"title": "IT Support"}))
    assert a.id is not None
    assert a.name_key == "acme"
    assert len(a.signals) == 1

    # Distinct payload (different title) so it doesn't dedup against the first.
    b = upsert_lead(conn, _candidate("Acme Inc.", payload={"title": "Senior SRE"}))
    assert b.id == a.id
    assert len(b.signals) == 2

    c = upsert_lead(conn, _candidate("Acme Industries"))
    assert c.id != a.id
    assert len(c.signals) == 1


def test_append_signal_preserves_prior(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Foo Co"))
    assert a.id is not None

    sig2 = Signal(
        type=SignalType.FUNDING_RAISED,
        source=SourceName.FUNDING,
        captured_at=datetime.now(timezone.utc).replace(tzinfo=None),
        payload={"amount_usd": 1_000_000},
    )
    append_signal(conn, a.id, sig2)

    result = get_lead(conn, lead_id=a.id)
    assert result is not None
    assert len(result.signals) == 2
    assert result.signals[0].type == SignalType.JOB_IT_SUPPORT
    assert result.signals[1].type == SignalType.FUNDING_RAISED
    assert result.signals[1].payload == {"amount_usd": 1_000_000}


def test_iter_leads_orders_by_niche_score(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Alpha Co"))
    b = upsert_lead(conn, _candidate("Beta Co"))
    c = upsert_lead(conn, _candidate("Gamma Co"))
    assert a.id is not None and b.id is not None and c.id is not None

    update_lead(conn, a.id, it_msp_score=50.0)
    update_lead(conn, b.id, it_msp_score=80.0)

    results = list(iter_leads(conn, niche=NicheName.IT_MSP))
    assert len(results) == 3
    scores = [r.it_msp_score for r in results]
    assert scores == [80.0, 50.0, None]


def test_update_lead_persists_and_bumps_updated_at(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Delta Co"))
    assert a.id is not None and a.updated_at is not None

    time.sleep(0.01)
    update_lead(conn, a.id, industry="saas", headcount=120, country="US")

    result = get_lead(conn, lead_id=a.id)
    assert result is not None
    assert result.industry == "saas"
    assert result.headcount == 120
    assert result.country == "US"
    assert result.updated_at is not None
    assert result.updated_at > a.updated_at


def test_get_lead_requires_exactly_one_arg(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")
    with pytest.raises(ValueError):
        get_lead(conn)
    with pytest.raises(ValueError):
        get_lead(conn, lead_id=1, name_key="acme")


def test_iter_leads_min_score_filter(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Alpha Co"))
    b = upsert_lead(conn, _candidate("Beta Co"))
    c = upsert_lead(conn, _candidate("Gamma Co"))
    assert a.id is not None and b.id is not None and c.id is not None
    update_lead(conn, a.id, it_msp_score=50.0)
    update_lead(conn, b.id, it_msp_score=80.0)
    update_lead(conn, c.id, it_msp_score=30.0)

    results = list(iter_leads(conn, niche=NicheName.IT_MSP, min_score=50.0))
    assert [r.it_msp_score for r in results] == [80.0, 50.0]


def test_iter_leads_limit(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")
    upsert_lead(conn, _candidate("Alpha Co"))
    upsert_lead(conn, _candidate("Beta Co"))
    upsert_lead(conn, _candidate("Gamma Co"))
    results = list(iter_leads(conn, limit=2))
    assert len(results) == 2


def test_iter_leads_min_score_without_niche_raises(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")
    with pytest.raises(ValueError):
        list(iter_leads(conn, min_score=50.0))


def test_update_lead_rejects_unknown_field(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Foo Co"))
    assert a.id is not None
    with pytest.raises(ValueError):
        update_lead(conn, a.id, secret_field="x")


def test_update_lead_raises_on_missing_id(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")
    with pytest.raises(ValueError):
        update_lead(conn, 99999, industry="saas")


def test_append_signal_raises_on_missing_id(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")
    with pytest.raises(ValueError):
        append_signal(conn, 99999, _signal())


def test_append_signal_dedups_identical_payload(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Dup Co"))
    assert a.id is not None

    sig_payload = {"url": "https://example.com/job/1", "title": "IT Support"}
    sig = Signal(
        type=SignalType.JOB_IT_SUPPORT,
        source=SourceName.JOBS,
        captured_at=datetime.now(timezone.utc).replace(tzinfo=None),
        payload=sig_payload,
    )
    append_signal(conn, a.id, sig)
    append_signal(conn, a.id, sig)
    append_signal(conn, a.id, sig)

    result = get_lead(conn, lead_id=a.id)
    assert result is not None
    matching = [s for s in result.signals if s.payload == sig_payload]
    # Three identical appends collapse to a single stored signal.
    assert len(matching) == 1


def test_append_signal_keeps_distinct_payloads(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Distinct Co"))
    assert a.id is not None

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    titles = ["Engineer I", "Engineer II", "Engineer III"]
    for title in titles:
        append_signal(
            conn,
            a.id,
            Signal(
                type=SignalType.JOB_IT_SUPPORT,
                source=SourceName.JOBS,
                captured_at=now,
                payload={"title": title},
            ),
        )
    result = get_lead(conn, lead_id=a.id)
    assert result is not None
    seen_titles = {
        s.payload.get("title")
        for s in result.signals
        if s.type == SignalType.JOB_IT_SUPPORT and s.payload.get("title")
    }
    assert seen_titles == set(titles)


def test_append_signal_dedups_jobs_with_tracking_suffix(tmp_path: Path) -> None:
    """Adzuna pattern: same posting, different `?se=...` tracking suffix."""
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Sky Co"))
    assert a.id is not None

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    base_url = "https://www.adzuna.com/land/ad/5708002240"
    for tracking in ("3AWQYyVJ8R", "uDCLv1lI8R", "mOQoOg1I8R"):
        append_signal(
            conn,
            a.id,
            Signal(
                type=SignalType.JOB_IT_SUPPORT,
                source=SourceName.JOBS,
                captured_at=now,
                payload={"url": f"{base_url}?se={tracking}", "title": "IT Support"},
            ),
        )
    result = get_lead(conn, lead_id=a.id)
    assert result is not None
    matching = [
        s for s in result.signals
        if s.type == SignalType.JOB_IT_SUPPORT and s.payload.get("title") == "IT Support"
    ]
    assert len(matching) == 1


def test_append_signal_dedups_linkedin_same_title_distinct_paths(tmp_path: Path) -> None:
    """LinkedIn job IDs are unique per repost; same title at same company
    is one hiring pain. Job dedup ignores URL entirely."""
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("LinkedIn Co"))
    assert a.id is not None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for jid in ("4403772813", "4403775828", "4403786667"):
        append_signal(
            conn,
            a.id,
            Signal(
                type=SignalType.JOB_CLOUD_DEVOPS,
                source=SourceName.JOBS,
                captured_at=now,
                payload={
                    "url": f"https://www.linkedin.com/jobs/view/{jid}",
                    "title": "Cloud/DevOps Engineer",
                },
            ),
        )
    result = get_lead(conn, lead_id=a.id)
    assert result is not None
    matching = [
        s for s in result.signals
        if s.type == SignalType.JOB_CLOUD_DEVOPS
        and s.payload.get("title") == "Cloud/DevOps Engineer"
    ]
    assert len(matching) == 1


def test_append_signal_dedups_breach_across_state_agencies(tmp_path: Path) -> None:
    """Same breach incident reported to multiple state AGs with different
    date formats should collapse to one signal."""
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(
        conn,
        _candidate(
            "Resort Co",
            signal_type=SignalType.BREACH_DISCLOSED,
            source=SourceName.BREACHES,
            payload={"agency": "ca_ag", "reported_date": "04/28/2026"},
        ),
    )
    assert a.id is not None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    append_signal(
        conn,
        a.id,
        Signal(
            type=SignalType.BREACH_DISCLOSED,
            source=SourceName.BREACHES,
            captured_at=now,
            payload={"agency": "me_ag", "reported_date": "2026-04-28"},
        ),
    )
    result = get_lead(conn, lead_id=a.id)
    assert result is not None
    breach_sigs = [s for s in result.signals if s.type == SignalType.BREACH_DISCLOSED]
    assert len(breach_sigs) == 1


def test_append_signal_dedups_indeed_jk_for_same_title(tmp_path: Path) -> None:
    """Indeed pattern: same role re-posted with different `jk=...` IDs."""
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Indeed Co"))
    assert a.id is not None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for jk in ("b0f5c71fab", "c762c41cb1", "c0321a204f", "1db845f2ad"):
        append_signal(
            conn,
            a.id,
            Signal(
                type=SignalType.JOB_CLOUD_DEVOPS,
                source=SourceName.JOBS,
                captured_at=now,
                payload={
                    "url": f"https://www.indeed.com/viewjob?jk={jk}",
                    "title": "PingOne Cloud Engineer",
                },
            ),
        )
    result = get_lead(conn, lead_id=a.id)
    assert result is not None
    matching = [
        s for s in result.signals
        if s.type == SignalType.JOB_CLOUD_DEVOPS
        and s.payload.get("title") == "PingOne Cloud Engineer"
    ]
    assert len(matching) == 1


def test_append_signal_keeps_jobs_with_distinct_titles(tmp_path: Path) -> None:
    """Same URL path, different titles — both kept (e.g. 'IT Support' vs
    'IT Support - Backfill' are real distinct postings)."""
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Two Roles Co"))
    assert a.id is not None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for title in ("IT Support", "IT Support - Backfill"):
        append_signal(
            conn,
            a.id,
            Signal(
                type=SignalType.JOB_IT_SUPPORT,
                source=SourceName.JOBS,
                captured_at=now,
                payload={
                    "url": "https://www.adzuna.com/land/ad/5708002240?se=tracking",
                    "title": title,
                },
            ),
        )
    result = get_lead(conn, lead_id=a.id)
    assert result is not None
    titles = {
        s.payload.get("title")
        for s in result.signals
        if s.type == SignalType.JOB_IT_SUPPORT and s.payload.get("title")
    }
    assert titles == {"IT Support", "IT Support - Backfill"}


def test_append_signal_keeps_funding_with_distinct_accessions(tmp_path: Path) -> None:
    """SEC funding signals: still dedup on full payload, not URL — so
    different accessions stay distinct."""
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Fund Co"))
    assert a.id is not None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for accession in ("0002132836-26-000002", "0002132836-26-000005", "0002132834-26-000003"):
        append_signal(
            conn,
            a.id,
            Signal(
                type=SignalType.FUNDING_RAISED,
                source=SourceName.FUNDING,
                captured_at=now,
                payload={"accession": accession, "filing_date": "2026-05-05", "form": "D"},
            ),
        )
    result = get_lead(conn, lead_id=a.id)
    assert result is not None
    accessions = {
        s.payload.get("accession")
        for s in result.signals
        if s.type == SignalType.FUNDING_RAISED
    }
    assert len(accessions) == 3


def test_append_signal_never_dedups_enrichment_run(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Marker Co"))
    assert a.id is not None

    base = datetime(2026, 5, 1, 12, 0, 0)
    for i in range(3):
        append_signal(
            conn,
            a.id,
            Signal(
                type=SignalType.ENRICHMENT_RUN,
                source=SourceName.COMPUTED,
                captured_at=base + timedelta(seconds=i),
                payload={},
            ),
        )
    result = get_lead(conn, lead_id=a.id)
    assert result is not None
    markers = [s for s in result.signals if s.type == SignalType.ENRICHMENT_RUN]
    assert len(markers) == 3  # all three preserved despite identical payload


def test_dedup_signals_pass_collapses_existing_dups(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Backfill Co"))
    assert a.id is not None

    # Simulate the bug: write duplicate signals directly to bypass dedup.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    dup_sig = {
        "type": SignalType.JOB_IT_SUPPORT.value,
        "source": SourceName.JOBS.value,
        "captured_at": now.isoformat(),
        "payload": {"url": "https://example.com/job/1"},
    }
    import json as _json
    raw_signals = [dup_sig, dup_sig, dup_sig, dup_sig]
    conn.execute(
        "UPDATE leads SET signals = ? WHERE id = ?",
        (_json.dumps(raw_signals), a.id),
    )
    conn.commit()

    modified = dedup_signals_pass(conn)
    assert modified == 1

    result = get_lead(conn, lead_id=a.id)
    assert result is not None
    job_sigs = [s for s in result.signals if s.type == SignalType.JOB_IT_SUPPORT]
    assert len(job_sigs) == 1


def test_iter_leads_tolerates_legacy_unknown_signal_type(tmp_path: Path) -> None:
    """A committed DB can carry signals whose `type` was later removed from
    SignalType (e.g. the insurance-niche 'job_posted_ops_role', dropped when
    insurance was decoupled). Reading such a lead must not raise — the stale
    signal is dropped, valid ones survive. Regression for the nightly cron
    crash in purge_disqualified -> iter_leads -> _row_to_lead."""
    import json as _json

    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Legacy Co", payload={"title": "IT Support"}))
    assert a.id is not None

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    good = {
        "type": SignalType.JOB_IT_SUPPORT.value,
        "source": SourceName.JOBS.value,
        "captured_at": now.isoformat(),
        "payload": {"title": "IT Support"},
    }
    legacy = {
        "type": "job_posted_ops_role",  # no longer a member of SignalType
        "source": SourceName.JOBS.value,
        "captured_at": now.isoformat(),
        "payload": {"title": "Operations Manager"},
    }
    conn.execute(
        "UPDATE leads SET signals = ? WHERE id = ?",
        (_json.dumps([good, legacy]), a.id),
    )
    conn.commit()

    # get_lead must not raise; the bad signal is dropped.
    result = get_lead(conn, lead_id=a.id)
    assert result is not None
    assert [s.type for s in result.signals] == [SignalType.JOB_IT_SUPPORT]

    # iter_leads (the path that crashed the cron) must not raise either.
    rows = list(iter_leads(conn))
    assert len(rows) == 1
    assert [s.type for s in rows[0].signals] == [SignalType.JOB_IT_SUPPORT]


def test_append_signal_tolerates_legacy_unknown_signal_type(tmp_path: Path) -> None:
    """Appending to a lead that already carries a legacy unknown-type signal
    must not raise in the dedup scan (_append_signal_row)."""
    import json as _json

    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Legacy Append Co"))
    assert a.id is not None

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    legacy = {
        "type": "job_posted_ops_role",
        "source": SourceName.JOBS.value,
        "captured_at": now.isoformat(),
        "payload": {"title": "Ops"},
    }
    conn.execute(
        "UPDATE leads SET signals = ? WHERE id = ?",
        (_json.dumps([legacy]), a.id),
    )
    conn.commit()

    append_signal(
        conn,
        a.id,
        Signal(
            type=SignalType.FUNDING_RAISED,
            source=SourceName.FUNDING,
            captured_at=now,
            payload={"amount_usd": 1},
        ),
    )
    result = get_lead(conn, lead_id=a.id)
    assert result is not None
    # Legacy signal dropped on read; the new valid one is present.
    assert [s.type for s in result.signals] == [SignalType.FUNDING_RAISED]


def test_delete_lead_removes_row_and_raises_on_missing(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Zeta Co"))
    assert a.id is not None
    delete_lead(conn, a.id)
    assert get_lead(conn, lead_id=a.id) is None
    with pytest.raises(ValueError):
        delete_lead(conn, a.id)


def test_name_key_unicode_and_legal_suffixes() -> None:
    assert _name_key("Café Inc") == "cafe"
    assert _name_key("Acme Corporation") == "acme"
    assert _name_key("Acme & Co, LLC") == "acme"
    assert _name_key("naïve Ltd") == "naive"
    with pytest.raises(ValueError):
        _name_key("Inc.")
    with pytest.raises(ValueError):
        _name_key("LLC")
    with pytest.raises(ValueError):
        _name_key("")


# --- merge_duplicates ------------------------------------------------------


def test_merge_duplicates_by_prefix_name(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Offchain", payload={"title": "IT role A"}))
    b = upsert_lead(conn, _candidate("Offchain Labs", payload={"title": "IT role B"}))
    assert a.id is not None and b.id is not None
    assert a.id != b.id  # fuzzy upsert left them separate

    removed = merge_duplicates(conn)
    assert removed == 1

    survivor = get_lead(conn, lead_id=min(a.id, b.id))
    assert survivor is not None
    assert get_lead(conn, lead_id=max(a.id, b.id)) is None
    job_sigs = [s for s in survivor.signals if s.type == SignalType.JOB_IT_SUPPORT]
    assert len(job_sigs) == 2  # both postings folded onto the survivor


def test_merge_duplicates_by_domain(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Northwind Traders", payload={"title": "a"}))
    b = upsert_lead(conn, _candidate("Aperture Co", payload={"title": "b"}))
    assert a.id is not None and b.id is not None
    # Same company under two names — only the domain reveals it.
    update_lead(conn, a.id, domain="shared-co.com")
    update_lead(conn, b.id, domain="https://www.shared-co.com/careers")

    removed = merge_duplicates(conn)
    assert removed == 1
    survivor = get_lead(conn, lead_id=min(a.id, b.id))
    assert survivor is not None
    assert survivor.domain is not None
    assert get_lead(conn, lead_id=max(a.id, b.id)) is None
    assert len([s for s in survivor.signals if s.type == SignalType.JOB_IT_SUPPORT]) == 2


def test_merge_duplicates_leaves_distinct_companies(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "leads.db")
    a = upsert_lead(conn, _candidate("Acme"))
    b = upsert_lead(conn, _candidate("Acme Logistics"))
    assert a.id is not None and b.id is not None
    update_lead(conn, a.id, domain="acme.com")
    update_lead(conn, b.id, domain="acmelogistics.com")

    # "acme" is too short/generic to be a confident prefix; domains differ.
    assert merge_duplicates(conn) == 0
    assert get_lead(conn, lead_id=a.id) is not None
    assert get_lead(conn, lead_id=b.id) is not None

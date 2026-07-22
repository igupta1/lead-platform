"""leadgen SQLite store — the single deduped company store.

One committed ``data/leads.db`` for the whole platform (replaces the old
per-package DBs). Two tables:
  * ``leads`` — one row per company, deduped across every source. Its
    ``signals`` column holds *all* signals the company has emitted (funding,
    job posts, breach), and ``scores`` holds one score per niche.
  * ``disqualified`` — one row per name_key that must never surface in any
    niche. Today only fed by the jobs source (an open full-time CFO
    posting). Persistent: a CFO posting seen on day 1 still blocks a Form D
    filing seen on day 10.

Dedup is global now: a company that filed a Form D *and* is hiring a
Controller is one row with two signals, so it can qualify for several niches
at once. Fuzzy-name dedup on upsert; signal-level dedup on append.
"""

import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process

from leadgen.models import (
    Disqualifier,
    Lead,
    LeadCandidate,
    Signal,
    SignalType,
)

_DDL = """
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    name_key        TEXT NOT NULL UNIQUE,
    domain          TEXT,
    industry        TEXT,
    niche           TEXT,
    headcount       INTEGER,
    city            TEXT,
    state           TEXT,
    country         TEXT,
    signals         TEXT NOT NULL DEFAULT '[]',
    scores          TEXT NOT NULL DEFAULT '{}',
    insight         TEXT,
    enriched_at     TIMESTAMP,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_DDL_DISQUALIFIED = """
CREATE TABLE IF NOT EXISTS disqualified (
    name_key   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    reason     TEXT NOT NULL,
    source     TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# Additive migrations for evolving legacy DBs — run BEFORE indexes.
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("niche", "TEXT"),
    ("city", "TEXT"),
    ("state", "TEXT"),
    ("scores", "TEXT NOT NULL DEFAULT '{}'"),
    ("enriched_at", "TIMESTAMP"),
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_leads_state ON leads(state)",
)

# Scalar columns callers may update() directly. ``scores`` is a dict, set via
# set_scores(); signals via append_signal().
_UPDATABLE_FIELDS = frozenset({
    "domain",
    "industry",
    "niche",
    "headcount",
    "city",
    "state",
    "country",
    "insight",
    "enriched_at",
})

_LEGAL_SUFFIXES = (
    "incorporated",
    "corporation",
    "limited",
    "company",
    "inc",
    "llc",
    "corp",
    "co",
    "ltd",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _adapt_datetime(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(sep=" ")


def _convert_timestamp(b: bytes) -> datetime:
    return datetime.fromisoformat(b.decode())


sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_converter("TIMESTAMP", _convert_timestamp)


def name_key(name: str) -> str:
    """Public so the disqualifier API can compute keys the same way."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    while True:
        stripped = False
        for suffix in _LEGAL_SUFFIXES:
            if s.endswith(" " + suffix):
                s = s[: -len(suffix) - 1].strip()
                stripped = True
                break
            if s == suffix:
                s = ""
                stripped = True
                break
        if not stripped:
            break
    if not s:
        raise ValueError(f"name_key for {name!r} is empty after normalization")
    return s


# Internal alias preserved for the few call sites inside this module.
_name_key = name_key


# Aggressive operational-suffix list. Stripped on top of name_key for the
# cross-source join only — NOT for the primary upsert (where it would
# conflate "Acme Holdings" with "Acme"). Form D carries legal names
# ("Estately Operations LLC"); job boards carry brand names ("Estately").
_OPERATIONAL_SUFFIXES = (
    "operations",
    "holdings",
    "global",
    "international",
    "group",
    "solutions",
    "ventures",
    "labs",
    "industries",
    "studios",
    "technologies",
    "systems",
    "services",
)


def brand_key(name: str) -> str:
    """Aggressive normalization for cross-source matching. Starts from
    name_key and strips the operational suffixes above. Returns "" when the
    entire name is operational suffixes (caller should discard)."""
    s = _name_key(name)
    while True:
        stripped = False
        for suffix in _OPERATIONAL_SUFFIXES:
            if s.endswith(" " + suffix):
                s = s[: -len(suffix) - 1].strip()
                stripped = True
                break
            if s == suffix:
                s = ""
                stripped = True
                break
        if not stripped:
            break
    return s


def _row_to_lead(row: sqlite3.Row) -> Lead:
    data = dict(row)
    data["signals"] = [Signal.model_validate(s) for s in json.loads(data.get("signals") or "[]")]
    data["scores"] = json.loads(data.get("scores") or "{}")
    return Lead.model_validate(data)


def _get_lead_by_id(conn: sqlite3.Connection, lead_id: int) -> Lead | None:
    cur = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,))
    row = cur.fetchone()
    return _row_to_lead(row) if row is not None else None


def _get_lead_by_name_key(conn: sqlite3.Connection, name_key: str) -> Lead | None:
    cur = conn.execute("SELECT * FROM leads WHERE name_key = ?", (name_key,))
    row = cur.fetchone()
    return _row_to_lead(row) if row is not None else None


_BRACKETED_ID_RE = re.compile(r"\([^)]*\)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]")
_WS_RE = re.compile(r"\s+")

# Every job-post signal dedups by normalized title (Indeed + LinkedIn +
# Google Jobs otherwise give the same role 3 rows). Funding / breach dedup by
# source_url (one filing / one disclosure == one URL).
_JOB_TYPES: frozenset[SignalType] = frozenset({
    SignalType.JOB_FRACTIONAL_CFO,
    SignalType.JOB_FINANCE_LEAD,
    SignalType.JOB_JUNIOR_FINANCE,
    SignalType.JOB_IT_SUPPORT,
    SignalType.JOB_IT_LEADERSHIP,
    SignalType.JOB_SECURITY,
    SignalType.JOB_CLOUD_DEVOPS,
})
_JOB_TYPE_VALUES: frozenset[str] = frozenset(t.value for t in _JOB_TYPES)


def _normalize_job_title(title: str) -> str:
    """Lowercase, strip bracketed IDs like '(10660JFXV)', strip punctuation,
    collapse whitespace. Used to dedup the same posting across job boards."""
    s = (title or "").lower()
    s = _BRACKETED_ID_RE.sub(" ", s)
    s = _NON_ALNUM_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _signal_dedup_key(sig_dict: dict[str, Any]) -> str:
    """Per-signal-type dedup key:
    - job posts: ``type | normalized_title`` (collapses multi-board reposts).
    - funding / breach: ``type | source_url`` (one filing / disclosure).
    """
    sig_type_str = sig_dict["type"]
    payload = sig_dict.get("payload") or {}
    if sig_type_str in _JOB_TYPE_VALUES:
        title = _normalize_job_title(
            str(payload.get("title") or sig_dict.get("evidence_text") or "")
        )
        return f"{sig_type_str}|{title}"
    return f"{sig_type_str}|{sig_dict.get('source_url') or ''}"


def _append_signal_row(conn: sqlite3.Connection, lead_id: int, signal: Signal) -> None:
    cur = conn.execute("SELECT signals FROM leads WHERE id = ?", (lead_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"No lead with id={lead_id}")
    existing = json.loads(row["signals"])
    new_dict = signal.model_dump(mode="json")
    new_key = _signal_dedup_key(new_dict)
    for s in existing:
        if _signal_dedup_key(s) == new_key:
            return
    existing.append(new_dict)
    conn.execute(
        "UPDATE leads SET signals = ?, updated_at = ? WHERE id = ?",
        (json.dumps(existing), _utcnow(), lead_id),
    )


def dedup_signals_pass(conn: sqlite3.Connection) -> int:
    modified = 0
    cur = conn.execute("SELECT id, signals FROM leads")
    rows = cur.fetchall()
    with conn:
        for row in rows:
            existing = json.loads(row["signals"])
            seen: set[str] = set()
            deduped: list[dict[str, Any]] = []
            for sig in existing:
                key = _signal_dedup_key(sig)
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(sig)
            if len(deduped) != len(existing):
                conn.execute(
                    "UPDATE leads SET signals = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(deduped), _utcnow(), row["id"]),
                )
                modified += 1
    return modified


def _backfill_from_lead(conn: sqlite3.Connection, target_id: int, src: Lead) -> None:
    """Fill any identity/geo field the target is missing from ``src``."""
    tgt = _get_lead_by_id(conn, target_id)
    if tgt is None:
        return
    updates: dict[str, Any] = {}
    for f in ("domain", "headcount", "city", "state", "country", "industry", "niche", "insight"):
        if getattr(tgt, f) is None and getattr(src, f) is not None:
            updates[f] = getattr(src, f)
    if updates:
        set_parts = ", ".join(f"{k} = ?" for k in updates) + ", updated_at = ?"
        conn.execute(
            f"UPDATE leads SET {set_parts} WHERE id = ?",
            [*updates.values(), _utcnow(), target_id],
        )


def merge_by_domain(conn: sqlite3.Connection) -> int:
    """Collapse leads that resolved to the SAME domain into one record.

    Name-key dedup misses cross-source dupes and SEC SPV/tranche name variants
    ("Acme", "Acme 07Cfc", "Acme Bef8C") — but once enriched they share a
    domain. The canonical keeper is the one with the most signals (then lowest
    id); every other row's signals are merged in and the row deleted. Returns
    how many rows were merged away. Run AFTER enrichment (domains resolved)."""
    merged = 0
    with conn:
        rows = conn.execute(
            "SELECT id, domain FROM leads WHERE domain IS NOT NULL AND TRIM(domain) != ''"
        ).fetchall()
        groups: dict[str, list[int]] = {}
        for row in rows:
            groups.setdefault(row["domain"].strip().lower(), []).append(row["id"])
        for _domain, ids in groups.items():
            if len(ids) < 2:
                continue
            leads = [ld for ld in (_get_lead_by_id(conn, i) for i in ids) if ld is not None]
            if len(leads) < 2:
                continue
            canonical = max(leads, key=lambda ld: (len(ld.signals), -(ld.id or 0)))
            assert canonical.id is not None
            for ld in leads:
                if ld.id == canonical.id:
                    continue
                for sig in ld.signals:
                    _append_signal_row(conn, canonical.id, sig)
                _backfill_from_lead(conn, canonical.id, ld)
                conn.execute("DELETE FROM leads WHERE id = ?", (ld.id,))
                merged += 1
    return merged


def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute(_DDL)
        conn.execute(_DDL_DISQUALIFIED)
        cur = conn.execute("PRAGMA table_info(leads)")
        existing_cols = {row[1] for row in cur.fetchall()}
        for col_name, col_type in _MIGRATIONS:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE leads ADD COLUMN {col_name} {col_type}")
        for stmt in _INDEXES:
            conn.execute(stmt)
    return conn


# --- Disqualifier table ---------------------------------------------------


def mark_disqualified(conn: sqlite3.Connection, dq: Disqualifier) -> str:
    """Insert (or replace) a disqualifier row. Returns the canonical name_key
    so callers can also delete an existing lead with that key."""
    key = _name_key(dq.name)
    with conn:
        conn.execute(
            """INSERT INTO disqualified (name_key, name, reason, source, payload)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name_key) DO UPDATE SET
                   name=excluded.name,
                   reason=excluded.reason,
                   source=excluded.source,
                   payload=excluded.payload""",
            (key, dq.name, dq.reason, dq.source.value, json.dumps(dq.payload)),
        )
    return key


def is_disqualified(conn: sqlite3.Connection, name: str) -> bool:
    try:
        key = _name_key(name)
    except ValueError:
        return False
    cur = conn.execute("SELECT 1 FROM disqualified WHERE name_key = ? LIMIT 1", (key,))
    return cur.fetchone() is not None


def iter_disqualified(conn: sqlite3.Connection) -> Iterator[tuple[str, str, str]]:
    """Yields (name_key, name, reason) for every disqualified entry. Used by
    run.py to sweep matching leads out of the leads table."""
    cur = conn.execute("SELECT name_key, name, reason FROM disqualified")
    for row in cur:
        yield (row["name_key"], row["name"], row["reason"])


def disqualified_count(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM disqualified")
    return int(cur.fetchone()[0])


# --- Leads table ----------------------------------------------------------


def _backfill_identity(
    conn: sqlite3.Connection, lead_id: int, candidate: LeadCandidate
) -> None:
    """Fill any identity/geo/size field the existing row is missing from what
    this source knows. Never overwrites a value already set."""
    updates: dict[str, Any] = {}
    for field in ("domain", "headcount", "city", "state", "country"):
        val = getattr(candidate, field)
        if val is None:
            continue
        cur = conn.execute(f"SELECT {field} FROM leads WHERE id = ?", (lead_id,))
        row = cur.fetchone()
        if row is not None and row[field] is None:
            updates[field] = val
    if updates:
        set_parts = ", ".join(f"{k} = ?" for k in updates) + ", updated_at = ?"
        conn.execute(
            f"UPDATE leads SET {set_parts} WHERE id = ?",
            [*updates.values(), _utcnow(), lead_id],
        )


def upsert_lead(
    conn: sqlite3.Connection,
    candidate: LeadCandidate,
    *,
    fuzz_threshold: int = 90,
) -> Lead | None:
    """Upsert a candidate into the shared company store. Returns the
    resulting Lead, or None if the candidate's name is disqualified."""
    new_key = _name_key(candidate.name)

    # Disqualifier gate — every source path consults the same table.
    cur = conn.execute(
        "SELECT 1 FROM disqualified WHERE name_key = ? LIMIT 1",
        (new_key,),
    )
    if cur.fetchone() is not None:
        return None

    with conn:
        existing = _get_lead_by_name_key(conn, new_key)
        if existing is not None and existing.id is not None:
            _append_signal_row(conn, existing.id, candidate.initial_signal)
            _backfill_identity(conn, existing.id, candidate)
            result = _get_lead_by_id(conn, existing.id)
            assert result is not None
            return result

        cur = conn.execute("SELECT id, name_key FROM leads")
        rows = cur.fetchall()
        if rows:
            choices = {row["id"]: row["name_key"] for row in rows}
            match = process.extractOne(
                new_key,
                choices,
                scorer=fuzz.ratio,
                score_cutoff=float(fuzz_threshold),
            )
            if match is not None:
                _, _, matched_id = match
                _append_signal_row(conn, matched_id, candidate.initial_signal)
                _backfill_identity(conn, matched_id, candidate)
                result = _get_lead_by_id(conn, matched_id)
                assert result is not None
                return result

        now = _utcnow()
        signals_json = json.dumps([candidate.initial_signal.model_dump(mode="json")])
        cur = conn.execute(
            """INSERT INTO leads
               (name, name_key, domain, headcount, city, state, country, signals,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                candidate.name,
                new_key,
                candidate.domain,
                candidate.headcount,
                candidate.city,
                candidate.state,
                candidate.country,
                signals_json,
                now,
                now,
            ),
        )
        lead_id = cur.lastrowid
        assert lead_id is not None
        result = _get_lead_by_id(conn, lead_id)
        assert result is not None
        return result


def get_lead(
    conn: sqlite3.Connection,
    *,
    lead_id: int | None = None,
    name_key: str | None = None,
) -> Lead | None:
    if (lead_id is None) == (name_key is None):
        raise ValueError("exactly one of lead_id or name_key must be provided")
    if lead_id is not None:
        return _get_lead_by_id(conn, lead_id)
    assert name_key is not None
    return _get_lead_by_name_key(conn, name_key)


def iter_leads(conn: sqlite3.Connection, *, limit: int | None = None) -> Iterator[Lead]:
    """Yield every company. Per-niche selection/ordering happens in run.py off
    ``Lead.scores`` — the store stays niche-agnostic."""
    sql = "SELECT * FROM leads ORDER BY updated_at DESC"
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cur = conn.execute(sql, params)
    for row in cur:
        yield _row_to_lead(row)


def append_signal(conn: sqlite3.Connection, lead_id: int, signal: Signal) -> None:
    with conn:
        _append_signal_row(conn, lead_id, signal)


def delete_lead(conn: sqlite3.Connection, lead_id: int) -> None:
    with conn:
        cur = conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
        if cur.rowcount == 0:
            raise ValueError(f"No lead with id={lead_id}")


def delete_lead_by_name_key(conn: sqlite3.Connection, key: str) -> int:
    """Sweep a disqualified lead out of the leads table. Safe when no match."""
    with conn:
        cur = conn.execute("DELETE FROM leads WHERE name_key = ?", (key,))
        return cur.rowcount


def update_lead(conn: sqlite3.Connection, lead_id: int, **fields: Any) -> None:
    if not fields:
        return
    invalid = set(fields) - _UPDATABLE_FIELDS
    if invalid:
        raise ValueError(f"Cannot update fields: {sorted(invalid)}")

    set_parts = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
    values: list[Any] = list(fields.values()) + [_utcnow(), lead_id]

    with conn:
        cur = conn.execute(f"UPDATE leads SET {set_parts} WHERE id = ?", values)
        if cur.rowcount == 0:
            raise ValueError(f"No lead with id={lead_id}")


def set_scores(conn: sqlite3.Connection, lead_id: int, scores: dict[str, float]) -> None:
    """Replace a lead's per-niche score map (niche -> score)."""
    with conn:
        cur = conn.execute(
            "UPDATE leads SET scores = ?, updated_at = ? WHERE id = ?",
            (json.dumps(scores), _utcnow(), lead_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"No lead with id={lead_id}")

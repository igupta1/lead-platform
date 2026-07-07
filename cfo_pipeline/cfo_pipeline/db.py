"""CFO pipeline SQLite layer.

Two tables:
  * ``leads`` — one row per company, same shape as
    ``insurance_pipeline.db``.
  * ``disqualified`` — one row per name_key that should never appear
    on the dashboard. Today only fed by the jobs source when it sees a
    full-time CFO posting (per spec). Persistent: a CFO posting seen
    on day 1 still blocks a Form D filing seen on day 10.

Same fuzzy-name dedup on upsert and signal-level dedup on append as
the insurance pipeline. The single new method here is
``mark_disqualified`` + a disqualified-aware ``upsert_lead``.
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

from cfo_pipeline.models import (
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
    country         TEXT,
    dm_name         TEXT,
    dm_title        TEXT,
    dm_email        TEXT,
    dm_linkedin_url TEXT,
    value_prop      TEXT,
    signals         TEXT NOT NULL DEFAULT '[]',
    score           REAL,
    insight         TEXT,
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

# Migrations land BEFORE indexes inside init_db so legacy DBs migrate
# cleanly. Lesson learned from msp_pipeline's init_db ordering bug.
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("niche", "TEXT"),
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(score DESC)",
)

_UPDATABLE_FIELDS = frozenset({
    "domain",
    "industry",
    "niche",
    "headcount",
    "country",
    "dm_name",
    "dm_title",
    "dm_email",
    "dm_linkedin_url",
    "value_prop",
    "score",
    "insight",
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


# Aggressive operational-suffix list. Stripped on top of name_key for
# the bullseye cross-source join only — NOT used for the primary
# upsert (where it would conflate "Acme Holdings" with "Acme", a
# legitimately different company).
#
# Form D filings carry legal-entity names ("Estately Operations LLC")
# while job boards carry brand names ("Estately"). The default
# name_key strips the legal suffix but keeps "Operations", so the two
# don't merge. brand_key strips this second tier for the join pass.
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
    name_key and strips the operational suffixes above. Returns ""
    when the entire name is operational suffixes (caller should
    discard)."""
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
    raw = data.get("signals") or "[]"
    data["signals"] = [Signal.model_validate(s) for s in json.loads(raw)]
    return Lead.model_validate(data)


def _get_lead_by_id(conn: sqlite3.Connection, lead_id: int) -> Lead | None:
    cur = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,))
    row = cur.fetchone()
    return _row_to_lead(row) if row is not None else None


def _get_lead_by_name_key(conn: sqlite3.Connection, name_key: str) -> Lead | None:
    cur = conn.execute("SELECT * FROM leads WHERE name_key = ?", (name_key,))
    row = cur.fetchone()
    return _row_to_lead(row) if row is not None else None


# Signals never deduped (one row per event, even if payload matches).
_NEVER_DEDUP: frozenset[SignalType] = frozenset({SignalType.ENRICHMENT_RUN})


_BRACKETED_ID_RE = re.compile(r"\([^)]*\)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]")
_WS_RE = re.compile(r"\s+")


def _normalize_job_title(title: str) -> str:
    """Lowercase, strip bracketed IDs like '(10660JFXV)', strip
    punctuation, collapse whitespace. Used to dedup the same posting
    across job boards (Indeed + LinkedIn + Google Jobs give the same
    role 3 distinct rows otherwise)."""
    s = (title or "").lower()
    s = _BRACKETED_ID_RE.sub(" ", s)
    s = _NON_ALNUM_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _signal_dedup_key(sig_dict: dict[str, Any]) -> str:
    """Per-signal-type dedup keys:

    - job_posted_finance_lead / job_posted_fractional_cfo:
      ``(type, normalized_title)``. Strips board / url / date_posted /
      bracketed IDs so multi-board cross-postings collapse into one
      signal. This was the regression reported on the 3rd review pass.
    - everything else: type + full payload (back-compat with prior
      behavior for FUNDING_RAISED / APOLLO_ENRICHED markers).
    """
    sig_type_str = sig_dict["type"]
    payload = sig_dict.get("payload") or {}
    if sig_type_str in (
        SignalType.JOB_POSTED_FINANCE_LEAD.value,
        SignalType.JOB_POSTED_FRACTIONAL_CFO.value,
    ):
        title = _normalize_job_title(str(payload.get("title") or ""))
        return f"{sig_type_str}|{title}"
    return f"{sig_type_str}|{json.dumps(payload, sort_keys=True, default=str)}"


def _append_signal_row(conn: sqlite3.Connection, lead_id: int, signal: Signal) -> None:
    cur = conn.execute("SELECT signals FROM leads WHERE id = ?", (lead_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"No lead with id={lead_id}")
    existing = json.loads(row["signals"])
    new_dict = signal.model_dump(mode="json")
    if signal.type not in _NEVER_DEDUP:
        new_key = _signal_dedup_key(new_dict)
        for s in existing:
            if SignalType(s["type"]) in _NEVER_DEDUP:
                continue
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
                if SignalType(sig["type"]) in _NEVER_DEDUP:
                    deduped.append(sig)
                    continue
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
    """Insert (or replace) a disqualifier row. Returns the canonical
    name_key so callers can also delete an existing lead with that key
    in the same transaction."""
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
    """Yields (name_key, name, reason) for every disqualified entry.
    Used by daily_run to sweep matching leads out of the leads table."""
    cur = conn.execute("SELECT name_key, name, reason FROM disqualified")
    for row in cur:
        yield (row["name_key"], row["name"], row["reason"])


def disqualified_count(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM disqualified")
    return int(cur.fetchone()[0])


# --- Leads table ----------------------------------------------------------


def upsert_lead(
    conn: sqlite3.Connection,
    candidate: LeadCandidate,
    *,
    fuzz_threshold: int = 90,
) -> Lead | None:
    """Upsert a candidate. Returns the resulting Lead, or None if the
    candidate's name is in the disqualified table (caller should
    discard)."""
    new_key = _name_key(candidate.name)

    # Disqualifier gate — checked here so every source path (jobs,
    # funding, edgar_form_d) consults the same table without each
    # source having to remember to.
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
            if candidate.headcount is not None and existing.headcount is None:
                conn.execute(
                    "UPDATE leads SET headcount = ?, updated_at = ? WHERE id = ?",
                    (candidate.headcount, _utcnow(), existing.id),
                )
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
                if candidate.headcount is not None:
                    cur2 = conn.execute(
                        "SELECT headcount FROM leads WHERE id = ?", (matched_id,)
                    )
                    row2 = cur2.fetchone()
                    if row2 is not None and row2["headcount"] is None:
                        conn.execute(
                            "UPDATE leads SET headcount = ?, updated_at = ? WHERE id = ?",
                            (candidate.headcount, _utcnow(), matched_id),
                        )
                result = _get_lead_by_id(conn, matched_id)
                assert result is not None
                return result

        now = _utcnow()
        signals_json = json.dumps([candidate.initial_signal.model_dump(mode="json")])
        cur = conn.execute(
            """INSERT INTO leads (name, name_key, domain, headcount, signals, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                candidate.name,
                new_key,
                candidate.domain,
                candidate.headcount,
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


def iter_leads(
    conn: sqlite3.Connection,
    *,
    min_score: float | None = None,
    limit: int | None = None,
) -> Iterator[Lead]:
    sql = "SELECT * FROM leads"
    params: list[Any] = []

    if min_score is not None:
        sql += " WHERE score >= ?"
        params.append(min_score)

    sql += " ORDER BY score IS NULL, score DESC"

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
    """Used by daily_run to sweep disqualified leads out of the leads
    table. Safe to call when no lead matches (returns 0)."""
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
        cur = conn.execute(
            f"UPDATE leads SET {set_parts} WHERE id = ?",
            values,
        )
        if cur.rowcount == 0:
            raise ValueError(f"No lead with id={lead_id}")

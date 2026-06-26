"""Insurance pipeline SQLite layer.

One table, one ``score`` column (no per-niche fan-out), additive
migrations via _MIGRATIONS, fuzzy-name dedup on upsert, signal-level
dedup on append. Same shape as ``msp_pipeline.db`` but with the
insurance-pipeline-specific schema.
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

from insurance_pipeline.models import (
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

# Migrations land BEFORE indexes inside init_db so legacy DBs migrate
# cleanly. Lesson learned from msp_pipeline's init_db ordering bug.
_MIGRATIONS: tuple[tuple[str, str], ...] = ()

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(score DESC)",
)

_UPDATABLE_FIELDS = frozenset({
    "domain",
    "industry",
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


def _name_key(name: str) -> str:
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


def _normalize_date(s: str) -> str:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


def _signal_dedup_key(sig_dict: dict[str, Any]) -> str:
    """Stable key per signal. Excludes captured_at and source. For new
    business / FMCSA signals, the unique identifier is in the payload
    (USDOT number, filing id, etc.). Default to type + sorted payload."""
    sig_type_str = sig_dict["type"]
    payload = sig_dict.get("payload") or {}
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
        # Migrations BEFORE indexes so a legacy DB without a newly-added
        # column doesn't fail CREATE INDEX. (Same lesson as msp_pipeline.)
        cur = conn.execute("PRAGMA table_info(leads)")
        existing_cols = {row[1] for row in cur.fetchall()}
        for col_name, col_type in _MIGRATIONS:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE leads ADD COLUMN {col_name} {col_type}")
        for stmt in _INDEXES:
            conn.execute(stmt)
    return conn


def upsert_lead(
    conn: sqlite3.Connection,
    candidate: LeadCandidate,
    *,
    fuzz_threshold: int = 90,
) -> Lead:
    # Sources with a unique external identifier (FMCSA → USDOT) pass
    # `dedup_key` so two carriers with the same legal name in different
    # states don't fuzzy-collapse into one lead. Exact match only;
    # the fuzzy fallback is skipped.
    if candidate.dedup_key is not None:
        new_key = candidate.dedup_key
        use_fuzzy = False
    else:
        new_key = _name_key(candidate.name)
        use_fuzzy = True

    with conn:
        existing = _get_lead_by_name_key(conn, new_key)
        if existing is not None and existing.id is not None:
            _append_signal_row(conn, existing.id, candidate.initial_signal)
            result = _get_lead_by_id(conn, existing.id)
            assert result is not None
            return result

        if use_fuzzy:
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
                    result = _get_lead_by_id(conn, matched_id)
                    assert result is not None
                    return result

        now = _utcnow()
        signals_json = json.dumps([candidate.initial_signal.model_dump(mode="json")])
        cur = conn.execute(
            """INSERT INTO leads (name, name_key, domain, signals, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (candidate.name, new_key, candidate.domain, signals_json, now, now),
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

import json
import logging
import re
import sqlite3
import unicodedata
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from rapidfuzz import fuzz, process

from msp_pipeline.models import (
    Lead,
    LeadCandidate,
    NicheName,
    Signal,
    SignalType,
)

_log = logging.getLogger(__name__)

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
    it_msp_score    REAL,
    mssp_score      REAL,
    cloud_score     REAL,
    it_msp_insight  TEXT,
    mssp_insight    TEXT,
    cloud_insight   TEXT,
    it_msp_outreach TEXT,
    mssp_outreach   TEXT,
    cloud_outreach  TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# Columns added after the original schema. init_db runs ALTER TABLE for any
# of these that don't exist yet, so an older committed DB picks up the new
# fields on first open.
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("dm_name", "TEXT"),
    ("dm_title", "TEXT"),
    ("dm_email", "TEXT"),
    ("dm_linkedin_url", "TEXT"),
    ("value_prop", "TEXT"),
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_leads_it_msp_score ON leads(it_msp_score DESC)",
    "CREATE INDEX IF NOT EXISTS idx_leads_mssp_score   ON leads(mssp_score DESC)",
    "CREATE INDEX IF NOT EXISTS idx_leads_cloud_score  ON leads(cloud_score DESC)",
)

_NICHE_SCORE_COLUMN: dict[NicheName, str] = {
    NicheName.IT_MSP: "it_msp_score",
    NicheName.MSSP: "mssp_score",
    NicheName.CLOUD: "cloud_score",
}

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
    "it_msp_score",
    "mssp_score",
    "cloud_score",
    "it_msp_insight",
    "mssp_insight",
    "cloud_insight",
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


def _parse_signals(raw_signals: list[Any], *, lead_id: Any = None) -> list[Signal]:
    """Validate stored signals, dropping any that no longer fit the schema.

    The committed leads.db outlives individual code versions, so a row can
    carry a signal whose ``type`` has since been removed from SignalType
    (e.g. the insurance-niche ``job_posted_ops_role``, dropped when insurance
    was decoupled). One such legacy signal must not crash the whole run —
    skip it with a warning instead of letting ``model_validate`` raise."""
    out: list[Signal] = []
    for s in raw_signals:
        try:
            out.append(Signal.model_validate(s))
        except ValidationError:
            sig_type = s.get("type") if isinstance(s, dict) else s
            _log.warning(
                "dropping unparseable signal on lead id=%s (type=%r)",
                lead_id, sig_type,
            )
    return out


def _row_to_lead(row: sqlite3.Row) -> Lead:
    data = dict(row)
    raw = data.get("signals") or "[]"
    data["signals"] = _parse_signals(json.loads(raw), lead_id=data.get("id"))
    return Lead.model_validate(data)


def _get_lead_by_id(conn: sqlite3.Connection, lead_id: int) -> Lead | None:
    cur = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,))
    row = cur.fetchone()
    return _row_to_lead(row) if row is not None else None


def _get_lead_by_name_key(conn: sqlite3.Connection, name_key: str) -> Lead | None:
    cur = conn.execute("SELECT * FROM leads WHERE name_key = ?", (name_key,))
    row = cur.fetchone()
    return _row_to_lead(row) if row is not None else None


# Signals of these types are timestamp markers (one per event) — never dedup
# them, even if their payloads happen to be identical. Skipping these ensures
# the M4 enrichment-skip logic still sees per-run timestamps.
_NEVER_DEDUP: frozenset[SignalType] = frozenset({SignalType.ENRICHMENT_RUN})


def _is_never_dedup(type_str: str) -> bool:
    """Whether a stored signal-type string is a known never-dedup marker.
    Unknown/legacy type strings (written by an older schema) are treated as
    dedupable and, crucially, never raise here — callers iterate over raw
    stored signals that may predate the current SignalType enum."""
    try:
        return SignalType(type_str) in _NEVER_DEDUP
    except ValueError:
        return False

# Job-style signals carry URLs that often have per-fetch tracking suffixes
# (Adzuna `?se=...`, Indeed `?jk=...`). Dedup on (type, title, url-path) so
# the same posting fetched multiple times collapses to one signal.
_JOB_LIKE: frozenset[SignalType] = frozenset({
    SignalType.JOB_IT_SUPPORT,
    SignalType.JOB_IT_LEADERSHIP,
    SignalType.JOB_SECURITY,
    SignalType.JOB_CLOUD_DEVOPS,
    SignalType.EXEC_HIRED,
})


def _normalize_date(s: str) -> str:
    """Best-effort canonicalize a date string to YYYY-MM-DD. Returns the
    original string if no known format matches."""
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


def _signal_dedup_key(sig_dict: dict[str, Any]) -> str:
    """Stable key per signal. Excludes captured_at and source so two fetches
    of the same event collapse. Per-type rules:

    - Job-type signals: dedup on (type, title) only. Same posting may carry
      slightly different URLs across reposts (LinkedIn IDs, Indeed `jk=`,
      Adzuna `?se=`); same role at the same company is one hiring pain.
    - BREACH_DISCLOSED: dedup on (type, normalized reported_date). State AGs
      report the same incident in different formats — collapse them.
    - Everything else: dedup on (type, full payload) — funding accessions,
      RSS links, etc. are genuinely distinct identifiers.
    """
    sig_type_str = sig_dict["type"]
    payload = sig_dict.get("payload") or {}
    try:
        sig_type = SignalType(sig_type_str)
    except ValueError:
        sig_type = None
    if sig_type in _JOB_LIKE:
        title = (payload.get("title") or "").strip().lower()
        return f"{sig_type_str}|{title}"
    if sig_type == SignalType.BREACH_DISCLOSED:
        date_raw = str(payload.get("reported_date") or "")
        return f"{sig_type_str}|{_normalize_date(date_raw)}"
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
            if _is_never_dedup(s["type"]):
                continue
            if _signal_dedup_key(s) == new_key:
                return  # already on file; skip the append + DB write
    existing.append(new_dict)
    conn.execute(
        "UPDATE leads SET signals = ?, updated_at = ? WHERE id = ?",
        (json.dumps(existing), _utcnow(), lead_id),
    )


def dedup_signals_pass(conn: sqlite3.Connection) -> int:
    """One-shot dedup over every lead's signals JSON. Returns count of leads
    whose array was modified. Markers (ENRICHMENT_RUN) preserved as-is."""
    modified = 0
    cur = conn.execute("SELECT id, signals FROM leads")
    rows = cur.fetchall()
    with conn:
        for row in rows:
            existing = json.loads(row["signals"])
            seen: set[str] = set()
            deduped: list[dict[str, Any]] = []
            for sig in existing:
                if _is_never_dedup(sig["type"]):
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


# Scalar fields filled onto a merge survivor from the rows being merged away,
# when the survivor's own value is missing. Lets the enriched duplicate donate
# its domain / headcount / DM data to the row we keep.
_MERGE_FILL_FIELDS = (
    "domain", "industry", "headcount", "country",
    "dm_name", "dm_title", "dm_email", "dm_linkedin_url", "value_prop",
)


def _normalize_domain(domain: str | None) -> str | None:
    if not domain:
        return None
    d = domain.strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0]
    d = re.sub(r"^www\.", "", d).rstrip("/")
    return d or None


def _is_prefix_subset(short_tokens: list[str], long_tokens: list[str]) -> bool:
    """True when ``short_tokens`` is a strict leading token-subsequence of
    ``long_tokens`` AND specific enough to be a confident match: at least two
    tokens, or a single token of >= 7 chars. Catches "offchain" ⊂ "offchain
    labs" and "sandhills medical" ⊂ "sandhills medical foundation" without
    collapsing generic short names like "acme" ⊂ "acme logistics"."""
    if not (0 < len(short_tokens) < len(long_tokens)):
        return False
    if long_tokens[: len(short_tokens)] != short_tokens:
        return False
    return len(short_tokens) >= 2 or len(short_tokens[0]) >= 7


def merge_duplicates(conn: sqlite3.Connection) -> int:
    """Merge leads that are the same company under different names. Two leads
    merge when they share a normalized domain, or when one name_key is a
    confident leading-subsequence of the other (see ``_is_prefix_subset``).

    The lowest-id row survives; the others donate their signals (deduped) and
    any scalar field the survivor is missing, then are deleted. Returns the
    number of rows removed. Run after enrichment (so domains are populated)
    and before scoring."""
    rows = conn.execute("SELECT id, name_key, domain FROM leads ORDER BY id").fetchall()
    if len(rows) < 2:
        return 0

    parent: dict[int, int] = {row["id"]: row["id"] for row in rows}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    by_domain: dict[str, list[int]] = {}
    for row in rows:
        dom = _normalize_domain(row["domain"])
        if dom:
            by_domain.setdefault(dom, []).append(row["id"])
    for ids in by_domain.values():
        for other in ids[1:]:
            union(ids[0], other)

    tokenized = [(row["id"], row["name_key"].split()) for row in rows]
    for i_id, i_tok in tokenized:
        for j_id, j_tok in tokenized:
            if i_id != j_id and _is_prefix_subset(i_tok, j_tok):
                union(i_id, j_id)

    groups: dict[int, list[int]] = {}
    for lead_id in parent:
        groups.setdefault(find(lead_id), []).append(lead_id)

    removed = 0
    with conn:
        for members in groups.values():
            if len(members) < 2:
                continue
            members.sort()
            survivor = members[0]
            survivor_row = dict(
                conn.execute("SELECT * FROM leads WHERE id = ?", (survivor,)).fetchone()
            )
            fills: dict[str, Any] = {}
            for other in members[1:]:
                other_row = conn.execute(
                    "SELECT * FROM leads WHERE id = ?", (other,)
                ).fetchone()
                if other_row is None:
                    continue
                for field in _MERGE_FILL_FIELDS:
                    if survivor_row.get(field) is None and fills.get(field) is None:
                        if other_row[field] is not None:
                            fills[field] = other_row[field]
                for sig in _parse_signals(json.loads(other_row["signals"]), lead_id=other):
                    _append_signal_row(conn, survivor, sig)
                _log.info(
                    "merge: folding lead %d into %d (name_key=%r)",
                    other, survivor, other_row["name_key"],
                )
                conn.execute("DELETE FROM leads WHERE id = ?", (other,))
                removed += 1
            if fills:
                set_parts = ", ".join(f"{k} = ?" for k in fills) + ", updated_at = ?"
                conn.execute(
                    f"UPDATE leads SET {set_parts} WHERE id = ?",
                    [*fills.values(), _utcnow(), survivor],
                )
    return removed


def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute(_DDL)
        # Idempotent column migrations FIRST: add any column from
        # _MIGRATIONS that isn't already on the table. Indexes below
        # may reference these columns, so the ALTER TABLEs must land
        # before CREATE INDEX runs against an older committed DB.
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
    new_key = _name_key(candidate.name)

    with conn:
        existing = _get_lead_by_name_key(conn, new_key)
        if existing is not None and existing.id is not None:
            _append_signal_row(conn, existing.id, candidate.initial_signal)
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
    niche: NicheName | None = None,
    min_score: float | None = None,
    limit: int | None = None,
) -> Iterator[Lead]:
    if min_score is not None and niche is None:
        raise ValueError("min_score requires niche")

    sql = "SELECT * FROM leads"
    params: list[Any] = []

    if niche is not None:
        col = _NICHE_SCORE_COLUMN[niche]
        if min_score is not None:
            sql += f" WHERE {col} >= ?"
            params.append(min_score)
        sql += f" ORDER BY {col} IS NULL, {col} DESC"

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

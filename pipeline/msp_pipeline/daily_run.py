"""End-to-end pipeline runner.

Fetch -> upsert -> enrich -> score -> regenerate copy -> write JSON.
Per-source / per-lead failures log and continue. Designed to be run
nightly by GitHub Actions cron (M10).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from msp_pipeline import apollo, db, enrichment, outreach, scoring
from msp_pipeline.models import (
    Lead,
    LeadCandidate,
    NicheName,
    Signal,
    SignalType,
    SourceName,
)
from msp_pipeline.sources import breaches, funding, jobs

log = logging.getLogger("daily_run")

DEFAULT_DB_PATH = Path("data/leads.db")
DEFAULT_OUTPUT_PATH = Path("data/leads.json")
LOOKBACK_DAYS = 14
INSIGHT_THRESHOLD = 20.0
INSIGHT_DELTA_THRESHOLD = 10.0
SIGNAL_LIMIT_IN_JSON = 6
APOLLO_TOP_N_DEFAULT = 30


_NICHE_SCORE_COL: dict[NicheName, str] = {
    NicheName.IT_MSP: "it_msp_score",
    NicheName.MSSP: "mssp_score",
    NicheName.CLOUD: "cloud_score",
}
_NICHE_INSIGHT_COL: dict[NicheName, str] = {
    NicheName.IT_MSP: "it_msp_insight",
    NicheName.MSSP: "mssp_insight",
    NicheName.CLOUD: "cloud_insight",
}


# Raw agency codes that must never survive in a prospect-facing insight. A
# stored insight containing one was generated before outreach.py learned to
# render readable agency names; force its regeneration (see _regen_copy).
_RAW_AGENCY_CODES: tuple[str, ...] = tuple(breaches.AGENCY_DISPLAY_NAMES)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _insight_has_raw_agency_code(text: str | None) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(code in low for code in _RAW_AGENCY_CODES)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.dry_run:
        log.info("dry-run: skipping all writes (DB, LLM calls, JSON)")
        candidates = _fetch_all(
            since=_utcnow() - timedelta(days=LOOKBACK_DAYS),
            per_source_limit=args.limit,
        )
        log.info("dry-run fetched %d candidates total", len(candidates))
        return 0

    if args.rescore_only:
        log.info("rescore-only: skipping fetch / upsert")
        args.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = db.init_db(args.db_path)

        if args.reenrich:
            existing_ids = [
                lead.id for lead in db.iter_leads(conn) if lead.id is not None
            ]
            log.info("re-enriching %d existing leads (force=True)", len(existing_ids))
            kept_ids = _enrich_all(conn, existing_ids, force=True)
            log.info("kept %d leads after re-enrichment", len(kept_ids))

        modified = db.dedup_signals_pass(conn)
        log.info("deduped signals on %d leads", modified)

        purged = enrichment.purge_disqualified(conn)
        log.info("purged %d disqualified leads", purged)

        merged = db.merge_duplicates(conn)
        log.info("merged %d duplicate leads", merged)

        all_ids = [lead.id for lead in db.iter_leads(conn) if lead.id is not None]
        log.info("re-scoring %d leads", len(all_ids))

        score_changes = _rescore_all(conn, all_ids)
        apollo_ids = _apollo_enrich_top_n(conn, n=args.apollo_top_n)
        copy_calls = _regen_copy(
            conn, score_changes,
            force_regen_ids=apollo_ids,
            model=args.copy_model,
        )
        log.info(
            "re-scored %d leads, apollo-enriched %d, regenerated copy %d times",
            len(score_changes), len(apollo_ids), copy_calls,
        )

        output = _build_output(conn)
        args.output_path.parent.mkdir(parents=True, exist_ok=True)
        args.output_path.write_text(json.dumps(output, indent=2, default=str))
        log.info("wrote %s", args.output_path)

        if args.upload:
            try:
                _upload_blob(args.output_path)
            except Exception:
                log.exception("upload failed")
                return 1
        return 0

    candidates = _fetch_all(
        since=_utcnow() - timedelta(days=LOOKBACK_DAYS),
        per_source_limit=args.limit,
    )
    log.info("fetched %d candidates across all sources", len(candidates))

    args.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.init_db(args.db_path)

    upserted_ids = _upsert_all(conn, candidates)
    log.info("upserted %d unique leads", len(upserted_ids))

    enriched_ids = _enrich_all(conn, upserted_ids, force=args.reenrich)
    log.info("kept %d enriched leads", len(enriched_ids))

    purged = enrichment.purge_disqualified(conn)
    log.info("purged %d disqualified leads", purged)

    merged = db.merge_duplicates(conn)
    log.info("merged %d duplicate leads", merged)

    # Re-score EVERY lead, not only the ones touched this run. A niche score
    # depends on recency decay (which changes daily) and on the current
    # scoring rules, so an untouched lead still needs refreshing — otherwise a
    # stale score keeps it pinned to a niche it no longer qualifies for (e.g. a
    # breach lead lingering in IT MSP with an empty signal list).
    all_ids = [lead.id for lead in db.iter_leads(conn) if lead.id is not None]
    score_changes = _rescore_all(conn, all_ids)
    apollo_ids = _apollo_enrich_top_n(conn, n=args.apollo_top_n)
    copy_calls = _regen_copy(
        conn, score_changes,
        force_regen_ids=apollo_ids,
        model=args.copy_model,
    )
    log.info(
        "re-scored %d leads, apollo-enriched %d, generated copy %d times",
        len(score_changes), len(apollo_ids), copy_calls,
    )

    output = _build_output(conn)
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(json.dumps(output, indent=2, default=str))
    log.info("wrote %s", args.output_path)

    if args.upload:
        try:
            _upload_blob(args.output_path)
        except Exception:
            log.exception("upload failed")
            return 1
    return 0


# --- Stages ----------------------------------------------------------------


def _fetch_all(
    *, since: datetime, per_source_limit: int | None
) -> list[LeadCandidate]:
    out: list[LeadCandidate] = []
    for name, fn in (
        ("jobs", jobs.fetch),
        ("funding", funding.fetch),
        ("breaches", breaches.fetch),
    ):
        try:
            cs = fn(since=since, limit=per_source_limit)
            log.info("source %s returned %d candidates", name, len(cs))
            out.extend(cs)
        except Exception as exc:
            log.exception("source %s failed: %s", name, exc)
    return out


def _upsert_all(
    conn: sqlite3.Connection, candidates: list[LeadCandidate]
) -> list[int]:
    ids: list[int] = []
    for c in candidates:
        try:
            lead = db.upsert_lead(conn, c)
            assert lead.id is not None
            ids.append(lead.id)
        except Exception:
            log.exception("upsert failed for %r", c.name)
    return list(dict.fromkeys(ids))


def _enrich_all(
    conn: sqlite3.Connection, lead_ids: list[int], *, force: bool
) -> list[int]:
    kept: list[int] = []
    for lead_id in lead_ids:
        lead = db.get_lead(conn, lead_id=lead_id)
        if lead is None:
            continue
        try:
            if enrichment.enrich(conn, lead, force=force):
                kept.append(lead_id)
        except Exception:
            log.exception(
                "enrichment failed for lead %s (id=%s)", lead.name, lead.id
            )
            kept.append(lead_id)
    return kept


def _rescore_all(
    conn: sqlite3.Connection, lead_ids: list[int]
) -> dict[int, dict[NicheName, tuple[float | None, float | None]]]:
    """Compute fresh scores for each lead and persist them. Returns
    per-lead per-niche (old, new) pairs so the regen stage can apply
    threshold-crossing logic without re-reading the pre-update state."""
    changes: dict[int, dict[NicheName, tuple[float | None, float | None]]] = {}
    for lead_id in lead_ids:
        lead = db.get_lead(conn, lead_id=lead_id)
        if lead is None:
            continue
        new_scores = scoring.score(lead)
        per_niche: dict[NicheName, tuple[float | None, float | None]] = {}
        updates: dict[str, Any] = {}
        for niche, new in new_scores.items():
            col = _NICHE_SCORE_COL[niche]
            old = getattr(lead, col)
            per_niche[niche] = (old, new)
            updates[col] = new
        try:
            db.update_lead(conn, lead_id, **updates)
            changes[lead_id] = per_niche
        except Exception:
            log.exception("rescore: update_lead failed for id=%s", lead_id)
    return changes


def _apollo_enrich_top_n(conn: sqlite3.Connection, *, n: int) -> set[int]:
    """For each niche, take the top-N by score and union into one set of
    company IDs. Skip leads that already carry an APOLLO_ENRICHED marker
    (we never re-call). Returns the set of IDs that received fresh DM
    data — the regen stage uses this to force outreach regeneration so
    new copy can address the DM by first name.

    Silently no-ops when ``APOLLO_API_KEY`` is unset, so the pipeline
    still runs end-to-end without an Apollo plan."""
    if not apollo.is_configured():
        log.info("apollo: APOLLO_API_KEY not set, skipping DM enrichment")
        return set()

    target_ids: set[int] = set()
    for niche in NicheName:
        for top_lead in db.iter_leads(conn, niche=niche, limit=n):
            if top_lead.id is None:
                continue
            if getattr(top_lead, _NICHE_SCORE_COL[niche]) is None:
                continue  # NULL-scored leads sort last; once we hit them, stop.
            target_ids.add(top_lead.id)

    enriched: set[int] = set()
    for lead_id in target_ids:
        lead = db.get_lead(conn, lead_id=lead_id)
        if lead is None:
            continue
        if any(s.type == SignalType.APOLLO_ENRICHED for s in lead.signals):
            continue
        try:
            result = apollo.find_decision_maker(lead.name, lead.domain)
        except Exception:
            log.exception("apollo: lookup raised for %r", lead.name)
            continue

        if not result.org_found:
            # Org not in Apollo — don't write the marker; cheap to retry next
            # night since the org-search calls don't burn credits.
            continue

        # Apollo's headcount is more reliable than Gemini's when both exist.
        # If it puts the company over the SMB cap, drop the lead entirely.
        if result.headcount is not None and result.headcount > 250:
            log.info(
                "apollo: deleting lead %d (%s) — apollo headcount=%d exceeds SMB cap",
                lead_id, lead.name, result.headcount,
            )
            try:
                db.delete_lead(conn, lead_id)
            except Exception:
                log.exception("apollo: delete_lead failed for id=%s", lead_id)
            continue

        updates: dict[str, Any] = {}
        if result.dm_name:
            updates["dm_name"] = result.dm_name
        if result.dm_title:
            updates["dm_title"] = result.dm_title
        if result.dm_email:
            updates["dm_email"] = result.dm_email
        if result.dm_linkedin_url:
            updates["dm_linkedin_url"] = result.dm_linkedin_url
        if result.headcount is not None:
            updates["headcount"] = result.headcount
        if updates:
            try:
                db.update_lead(conn, lead_id, **updates)
                if result.dm_found:
                    enriched.add(lead_id)
            except Exception:
                log.exception("apollo: update_lead failed for id=%s", lead_id)

        try:
            db.append_signal(
                conn, lead_id,
                Signal(
                    type=SignalType.APOLLO_ENRICHED,
                    source=SourceName.APOLLO,
                    captured_at=_utcnow(),
                    payload={
                        "dm_found": result.dm_found,
                        "apollo_person_id": result.apollo_person_id,
                    },
                ),
            )
        except Exception:
            log.exception("apollo: append marker failed for id=%s", lead_id)

    return enriched


def _regen_copy(
    conn: sqlite3.Connection,
    score_changes: dict[int, dict[NicheName, tuple[float | None, float | None]]],
    *,
    force_regen_ids: set[int],
    model: str,
) -> int:
    """Apply threshold/delta gating to decide which (lead, niche) pairs need
    fresh insight copy. ``force_regen_ids`` is unused by the insight prompt
    (no DM-aware greeting) but kept for parity with the apollo step's return
    type."""
    del force_regen_ids  # not used now that outreach emails are gone
    copy_calls = 0
    for lead_id, per_niche in score_changes.items():
        lead = db.get_lead(conn, lead_id=lead_id)
        if lead is None:
            continue
        updates: dict[str, Any] = {}
        for niche, (old, new) in per_niche.items():
            insight_col = _NICHE_INSIGHT_COL[niche]
            if new is None:
                # Lead no longer qualifies for this niche; drop stale insight.
                if getattr(lead, insight_col) is not None:
                    updates[insight_col] = None
                continue
            existing_insight = getattr(lead, insight_col)
            insight_missing = existing_insight is None
            # A stored insight that still leaks a raw agency code ("me_ag")
            # predates the readable-name fix — refresh it even when the score
            # barely moved, so the leak self-heals on the next run.
            stale_agency_code = _insight_has_raw_agency_code(existing_insight)
            should_regen = new >= INSIGHT_THRESHOLD and (
                insight_missing
                or stale_agency_code
                or old is None
                or old < INSIGHT_THRESHOLD  # first crossing-from-below
                or abs(new - old) > INSIGHT_DELTA_THRESHOLD
            )
            if should_regen:
                try:
                    copy = outreach.generate(lead, niche, new, model=model)
                    updates[insight_col] = copy.insight
                    copy_calls += 1
                except Exception:
                    log.exception(
                        "outreach.generate failed for %s / %s",
                        lead.name,
                        niche.value,
                    )
            elif new < INSIGHT_THRESHOLD and getattr(lead, insight_col) is not None:
                # Lead dropped below threshold; existing insight is stale.
                updates[insight_col] = None
        if updates:
            try:
                db.update_lead(conn, lead_id, **updates)
            except Exception:
                log.exception("regen: update_lead failed for id=%s", lead_id)
    return copy_calls


def _rescore_and_regen_copy(
    conn: sqlite3.Connection, lead_ids: list[int], *, model: str
) -> tuple[list[int], int]:
    """Convenience wrapper that runs rescore + regen with no Apollo step.
    Kept for callers (and tests) that don't need Apollo in the loop."""
    changes = _rescore_all(conn, lead_ids)
    copy_calls = _regen_copy(conn, changes, force_regen_ids=set(), model=model)
    return list(changes.keys()), copy_calls


# --- JSON output -----------------------------------------------------------


def _build_output(conn: sqlite3.Connection) -> dict[str, Any]:
    now = _utcnow()
    return {
        "generated_at": now.isoformat(),
        "niches": {
            # Only leads with a non-NULL score for this niche appear in its
            # slice. A NULL score means the lead carries no signal that fits
            # this niche (see scoring.SIGNAL_WEIGHTS) — so it belongs on a
            # different dashboard, or none. This is the niche-matching filter:
            # it lives in the pipeline, not the UI.
            niche.value: [
                _lead_to_json(lead, niche, now=now)
                for lead in db.iter_leads(conn, niche=niche)
                if getattr(lead, _NICHE_SCORE_COL[niche]) is not None
            ]
            for niche in NicheName
        },
    }


def _city_state(lead: Lead) -> tuple[str | None, str | None]:
    locs = sorted(
        (s for s in lead.signals if s.type == SignalType.LOCATION_CAPTURED),
        key=lambda s: s.captured_at,
        reverse=True,
    )
    if not locs:
        return None, None
    p = locs[0].payload
    return p.get("city"), p.get("state")


def _lead_to_json(
    lead: Lead, niche: NicheName, *, now: datetime
) -> dict[str, Any]:
    city, state = _city_state(lead)
    # Only signals that qualify the lead for THIS niche render on its cards.
    # A Cloud card shows cloud/DevOps + funding signals; the breach that lead
    # may also carry stays off the Cloud dashboard entirely.
    relevant = sorted(
        (s for s in lead.signals if scoring.signal_matches_niche(s, niche)),
        key=scoring.effective_date,
        reverse=True,
    )[:SIGNAL_LIMIT_IN_JSON]
    return {
        "name": lead.name,
        "domain": lead.domain,
        "industry": lead.industry,
        "headcount": lead.headcount,
        "country": lead.country,
        "city": city,
        "state": state,
        "dm_name": lead.dm_name,
        "dm_title": lead.dm_title,
        "dm_email": lead.dm_email,
        "dm_linkedin_url": lead.dm_linkedin_url,
        "score": getattr(lead, _NICHE_SCORE_COL[niche]),
        "insight": getattr(lead, _NICHE_INSIGHT_COL[niche]),
        "signals": [_signal_to_json(s, now) for s in relevant],
    }


def _signal_to_json(s: Signal, now: datetime) -> dict[str, Any]:
    # Age is measured from the event date, not our capture time. For breaches
    # that's the disclosure date, so a breach reported two weeks ago reads
    # "14d ago" instead of "0 days ago" (the latter was the literal bug).
    return {
        "type": s.type.value,
        "captured_at": s.captured_at.isoformat(),
        "days_ago": max(0, (now - scoring.effective_date(s)).days),
        "payload": s.payload,
    }


# --- Upload ----------------------------------------------------------------


def _upload_blob(json_path: Path) -> None:
    url = os.environ.get("LEADS_UPLOAD_URL")
    api_key = os.environ.get("LEADS_UPLOAD_API_KEY")
    if not url or not api_key:
        raise RuntimeError(
            "--upload requires LEADS_UPLOAD_URL and LEADS_UPLOAD_API_KEY env vars"
        )
    payload = json.loads(json_path.read_text())
    resp = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    resp.raise_for_status()
    log.info("uploaded blob: %s", resp.json())


# --- CLI -------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="msp_pipeline.daily_run")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output-path", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument(
        "--limit", type=int, default=None, help="Per-source candidate cap"
    )
    parser.add_argument(
        "--reenrich",
        action="store_true",
        help="Force re-enrichment on every lead",
    )
    parser.add_argument("--copy-model", default="gpt-4o-mini")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run sources only; skip DB writes, LLM calls, JSON write",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="POST the local JSON to LEADS_UPLOAD_URL after writing it",
    )
    parser.add_argument(
        "--rescore-only",
        action="store_true",
        help="Skip fetch / upsert / enrichment. Dedup existing signals, "
        "rescore every lead, regenerate / clear outreach copy as needed, "
        "write JSON, upload (if --upload).",
    )
    parser.add_argument(
        "--apollo-top-n",
        type=int,
        default=APOLLO_TOP_N_DEFAULT,
        help="Apollo DM enrichment runs only on the union of top-N leads "
        "per niche by score. New entrants to the top-N each night are the "
        "only credit-burning calls. Default: %(default)d.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())

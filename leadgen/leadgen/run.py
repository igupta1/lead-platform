"""leadgen daily run — the single orchestrator for the whole platform.

    fetch(all shared sources) -> upsert into the deduped company store
    -> LLM-enrich (domain, headcount, industry, insight; purge junk / >=100)
    -> score every company for every niche -> project one inventory per niche
    -> (optionally) upload.

A niche is a query over the store, not a pipeline: a company that filed a
Form D and is hiring a Controller is one deduped record scored for several
niches, and appears in each niche inventory it qualifies for. Output never
pads below the requested count — it returns fewer.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from leadgen import db, enrichment, monitoring, scoring, taxonomy
from leadgen.models import Disqualifier, Lead, LeadCandidate
from leadgen.niches import NICHES, ORDER
from leadgen.niches.base import NicheConfig
from leadgen.sources import (
    breaches,
    edgar_form_c,
    edgar_form_d,
    fractional_boards,
    jobs,
)

log = logging.getLogger("leadgen.run")

# Shared sources, fetched once per run. Order is cosmetic (dedup is global).
_SOURCES = (edgar_form_d, edgar_form_c, jobs, fractional_boards, breaches)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "leads.db"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_SINCE_DAYS = 45
# Drop leads with no signal in this many days — a still-in-market company
# re-signals, so a long-silent lead is stale. Past the 60-day recency window.
STALE_MAX_DAYS = 90


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- Fetch ----------------------------------------------------------------


def _call_source(mod: Any, since: datetime) -> tuple[list[LeadCandidate], list[Disqualifier]]:
    """Call a source's ``fetch(since=...)`` and normalize the return to
    (candidates, disqualifiers). Sources may return either a bare candidate
    list or a (candidates, disqualifiers) tuple."""
    res = mod.fetch(since=since)
    if isinstance(res, tuple):
        cands, dqs = res
        return list(cands or []), list(dqs or [])
    return list(res or []), []


def fetch_all(
    since: datetime,
) -> tuple[list[LeadCandidate], list[Disqualifier], dict[str, int]]:
    candidates: list[LeadCandidate] = []
    disqualifiers: list[Disqualifier] = []
    source_counts: dict[str, int] = {}
    for mod in _SOURCES:
        name = mod.__name__.rsplit(".", 1)[-1]
        try:
            cands, dqs = _call_source(mod, since)
            log.info("source %s: %d candidates, %d disqualifiers", name, len(cands), len(dqs))
            candidates.extend(cands)
            disqualifiers.extend(dqs)
            source_counts[name] = len(cands)
        except Exception:  # one flaky source must not kill the run
            log.exception("source %s failed", name)
            source_counts[name] = 0  # a source that raised reads as 0 -> alertable
    return candidates, disqualifiers, source_counts


# --- Ingest ---------------------------------------------------------------


def ingest(conn: Any, candidates: list[LeadCandidate], disqualifiers: list[Disqualifier]) -> int:
    # Disqualifiers first, so a candidate that arrives the same run as its own
    # disqualifier is blocked and any pre-existing row is swept out.
    for dq in disqualifiers:
        try:
            key = db.mark_disqualified(conn, dq)
            db.delete_lead_by_name_key(conn, key)
        except ValueError:
            log.debug("skip un-keyable disqualifier %r", dq.name)

    kept = 0
    for cand in candidates:
        try:
            lead = db.upsert_lead(conn, cand)
        except ValueError:
            continue  # un-keyable company name
        if lead is not None:
            kept += 1
    db.dedup_signals_pass(conn)
    return kept


# --- Enrich ---------------------------------------------------------------


def enrich_all(conn: Any, *, budget: int | None, force: bool) -> int:
    enriched = 0
    for lead in list(db.iter_leads(conn)):
        if budget is not None and enriched >= budget:
            break
        if not enrichment.needs_enrichment(lead, force=force):
            continue
        try:
            if enrichment.enrich(conn, lead, force=force):
                enriched += 1
        except Exception:
            log.exception("enrich failed for lead id=%s (%s)", lead.id, lead.name)
    return enriched


# --- Score ----------------------------------------------------------------


def score_all(conn: Any) -> None:
    now = _utcnow()
    for lead in list(db.iter_leads(conn)):
        assert lead.id is not None
        db.set_scores(conn, lead.id, scoring.score_all(lead, now=now))


# --- Project + emit -------------------------------------------------------


def _primary_signal(lead: Lead, niche: NicheConfig):
    """The signal that headlines this lead for this niche: strongest tier,
    then most recent."""
    qualifying = [s for s in lead.signals if s.type in niche.all_types]
    if not qualifying:
        return None
    return min(
        qualifying,
        key=lambda s: (
            niche.tier_index(s.type) or 0,
            -( (s.event_date or s.captured_at).timestamp() ),
        ),
    )


def _lead_record(lead: Lead, niche: NicheConfig, score: float) -> dict[str, Any]:
    primary = _primary_signal(lead, niche)
    qualifying = [s for s in lead.signals if s.type in niche.all_types]
    return {
        "id": lead.id,
        "name": lead.name,
        "domain": lead.domain,
        "signal_type": primary.type.value if primary else None,
        "evidence_text": primary.evidence_text if primary else None,
        "source_url": primary.source_url if primary else None,
        "industry": lead.industry,
        "niche": lead.niche,
        "headcount": lead.headcount,
        "city": lead.city,
        "state": lead.state,
        "score": score,
        "insight": lead.insight,
        "signals": [
            {
                "type": s.type.value,
                "event_date": s.event_date.isoformat() if s.event_date else None,
                "evidence_text": s.evidence_text,
                "source_url": s.source_url,
                "payload": s.payload,
            }
            for s in qualifying
        ],
    }


def project_niche(conn: Any, niche: NicheConfig, *, state: str | None, limit: int | None) -> dict[str, Any]:
    rows: list[tuple[float, Lead]] = []
    for lead in db.iter_leads(conn):
        score = lead.scores.get(niche.key)
        if score is None:
            continue
        if state and (lead.state or "").upper() != state.upper():
            continue
        rows.append((score, lead))
    # Strongest first, then freshest; never pad below the count.
    rows.sort(key=lambda r: r[0], reverse=True)
    if limit is not None:
        rows = rows[:limit]
    return {
        "generated_at": _utcnow().isoformat(),
        "niche": niche.key,
        "count": len(rows),
        "leads": [_lead_record(lead, niche, score) for score, lead in rows],
    }


def emit(
    conn: Any, out_dir: Path, *, only: str | None, state: str | None, limit: int | None
) -> tuple[dict[str, Path], dict[str, int]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    niche_counts: dict[str, int] = {}
    for niche in ORDER:
        if only and niche.key != only:
            continue
        payload = project_niche(conn, niche, state=state, limit=limit)
        path = out_dir / niche.output_filename
        path.write_text(json.dumps(payload, indent=2))
        log.info("wrote %s (%d leads)", path.name, payload["count"])
        written[niche.key] = path
        niche_counts[niche.key] = payload["count"]
    # Shared vertical taxonomy (parent -> children), consumed by the outreach
    # engine's research/Gate-B matching.
    (out_dir / "taxonomy.json").write_text(
        json.dumps({"taxonomy": taxonomy.PARENT_CHILDREN}, indent=2)
    )
    return written, niche_counts


# --- Upload ---------------------------------------------------------------


def upload(niche_key: str, path: Path) -> None:
    base = os.environ.get("LEADS_UPLOAD_URL")
    token = os.environ.get("LEADS_UPLOAD_API_KEY")
    if not base:
        log.info("LEADS_UPLOAD_URL unset — skipping upload of %s", path.name)
        return
    url = f"{base.rstrip('/')}?niche={niche_key}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.post(url, data=path.read_bytes(), headers=headers, timeout=60)
    resp.raise_for_status()
    log.info("uploaded %s -> %s (%s)", path.name, url, resp.status_code)


# --- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="leadgen daily run")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS)
    parser.add_argument("--niche", choices=sorted(NICHES), default=None,
                        help="emit only this niche")
    parser.add_argument("--state", default=None, help="filter output to a 2-letter state")
    parser.add_argument("--limit", type=int, default=None, help="cap leads per niche")
    parser.add_argument("--enrich-budget", type=int, default=0,
                        help="max leads to enrich this run (0 = unlimited)")
    parser.add_argument("--reenrich", action="store_true", help="force re-enrichment")
    parser.add_argument("--skip-fetch", action="store_true", help="score/emit only")
    parser.add_argument("--skip-enrich", action="store_true")
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    conn = db.init_db(args.db)
    since = _utcnow() - timedelta(days=args.since_days)

    source_counts: dict[str, int] = {}
    if not args.skip_fetch:
        candidates, disqualifiers, source_counts = fetch_all(since)
        kept = ingest(conn, candidates, disqualifiers)
        log.info("ingest: %d candidates upserted", kept)

    if not args.skip_enrich:
        budget = args.enrich_budget if args.enrich_budget > 0 else None
        enriched = enrich_all(conn, budget=budget, force=args.reenrich)
        log.info("enriched %d leads", enriched)

    purged = enrichment.purge_disqualified(conn)
    log.info("purged %d disqualified leads", purged)

    merged = db.merge_by_domain(conn)
    log.info("merged %d domain-duplicate leads", merged)

    pruned = db.prune_stale(conn, max_age_days=STALE_MAX_DAYS)
    log.info("pruned %d stale leads (no signal in %d days)", pruned, STALE_MAX_DAYS)

    score_all(conn)
    written, niche_counts = emit(
        conn, args.out_dir, only=args.niche, state=args.state, limit=args.limit
    )

    # Anomaly guard: diff this run vs. the previous one (alert on a silent
    # source failure or a sharp niche drop), then record this run's counts.
    # Only on a real fetch — an emit-only run has no source counts to compare.
    if not args.skip_fetch:
        stats = {"sources": source_counts, "niches": niche_counts}
        monitoring.check(db.last_run_stats(conn), stats)
        db.record_run_stats(conn, stats)

    if args.upload:
        for niche_key, path in written.items():
            try:
                upload(niche_key, path)
            except Exception:
                log.exception("upload failed for %s", niche_key)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

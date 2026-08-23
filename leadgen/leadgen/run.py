"""leadgen daily run — the single orchestrator for the whole platform.

    fetch(all shared sources) -> upsert into the deduped company store
    -> LLM-enrich (domain, headcount, industry, niche; purge junk / >=100)
    -> score every company for every niche -> project one inventory per niche
    -> (optionally) upload.

A niche is a query over the store, not a pipeline: a company hiring both a
Controller and a DevOps engineer is one deduped record scored for several
niches, and appears in each niche inventory it qualifies for. Output never
pads below the requested count — it returns fewer.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from leadgen import db, enrichment, llm, monitoring, scoring, taxonomy
from leadgen import filters
from leadgen.filters import is_untargetable_name
from leadgen.models import Disqualifier, Lead, LeadCandidate, effective_size
from leadgen.niches import NICHES, ORDER
from leadgen.niches.base import NicheConfig
from leadgen.sources import breaches, fractional_boards, jobs

log = logging.getLogger("leadgen.run")

# Shared sources, fetched once per run. Order is cosmetic (dedup is global).
_SOURCES = (jobs, fractional_boards, breaches)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "leads.db"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_SINCE_DAYS = 45
# Seconds reserved AFTER enrichment for purge/prune/score/emit/upload so a
# time-budgeted run always reaches the commit (never gets SIGKILLed mid-write).
POST_ENRICH_RESERVE_S = 300
# Concurrent Gemini/OpenAI lookups during enrichment. The per-lead network work
# is I/O-bound, so a bounded pool cuts wall-clock ~N×; DB writes stay serial.
# Requires a paid Gemini tier (GEMINI_MIN_INTERVAL_S=0) — free-tier RPM caps
# would 429 under concurrency. Set 1 to force the old serial path.
DEFAULT_ENRICH_WORKERS = 12
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
    if filters.REJECT_COUNTS:
        log.info("name gate rejected: %s", dict(filters.REJECT_COUNTS.most_common()))
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


def _plan_chunk(leads: list[Lead], workers: int, *, force: bool = False) -> dict[Any, Any]:
    """Compute an `EnrichPlan` for each lead CONCURRENTLY (all the Gemini/OpenAI
    network work; no DB). Returns {lead.id: plan|None}; None means the lookup
    errored and the lead is skipped this run (retried next run).

    ``force`` must be threaded through: ``plan_enrichment`` re-checks
    ``needs_enrichment`` itself, so hardcoding False here made ``--reenrich``
    a silent no-op on this path (every already-enriched lead planned as
    "skip"). Only the serial path honored it."""
    plans: dict[Any, Any] = {}
    quota_skipped = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(enrichment.plan_enrichment, lead, force=force): lead
                   for lead in leads}
        for fut in as_completed(futures):
            lead = futures[fut]
            try:
                plans[lead.id] = fut.result()
            except llm.GeminiQuotaExhausted:
                # Expected on a big backlog: the day's Gemini quota is spent. These
                # leads stay un-enriched and retry next run (needs_enrichment True).
                plans[lead.id] = None
                quota_skipped += 1
            except Exception:
                log.exception("plan_enrichment failed for lead id=%s (%s)", lead.id, lead.name)
                plans[lead.id] = None
    if quota_skipped:
        log.warning("enrich: %d leads skipped this chunk — gemini quota exhausted "
                    "(they retry next run)", quota_skipped)
    return plans


def enrich_all(
    conn: Any, *, budget: int | None, force: bool, deadline: float | None = None,
    workers: int = DEFAULT_ENRICH_WORKERS,
) -> int:
    """Enrich up to `budget` leads, stopping early if `deadline`
    (a `time.monotonic()` value) is reached. The per-lead network work
    (`plan_enrichment`) runs concurrently across `workers` threads; the DB writes
    (`apply_enrichment`) stay serial on the one connection. Each applied lead is
    committed as it goes (`needs_enrichment` skips done ones), so stopping early
    just resumes next run — a big backlog no longer times the job out.

    `workers <= 1` forces the old fully-serial path (used by tests / free tier)."""
    candidates = [lead for lead in db.iter_leads(conn)
                  if enrichment.needs_enrichment(lead, force=force)]
    if budget is not None:
        candidates = candidates[:budget]
    if not candidates:
        return 0
    # State the size of the backlog going in. Without it the phase summary
    # reports only how many leads were enriched, so a run that planned 520 and
    # managed 49 on a spent quota reads exactly like a quiet night.
    log.info("enrich: %d lead(s) need enrichment", len(candidates))

    enriched = 0
    if workers <= 1:
        for lead in candidates:
            if deadline is not None and time.monotonic() >= deadline:
                log.info("enrich: reached time budget after %d leads — resumes next run", enriched)
                break
            try:
                if enrichment.enrich(conn, lead, force=force):
                    enriched += 1
            except Exception:
                log.exception("enrich failed for lead id=%s (%s)", lead.id, lead.name)
        return enriched

    # Parallel: plan a chunk concurrently, then apply it serially. Chunking lets
    # the time budget stop the run between chunks.
    chunk_size = max(workers * 4, 32)
    for start in range(0, len(candidates), chunk_size):
        if deadline is not None and time.monotonic() >= deadline:
            log.info("enrich: reached time budget after %d leads — resumes next run", enriched)
            break
        chunk = candidates[start:start + chunk_size]
        plans = _plan_chunk(chunk, workers, force=force)
        for lead in chunk:                     # serial DB writes, original order
            plan = plans.get(lead.id)
            if plan is None:
                continue
            try:
                if enrichment.apply_enrichment(conn, lead, plan):
                    enriched += 1
            except Exception:
                log.exception("apply_enrichment failed for lead id=%s (%s)", lead.id, lead.name)
    if enriched < len(candidates):
        log.warning("enrich: %d of %d lead(s) left un-enriched this run "
                    "(quota, error, or time budget) — they retry next run",
                    len(candidates) - enriched, len(candidates))
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
            # Undated sorts LAST within a tier: captured_at is scrape time, so
            # falling back to it made an undated signal look like today's news
            # and headline the lead. A dated signal always wins the headline.
            0 if s.event_date is not None else 1,
            -(s.event_date.timestamp() if s.event_date is not None else 0.0),
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
        # Published for ONE consumer: the outreach engine's Gate B fit check
        # (system_b/gift/fit.py), which asks an LLM whether each gift lead's
        # description genuinely reads as the claimed vertical before the copy
        # claims it. It is the only field that says what the company DOES, so
        # without it Gate B judges empty strings, answers false for every lead
        # (it is instructed to), and silently drops every niche claim to a
        # generalist geo email. That is exactly what happened between
        # 2026-08-04 and 2026-08-05: the cfo niche rate went 11/23 -> 0/23.
        # It must never reach a rendered email — see `_is_complete`.
        "insight": lead.insight,
        "headcount": lead.headcount,
        # The coarse band is how most of these companies are sized at all — an
        # exact count is undiscoverable for a small private company, so
        # publishing only `headcount` makes a band-sized lead read as unsized
        # to anything downstream that filters on size.
        "headcount_band": lead.headcount_band,
        "city": lead.city,
        "state": lead.state,
        "score": score,
        "signals": [
            {
                "type": s.type.value,
                "event_date": s.event_date.isoformat() if s.event_date else None,
                # Published so the outreach engine can tell a real posting date
                # from a first-seen stamp. Without it every date read as "high"
                # and the copy printed a relative date off a Webflow build
                # timestamp.
                "date_confidence": s.date_confidence,
                "evidence_text": s.evidence_text,
                "source_url": s.source_url,
                "payload": s.payload,
            }
            for s in qualifying
        ],
    }


def _is_complete(lead: Lead) -> bool:
    """A lead the gift copy can actually use.

    Domain ONLY. `insight` is deliberately NOT part of this test: every lead
    line is code-templated from the signal itself, so a missing insight never
    blocks usable copy, and requiring it here demoted good leads. It is a
    different thing from whether the field is PUBLISHED — it is (see
    `_lead_record`), because the outreach engine's Gate B reads it to verify a
    vertical claim. Published for judging, never for rendering: no email text
    is ever derived from it."""
    return bool((lead.domain or "").strip())


def project_niche(conn: Any, niche: NicheConfig, *, state: str | None, limit: int | None) -> dict[str, Any]:
    """Project one niche's inventory. Fails CLOSED on size and enrichment:
    a lead that has never been enriched, or that we cannot size at all, is
    held back rather than published.

    Both guards exist because the alternative is worse than a short list. An
    unenriched lead has no domain / vertical / insight, and an unsized one has
    never been tested against the size cap (that is how enterprises reached a
    gift built for small companies). They stay in the store and ship the night
    they can be sized."""
    rows: list[tuple[float, Lead]] = []
    unenriched = unsized = untargetable = 0
    for lead in db.iter_leads(conn):
        score = lead.scores.get(niche.key)
        if score is None:
            continue
        if state and (lead.state or "").upper() != state.upper():
            continue
        if lead.enriched_at is None:
            unenriched += 1
            continue
        if effective_size(lead) is None:
            unsized += 1
            continue
        if is_untargetable_name(lead.name, lead.domain):
            untargetable += 1
            continue
        rows.append((score, lead))
    if unenriched or unsized or untargetable:
        log.info(
            "project %s: held back %d unenriched + %d unsized + %d untargetable lead(s)",
            niche.key, unenriched, unsized, untargetable,
        )
    # Complete leads first, then strongest. A gift lead with no domain is one
    # the recipient cannot check, so it ranks below every complete one rather
    # than being dropped (the store has the depth to spare, and the lead is
    # still real). Within each group it is strongest-first as before.
    rows.sort(key=lambda r: (0 if _is_complete(r[1]) else 1, -r[0]))
    if limit is not None:
        rows = rows[:limit]
    return {
        "generated_at": _utcnow().isoformat(),
        "niche": niche.key,
        "count": len(rows),
        "leads": [_lead_record(lead, niche, score) for score, lead in rows],
    }


def _insight_fill(payload: dict[str, Any]) -> dict[str, int]:
    """{total, filled} for `insight` across one niche's published leads. Fed to
    the monitoring floor: `insight` is the only published field whose consumer
    (outreach Gate B) fails silently when it goes missing, so it is counted at
    the point it is actually written rather than inferred later."""
    leads = payload.get("leads") or []
    filled = sum(1 for lead in leads if str(lead.get("insight") or "").strip())
    return {"total": len(leads), "filled": filled}


def emit(
    conn: Any, out_dir: Path, *, only: str | None, state: str | None, limit: int | None
) -> tuple[dict[str, Path], dict[str, int], dict[str, dict[str, int]]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    niche_counts: dict[str, int] = {}
    insight_fill: dict[str, dict[str, int]] = {}
    for niche in ORDER:
        if only and niche.key != only:
            continue
        payload = project_niche(conn, niche, state=state, limit=limit)
        path = out_dir / niche.output_filename
        path.write_text(json.dumps(payload, indent=2))
        log.info("wrote %s (%d leads)", path.name, payload["count"])
        written[niche.key] = path
        niche_counts[niche.key] = payload["count"]
        insight_fill[niche.key] = _insight_fill(payload)
    # Shared vertical taxonomy (parent -> children), consumed by the outreach
    # engine's research/Gate-B matching.
    (out_dir / "taxonomy.json").write_text(
        json.dumps({"taxonomy": taxonomy.PARENT_CHILDREN}, indent=2)
    )
    return written, niche_counts, insight_fill


# --- Publish --------------------------------------------------------------
# Publishing the per-niche JSON to Vercel Blob is done in CI (see
# .github/workflows/daily-leads.yml) with the official `vercel blob put` CLI,
# AFTER this run writes the files locally — stable public pathnames, overwritten
# each night, with a short cache-control so the outreach engine reads same-day
# data. This module stays pure-Python and fully offline.


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
    parser.add_argument("--enrich-workers", type=int, default=DEFAULT_ENRICH_WORKERS,
                        help="concurrent Gemini/OpenAI lookups during enrichment "
                             "(needs a paid Gemini tier; 1 = serial)")
    parser.add_argument("--time-budget-s", type=int, default=0,
                        help="soft wall-clock budget for the whole run in seconds "
                             "(0 = unlimited). Enrichment stops early to reserve "
                             f"{POST_ENRICH_RESERVE_S}s for score/emit/commit, so the "
                             "job always finishes cleanly under a CI timeout and "
                             "commits its progress (which resumes next run).")
    parser.add_argument("--reenrich", action="store_true", help="force re-enrichment")
    parser.add_argument("--skip-fetch", action="store_true", help="score/emit only")
    parser.add_argument("--skip-enrich", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    run_start = time.monotonic()
    conn = db.init_db(args.db)
    since = _utcnow() - timedelta(days=args.since_days)

    source_counts: dict[str, int] = {}
    if not args.skip_fetch:
        t0 = time.monotonic()
        candidates, disqualifiers, source_counts = fetch_all(since)
        kept = ingest(conn, candidates, disqualifiers)
        log.info("phase fetch+ingest: %d upserted in %.0fs", kept, time.monotonic() - t0)

    if not args.skip_enrich:
        budget = args.enrich_budget if args.enrich_budget > 0 else None
        # Leave POST_ENRICH_RESERVE_S for the tail phases so the run always
        # reaches emit/commit before any CI timeout kills the process.
        deadline = (
            run_start + args.time_budget_s - POST_ENRICH_RESERVE_S
            if args.time_budget_s > 0 else None
        )
        t0 = time.monotonic()
        enriched = enrich_all(conn, budget=budget, force=args.reenrich,
                              deadline=deadline, workers=args.enrich_workers)
        log.info("phase enrich: %d leads in %.0fs (%d workers)",
                 enriched, time.monotonic() - t0, args.enrich_workers)

    purged = enrichment.purge_disqualified(conn)
    log.info("purged %d disqualified leads", purged)

    pruned = db.prune_stale(conn, max_age_days=STALE_MAX_DAYS)
    log.info("pruned %d stale leads (no signal in %d days)", pruned, STALE_MAX_DAYS)

    t0 = time.monotonic()
    score_all(conn)
    written, niche_counts, insight_fill = emit(
        conn, args.out_dir, only=args.niche, state=args.state, limit=args.limit
    )
    log.info("phase score+emit: %d niche file(s) in %.0fs", len(written), time.monotonic() - t0)

    # Anomaly guard. The count diff needs a previous run to compare against, and
    # an emit-only run has no source counts — so on --skip-fetch we pass prev=None,
    # which runs ONLY the checks that stand alone (quota latch, insight floor).
    # Those still matter there: --skip-fetch is the publish path, so it can ship a
    # broken inventory just as easily as a full run.
    stats: dict[str, Any] = {"niches": niche_counts, "insight_fill": insight_fill}
    if not args.skip_fetch:
        stats["sources"] = source_counts
    # A latched Gemini quota is invisible to the count-diff guard: the
    # run exits 0, and unenriched leads used to INFLATE niche counts
    # rather than drop them. Report it explicitly.
    if llm.gemini_quota_exhausted():
        stats["gemini_quota_exhausted"] = True
    monitoring.check(db.last_run_stats(conn) if not args.skip_fetch else None, stats)
    # Only a real fetch records a baseline: storing an emit-only run's empty
    # source counts would make the NEXT run diff against zeros and alert on
    # every source "recovering".
    if not args.skip_fetch:
        db.record_run_stats(conn, stats)

    log.info("run complete in %.0fs", time.monotonic() - run_start)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

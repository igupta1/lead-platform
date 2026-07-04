"""End-to-end pipeline runner for the fractional-CFO niche.

fetch -> record-disqualifiers -> upsert -> enrich -> purge -> score ->
apollo -> write -> upload. Output JSON shape
``{generated_at, leads: [...]}`` — flat, matches what
``api/generate-cfo-leads.js`` expects.

The new stage relative to insurance_pipeline is
``_record_disqualifiers``: the jobs source returns CFO postings as
``Disqualifier`` objects (separate from the regular lead candidates).
We persist those to the ``disqualified`` table and sweep matching
leads out of the leads table before anything else runs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from cfo_pipeline import apollo, db, enrichment, llm, scoring
from cfo_pipeline.models import (
    Disqualifier,
    Lead,
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)
from cfo_pipeline.sources import edgar_form_d, funding, jobs
from cfo_pipeline import outreach  # kept for trigger_type helper

log = logging.getLogger("cfo.daily_run")

DEFAULT_DB_PATH = Path("data/leads.db")
DEFAULT_OUTPUT_PATH = Path("data/leads.json")
# Form D window is 90 days per spec; jobs source uses its own
# hours_old window internally (capped by since for safety).
LOOKBACK_DAYS = 90
SIGNAL_LIMIT_IN_JSON = 6
APOLLO_TOP_N_DEFAULT = 30

# Funding-only output gate. Form-D-only leads with a known offering
# below this are too small to be in the fractional-CFO window; leads
# with an unknown / "Indefinite" amount (including signals ingested
# before amounts were mined) pass on the domain requirement alone.
FUNDING_ONLY_MIN_OFFERING_USD = 500_000.0
# Cap funding-only cards so hiring-signal leads dominate the page.
FUNDING_ONLY_MAX_CARDS = 75


_SCORING_SIGNAL_TYPES: frozenset[SignalType] = frozenset(
    {
        SignalType.JOB_POSTED_FRACTIONAL_CFO,
        SignalType.JOB_POSTED_FINANCE_LEAD,
        SignalType.FUNDING_RAISED,
    }
)

# Hiring-side signal types — used for bullseye detection and the
# hiring-vs-funding split in merge reconciliation.
_HIRING_SIGNAL_TYPES: frozenset[SignalType] = frozenset(
    {
        SignalType.JOB_POSTED_FRACTIONAL_CFO,
        SignalType.JOB_POSTED_FINANCE_LEAD,
    }
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _enrich_budget(args: argparse.Namespace) -> int | None:
    """0 (or negative) means unlimited."""
    return args.enrich_budget if args.enrich_budget > 0 else None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.dry_run:
        log.info("dry-run: skipping all writes (DB, LLM calls, JSON)")
        candidates, disqualifiers = _fetch_all(
            since=_utcnow() - timedelta(days=LOOKBACK_DAYS),
            per_source_limit=args.limit,
        )
        log.info(
            "dry-run fetched %d candidates, %d disqualifiers",
            len(candidates), len(disqualifiers),
        )
        return 0

    args.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.init_db(args.db_path)

    if args.rescore_only:
        log.info("rescore-only: skipping fetch / upsert")

        if args.reenrich:
            existing_ids = [
                lead.id for lead in db.iter_leads(conn) if lead.id is not None
            ]
            log.info("re-enriching %d existing leads (force=True)", len(existing_ids))
            _, re_spent, re_deferred = _enrich_all(
                conn, existing_ids, force=True, budget=_enrich_budget(args)
            )
            log.info("re-enrichment: llm_spent=%d deferred=%d", re_spent, re_deferred)

        modified = db.dedup_signals_pass(conn)
        log.info("deduped signals on %d leads", modified)

        purged = enrichment.purge_disqualified(conn)
        log.info("purged %d disqualified leads", purged)

        all_ids = [lead.id for lead in db.iter_leads(conn) if lead.id is not None]
        log.info("re-scoring %d leads", len(all_ids))

        score_changes = _rescore_all(conn, all_ids)
        apollo_ids = _apollo_enrich_top_n(conn, n=args.apollo_top_n)
        log.info(
            "re-scored %d, apollo-enriched %d",
            len(score_changes), len(apollo_ids),
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

    candidates, disqualifiers = _fetch_all(
        since=_utcnow() - timedelta(days=LOOKBACK_DAYS),
        per_source_limit=args.limit,
    )
    log.info(
        "fetched %d candidates and %d disqualifiers across all sources",
        len(candidates), len(disqualifiers),
    )

    # Record disqualifiers BEFORE upserting candidates: a CFO posting
    # for "Acme" should block the same-day Form D filing for "Acme" in
    # the same run, not just future runs.
    dq_marked, dq_swept = _record_disqualifiers(conn, disqualifiers)
    log.info(
        "recorded %d disqualifiers, swept %d existing leads",
        dq_marked, dq_swept,
    )

    upserted_ids = _upsert_all(conn, candidates)
    log.info("upserted %d unique leads", len(upserted_ids))

    # Dedup signals on every lead. dedup_signals_pass was previously
    # only called in --rescore-only mode, which meant any lead that
    # accumulated duplicate signals under the old keying (site +
    # url + date in the hash) stayed stuck at 2-3 signals on the
    # dashboard until a manual rescore. Run it in the regular flow
    # so the new (type, normalized_title) dedup applies to existing
    # rows too.
    deduped_signals = db.dedup_signals_pass(conn)
    log.info("deduped signals on %d leads", deduped_signals)

    # Bullseye cross-source join. Form D's legal-entity name
    # ("Estately Operations LLC") and the job board's brand name
    # ("Estately") collapse to different name_keys, so the default
    # upsert dedup keeps them as separate leads. After upsert, sweep
    # for leads sharing brand_key (aggressive normalization) OR domain
    # and merge them.
    merged_count = _reconcile_bullseyes(conn)
    log.info("reconciled %d bullseye merges", merged_count)

    # Unified, best-lead-first worklist: this run's new upserts and
    # leads deferred on prior nights compete on equal footing, so a
    # fractional-CFO posting deferred last night still enriches before
    # tonight's funding-only Form D leads.
    worklist = _enrichment_worklist(conn, force=args.reenrich)
    log.info(
        "enrichment worklist: %d leads need a pass (budget=%s)",
        len(worklist), _enrich_budget(args) if _enrich_budget(args) else "unlimited",
    )
    enriched_ids, enrich_spent, enrich_deferred = _enrich_all(
        conn, worklist, force=args.reenrich, budget=_enrich_budget(args),
    )
    log.info(
        "kept %d enriched leads (llm_spent=%d, deferred=%d)",
        len(enriched_ids), enrich_spent, enrich_deferred,
    )

    purged = enrichment.purge_disqualified(conn)
    log.info("purged %d disqualified leads", purged)

    # Rescore EVERY surviving lead, not just the ones enriched this
    # run: scores decay from the event date (Phase 4), so an
    # un-touched lead's stored score drifts stale every night. Scoring
    # is pure compute (no API calls) — cheap to run across the table.
    all_ids = [l.id for l in db.iter_leads(conn) if l.id is not None]
    score_changes = _rescore_all(conn, all_ids)
    apollo_ids = _apollo_enrich_top_n(conn, n=args.apollo_top_n)
    log.info(
        "re-scored %d, apollo-enriched %d",
        len(score_changes), len(apollo_ids),
    )

    output = _build_output(conn)
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(json.dumps(output, indent=2, default=str))
    log.info("wrote %s", args.output_path)

    # One-line funnel so quota tuning is evidence-based instead of blind.
    log.info(
        "funnel: candidates=%d disqualifiers=%d upserted=%d worklist=%d "
        "llm_spent=%d deferred=%d purged=%d output=%d",
        len(candidates), len(disqualifiers), len(upserted_ids),
        len(worklist), enrich_spent, enrich_deferred, purged,
        len(output["leads"]),
    )

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
) -> tuple[list[LeadCandidate], list[Disqualifier]]:
    candidates: list[LeadCandidate] = []
    disqualifiers: list[Disqualifier] = []

    # Jobs + EDGAR have two-return signatures (they produce
    # disqualifiers: CFO postings / CFO-officer Form D listings).
    for name, fn in (("jobs", jobs.fetch), ("edgar_form_d", edgar_form_d.fetch)):
        try:
            c, d = fn(since=since, limit=per_source_limit)
            log.info(
                "source %s returned %d candidates, %d disqualifiers",
                name, len(c), len(d),
            )
            candidates.extend(c)
            disqualifiers.extend(d)
        except Exception:
            log.exception("source %s failed", name)

    try:
        cs = funding.fetch(since=since, limit=per_source_limit)
        log.info("source funding returned %d candidates", len(cs))
        candidates.extend(cs)
    except Exception:
        log.exception("source funding failed")

    return candidates, disqualifiers


def _record_disqualifiers(
    conn: sqlite3.Connection, disqualifiers: list[Disqualifier]
) -> tuple[int, int]:
    """Persist disqualifiers and sweep any existing matching lead rows.
    Returns (marked, swept) counts."""
    marked = 0
    swept = 0
    for dq in disqualifiers:
        try:
            key = db.mark_disqualified(conn, dq)
            marked += 1
        except Exception:
            log.exception("disqualifier: mark failed for %r", dq.name)
            continue
        try:
            removed = db.delete_lead_by_name_key(conn, key)
            if removed:
                log.info(
                    "disqualifier: swept %d existing lead row(s) for %s (%s)",
                    removed, dq.name, dq.reason,
                )
                swept += removed
        except Exception:
            log.exception("disqualifier: sweep failed for %r", dq.name)
    return marked, swept


def _upsert_all(
    conn: sqlite3.Connection, candidates: list[LeadCandidate]
) -> list[int]:
    ids: list[int] = []
    refused = 0
    for c in candidates:
        try:
            lead = db.upsert_lead(conn, c)
        except Exception:
            log.exception("upsert failed for %r", c.name)
            continue
        if lead is None:
            refused += 1
            continue
        assert lead.id is not None
        ids.append(lead.id)
    if refused:
        log.info("upsert: refused %d disqualified candidates", refused)
    return list(dict.fromkeys(ids))


_MIN_BRAND_KEY_LEN_FOR_SUBSTRING = 5  # avoid "ai"/"co" matching everything


def _reconcile_bullseyes(conn: sqlite3.Connection) -> int:
    """Merge leads that the upsert dedup missed.

    Three passes, most precise first:
    1. Exact brand_key match (strips "Operations"/"Holdings"/etc.).
    2. Domain match (Apollo / Gemini may have filled the same domain
       on both rows).
    3. Substring brand_key match — one lead's brand_key fully
       contains the other's, AND the shorter key is at least
       _MIN_BRAND_KEY_LEN_FOR_SUBSTRING chars. Catches the
       Estately-Operations-LLC ↔ Estately case without conflating
       short keys like "ai" / "co".

    When the merge produces zero hits, the function logs the
    cross-source brand_key pools so we can audit why nothing matched
    (the user's req #3 explicit ask in the 4th-review pass).
    """
    leads = list(db.iter_leads(conn))
    by_brand: dict[str, list[Lead]] = {}
    by_domain: dict[str, list[Lead]] = {}
    for lead in leads:
        if lead.id is None:
            continue
        bk = db.brand_key(lead.name)
        if bk:
            by_brand.setdefault(bk, []).append(lead)
        if lead.domain:
            dk = lead.domain.lower().strip()
            if dk:
                by_domain.setdefault(dk, []).append(lead)

    merged_ids: set[int] = set()
    merges = 0
    _NON_SCORING = frozenset(
        {
            SignalType.ENRICHMENT_RUN,
            SignalType.LOCATION_CAPTURED,
            SignalType.APOLLO_ENRICHED,
        }
    )

    def _merge_group(group: list[Lead], why: str) -> None:
        nonlocal merges
        live = [l for l in group if l.id is not None and l.id not in merged_ids]
        if len(live) < 2:
            return
        live.sort(key=lambda l: (-len(l.signals), l.id or 0))
        canonical, *duplicates = live
        for dup in duplicates:
            assert dup.id is not None and canonical.id is not None
            for sig in dup.signals:
                if sig.type in _NON_SCORING:
                    continue
                try:
                    db.append_signal(conn, canonical.id, sig)
                except Exception:
                    log.exception(
                        "bullseye-merge: append_signal failed canonical=%s dup=%s",
                        canonical.id, dup.id,
                    )
            try:
                db.delete_lead(conn, dup.id)
                merged_ids.add(dup.id)
                merges += 1
                log.info("bullseye-merge (%s): %r <- %r", why, canonical.name, dup.name)
            except Exception:
                log.exception("bullseye-merge: delete_lead failed for id=%s", dup.id)

    # Pass 1: exact brand_key.
    for group in by_brand.values():
        if len(group) > 1:
            _merge_group(group, "exact_brand_key")
    # Pass 2: exact domain.
    for group in by_domain.values():
        if len(group) > 1:
            _merge_group(group, "domain")
    # Pass 3: substring brand_key. Build pools by source side so we
    # only attempt cross-source matches (a hiring lead with two
    # postings shouldn't merge with another hiring lead under
    # substring rules — that's a different problem handled by
    # dedup_signals_pass).
    def _signal_side(lead: Lead) -> str:
        types = {s.type for s in lead.signals}
        has_hire = bool(types & _HIRING_SIGNAL_TYPES)
        has_fund = SignalType.FUNDING_RAISED in types
        if has_hire and has_fund:
            return "both"
        if has_hire:
            return "hire"
        if has_fund:
            return "fund"
        return "none"

    hire_leads: list[tuple[str, Lead]] = []
    fund_leads: list[tuple[str, Lead]] = []
    for lead in db.iter_leads(conn):
        if lead.id is None or lead.id in merged_ids:
            continue
        bk = db.brand_key(lead.name)
        if not bk:
            continue
        side = _signal_side(lead)
        if side == "hire":
            hire_leads.append((bk, lead))
        elif side == "fund":
            fund_leads.append((bk, lead))

    for h_bk, h_lead in hire_leads:
        if h_lead.id in merged_ids:
            continue
        for f_bk, f_lead in fund_leads:
            if f_lead.id in merged_ids:
                continue
            short, long_ = (h_bk, f_bk) if len(h_bk) <= len(f_bk) else (f_bk, h_bk)
            if len(short) < _MIN_BRAND_KEY_LEN_FOR_SUBSTRING:
                continue
            # Must be a token-boundary match — "ai" appearing inside
            # "miami" should not match. Use word-boundary substring
            # check: short must equal long, OR be a prefix/suffix
            # followed/preceded by space, OR appear surrounded by
            # spaces.
            padded = f" {long_} "
            if f" {short} " in padded or padded.startswith(f" {short} ") or padded.endswith(f" {short} "):
                _merge_group([h_lead, f_lead], f"substring_brand_key({short}⊂{long_})")
                break

    if merges == 0:
        # User explicitly asked for this: dump both pools so we can
        # see why nothing matched. Limit to 25 per side to keep log
        # output manageable.
        log.info(
            "bullseye: zero merges. Hiring-pool brand_keys (%d): %s",
            len(hire_leads),
            sorted({bk for bk, _ in hire_leads})[:25],
        )
        log.info(
            "bullseye: zero merges. Funding-pool brand_keys (%d): %s",
            len(fund_leads),
            sorted({bk for bk, _ in fund_leads})[:25],
        )
    return merges


def _enrich_all(
    conn: sqlite3.Connection,
    lead_ids: list[int],
    *,
    force: bool,
    budget: int | None = None,
) -> tuple[list[int], int, int]:
    """Enrich each lead, spending at most ``budget`` LLM lookups
    (None = unlimited). Signal-aware skips are free. Leads deferred
    past the budget stay in the DB and drain via
    ``_enrichment_backlog`` on subsequent nightly runs.

    Returns ``(kept_ids, spent, deferred)``."""
    kept: list[int] = []
    spent = 0
    deferred = 0
    for idx, lead_id in enumerate(lead_ids):
        lead = db.get_lead(conn, lead_id=lead_id)
        if lead is None:
            continue
        needs_call = not enrichment._should_skip(lead, force)
        if needs_call and budget is not None and spent >= budget:
            deferred += 1
            kept.append(lead_id)
            continue
        try:
            if enrichment.enrich(conn, lead, force=force):
                kept.append(lead_id)
        except llm.GeminiQuotaExhausted:
            # Daily free-tier Gemini quota is spent. Stop enriching —
            # this lead and everything after it defers to a later run
            # (the worklist re-ranks them next time). Better than
            # burning the timeout sleeping on a wall we can't clear.
            remaining = lead_ids[idx:]
            deferred += len(remaining)
            kept.extend(remaining)
            log.warning(
                "enrichment stopped early: Gemini quota exhausted after "
                "%d lookups; %d leads deferred to a later run",
                spent, len(remaining),
            )
            break
        except Exception:
            log.exception(
                "enrichment failed for lead %s (id=%s)", lead.name, lead.id
            )
            kept.append(lead_id)
            if needs_call:
                spent += 1
        else:
            if needs_call:
                spent += 1
    # Dedup while preserving order — a deferred lead already in `kept`
    # from a successful earlier pass shouldn't appear twice.
    return list(dict.fromkeys(kept)), spent, deferred


def _enrichment_worklist(conn: sqlite3.Connection, *, force: bool) -> list[int]:
    """Every lead needing an enrichment pass this run (never enriched,
    or a fresh signal since the last pass), ranked best-lead-first.
    Unifies this run's new upserts with leads deferred on prior nights
    so the budget always spends on the strongest leads regardless of
    when they were ingested.

    Priority tiers, each newest-first (highest lead id):
    0. fractional-CFO postings (in-market)
    1. below-CFO finance hires (the gate signal)
    2. funding-only (the weak, high-volume tail)"""
    ranked: list[tuple[int, int, int]] = []
    for lead in db.iter_leads(conn):
        if lead.id is None:
            continue
        if enrichment._should_skip(lead, force):
            continue
        types = {s.type for s in lead.signals}
        if SignalType.JOB_POSTED_FRACTIONAL_CFO in types:
            tier = 0
        elif SignalType.JOB_POSTED_FINANCE_LEAD in types:
            tier = 1
        else:
            tier = 2
        ranked.append((tier, -lead.id, lead.id))
    ranked.sort()
    return [lead_id for _, _, lead_id in ranked]


def _rescore_all(
    conn: sqlite3.Connection, lead_ids: list[int]
) -> dict[int, tuple[float | None, float]]:
    changes: dict[int, tuple[float | None, float]] = {}
    for lead_id in lead_ids:
        lead = db.get_lead(conn, lead_id=lead_id)
        if lead is None:
            continue
        new_score = scoring.score(lead)
        old = lead.score
        try:
            db.update_lead(conn, lead_id, score=new_score)
            changes[lead_id] = (old, new_score)
        except Exception:
            log.exception("rescore: update_lead failed for id=%s", lead_id)
    return changes


def _apollo_enrich_top_n(conn: sqlite3.Connection, *, n: int) -> set[int]:
    """Top-N by score, run Apollo on each lead not yet marked. If
    Apollo surfaces a full-time CFO at the org, the lead is deleted
    and the name is written to the disqualified table — same hard-
    exclude semantics as a CFO job posting."""
    if not apollo.is_configured():
        log.info("apollo: APOLLO_API_KEY not set, skipping DM enrichment")
        return set()

    target_ids: set[int] = set()
    for top_lead in db.iter_leads(conn, limit=n):
        if top_lead.id is None or top_lead.score is None:
            continue
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
            continue

        # Late disqualifier: if Apollo says the company has a full-time
        # CFO, drop the lead and remember it.
        if result.has_full_time_cfo:
            log.info(
                "apollo: deleting lead %d (%s) — Apollo surfaced a CFO at the org",
                lead_id, lead.name,
            )
            try:
                db.delete_lead(conn, lead_id)
                db.mark_disqualified(
                    conn,
                    Disqualifier(
                        name=lead.name,
                        reason="has_full_time_cfo_per_apollo",
                        source=SourceName.APOLLO,
                        payload={},
                    ),
                )
            except Exception:
                log.exception("apollo: cfo-disqualifier write failed for id=%s", lead_id)
            continue

        if result.headcount is not None and result.headcount > enrichment._SMB_HEADCOUNT_CAP:
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


# --- JSON output -----------------------------------------------------------


def _build_output(conn: sqlite3.Connection) -> dict[str, Any]:
    now = _utcnow()
    # Per req #10: drop leads where every scoring signal is > 30 days
    # old. Bullseye leads sort first (req #9) so they surface above
    # single-signal leads at the same score.
    leads_out: list[dict[str, Any]] = []
    for lead in db.iter_leads(conn):
        rendered = _lead_to_json(lead, now=now)
        if not rendered["signals"]:
            continue  # all signals were beyond the recency window / non-scoring
        # A fractional CFO can only act on a company they can find. If
        # we couldn't resolve a website there's nothing to reach out to,
        # so a domainless card is dropped at output (kept in the DB — a
        # later enrichment run might resolve the domain).
        if not rendered.get("domain"):
            continue
        # Belt-and-suspenders empty-shell filter (a lead with no domain
        # AND no DM AND no headcount AND no location). The domain check
        # above already covers it, but keep this in case the domain rule
        # is ever relaxed.
        if _is_empty_shell(rendered):
            continue
        leads_out.append(rendered)
    # Strict tier ordering: in-market fractional-CFO postings first,
    # then finance-lead hires, then funding-only — score orders WITHIN
    # a tier. The tiered score already encodes this (bands don't
    # overlap), but sorting on the tier explicitly keeps the ordering
    # correct even if the score weights are ever retuned.
    def _rank_key(l: dict[str, Any]) -> tuple[int, float]:
        types = {s["type"] for s in l["signals"]}
        if SignalType.JOB_POSTED_FRACTIONAL_CFO.value in types:
            tier = 0
        elif SignalType.JOB_POSTED_FINANCE_LEAD.value in types:
            tier = 1
        else:
            tier = 2
        return (tier, -(l.get("score") or 0))
    leads_out.sort(key=_rank_key)
    leads_out = _gate_funding_only(leads_out)
    return {
        "generated_at": now.isoformat(),
        "leads": leads_out,
    }


def _gate_funding_only(leads_out: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Quality gate for leads whose only signals are funding-side.
    They're the weak class (their need is implicit, not expressed), so
    they must earn their card: a resolved domain, and — when the Form D
    offering amount is known — a raise of at least
    ``FUNDING_ONLY_MIN_OFFERING_USD``. Unknown / "Indefinite" amounts
    (including signals ingested before amounts were mined) pass on the
    domain requirement alone. At most ``FUNDING_ONLY_MAX_CARDS``
    survive; the list arrives sorted, so the best-scored ones win."""
    gated: list[dict[str, Any]] = []
    kept_funding_only = 0
    dropped_small = 0
    dropped_domainless = 0
    dropped_over_cap = 0
    for lead in leads_out:
        types = {s["type"] for s in lead["signals"]}
        if any(t.value in types for t in _HIRING_SIGNAL_TYPES):
            gated.append(lead)
            continue
        if not lead.get("domain"):
            dropped_domainless += 1
            continue
        known_amounts: list[float] = []
        for s in lead["signals"]:
            payload = s.get("payload") or {}
            if payload.get("filing_type") != "Form D":
                continue
            amt = payload.get("offering_amount")
            if isinstance(amt, (int, float)):
                known_amounts.append(float(amt))
        if known_amounts and max(known_amounts) < FUNDING_ONLY_MIN_OFFERING_USD:
            dropped_small += 1
            continue
        if kept_funding_only >= FUNDING_ONLY_MAX_CARDS:
            dropped_over_cap += 1
            continue
        kept_funding_only += 1
        gated.append(lead)
    if dropped_small or dropped_domainless or dropped_over_cap:
        log.info(
            "funding-only gate: kept %d, dropped %d small-offering, "
            "%d domainless, %d over cap",
            kept_funding_only, dropped_small, dropped_domainless,
            dropped_over_cap,
        )
    return gated


def _is_empty_shell(rendered: dict[str, Any]) -> bool:
    """Empty-shell lead has none of: domain, dm_name, headcount, city,
    state. There's nothing actionable on the card, so it shouldn't
    surface even with a valid hiring/funding signal."""
    return (
        not rendered.get("domain")
        and not rendered.get("dm_name")
        and not rendered.get("headcount")
        and not rendered.get("city")
        and not rendered.get("state")
    )


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


_GENERIC_EMAIL_LOCALS: frozenset[str] = frozenset({
    "info", "contact", "hello", "support", "help", "sales", "team",
    "admin", "office", "general", "inquiries", "press", "media",
    "intercom", "noreply", "no-reply", "donotreply", "do-not-reply",
    "marketing", "service", "services", "billing", "accounts",
    "hr", "careers", "jobs", "recruiting",
})

_OPERATOR_TITLE_RE = re.compile(
    r"\b("
    r"founder|co[-\s]?founder|"
    r"ceo|chief\s+executive(?:\s+officer)?|"
    r"president|"
    r"managing\s+(?:partner|director)|"
    r"owner|principal|proprietor|"
    r"coo|chief\s+operating(?:\s+officer)?"
    r")\b",
    re.IGNORECASE,
)


def _is_doubled_dm_name(name: str | None) -> bool:
    if not name:
        return False
    tokens = name.strip().split()
    if len(tokens) < 2:
        return False
    return tokens[0].lower() == tokens[-1].lower()


def _is_generic_email_local(email: str | None) -> bool:
    if not email:
        return False
    local = email.split("@", 1)[0].strip().lower()
    return local in _GENERIC_EMAIL_LOCALS


def _sanitize_dm_for_output(
    lead: Lead,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Apply the round-4 DM quality gates at render time. The Apollo
    module enforces these on fresh lookups, but rows that were
    Apollo-enriched under prior versions still carry bad data
    (Paces 'Paces Paces' + intercom@, Athos VP Analytical Chemistry).
    Cleaning at the render layer means we don't need to re-burn
    Apollo credits to fix the dashboard.

    Returns (dm_name, dm_title, dm_email, dm_linkedin_url) with any
    failing fields nulled. If the picked title doesn't actually
    describe an operator role, the entire DM block is suppressed —
    a VP Analytical Chemistry isn't the fractional-CFO buyer."""
    name = lead.dm_name
    title = lead.dm_title
    email = lead.dm_email
    linkedin = lead.dm_linkedin_url

    if title and not _OPERATOR_TITLE_RE.search(title):
        # Title says non-operator (VP of X, Director of X, ...). Drop
        # the whole panel; the lead falls back to the cold badge.
        return None, None, None, None

    if _is_doubled_dm_name(name):
        name = None
    if _is_generic_email_local(email):
        email = None

    # If nothing actionable is left, suppress the panel entirely.
    if not name and not email and not linkedin:
        return None, None, None, None
    return name, title, email, linkedin


def _lead_to_json(lead: Lead, *, now: datetime) -> dict[str, Any]:
    city, state = _city_state(lead)
    dm_name, dm_title, dm_email, dm_linkedin = _sanitize_dm_for_output(lead)
    # Per req #10: drop scoring signals where the payload's own date
    # (posting date for jobs, filing date for Form D) is > 30 days
    # old. captured_at reflects when the pipeline observed the signal,
    # not when the event itself occurred — and JobSpy returns leads
    # going back ~90 days. The 30-day window is enforced here so the
    # output reflects the actual recency story.
    scoring_sigs = [s for s in lead.signals if s.type in _SCORING_SIGNAL_TYPES]
    fresh_sigs = [s for s in scoring_sigs if not _signal_too_old(s, now)]
    relevant = sorted(
        fresh_sigs, key=lambda s: _signal_age_days(s, now)
    )[:SIGNAL_LIMIT_IN_JSON]
    top_sig = relevant[0] if relevant else None
    cleaned_dm = _clean_dm_name(dm_name)
    return {
        "name": lead.name,
        "domain": lead.domain,
        "industry": lead.industry,
        "headcount": lead.headcount if (lead.headcount or 0) > 1 else None,
        "country": lead.country,
        "city": city,
        "state": state,
        "dm_name": cleaned_dm,
        "dm_title": dm_title,
        "dm_email": dm_email,
        "dm_linkedin_url": dm_linkedin,
        "contact_strength": _contact_strength(
            lead.name, cleaned_dm, dm_email, dm_linkedin
        ),
        "score": lead.score,
        # Insight generation was removed (LLM copy read as templated
        # across leads). Field kept null for schema stability; the
        # LeadCard collapses null cleanly.
        "insight": None,
        "trigger_type": outreach.trigger_type(top_sig) if top_sig else "other",
        "signals": [_signal_to_json(s, now) for s in relevant],
    }


def _clean_dm_name(name: str | None) -> str | None:
    if not name:
        return name
    import re as _re
    cleaned = _re.sub(r"\s+", " ", name).strip()
    return cleaned or None


def _contact_strength(
    lead_name: str,
    dm_name: str | None,
    dm_email: str | None,
    dm_linkedin: str | None,
) -> str:
    if not dm_name:
        return "cold"
    if _names_match(lead_name, dm_name):
        return "cold"
    if dm_email or dm_linkedin:
        return "verified"
    return "partial"


def _names_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    import re as _re

    def _norm(s: str) -> str:
        return _re.sub(r"\s+", " ", _re.sub(r"[^a-z\s]", "", s.lower())).strip()

    return _norm(a) == _norm(b)


def _payload_event_date(s: Signal) -> datetime | None:
    """Per req #10: the date the EVENT happened (job posted, Form D
    filed) — not when the pipeline captured it. Canonical logic lives
    in ``scoring.payload_event_date`` (it drives score decay too);
    reused here for the days_ago label and the 30-day output window."""
    return scoring.payload_event_date(s)


def _signal_age_days(s: Signal, now: datetime) -> int:
    """Days between the EVENT date (preferred) and now. Falls back to
    captured_at when the payload has no event date."""
    event_dt = _payload_event_date(s)
    base = event_dt if event_dt is not None else s.captured_at
    return max(0, (now - base).days)


# Output-time recency window per signal type. Matches the scrape
# windows: fractional-CFO postings are scarce and long-lived, so they
# stay on the page up to 60 days; everything else drops at 30.
_MAX_SIGNAL_AGE_DAYS = 30
_FRACTIONAL_MAX_SIGNAL_AGE_DAYS = 60


def _signal_too_old(s: Signal, now: datetime) -> bool:
    """Drop stale signals at output time. Uses the EVENT date, not
    captured_at. Fractional-CFO postings get the wider 60-day window."""
    max_days = (
        _FRACTIONAL_MAX_SIGNAL_AGE_DAYS
        if s.type == SignalType.JOB_POSTED_FRACTIONAL_CFO
        else _MAX_SIGNAL_AGE_DAYS
    )
    return _signal_age_days(s, now) > max_days


def _signal_to_json(s: Signal, now: datetime) -> dict[str, Any]:
    return {
        "type": s.type.value,
        "captured_at": s.captured_at.isoformat(),
        "days_ago": _signal_age_days(s, now),
        "payload": s.payload,
    }


# --- Upload ----------------------------------------------------------------


def _upload_blob(json_path: Path) -> None:
    url = os.environ.get("CFO_UPLOAD_URL")
    api_key = os.environ.get("CFO_UPLOAD_API_KEY")
    if not url or not api_key:
        raise RuntimeError(
            "--upload requires CFO_UPLOAD_URL and CFO_UPLOAD_API_KEY env vars"
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
    parser = argparse.ArgumentParser(prog="cfo_pipeline.daily_run")
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run sources only; skip DB writes, LLM calls, JSON write",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="POST the local JSON to CFO_UPLOAD_URL after writing it",
    )
    parser.add_argument(
        "--rescore-only",
        action="store_true",
        help="Skip fetch / upsert / disqualifier recording. Dedup, rescore, write, upload.",
    )
    parser.add_argument(
        "--apollo-top-n",
        type=int,
        default=APOLLO_TOP_N_DEFAULT,
        help="Apollo runs only on the top-N leads by score. Default: %(default)d.",
    )
    parser.add_argument(
        "--enrich-budget",
        type=int,
        default=300,
        help="Max Gemini web lookups per run (0 = unlimited). Overflow "
        "leads stay in the DB and drain on subsequent nightly runs. "
        "Default %(default)d — safely inside the free-tier 500/day "
        "grounded-search quota.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())

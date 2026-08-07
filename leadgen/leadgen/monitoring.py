"""Nightly anomaly guard.

Sources fail quietly — a scraper gets Cloudflare-blocked, an AG changes its
HTML, SEC hiccups — and the run just logs "0 candidates", which looks the same
as "no filings today". This compares a run's per-source and per-niche counts
against the previous run (stored in ``run_stats``) and pushes an ntfy alert on
a likely silent failure: a source that dropped to zero, or a niche that lost a
large fraction of its leads.

``NTFY_TOPIC`` unset -> the check still runs and logs, it just doesn't push.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

log = logging.getLogger(__name__)

# A niche losing more than this fraction of its leads vs. the previous run is
# treated as an anomaly worth a heads-up.
_NICHE_DROP_THRESHOLD = 0.5


def detect_anomalies(
    prev: dict[str, Any] | None, curr: dict[str, Any]
) -> list[str]:
    """Return human-readable anomaly messages comparing ``curr`` to ``prev``.
    Empty list when there's nothing to flag. The count diffs need a previous
    run; the enrichment-health checks do not, so they run either way."""
    if not prev:
        return _enrichment_anomalies(curr)
    messages: list[str] = []

    prev_sources: dict[str, int] = prev.get("sources", {})
    for name, count in curr.get("sources", {}).items():
        was = prev_sources.get(name, 0)
        if count == 0 and was > 0:
            messages.append(
                f"source '{name}': 0 candidates (was {was}) — likely broken/blocked"
            )

    prev_niches: dict[str, int] = prev.get("niches", {})
    for niche, count in curr.get("niches", {}).items():
        was = prev_niches.get(niche, 0)
        if was > 0 and count < was * (1 - _NICHE_DROP_THRESHOLD):
            pct = round((1 - count / was) * 100)
            messages.append(f"niche '{niche}': {count} leads (was {was}, -{pct}%)")

    return messages + _enrichment_anomalies(curr)


def _enrichment_anomalies(curr: dict[str, Any]) -> list[str]:
    """Anomalies that need no previous run to spot.

    A latched Gemini quota is the one that hurt: the run still exits 0, so the
    workflow's failure alert never fires, and the count diff above cannot see
    it either (unenriched leads used to push niche counts UP, not down). It
    has to be reported on its own terms."""
    messages: list[str] = []
    if curr.get("gemini_quota_exhausted"):
        messages.append(
            "gemini quota exhausted mid-run — enrichment stopped early, leads are "
            "unenriched and held back from publish. Check the AI Studio balance."
        )
    messages.extend(_insight_anomalies(curr))
    return messages


# `insight` is the ONE field with a consumer that cannot degrade gracefully:
# the outreach engine's Gate B judges each gift lead's description to verify a
# vertical claim, so an empty description fails every lead and silently drops
# every gift to a generalist email. Nothing else notices — niche counts are
# unchanged, the run exits 0.
#
# This floor is deliberately FAR below normal (observed fill: 97-100% on every
# niche) and far above zero. It is not a quality bar: model variance moves fill
# a few points, never to 20%. Only a systematic failure gets here — a dead or
# rotated API key, a drained balance, a provider outage, or the field being
# dropped from the published record. Keep it low; a tight threshold would page
# on ordinary variance and get muted, which is how a real alert gets missed.
_INSIGHT_FLOOR = 0.20


def _insight_anomalies(curr: dict[str, Any]) -> list[str]:
    """Alert when published leads have essentially no `insight`. Reported per
    niche so a single broken niche is distinguishable from a whole-feed
    failure. Niches with no published leads are skipped — an empty inventory is
    the count guard's business, and 0/0 is not a fill-rate failure."""
    fill: dict[str, Any] = curr.get("insight_fill") or {}
    messages: list[str] = []
    for niche, stats in sorted(fill.items()):
        total = (stats or {}).get("total") or 0
        filled = (stats or {}).get("filled") or 0
        if total <= 0:
            continue
        ratio = filled / total
        if ratio < _INSIGHT_FLOOR:
            messages.append(
                f"niche '{niche}': only {filled}/{total} published leads "
                f"({ratio:.0%}) have an insight — below the {_INSIGHT_FLOOR:.0%} "
                "floor. Gate B judges gift leads on that field, so outreach will "
                "drop every vertical claim to a generalist email. Check the "
                "Gemini/OpenAI API key and balance."
            )
    return messages


def alert(messages: list[str]) -> None:
    """Log the anomalies and, if ``NTFY_TOPIC`` is set, push them. Never raises
    — a failed alert must not fail the run."""
    if not messages:
        return
    body = "leadgen nightly anomalies:\n" + "\n".join(f"- {m}" for m in messages)
    log.warning(body)

    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    try:
        requests.post(
            f"{server}/{topic}",
            data=body.encode("utf-8"),
            headers={"Title": "leadgen anomaly", "Priority": "high"},
            timeout=15,
        )
    except Exception:
        log.exception("ntfy alert failed")


def check(
    prev: dict[str, Any] | None, curr: dict[str, Any]
) -> list[str]:
    """Detect + alert in one call. Returns the messages (for logging/tests)."""
    messages = detect_anomalies(prev, curr)
    alert(messages)
    return messages

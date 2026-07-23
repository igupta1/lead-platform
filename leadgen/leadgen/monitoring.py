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
    Empty list when there's nothing to flag (including the first ever run,
    where ``prev`` is None)."""
    if not prev:
        return []
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

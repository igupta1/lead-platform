"""Bookkeeping sub-inventory runner.

Captures junior finance hires (see `sources.bookkeeping`) and writes them to
their OWN inventory — separate from the CFO pipeline, which is left completely
untouched. A junior finance hire is not a fractional-CFO buying signal; it's the
buying signal for a bookkeeping / outsourced-accounting firm.

    fetch (junior job postings) -> dedup by company -> write JSON -> upload

The output is served under its own Blob key (BOOKKEEPING_UPLOAD_URL); the
outreach bookkeeping pack reads it directly. Best-effort upload: if the env
isn't set the local JSON is still written and the upload is skipped.
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

from cfo_pipeline.models import LeadCandidate, SignalType
from cfo_pipeline.sources import bookkeeping

log = logging.getLogger("cfo.bookkeeping_run")

DEFAULT_OUTPUT_PATH = Path("data/bookkeeping-leads.json")
LOOKBACK_DAYS = 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _title_of(cand: LeadCandidate) -> str:
    return str((cand.initial_signal.payload or {}).get("title") or "")


def _date_of(cand: LeadCandidate) -> str:
    return str((cand.initial_signal.payload or {}).get("date_posted") or "")


def _name_key(name: str) -> str:
    return " ".join(name.lower().split())


def _dedup(cands: list[LeadCandidate]) -> list[LeadCandidate]:
    """One card per company — keep the posting with the most recent date."""
    best: dict[str, LeadCandidate] = {}
    for c in cands:
        key = _name_key(c.name)
        cur = best.get(key)
        if cur is None or _date_of(c) > _date_of(cur):
            best[key] = c
    return list(best.values())


def _insight(title: str) -> str:
    t = title.strip().lower()
    return f"hiring a {t}" if t else "hiring junior finance staff"


def _candidate_to_json(cand: LeadCandidate) -> dict[str, Any]:
    sig = cand.initial_signal
    payload = sig.payload or {}
    return {
        "name": cand.name,
        "domain": cand.domain,
        "city": payload.get("city"),
        "state": payload.get("state"),
        "industry": None,
        "role_tier": "junior",
        "insight": _insight(str(payload.get("title") or "")),
        "signals": [
            {
                "type": sig.type.value,
                "captured_at": sig.captured_at.isoformat(),
                "payload": payload,
            }
        ],
    }


def build_output(cands: list[LeadCandidate], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or _utcnow()
    leads = [_candidate_to_json(c) for c in _dedup(cands)]
    # freshest first
    leads.sort(key=lambda l: (l["signals"][0]["payload"].get("date_posted") or ""), reverse=True)
    return {"generated_at": now.isoformat(), "leads": leads}


def _upload_blob(json_path: Path, *, required: bool = False) -> None:
    url = os.environ.get("BOOKKEEPING_UPLOAD_URL")
    api_key = os.environ.get("BOOKKEEPING_UPLOAD_API_KEY")
    if not url or not api_key:
        if required:
            raise RuntimeError("--upload requires BOOKKEEPING_UPLOAD_URL and BOOKKEEPING_UPLOAD_API_KEY")
        log.info("BOOKKEEPING_UPLOAD_URL not set; skipping upload of %s", json_path.name)
        return
    resp = requests.post(
        url, json=json.loads(json_path.read_text()),
        headers={"Authorization": f"Bearer {api_key}"}, timeout=30,
    )
    resp.raise_for_status()
    log.info("uploaded blob %s: %s", json_path.name, resp.json())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cfo_pipeline.bookkeeping_run")
    parser.add_argument("--output-path", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--limit", type=int, default=None, help="Per-run candidate cap")
    parser.add_argument("--dry-run", action="store_true", help="Fetch only; no JSON write")
    parser.add_argument("--upload", action="store_true", help="POST the JSON to BOOKKEEPING_UPLOAD_URL")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    since = _utcnow() - timedelta(days=LOOKBACK_DAYS)
    cands = bookkeeping.fetch(since=since, limit=args.limit)
    log.info("fetched %d bookkeeping candidates", len(cands))
    if args.dry_run:
        log.info("dry-run: skipping JSON write")
        return 0

    output = build_output(cands)
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(json.dumps(output, indent=2, default=str))
    log.info("wrote %s (%d leads)", args.output_path, len(output["leads"]))

    if args.upload:
        try:
            _upload_blob(args.output_path, required=True)
        except Exception:
            log.exception("upload failed")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

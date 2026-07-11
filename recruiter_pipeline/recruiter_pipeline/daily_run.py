"""Recruiter hiring-volume runner.

    load seed boards -> fetch each company's open roles (ATS) -> aggregate to
    unique-role counts -> keep heavy hirers (3+ unique / 30d) -> write inventory
    -> upload.

Company-centric, so each run rebuilds the current snapshot from the ATS boards —
no incremental DB needed. Seed boards live in `seed_boards.json` (a growing list
of {provider, slug, company}); discovery of new boards is a later enhancement.

Output is served under its own Blob key (RECRUITER_UPLOAD_URL); the outreach
recruiter pack reads it directly. Best-effort upload (skipped if env unset).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

from recruiter_pipeline.aggregate import CompanyHiring, aggregate, is_heavy
from recruiter_pipeline.sources import ats

log = logging.getLogger("recruiter.daily_run")

DEFAULT_OUTPUT_PATH = Path("data/recruiter-leads.json")
DEFAULT_SEEDS_PATH = Path(__file__).resolve().parent / "seed_boards.json"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def load_seeds(path: Path) -> list[dict[str, str]]:
    """[{provider, slug, company}, ...] — the ATS boards to poll."""
    data = json.loads(Path(path).read_text())
    return list(data.get("boards") or [])


def collect(
    seeds: list[dict[str, str]], *, today: date,
    http_get_json: ats.HttpGetJson | None = None,
) -> list[CompanyHiring]:
    """Fetch + aggregate every seed board (a dead board yields no roles, never
    an exception — fetch_board swallows errors)."""
    out: list[CompanyHiring] = []
    for b in seeds:
        provider, slug, company = b.get("provider", ""), b.get("slug", ""), b.get("company", "")
        if not provider or not slug:
            continue
        roles = ats.fetch_board(provider, slug, http_get_json=http_get_json)
        out.append(aggregate(company or slug, provider, slug, roles, today=today))
    return out


def _insight(ch: CompanyHiring) -> str:
    n = ch.unique_role_count
    funcs = sorted(ch.functions.items(), key=lambda kv: -kv[1])
    top = ", ".join(f for f, _ in funcs[:2]) or ch.primary_function
    return f"hiring {n} roles right now, mostly {top}"


def _to_json(ch: CompanyHiring, *, now: datetime) -> dict[str, Any]:
    return {
        "name": ch.company,
        "domain": None,
        "city": ch.city,
        "state": ch.state,
        "unique_role_count": ch.unique_role_count,
        "primary_function": ch.primary_function,
        "functions": ch.functions,
        "insight": _insight(ch),
        "signals": [
            {
                "type": "hiring_volume",
                "captured_at": now.isoformat(),
                "payload": {
                    "unique_role_count": ch.unique_role_count,
                    "primary_function": ch.primary_function,
                    "functions": ch.functions,
                    "role_titles": ch.role_titles,
                    "date": ch.latest_date,
                    "provider": ch.provider,
                    "slug": ch.slug,
                },
            }
        ],
    }


def build_output(
    hirings: list[CompanyHiring], *, now: datetime | None = None, min_unique: int = 3
) -> dict[str, Any]:
    now = now or _utcnow()
    heavy = [h for h in hirings if is_heavy(h, min_unique=min_unique)]
    heavy.sort(key=lambda h: h.unique_role_count, reverse=True)   # loudest first
    return {"generated_at": now.isoformat(), "leads": [_to_json(h, now=now) for h in heavy]}


def _upload_blob(json_path: Path, *, required: bool = False) -> None:
    url = os.environ.get("RECRUITER_UPLOAD_URL")
    api_key = os.environ.get("RECRUITER_UPLOAD_API_KEY")
    if not url or not api_key:
        if required:
            raise RuntimeError("--upload requires RECRUITER_UPLOAD_URL and RECRUITER_UPLOAD_API_KEY")
        log.info("RECRUITER_UPLOAD_URL not set; skipping upload of %s", json_path.name)
        return
    resp = requests.post(
        url, json=json.loads(json_path.read_text()),
        headers={"Authorization": f"Bearer {api_key}"}, timeout=30,
    )
    resp.raise_for_status()
    log.info("uploaded blob %s: %s", json_path.name, resp.json())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recruiter_pipeline.daily_run")
    parser.add_argument("--output-path", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--seeds-path", type=Path, default=DEFAULT_SEEDS_PATH)
    parser.add_argument("--min-unique", type=int, default=3, help="Heavy-hirer threshold")
    parser.add_argument("--dry-run", action="store_true", help="Fetch + aggregate only; no write")
    parser.add_argument("--upload", action="store_true", help="POST the JSON to RECRUITER_UPLOAD_URL")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    seeds = load_seeds(args.seeds_path)
    log.info("polling %d ATS boards", len(seeds))
    hirings = collect(seeds, today=_utcnow().date())
    heavy = [h for h in hirings if is_heavy(h, min_unique=args.min_unique)]
    log.info("%d/%d companies are heavy hirers (>=%d unique roles)", len(heavy), len(hirings), args.min_unique)
    if args.dry_run:
        return 0

    output = build_output(hirings, min_unique=args.min_unique)
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

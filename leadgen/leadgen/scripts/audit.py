"""Data-quality audit for the generated lead inventories.

Reads the per-niche ``*-leads.json`` a run emits and reports quantitative
quality metrics — no API keys needed. The evidence contract makes the data
self-auditing: every lead carries verbatim evidence + a source_url, so this
tool can check completeness and (optionally) URL liveness directly.

    python -m leadgen.scripts.audit --dir data                # metrics only
    python -m leadgen.scripts.audit --dir data --check-urls    # + HTTP liveness
    python -m leadgen.scripts.audit --dir data --sample 20     # print a QA sample

What it reports per niche:
  * lead count, signal-type mix, top states
  * score distribution (min / median / max)
  * freshness: age of each lead's headline signal, bucketed
  * enrichment coverage: % domain, % headcount, and a HARD check that
    nothing >=100 employees slipped through (contract violation)
  * evidence completeness: % leads with non-empty evidence_text + source_url
And across niches:
  * missed-dedup scan: near-duplicate company names (rapidfuzz)
  * (opt-in) URL liveness: % of source_urls returning a live response
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from leadgen.models import SignalType
from leadgen.niches import NICHES

FRESH_BUCKETS = [(7, "<=7d"), (14, "<=14d"), (30, "<=30d"), (60, "<=60d")]


def _cap_for_lead(niche_key: str, lead: dict[str, Any]) -> int:
    """The real per-niche, per-signal size cap (accounting/cfo 100, IT niches
    250, breach uncapped) — matches leadgen.scoring, so the audit doesn't flag
    legitimately-sized leads."""
    cfg = NICHES.get(niche_key)
    if cfg is None:
        return 100
    try:
        st = SignalType(lead.get("signal_type"))
    except ValueError:
        return cfg.size_cap
    return cfg.cap_for([st])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
        return d.replace(tzinfo=None) if d.tzinfo else d
    except ValueError:
        return None


def _age_days(iso: str | None, *, now: datetime) -> float | None:
    d = _parse_dt(iso)
    return None if d is None else max(0.0, (now - d).total_seconds() / 86400.0)


def _load(dir_: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(dir_.glob("*-leads.json")):
        payload = json.loads(path.read_text())
        out[payload.get("niche", path.stem)] = payload
    return out


def _pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.0f}%" if d else "n/a"


def _bucket_freshness(ages: list[float]) -> str:
    if not ages:
        return "no dated signals"
    parts = []
    for lim, label in FRESH_BUCKETS:
        parts.append(f"{label}={sum(1 for a in ages if a <= lim)}")
    parts.append(f">60d={sum(1 for a in ages if a > 60)}")
    return "  ".join(parts)


def audit_niche(niche: str, payload: dict[str, Any], *, now: datetime) -> list[str]:
    leads: list[dict[str, Any]] = payload.get("leads", [])
    lines = [f"\n=== {niche}  ({len(leads)} leads) ==="]
    if not leads:
        lines.append("  (empty)")
        return lines

    sigmix = Counter(ld.get("signal_type") for ld in leads)
    states = Counter((ld.get("state") or "?") for ld in leads)
    scores = [ld["score"] for ld in leads if ld.get("score") is not None]
    ages = [a for ld in leads
            if (a := _age_days((ld.get("signals") or [{}])[0].get("event_date"), now=now)) is not None]

    with_domain = sum(1 for ld in leads if ld.get("domain"))
    with_hc = sum(1 for ld in leads if ld.get("headcount") is not None)
    oversized = [ld for ld in leads
                 if ld.get("headcount") and ld["headcount"] >= _cap_for_lead(niche, ld)]
    missing_evidence = [ld for ld in leads
                        if not (ld.get("evidence_text") and ld.get("source_url"))]

    lines.append(f"  signal mix : {dict(sigmix)}")
    lines.append(f"  top states : {dict(states.most_common(5))}")
    if scores:
        lines.append(f"  score      : min={min(scores):.0f} median={statistics.median(scores):.0f} max={max(scores):.0f}")
    lines.append(f"  freshness  : {_bucket_freshness(ages)}")
    lines.append(f"  enrichment : domain {_pct(with_domain,len(leads))}  headcount {_pct(with_hc,len(leads))}")
    if oversized:
        lines.append(f"  ** VIOLATION: {len(oversized)} lead(s) >= {SIZE_CAP} employees: "
                     f"{[ld['name'] for ld in oversized][:5]}")
    if missing_evidence:
        lines.append(f"  ** VIOLATION: {len(missing_evidence)} lead(s) missing evidence/url: "
                     f"{[ld['name'] for ld in missing_evidence][:5]}")
    else:
        lines.append("  evidence   : 100% carry evidence_text + source_url")
    return lines


def dedup_scan(niches: dict[str, dict[str, Any]], *, threshold: int = 92) -> list[str]:
    """Surface near-duplicate company names that survived dedup. Same company
    across niches is expected (informational); near-duplicate *distinct*
    names hint at a missed merge."""
    rows: list[tuple[str, str, str | None]] = []  # (niche, name, domain)
    for niche, payload in niches.items():
        for ld in payload.get("leads", []):
            rows.append((niche, ld.get("name", ""), ld.get("domain")))

    lines = ["\n=== cross-niche dedup scan ==="]
    # exact same domain across different displayed names -> likely missed merge
    by_domain: dict[str, set[str]] = {}
    for _, name, domain in rows:
        if domain:
            by_domain.setdefault(domain.lower(), set()).add(name)
    domain_collisions = {d: ns for d, ns in by_domain.items() if len(ns) > 1}
    if domain_collisions:
        lines.append(f"  same domain / different names ({len(domain_collisions)}):")
        for d, ns in list(domain_collisions.items())[:10]:
            lines.append(f"    {d}: {sorted(ns)}")
    else:
        lines.append("  no same-domain/different-name collisions")

    # fuzzy near-duplicate distinct names (across the whole product)
    names = sorted({n for _, n, _ in rows if n})
    near = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if names[i][:1] != names[j][:1]:
                continue  # cheap prefilter
            if fuzz.ratio(names[i].lower(), names[j].lower()) >= threshold:
                near.append((names[i], names[j]))
    if near:
        lines.append(f"  fuzzy near-duplicate names (>={threshold}), review for missed merge ({len(near)}):")
        for a, b in near[:15]:
            lines.append(f"    {a!r} ~ {b!r}")
    else:
        lines.append("  no fuzzy near-duplicate names")
    return lines


def check_urls(niches: dict[str, dict[str, Any]], *, workers: int = 12) -> list[str]:
    import requests

    urls = sorted({ld.get("source_url") for p in niches.values()
                   for ld in p.get("leads", []) if ld.get("source_url")})

    def probe(u: str) -> tuple[str, int | str]:
        try:
            r = requests.head(u, timeout=15, allow_redirects=True)
            if r.status_code >= 400:  # some hosts reject HEAD
                r = requests.get(u, timeout=20, stream=True, allow_redirects=True)
            return u, r.status_code
        except Exception as e:  # noqa: BLE001
            return u, type(e).__name__

    results = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for u, status in ex.map(probe, urls):
            results[u] = status
    live = sum(1 for s in results.values() if isinstance(s, int) and s < 400)
    dead = [(u, s) for u, s in results.items() if not (isinstance(s, int) and s < 400)]
    lines = [f"\n=== URL liveness ({live}/{len(urls)} live) ==="]
    for u, s in dead[:20]:
        lines.append(f"  DEAD [{s}] {u}")
    if len(dead) > 20:
        lines.append(f"  ... and {len(dead) - 20} more")
    return lines


def print_sample(niches: dict[str, dict[str, Any]], per_niche: int) -> list[str]:
    lines = ["\n=== QA sample (open each source_url and label valid/invalid) ==="]
    for niche, payload in niches.items():
        leads = payload.get("leads", [])[:per_niche]
        lines.append(f"\n-- {niche} --")
        for ld in leads:
            lines.append(f"  {ld.get('name')}  [{ld.get('signal_type')}]  score={ld.get('score')}")
            lines.append(f"     {ld.get('evidence_text')}")
            lines.append(f"     {ld.get('source_url')}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Audit generated lead inventories")
    ap.add_argument("--dir", type=Path, default=Path("data"))
    ap.add_argument("--check-urls", action="store_true", help="probe every source_url (network)")
    ap.add_argument("--sample", type=int, default=0, help="print N leads/niche for human labeling")
    args = ap.parse_args(argv)

    niches = _load(args.dir)
    if not niches:
        print(f"No *-leads.json found in {args.dir}")
        return 1

    now = _now()
    out: list[str] = ["LEADGEN DATA-QUALITY AUDIT",
                      f"dir={args.dir}  niches={list(niches)}"]
    total = sum(len(p.get("leads", [])) for p in niches.values())
    out.append(f"total leads across niches: {total}")
    for niche, payload in niches.items():
        out += audit_niche(niche, payload, now=now)
    out += dedup_scan(niches)
    if args.check_urls:
        out += check_urls(niches)
    if args.sample:
        out += print_sample(niches, args.sample)
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

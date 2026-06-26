"""One-shot evaluation: see what Apollo returns for a sample of existing
leads, side-by-side with what Gemini already populated.

Run it from the pipeline/ dir:

    APOLLO_API_KEY=... python -m msp_pipeline.scripts.apollo_probe [--limit 10]

The script makes free org-search + free people-search calls. It does NOT
unlock emails (which would consume Apollo credits). Output is human-
readable text — there's nothing persisted.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from msp_pipeline import db

load_dotenv()

_BASE = "https://api.apollo.io/api/v1"

# Titles we'd consider a decision-maker for an IT MSP / MSSP / Cloud sale.
# Apollo's `person_titles` filter is OR-matched.
_DM_TITLES = [
    # Tech execs first — these are the gold-standard buyers.
    "Chief Information Officer",
    "CIO",
    "Chief Technology Officer",
    "CTO",
    "Chief Information Security Officer",
    "CISO",
    "VP of Information Technology",
    "VP of Technology",
    "Vice President of IT",
    "Director of Information Technology",
    "Director of IT",
    "Head of IT",
    "IT Director",
    "IT Manager",
    "Head of Engineering",
    # Fallback for very small SMBs without a tech exec
    "Chief Operating Officer",
    "COO",
    "President",
    "Owner",
    "Founder",
    "Co-Founder",
    "Chief Executive Officer",
    "CEO",
]


def _headers(api_key: str) -> dict[str, str]:
    return {
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "Accept": "application/json",
    }


def _enrich_org_by_domain(api_key: str, domain: str) -> dict[str, Any] | None:
    try:
        r = requests.post(
            f"{_BASE}/organizations/enrich",
            params={"domain": domain},
            headers=_headers(api_key),
            timeout=20,
        )
        r.raise_for_status()
        return r.json().get("organization")
    except Exception as e:
        print(f"    [enrich-by-domain error: {e}]")
        return None


def _search_org_by_name(api_key: str, name: str) -> dict[str, Any] | None:
    try:
        r = requests.post(
            f"{_BASE}/mixed_companies/search",
            json={"q_organization_name": name, "page": 1, "per_page": 1},
            headers=_headers(api_key),
            timeout=20,
        )
        r.raise_for_status()
        orgs = r.json().get("organizations") or []
        return orgs[0] if orgs else None
    except Exception as e:
        print(f"    [search-by-name error: {e}]")
        return None


def _top_people_at_org(api_key: str, org_id: str) -> list[dict[str, Any]]:
    """Apollo's purpose-built "top N people at this org" endpoint."""
    try:
        r = requests.post(
            f"{_BASE}/mixed_people/organization_top_people",
            json={"organization_id": org_id},
            headers=_headers(api_key),
            timeout=20,
        )
        r.raise_for_status()
        return r.json().get("people") or []
    except Exception as e:
        print(f"    [top-people error: {e}]")
        return []


def _search_people(api_key: str, org_id: str) -> list[dict[str, Any]]:
    """Fallback: filtered people search by title at the org."""
    try:
        r = requests.post(
            f"{_BASE}/mixed_people/api_search",
            json={
                "organization_ids": [org_id],
                "person_titles": _DM_TITLES,
                "page": 1,
                "per_page": 5,
            },
            headers=_headers(api_key),
            timeout=20,
        )
        r.raise_for_status()
        return r.json().get("people") or []
    except Exception as e:
        print(f"    [api_search error: {e}]")
        return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--db-path", type=Path, default=Path("data/leads.db"))
    args = parser.parse_args()

    api_key = os.environ.get("APOLLO_API_KEY")
    if not api_key:
        print("ERROR: APOLLO_API_KEY not set (try `source .env` or export it).", file=sys.stderr)
        return 1

    conn = db.init_db(args.db_path)
    rows = conn.execute(
        """
        SELECT id, name, domain, headcount, industry, dm_name, dm_title
        FROM leads
        ORDER BY RANDOM()
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()

    if not rows:
        print("No leads in DB.")
        return 1

    org_found = 0
    people_found = 0
    print(f"Probing Apollo for {len(rows)} random leads...\n")

    for row in rows:
        print(f"=== {row['name']} ===")
        print(
            f"  domain: {row['domain'] or '(none)'}"
            f" | hc: {row['headcount'] or '?'}"
            f" | industry: {row['industry'] or '?'}"
        )
        print(
            f"  Gemini DM: {row['dm_name'] or '(none)'} / "
            f"{row['dm_title'] or '(none)'}"
        )

        org = None
        if row["domain"]:
            org = _enrich_org_by_domain(api_key, row["domain"])
        if not org:
            org = _search_org_by_name(api_key, row["name"])

        if not org:
            print("  Apollo org: NOT FOUND")
            print()
            continue

        org_found += 1
        print(
            f"  Apollo org: {org.get('name')!r}"
            f" | hc: {org.get('estimated_num_employees') or '?'}"
            f" | domain: {org.get('primary_domain') or org.get('website_url') or '?'}"
            f" | industry: {org.get('industry') or '?'}"
        )

        # Try the purpose-built top-people endpoint first.
        people = _top_people_at_org(api_key, org["id"])
        source = "organization_top_people"
        if not people:
            people = _search_people(api_key, org["id"])
            source = "api_search"

        if not people:
            print("  Apollo people: none matching DM titles")
            print()
            continue

        people_found += 1
        print(f"  Apollo top DMs ({len(people)} via {source}):")
        for p in people[:5]:
            seniority = p.get("seniority") or "?"
            li = p.get("linkedin_url") or "(no linkedin)"
            email = p.get("email") or p.get("email_status") or "(locked)"
            print(
                f"    · {p.get('name')!r} | {p.get('title')!r}"
                f" | seniority={seniority} | email={email}"
                f"\n      {li}"
            )
        print()

    print("─" * 60)
    print(f"Summary: {org_found}/{len(rows)} orgs found, "
          f"{people_found}/{len(rows)} had at least one DM-titled person.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

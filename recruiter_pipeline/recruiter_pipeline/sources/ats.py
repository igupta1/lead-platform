"""ATS job-board ingestion (Greenhouse / Lever / Ashby).

These boards are *company-centric*: one call returns a company's entire open-role
list, which is exactly what the recruiter signal needs (count a company's unique
open roles). Each provider parser normalizes its payload into `Role`. HTTP is
injected (`http_get_json`) so parsing is unit-testable without network.

Public board endpoints (no auth):
  Greenhouse: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
  Lever:      https://api.lever.co/v0/postings/{slug}?mode=json
  Ashby:      https://api.ashbyhq.com/posting-api/job-board/{slug}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("recruiter.ats")

HttpGetJson = Callable[[str], Any]

GREENHOUSE = "greenhouse"
LEVER = "lever"
ASHBY = "ashby"


@dataclass(frozen=True)
class Role:
    title: str
    location: str | None
    updated_at: str | None       # ISO date string (posting/updated date), or None
    department: str | None


def _default_http_get_json(url: str) -> Any:
    import requests  # lazy so importing this module never requires requests

    resp = requests.get(url, timeout=20, headers={"User-Agent": "recruiter-pipeline"})
    resp.raise_for_status()
    return resp.json()


# --- per-provider parsers --------------------------------------------------

def _parse_greenhouse(data: dict[str, Any]) -> list[Role]:
    out: list[Role] = []
    for j in (data.get("jobs") or []):
        loc = (j.get("location") or {}).get("name")
        depts = j.get("departments") or []
        dept = depts[0].get("name") if depts else None
        out.append(Role(
            title=str(j.get("title") or "").strip(),
            location=loc,
            updated_at=j.get("updated_at") or j.get("first_published"),
            department=dept,
        ))
    return out


def _parse_lever(data: list[dict[str, Any]]) -> list[Role]:
    out: list[Role] = []
    for j in (data or []):
        cats = j.get("categories") or {}
        created = j.get("createdAt")
        # Lever createdAt is epoch millis; keep as-is string if not int.
        updated = None
        if isinstance(created, (int, float)):
            from datetime import datetime, timezone
            updated = datetime.fromtimestamp(created / 1000, tz=timezone.utc).date().isoformat()
        elif created:
            updated = str(created)
        out.append(Role(
            title=str(j.get("text") or "").strip(),
            location=cats.get("location"),
            updated_at=updated,
            department=cats.get("team") or cats.get("department"),
        ))
    return out


def _parse_ashby(data: dict[str, Any]) -> list[Role]:
    out: list[Role] = []
    for j in (data.get("jobs") or []):
        out.append(Role(
            title=str(j.get("title") or "").strip(),
            location=j.get("location"),
            updated_at=j.get("publishedDate") or j.get("updatedAt"),
            department=j.get("department") or j.get("team"),
        ))
    return out


_PROVIDERS: dict[str, tuple[str, Callable[[Any], list[Role]]]] = {
    GREENHOUSE: ("https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false", _parse_greenhouse),
    LEVER: ("https://api.lever.co/v0/postings/{slug}?mode=json", _parse_lever),
    ASHBY: ("https://api.ashbyhq.com/posting-api/job-board/{slug}", _parse_ashby),
}


def fetch_board(
    provider: str, slug: str, *, http_get_json: HttpGetJson | None = None
) -> list[Role]:
    """Fetch and normalize one company's open roles. Returns [] on any error
    (a dead board must not sink the whole run)."""
    if provider not in _PROVIDERS:
        raise ValueError(f"unknown ATS provider: {provider!r}")
    url_tmpl, parser = _PROVIDERS[provider]
    getter = http_get_json or _default_http_get_json
    try:
        data = getter(url_tmpl.format(slug=slug))
    except Exception:
        log.exception("ATS fetch failed: provider=%s slug=%s", provider, slug)
        return []
    try:
        return [r for r in parser(data) if r.title]
    except Exception:
        log.exception("ATS parse failed: provider=%s slug=%s", provider, slug)
        return []

"""Per-company hiring-volume aggregation.

"Hiring heavily" = 3+ UNIQUE roles in a rolling 30-day window (dedupe by
normalized title — three posts of the same job is not heavy). A company that
clears the bar becomes a recruiter lead, tagged with its function mix (so a
finance recruiter can be matched finance-heavy hirers) and a primary location.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

from recruiter_pipeline.functions import classify_function
from recruiter_pipeline.sources.ats import Role

MIN_UNIQUE_ROLES = 3
WINDOW_DAYS = 30

_WS_RE = re.compile(r"\s+")
# Strip seniority / location / employment-type qualifiers so "Senior Software
# Engineer" and "Software Engineer II, Remote" collapse to one unique role.
_QUALIFIER_RE = re.compile(
    r"\b(senior|sr|junior|jr|staff|principal|lead|associate|entry[\s-]?level|"
    r"i{1,3}|iv|v|remote|hybrid|onsite|contract|full[\s-]?time|part[\s-]?time|"
    r"intern|internship)\b",
    re.IGNORECASE,
)
_NONWORD_RE = re.compile(r"[^a-z0-9]+")

_US_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}


def normalize_title(title: str) -> str:
    """Canonical form for uniqueness: lowercase, drop seniority/mode qualifiers,
    collapse to word tokens. 'Sr. Software Engineer II (Remote)' -> 'software engineer'."""
    t = title.lower()
    t = _QUALIFIER_RE.sub(" ", t)
    t = _NONWORD_RE.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


def _in_window(updated_at: str | None, today: date, window_days: int) -> bool:
    """True when the role is undated (keep — many boards omit dates) or within
    the window."""
    if not updated_at:
        return True
    try:
        d = datetime.fromisoformat(updated_at[:10]).date()
    except (ValueError, TypeError):
        return True
    delta = (today - d).days
    return 0 <= delta <= window_days if delta >= 0 else True


def _split_location(loc: str | None) -> tuple[str | None, str | None]:
    s = (loc or "").strip()
    if not s or s.lower().startswith("remote"):
        return None, None
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if not parts:
        return None, None
    city = parts[0]
    state = None
    if len(parts) >= 2 and parts[1].split():
        tok = parts[1].split()[0].upper()
        if tok in _US_STATE_ABBR:
            state = tok
    return city or None, state


@dataclass
class CompanyHiring:
    company: str
    provider: str
    slug: str
    unique_role_count: int
    functions: dict[str, int]            # function -> unique-role count
    primary_function: str
    city: str | None
    state: str | None
    role_titles: list[str] = field(default_factory=list)   # one per unique role
    latest_date: str | None = None


def aggregate(
    company: str, provider: str, slug: str, roles: list[Role], *,
    today: date, window_days: int = WINDOW_DAYS,
) -> CompanyHiring:
    """Collapse a company's open roles into a hiring-volume summary."""
    seen: dict[str, Role] = {}
    for r in roles:
        if not _in_window(r.updated_at, today, window_days):
            continue
        key = normalize_title(r.title)
        if key and key not in seen:
            seen[key] = r

    unique_roles = list(seen.values())
    func_counts: Counter[str] = Counter(classify_function(r.title) for r in unique_roles)
    loc_counts: Counter[tuple[str | None, str | None]] = Counter()
    dates: list[str] = []
    for r in unique_roles:
        loc_counts[_split_location(r.location)] += 1
        if r.updated_at:
            dates.append(r.updated_at[:10])

    primary_function = func_counts.most_common(1)[0][0] if func_counts else "other"
    # Primary location = the most common non-empty (city,state); else most common.
    city = state = None
    for (c, s), _n in loc_counts.most_common():
        if c or s:
            city, state = c, s
            break

    return CompanyHiring(
        company=company,
        provider=provider,
        slug=slug,
        unique_role_count=len(unique_roles),
        functions=dict(func_counts),
        primary_function=primary_function,
        city=city,
        state=state,
        role_titles=[r.title for r in unique_roles],
        latest_date=max(dates) if dates else None,
    )


def is_heavy(ch: CompanyHiring, *, min_unique: int = MIN_UNIQUE_ROLES) -> bool:
    """3+ unique roles clears the bar."""
    return ch.unique_role_count >= min_unique

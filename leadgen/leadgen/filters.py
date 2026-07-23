"""Shared company-NAME exclusion filters — "a company we'd never target".

One home for the name-based exclusion predicates used across the platform
(the jobs source's per-row loops, ``enrichment``'s purge pass, and the
``fractional_boards`` source): recruiters / staffing firms, auto dealerships,
branded hotels, government / public-sector entities, and lone generic-stub
company names. :func:`is_untargetable_name` is the single gate that ORs them
all together.

These are NAME filters. TITLE filters (e.g. ``_is_automotive_title``) stay in
``leadgen.sources.jobs``. This module intentionally imports nothing from
``leadgen.sources.jobs`` — jobs imports filters, never the reverse.
"""

from __future__ import annotations

import re

# --- Company-name exclusions -----------------------------------------------
# Recruiter / staffing firms: a "Robert Half" posting is on BEHALF of an
# unnamed client — the lead would be the staffing firm. "Search" alone is
# too generic, so it's only flagged when paired with a firm suffix.
_RECRUITER_NAME_PATTERN = re.compile(
    r"\b(staffing|recruit(?:ing|er|ers|ment)|headhunter|jobot|"
    r"personnel\s+services?|talent\s+(?:group|agency|partners|solutions|acquisition)|"
    r"\btalent$|"
    r"robert\s+half|aerotek|kelly\s+services|adecco|"
    r"randstad|manpower|teksystems|insight\s+global|"
    r"executive\s+search|"
    r"search\s+(?:group|partners|partner|masters|consultants|associates|advisors|firm)\b)",
    re.IGNORECASE,
)
_RECRUITER_SUFFIX_RE = re.compile(
    r"\bsearch\s+(?:inc|llc|ltd|co)\.?\s*$|"
    r"\bsearch\s*$",
    re.IGNORECASE,
)

# Branded / property-level hotels. A hotel's "Director of Finance" reports
# to a management company or REIT, not a fractional-CFO buyer.
_HOTEL_NAME_RE = re.compile(
    r"\b(hotel|hyatt|kimpton|marriott|hilton|sheraton|westin|fairmont|"
    r"ritz[-\s]?carlton|four\s+seasons|auberge|intercontinental|"
    r"hospitality|resort|lodge|\binn\b|suites)\b",
    re.IGNORECASE,
)

# Government / public-sector entities — legally not fractional-CFO buyers.
# Careful entity matching so private nonprofits that merely reference a
# place survive ("Sickle Cell Foundation of Palm Beach County").
_PUBLIC_SECTOR_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:city|town|village|borough|township|county|state|commonwealth)\s+of\b", re.IGNORECASE),
    re.compile(r"\btownship\b", re.IGNORECASE),
    re.compile(r"\bcounty\s*$", re.IGNORECASE),
    re.compile(r"\bcounty\s+(?:schools?|school\s+district|government|treasurer)\b", re.IGNORECASE),
    re.compile(r"\b(?:public|community|city|unified|independent)\s+school(?:s|\s+district)?\b", re.IGNORECASE),
    re.compile(r"\bschool\s+district\b|\bisd\b", re.IGNORECASE),
    re.compile(r"\bpolice\s+department\b|\bsheriff(?:'s)?\s+(?:office|department)\b", re.IGNORECASE),
    re.compile(r"\b(?:rapid\s+transit|transit\s+authority|transit\s+district)\b", re.IGNORECASE),
    re.compile(r"\bmunicipal(?:ity)?\b", re.IGNORECASE),
)
# Private-nonprofit indicators that legitimately reference a locality —
# these are real orgs, so they override the public-sector patterns above.
_NONPROFIT_INDICATOR_RE = re.compile(
    r"\b(foundation|association|coalition|alliance|charit(?:y|ies)|"
    r"non-?profit|ministries|ministry|church|synagogue|diocese|temple|"
    r"chamber\s+of\s+commerce|society|institute|council|united\s+way|"
    r"habitat\s+for\s+humanity|goodwill|ymca|ywca)\b",
    re.IGNORECASE,
)

# Auto dealership exclusion. Brand at any position OR dealer-specific suffix.
_AUTO_BRAND_RE = re.compile(
    r"\b(honda|toyota|ford|chevrolet|chevy|bmw|mercedes(?:[-\s]benz)?|"
    r"nissan|hyundai|subaru|kia|volkswagen|vw|audi|lexus|infiniti|"
    r"acura|cadillac|jeep|ram|dodge|chrysler|mazda|porsche|jaguar|"
    r"land\s+rover|range\s+rover|mini|fiat|gmc|buick|lincoln|volvo)\b",
    re.IGNORECASE,
)
_AUTO_SUFFIX_RE = re.compile(
    r"\b(auto\s+(?:mall|group|center|nation|park|haus|world|plaza)|"
    r"automotive\s+group|"
    r"dealership|car\s+(?:store|center)|"
    r"motors|motor\s+(?:co|company|cars)|"
    r"carwarriors)\b",
    re.IGNORECASE,
)

# A company field that is nothing but a single generic corporate word
# ("Enterprises", "Solutions", "Group", ...), optionally with a legal form
# ("Enterprises LLC"), is a truncated / junk posting, not a targetable
# company. Multi-token names ("Acme Enterprises") are fine.
_GENERIC_STUB_TERMS = frozenset({
    "enterprise", "enterprises", "solutions", "holding", "holdings", "group",
    "services", "company", "corporation", "industries", "ventures",
    "partners", "associates", "consulting", "technologies", "systems",
    "international", "global", "management", "capital",
})
_LEGAL_FORM_TOKENS = frozenset({
    "llc", "inc", "corp", "co", "ltd", "lp", "llp", "plc", "pllc", "pc",
})


# --- Name exclusion predicates ---------------------------------------------

def _is_recruiter_name(name: str) -> bool:
    return bool(
        _RECRUITER_NAME_PATTERN.search(name)
        or _RECRUITER_SUFFIX_RE.search(name)
    )


def _is_auto_dealer_name(name: str) -> bool:
    return bool(_AUTO_BRAND_RE.search(name) or _AUTO_SUFFIX_RE.search(name))


def _is_hotel_name(name: str) -> bool:
    return bool(_HOTEL_NAME_RE.search(name))


def _is_generic_stub_name(name: str) -> bool:
    """True when the company name is a single generic corporate word (e.g. a
    lone "Enterprises"), ignoring any trailing legal form ("Enterprises LLC")
    — a truncated/junk value, not a real company. Any multi-token name with a
    real second word is never a stub."""
    tokens = [
        t for t in re.findall(r"[a-z0-9]+", name.lower())
        if t not in _LEGAL_FORM_TOKENS
    ]
    return len(tokens) == 1 and tokens[0] in _GENERIC_STUB_TERMS


def _is_public_sector(name: str, domain: str | None = None) -> bool:
    """Government / public-sector entity — not a fractional-CFO buyer.
    Private nonprofits that merely name a locality are exempted."""
    if _NONPROFIT_INDICATOR_RE.search(name):
        return False
    if any(p.search(name) for p in _PUBLIC_SECTOR_RES):
        return True
    if domain:
        d = domain.lower()
        if ".k12." in d or d.endswith(".k12.us"):
            return True
    return False


def is_untargetable_name(name: str, domain: str | None = None) -> bool:
    """One gate for 'a company we'd never target': recruiters, auto dealers,
    hotels, public-sector/education, and lone generic-stub names."""
    return (
        _is_recruiter_name(name)
        or _is_auto_dealer_name(name)
        or _is_hotel_name(name)
        or _is_public_sector(name, domain)
        or _is_generic_stub_name(name)
    )

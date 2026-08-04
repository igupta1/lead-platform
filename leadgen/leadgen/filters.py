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


# A missing company field that got stringified on the way in. pandas turns an
# empty cell into the float NaN, and ``str(nan)`` is the literal "nan" — which
# is how a lead named `nan` reached a customer-facing email. Matched on the
# WHOLE name only, so a real company that merely starts with one of these
# (e.g. "NAN, Inc.", a Hawaii contractor) is untouched.
_NULL_PLACEHOLDER_NAMES = frozenset({
    "nan", "none", "null", "n/a", "na", "nil", "unknown", "undefined",
    "-", "--", "?",
})


def _is_null_placeholder_name(name: str) -> bool:
    """True when the company name is just a stringified null."""
    return name.strip().lower() in _NULL_PLACEHOLDER_NAMES


# A job board's employer field that describes the employer instead of naming it
# ("A great organization!", "Confidential Startup SaaS Company"), or a test
# record that escaped the poster's sandbox ("Smart Apply Test Company" — which
# reached rank 1 of the published cloud inventory). `_is_generic_stub_name`
# cannot catch these: they are multi-token and every token is ordinary.
_PLACEHOLDER_PHRASE_RE = re.compile(
    r"\b(?:"
    r"confidential|undisclosed|unnamed|anonymous|stealth\s*mode"
    r"|(?:test|demo|sample|dummy|example|placeholder|fake)\s+(?:company|account|org\w*|employer)"
    r"|our\s+client|a\s+client\b|client\s+of\s+ours"
    r"|company\s+name|your\s+company|employer\s+name"
    r")\b",
    re.IGNORECASE,
)


def _is_placeholder_phrase_name(name: str) -> bool:
    """True when the 'name' is a description or a test record rather than a
    company. Also catches sentence punctuation: a real company name does not
    end in an exclamation or question mark, and that alone is what unmasks
    "A great organization!"."""
    if _PLACEHOLDER_PHRASE_RE.search(name):
        return True
    return bool(re.search(r"[!?]", name))


# An applicant-tracking system's employer field with its own label welded onto
# the end ("Salamander Palm Beach Employer") — the board's word, not part of
# the company's name. `_is_placeholder_phrase_name` cannot catch these: the
# trailing word only reads as an artifact because it is LAST, and the same word
# mid-name is ordinary ("Employer Solutions Group" is a real company).
_ATS_TRAILING_TERMS = frozenset({
    "employer", "careers", "career", "hiring", "jobs", "job", "opportunities",
})


_ATS_TRAILING_RE = re.compile(
    r"\s+(?:" + "|".join(sorted(_ATS_TRAILING_TERMS)) + r")\s*$", re.IGNORECASE
)


def strip_ats_artifact(name: str) -> str:
    """Drop a job board's own trailing label from a company name
    ("Canopy Careers" -> "Canopy"). A REPAIR, not a rejection: the remainder is
    the real company, so stripping keeps a good lead that dropping would lose.

    Conservative on both ends — only the final token is considered (the same
    word mid-name is ordinary, as in "Employer Solutions Group"), and a name
    that is ONLY the label is left alone for the stub gate to reject."""
    stripped = _ATS_TRAILING_RE.sub("", name).strip()
    return stripped or name


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
    hotels, public-sector/education, lone generic-stub names, stringified
    nulls, and descriptions/test records posing as a name.

    Call `strip_ats_artifact` on the name FIRST: a job-board label welded onto
    the end is repaired, not rejected, and the gates below then judge the real
    name underneath."""
    return (
        _is_recruiter_name(name)
        or _is_auto_dealer_name(name)
        or _is_hotel_name(name)
        or _is_public_sector(name, domain)
        or _is_generic_stub_name(name)
        or _is_null_placeholder_name(name)
        or _is_placeholder_phrase_name(name)
    )

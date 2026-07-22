"""Unified job-board source for the whole leadgen platform.

One fetch, one classifier, every job niche. Each posting title is routed
into EXACTLY ONE :class:`SignalType` bucket (or dropped):

* ``JOB_FRACTIONAL_CFO`` — fractional / interim / part-time / outsourced /
  virtual CFO (and fractional finance-leadership). The hottest, explicit-
  intent class: the company is literally shopping for the service sold.
* ``JOB_FINANCE_LEAD`` — finance leadership one rung below CFO (Controller,
  VP / Head / Director of Finance, Accounting / Finance Manager, FP&A lead,
  Senior Accountant, Chief Accounting Officer).
* ``JOB_JUNIOR_FINANCE`` — junior IC finance (bookkeeper, staff / junior
  accountant, AP / AR clerk, payroll / billing specialist).
* ``JOB_IT_SUPPORT`` — help desk / IT support / desktop / sysadmin.
* ``JOB_IT_LEADERSHIP`` — IT Director / Manager, Head of IT / Technology.
* ``JOB_SECURITY`` — security engineer, infosec, SOC analyst, CISO.
* ``JOB_CLOUD_DEVOPS`` — DevOps, SRE, cloud / platform engineering.

A second, negative output: ``Disqualifier`` rows for *full-time* Chief
Financial Officer postings. Those companies are buying a CFO, not a
fractional one, so per spec they're dropped from every niche. The
disqualifier is sticky: a CFO posting on day 1 still blocks a later Form D.

Where headcount is available from the posting (Indeed's
``company_num_employees``, surfaced by JobSpy) it's carried on the
candidate so the SMB cap can short-circuit enrichment. City / state are
parsed from the posting location onto the candidate.

Backends: JobSpy (Indeed + ZipRecruiter + Google Jobs at volume; LinkedIn +
Glassdoor scraped gently) + Adzuna API (paginated). No HN — the Who's-Hiring
threads index the wrong demographic for these buyers.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import jobspy
import requests

from leadgen.models import (
    Disqualifier,
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)

_log = logging.getLogger(__name__)

# --- Search queries --------------------------------------------------------
#
# Merged across every niche. The classifier — not the query — decides a
# posting's bucket, so the query lists exist only to feed the scrape; the
# per-group age window (fractional lives longer) is the one thing that
# still travels with the query group.

# Finance leadership one rung below CFO. Primary buy signal. "Chief
# Financial Officer" is queried separately (see _CFO_QUERIES) so full-time
# CFO postings become disqualifiers, not signals.
_FINANCE_LEAD_QUERIES: tuple[str, ...] = (
    "Controller",
    "Assistant Controller",
    "VP Finance",
    "Head of Finance",
    "Director of Finance",
    "Accounting Manager",
    "Finance Manager",
    "FP&A Manager",
    "Senior Accountant",
    # "Corporate" / "Divisional" Controller aren't queried separately — the
    # plain "Controller" search already returns them and _CONTROLLER_RE
    # classifies them, so a dedicated query would just burn scrape budget.
    "Chief Accounting Officer",
)
_CFO_QUERIES: tuple[str, ...] = (
    "Chief Financial Officer",
)
# In-market queries. A company posting a Fractional / Interim / Part-time
# CFO role is shopping for exactly the service sold — the hottest class.
# The fractional universe is small, so cast the widest net of phrasings.
_FRACTIONAL_CFO_QUERIES: tuple[str, ...] = (
    "Fractional CFO",
    "Interim CFO",
    "Part-time CFO",
    "Outsourced CFO",
    "Contract CFO",
    "Virtual CFO",
    "Fractional Chief Financial Officer",
    "Interim Chief Financial Officer",
    "Part-Time Chief Financial Officer",
    "Fractional Controller",
    "Interim Controller",
    "CFO Consultant",
)
# Junior IC finance. The roles a small business hires *before* it commits
# to an in-house finance department — the outsourced-accounting window.
_JUNIOR_FINANCE_QUERIES: tuple[str, ...] = (
    "Bookkeeper",
    "Staff Accountant",
    "Junior Accountant",
    "Accounting Clerk",
    "Accounts Payable Specialist",
    "Accounts Receivable Specialist",
    "Accounts Payable Clerk",
    "Payroll Specialist",
    "Billing Specialist",
    "Accounting Assistant",
)
# IT / security / cloud. One list feeds all four tech buckets.
_IT_QUERIES: tuple[str, ...] = (
    "help desk",
    "IT support",
    "system administrator",
    "CISO",
    "security engineer",
    "DevOps engineer",
    "cloud engineer",
    "VP of IT",
    "Director of IT",
)

# --- Finance classifier regexes --------------------------------------------
# Title text comes back messy ("Senior Controller, Manufacturing — Greater
# Boston (Remote)") so we use word-boundary regexes, not exact matches.
_CONTROLLER_RE = re.compile(r"\b(controller|comptroller)\b", re.IGNORECASE)
_VP_FINANCE_RE = re.compile(
    r"\b(?:vp|vice\s+president|head|director)\s+of\s+finance\b",
    re.IGNORECASE,
)
_VP_FINANCE_ALT_RE = re.compile(
    r"\b(?:vp|vice\s+president)\s+finance\b",
    re.IGNORECASE,
)
_FINANCE_DIRECTOR_RE = re.compile(
    r"\bfinance\s+director\b",
    re.IGNORECASE,
)
_FINANCE_MANAGER_RE = re.compile(
    r"\b(?:accounting|finance)\s+manager\b",
    re.IGNORECASE,
)
_HEAD_OF_ACCOUNTING_RE = re.compile(
    r"\b(?:head|director)\s+of\s+accounting\b",
    re.IGNORECASE,
)
# FP&A leadership: matches "FP&A Manager", "Sr FP&A Director", "VP FP&A",
# etc. Excludes "FP&A Analyst" (too junior — not the buying signal).
_FPA_LEAD_RE = re.compile(
    r"\bfp\s*&?\s*a\b.*?\b(?:manager|director|head|lead|leader|vp|vice\s+president)\b"
    r"|\b(?:manager|director|head|lead|leader|vp|vice\s+president)\b.*?\bfp\s*&?\s*a\b",
    re.IGNORECASE,
)
# Senior Accountant: weaker than Controller but still a finance-org gap.
# Excludes "Senior Tax Accountant" / "Senior Audit Accountant" — those are
# specialized IC roles that don't signal a finance-leadership gap.
_SENIOR_ACCOUNTANT_RE = re.compile(
    r"\b(?:senior|sr\.?)\s+(?:staff\s+)?accountant\b",
    re.IGNORECASE,
)
_SENIOR_ACCOUNTANT_EXCLUDE_RE = re.compile(
    r"\b(?:tax|audit|cost|payroll|forensic|fixed[\s-]asset)\s+accountant\b",
    re.IGNORECASE,
)
# Chief Accounting Officer: a company hiring a CAO with no CFO is a strong
# fractional-CFO target. Distinct from the CFO disqualifier.
_CAO_RE = re.compile(
    r"\bchief\s+accounting\s+officer\b|\bcao\b",
    re.IGNORECASE,
)
# Treasurer counts ONLY when bundled with a corporate finance-leadership
# word. Standalone "Treasurer" is overwhelmingly a government / school /
# volunteer-board role — not a buyer — so it is not a signal on its own.
_BUNDLED_TREASURER_RE = re.compile(
    r"\btreasurer\b.*\b(controller|finance|financial|accounting)\b"
    r"|\b(controller|finance|financial|accounting)\b.*\btreasurer\b",
    re.IGNORECASE,
)

# Core finance-leadership keywords. Used to rescue a clerical-looking title
# that nonetheless names a real leadership role ("Assistant Controller").
_FINANCE_CORE_RE = re.compile(
    r"\b(controller|comptroller|"
    r"chief\s+financial(?:\s+officer)?|chief\s+accounting(?:\s+officer)?|cfo|cao|"
    r"vp\s+(?:of\s+)?finance|vice\s+president[,\s].*finance|"
    r"head\s+of\s+finance|director\s+of\s+finance|finance\s+director|"
    r"head\s+of\s+accounting|director\s+of\s+accounting)\b",
    re.IGNORECASE,
)

# Clerical / junior IC titles that are NOT finance leadership. A title
# matching this and lacking a _FINANCE_CORE_RE keyword is rejected from the
# finance-lead tier ("Accounting Clerk", "Financial Services Technician"),
# while "Assistant Controller" survives.
_CLERICAL_EXCLUDE_RE = re.compile(
    r"\b(clerk|technician|administrative\s+assistant|admin\s+assistant|"
    r"executive\s+assistant|office\s+assistant|office\s+administrator|"
    r"support\s+associate|representative\s+payee|receptionist|"
    r"data\s+manager|front\s+office|secretary|intern|apprentice|"
    r"reservationist)\b",
    re.IGNORECASE,
)

# Finance-LEADERSHIP titles (a rung the fractional service can fill). Used
# to promote a part-time / interim / fractional posting of one of these to
# the in-market tier. Deliberately excludes IC-level finance titles.
_FINANCE_LEADERSHIP_RES: tuple[re.Pattern[str], ...] = (
    _CONTROLLER_RE,
    _VP_FINANCE_RE,
    _VP_FINANCE_ALT_RE,
    _FINANCE_DIRECTOR_RE,
    _HEAD_OF_ACCOUNTING_RE,
    _FPA_LEAD_RE,
    _CAO_RE,
)

# --- Junior IC finance classifier regexes ----------------------------------
_BOOKKEEPER_RE = re.compile(r"\bbook\s?keep(?:er|ing)?\b", re.IGNORECASE)
_STAFF_ACCOUNTANT_RE = re.compile(
    r"\b(?:staff|junior|jr\.?|entry[\s-]level|associate)\s+accountant\b", re.IGNORECASE
)
_PLAIN_ACCOUNTANT_RE = re.compile(r"\baccountant\b", re.IGNORECASE)
_AP_AR_RE = re.compile(
    r"\b(?:accounts?\s+payable|accounts?\s+receivable|a/?p|a/?r)\s+"
    r"(?:specialist|clerk|coordinator|associate|analyst|administrator)\b",
    re.IGNORECASE,
)
_PAYROLL_RE = re.compile(
    r"\bpayroll\s+(?:specialist|clerk|coordinator|administrator|associate)\b", re.IGNORECASE
)
_BILLING_RE = re.compile(
    r"\bbilling\s+(?:specialist|clerk|coordinator|associate)\b", re.IGNORECASE
)
_ACCOUNTING_ASSISTANT_RE = re.compile(r"\baccounting\s+assistant\b", re.IGNORECASE)
_ACCOUNTING_CLERK_RE = re.compile(r"\baccounting\s+clerk\b", re.IGNORECASE)
# A plain "Accountant" is junior only when not narrowed to a specialist /
# senior track the finance-lead tier already owns.
_ACCOUNTANT_EXCLUDE_RE = re.compile(
    r"\b(?:senior|sr\.?|lead|principal|chief|tax|audit|cost|forensic|"
    r"fixed[\s-]asset|fund|corporate)\b",
    re.IGNORECASE,
)
_JUNIOR_RES: tuple[re.Pattern[str], ...] = (
    _BOOKKEEPER_RE,
    _STAFF_ACCOUNTANT_RE,
    _AP_AR_RE,
    _PAYROLL_RE,
    _BILLING_RE,
    _ACCOUNTING_ASSISTANT_RE,
    _ACCOUNTING_CLERK_RE,
)

# --- IT / security / cloud classifier regexes ------------------------------
# Ported from the MSP pipeline. The exec_hired classifier it also carried is
# intentionally NOT ported — a CISO now routes to JOB_SECURITY, an IT
# Director / VP of IT to JOB_IT_LEADERSHIP.
_SECURITY_PATTERN = re.compile(
    r"\b(security|infosec|soc analyst|ciso)\b",
    re.IGNORECASE,
)
_CLOUD_PATTERN = re.compile(
    r"\b(devops|site reliability|sre|cloud engineer|aws|azure|gcp|kubernetes)\b",
    re.IGNORECASE,
)
_IT_LEADERSHIP_PATTERN = re.compile(
    r"\b(director of it|it manager|it director|head of it|head of technology|"
    r"vp of it|vp it|vice president of it)\b",
    re.IGNORECASE,
)
_IT_SUPPORT_PATTERN = re.compile(
    r"\b(help desk|helpdesk|it support|desktop support|system admin|sysadmin|network admin)\b",
    re.IGNORECASE,
)

# --- CFO disqualifier regexes ----------------------------------------------
# Detects "Chief Financial Officer" / stand-alone "CFO" — but excludes
# part-time variants: a company hiring a Fractional / Interim / Part-time CFO
# is the opposite of disqualified.
_CFO_TITLE_RE = re.compile(
    r"\b(chief\s+financial\s+officer|cfo)\b",
    re.IGNORECASE,
)
_PART_TIME_QUALIFIER_RE = re.compile(
    r"\b(fractional|interim|part[\s-]?time|outsourced|virtual|contract|temp|temporary|consultant|consulting|advisory)\b",
    re.IGNORECASE,
)

# --- Company-name / title exclusions ---------------------------------------
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
_AUTOMOTIVE_TITLE_RE = re.compile(r"^\s*automotive\b", re.IGNORECASE)

# --- Posting-age caps ------------------------------------------------------
# JobSpy filters by hours_old at query time but Adzuna returns older results;
# enforce a consistent cap at the candidate level. Finance-lead / junior / IT
# use 30 days; the scarce, long-lived fractional-CFO postings use 60.
_MAX_POSTING_AGE_DAYS = 30
_FRACTIONAL_MAX_POSTING_AGE_DAYS = 60

# Scrape plans per query: Indeed + Google take the big ask; LinkedIn is scraped
# gently to avoid anti-bot blocks. ZipRecruiter (HTTP 403 Cloudflare block on
# every request) and Glassdoor (HTTP 400 "location not parsed" — it rejects the
# nationwide "United States" location) fail closed on every call in both CI and
# local runs, returning zero leads while adding latency + error-log noise, so
# they're dropped. Re-add if the upstream blocks lift.
_JOBSPY_PLANS: tuple[tuple[tuple[str, ...], int], ...] = (
    (("indeed", "google"), 100),
    (("linkedin",), 25),
)

# Indeed's company_employees_label / JobSpy's company_num_employees returns
# strings like "1 to 10", "201 to 500", "10,001+". Parse to an integer upper
# bound; None when unparseable.
_HEADCOUNT_BAND_RE = re.compile(
    r"^\s*(\d[\d,]*)\s*(?:to|-|–)\s*(\d[\d,]*)\s*\+?\s*$",
    re.IGNORECASE,
)
_HEADCOUNT_PLUS_RE = re.compile(
    r"^\s*(\d[\d,]*)\s*\+\s*$",
)

# Adzuna paging. Free keys have unpublished caps (~25 calls/min); space calls
# out and stop everything on a 429.
_ADZUNA_API_BASE = "https://api.adzuna.com/v1/api/jobs/us/search"  # + /{page}
_ADZUNA_MAX_PAGES = 5
_ADZUNA_RESULTS_PER_PAGE = 50  # Adzuna's hard max.
_ADZUNA_CALL_SPACING_S = 2.5

# US state abbreviations, for parsing "City, ST" posting locations.
_US_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}


# --- Name / title exclusion predicates -------------------------------------

def _is_recruiter_name(name: str) -> bool:
    return bool(
        _RECRUITER_NAME_PATTERN.search(name)
        or _RECRUITER_SUFFIX_RE.search(name)
    )


def _is_auto_dealer_name(name: str) -> bool:
    return bool(_AUTO_BRAND_RE.search(name) or _AUTO_SUFFIX_RE.search(name))


def _is_hotel_name(name: str) -> bool:
    return bool(_HOTEL_NAME_RE.search(name))


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


def _is_automotive_title(title: str) -> bool:
    return bool(_AUTOMOTIVE_TITLE_RE.search(title))


# --- Date / age helpers ----------------------------------------------------

def _parse_posted_date(value: Any) -> datetime | None:
    """Best-effort parse of JobSpy / Adzuna's date_posted field.
    Returns None when unparseable."""
    if not value:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none"):
        return None
    # JobSpy returns YYYY-MM-DD. Adzuna returns ISO-8601.
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s[: len(fmt) + 2], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def _is_too_old(
    date_posted: str | None,
    now: datetime,
    max_days: int = _MAX_POSTING_AGE_DAYS,
) -> bool:
    """Drop postings older than ``max_days`` at candidate construction time.
    Fractional-CFO queries pass the wider 60-day window."""
    parsed = _parse_posted_date(date_posted)
    if parsed is None:
        return False  # unknown age — keep, let downstream score decay it
    return (now - parsed).days > max_days


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- Finance classifier predicates -----------------------------------------

def _is_finance_lead_title(title: str) -> bool:
    """True when the posting is for a finance lead one rung below CFO. Always
    called AFTER ``_is_cfo_disqualifier_title`` returns False, so a full-time
    CFO posting isn't double-classified as both signal and disqualifier."""
    if not title:
        return False
    # Clerical / junior IC exclusion, UNLESS the title also names a genuine
    # finance-leadership role ("Assistant Controller" survives).
    if _CLERICAL_EXCLUDE_RE.search(title) and not _FINANCE_CORE_RE.search(title):
        return False
    # Senior Accountant: include unless narrowed to a specialist IC track.
    if _SENIOR_ACCOUNTANT_RE.search(title) and not _SENIOR_ACCOUNTANT_EXCLUDE_RE.search(title):
        return True
    if _BUNDLED_TREASURER_RE.search(title):
        return True
    return bool(
        _CONTROLLER_RE.search(title)
        or _VP_FINANCE_RE.search(title)
        or _VP_FINANCE_ALT_RE.search(title)
        or _FINANCE_DIRECTOR_RE.search(title)
        or _FINANCE_MANAGER_RE.search(title)
        or _HEAD_OF_ACCOUNTING_RE.search(title)
        or _FPA_LEAD_RE.search(title)
        or _CAO_RE.search(title)
    )


def _is_cfo_disqualifier_title(title: str) -> bool:
    """True when the title is for a full-time CFO. False for fractional /
    interim / part-time variants — those companies are the buyer, not
    disqualified."""
    if not title:
        return False
    if not _CFO_TITLE_RE.search(title):
        return False
    if _PART_TIME_QUALIFIER_RE.search(title):
        return False
    return True


def _is_fractional_cfo_title(title: str) -> bool:
    """True when the company is in-market for fractional finance leadership:
    a CFO title or a finance-LEADERSHIP title, either carrying a part-time /
    interim / fractional / outsourced / contract qualifier. IC-level finance
    titles are NOT promoted here.

    Checked after ``_is_cfo_disqualifier_title`` (which excludes full-time
    CFOs) and before ``_is_finance_lead_title``."""
    if not title:
        return False
    if not _PART_TIME_QUALIFIER_RE.search(title):
        return False
    if _CFO_TITLE_RE.search(title):
        return True
    return any(r.search(title) for r in _FINANCE_LEADERSHIP_RES)


def _is_junior_finance_title(title: str) -> bool:
    """True for a junior IC finance title (bookkeeper, staff / junior
    accountant, AP / AR clerk, payroll / billing specialist). Reached only
    after the CFO / fractional / finance-lead tiers have been ruled out, so
    the tiers never overlap."""
    if not title:
        return False
    if any(r.search(title) for r in _JUNIOR_RES):
        return True
    # Plain "Accountant" (no seniority / specialist qualifier) is junior IC.
    if _PLAIN_ACCOUNTANT_RE.search(title) and not _ACCOUNTANT_EXCLUDE_RE.search(title):
        return True
    return False


def classify(title: str) -> SignalType | None:
    """Route a posting title into exactly one of the seven signal buckets,
    or None to drop it.

    Finance tiers take precedence over the IT / security / cloud buckets, and
    are ordered most-specific first (fractional CFO -> finance lead -> junior
    IC). Full-time CFO postings are diverted to the disqualifier gate by the
    caller before this is reached, so they never surface here as a signal."""
    if not title:
        return None
    # Finance ladder.
    if _is_fractional_cfo_title(title):
        return SignalType.JOB_FRACTIONAL_CFO
    if _is_finance_lead_title(title):
        return SignalType.JOB_FINANCE_LEAD
    if _is_junior_finance_title(title):
        return SignalType.JOB_JUNIOR_FINANCE
    # IT / security / cloud (exec_hired intentionally not ported).
    if _SECURITY_PATTERN.search(title):
        return SignalType.JOB_SECURITY
    if _CLOUD_PATTERN.search(title):
        return SignalType.JOB_CLOUD_DEVOPS
    if _IT_LEADERSHIP_PATTERN.search(title):
        return SignalType.JOB_IT_LEADERSHIP
    if _IT_SUPPORT_PATTERN.search(title):
        return SignalType.JOB_IT_SUPPORT
    return None


# --- Location / headcount parsing ------------------------------------------

def _split_location(location: str) -> tuple[str | None, str | None]:
    """Best-effort 'City, ST' -> (city, state). Returns (None, None) for
    remote or unparseable strings."""
    loc = (location or "").strip()
    if not loc or loc.lower().startswith("remote"):
        return None, None
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    if not parts:
        return None, None
    city = parts[0]
    state = None
    if len(parts) >= 2:
        tok = parts[1].split()[0].upper() if parts[1].split() else ""
        if tok in _US_STATE_ABBR:
            state = tok
    return (city or None), state


def _parse_headcount_label(label: str | None) -> int | None:
    """Parse Indeed's company size band into an integer upper bound. Returns
    None when unknown / unparseable. Upper bound (not midpoint) keeps the SMB
    cap conservative — "11 to 50" returns 50, the cap line in the spec."""
    if not label:
        return None
    s = str(label).strip()
    if not s or s.lower() in ("unknown", "n/a", "none"):
        return None

    m = _HEADCOUNT_BAND_RE.match(s)
    if m:
        try:
            return int(m.group(2).replace(",", ""))
        except ValueError:
            return None

    m = _HEADCOUNT_PLUS_RE.match(s)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            return None

    # Plain integer string.
    try:
        return int(s.replace(",", ""))
    except ValueError:
        return None


def _read_jobspy_headcount(row: Any) -> int | None:
    """JobSpy versions vary on the field name; check the known aliases."""
    for field in ("company_num_employees", "company_employees_label", "company_size"):
        val = row.get(field) if hasattr(row, "get") else None
        if val is not None and str(val) and str(val).lower() != "nan":
            parsed = _parse_headcount_label(str(val))
            if parsed is not None:
                return parsed
    return None


# --- Candidate / disqualifier emission -------------------------------------

def _make_candidate(
    *,
    company: str,
    title: str,
    url: str,
    date_posted: str,
    site: str,
    description: str,
    location: str,
    city: str | None,
    state: str | None,
    headcount: int | None,
    captured_at: datetime,
    signal_type: SignalType,
) -> LeadCandidate:
    return LeadCandidate(
        name=company,
        domain=None,
        headcount=headcount,
        city=city,
        state=state,
        initial_signal=Signal(
            type=signal_type,
            source=SourceName.JOBS,
            captured_at=captured_at,
            event_date=_parse_posted_date(date_posted),
            evidence_text=title,   # verbatim posting title (required non-empty)
            source_url=url,        # posting URL (required non-empty)
            payload={
                "title": title,
                "url": url,
                "date_posted": date_posted,
                "site": site,
                "description": description,
                "location": location,
            },
        ),
    )


def _make_cfo_disqualifier(
    *, company: str, title: str, site: str, url: str
) -> Disqualifier:
    return Disqualifier(
        name=company,
        reason="open_full_time_cfo_posting",
        source=SourceName.JOBS,
        payload={"title": title, "site": site, "url": url},
    )


# --- Backends --------------------------------------------------------------

def _fetch_from_jobspy(
    since: datetime,
    *,
    queries: tuple[str, ...],
    max_age_days: int = _MAX_POSTING_AGE_DAYS,
) -> tuple[list[LeadCandidate], list[Disqualifier]]:
    captured_at = _utcnow()
    hours_old = max(
        1, min(int((captured_at - since).total_seconds() / 3600), max_age_days * 24)
    )
    # Google Jobs takes a natural-language recency phrase instead of
    # hours_old; match the window to max_age_days.
    google_recency = "in the last 2 months" if max_age_days > 30 else "in the last month"
    candidates: list[LeadCandidate] = []
    disqualifiers: list[Disqualifier] = []
    frames: list[Any] = []
    for query in queries:
        for sites, wanted in _JOBSPY_PLANS:
            try:
                df = jobspy.scrape_jobs(
                    site_name=list(sites),
                    search_term=query,
                    # Google Jobs ignores search_term / hours_old — it takes
                    # its own natural-language query with a recency phrase.
                    google_search_term=(
                        f"{query} jobs in United States {google_recency}"
                    ),
                    location="United States",
                    results_wanted=wanted,
                    hours_old=hours_old,
                    country_indeed="usa",
                )
            except Exception:
                _log.exception("jobspy query failed: %s (sites=%s)", query, sites)
                continue
            if df is not None and len(df) > 0:
                frames.append(df)
    for df in frames:
        for _, row in df.iterrows():
            title = str(row.get("title") or "").strip()
            company = str(row.get("company") or "").strip()
            if not title or not company:
                continue
            if (
                _is_recruiter_name(company)
                or _is_auto_dealer_name(company)
                or _is_hotel_name(company)
                or _is_public_sector(company)
                or _is_generic_stub_name(company)
            ):
                continue
            if _is_automotive_title(title):
                continue

            url = str(row.get("job_url") or "").strip()
            date_posted = str(row.get("date_posted") or "")
            site = str(row.get("site") or "")

            # Disqualifier gate first (sticky, and age-independent).
            if _is_cfo_disqualifier_title(title):
                disqualifiers.append(
                    _make_cfo_disqualifier(
                        company=company, title=title, site=site, url=url,
                    )
                )
                continue
            if _is_too_old(date_posted, captured_at, max_age_days):
                continue
            sig_type = classify(title)
            if sig_type is None:
                continue
            if not url:
                continue  # Signal.source_url is required non-empty — drop it.

            location = str(row.get("location") or "")
            city, state = _split_location(location)
            candidates.append(
                _make_candidate(
                    company=company,
                    title=title,
                    url=url,
                    date_posted=date_posted,
                    site=site,
                    description=str(row.get("description") or ""),
                    location=location,
                    city=city,
                    state=state,
                    headcount=_read_jobspy_headcount(row),
                    captured_at=captured_at,
                    signal_type=sig_type,
                )
            )
    return candidates, disqualifiers


def _fetch_from_adzuna(
    since: datetime,
    *,
    queries: tuple[str, ...],
    max_age_days: int = _MAX_POSTING_AGE_DAYS,
) -> tuple[list[LeadCandidate], list[Disqualifier]]:
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        _log.warning("ADZUNA_APP_ID/ADZUNA_APP_KEY not set; skipping Adzuna")
        return [], []

    captured_at = _utcnow()
    max_days_old = max(1, min((captured_at - since).days, max_age_days))
    candidates: list[LeadCandidate] = []
    disqualifiers: list[Disqualifier] = []
    rate_limited = False
    first_call = True
    for query in queries:
        if rate_limited:
            break
        for page in range(1, _ADZUNA_MAX_PAGES + 1):
            if not first_call:
                time.sleep(_ADZUNA_CALL_SPACING_S)  # stay under the per-minute cap
            first_call = False
            try:
                adzuna_params: dict[str, str | int] = {
                    "app_id": app_id,
                    "app_key": app_key,
                    "what": query,
                    "max_days_old": max_days_old,
                    "results_per_page": _ADZUNA_RESULTS_PER_PAGE,
                }
                response = requests.get(
                    f"{_ADZUNA_API_BASE}/{page}",
                    params=adzuna_params,
                    timeout=15,
                )
                if response.status_code == 429:
                    _log.warning(
                        "adzuna rate-limited (429) on %r page %d; "
                        "stopping all Adzuna fetches this run",
                        query, page,
                    )
                    rate_limited = True
                    break
                response.raise_for_status()
                data = response.json()
            except Exception:
                _log.exception("adzuna query failed: %s (page %d)", query, page)
                break
            results = data.get("results", [])
            for item in results:
                title = str(item.get("title") or "").strip()
                company = str((item.get("company") or {}).get("display_name") or "").strip()
                if not title or not company:
                    continue
                if (
                    _is_recruiter_name(company)
                    or _is_auto_dealer_name(company)
                    or _is_generic_stub_name(company)
                ):
                    continue
                if _is_automotive_title(title):
                    continue

                url = str(item.get("redirect_url") or "").strip()
                date_posted = str(item.get("created") or "")

                if _is_cfo_disqualifier_title(title):
                    disqualifiers.append(
                        _make_cfo_disqualifier(
                            company=company, title=title, site="adzuna", url=url,
                        )
                    )
                    continue
                if _is_too_old(date_posted, captured_at, max_age_days):
                    continue
                sig_type = classify(title)
                if sig_type is None:
                    continue
                if not url:
                    continue  # Signal.source_url is required non-empty.

                location = str((item.get("location") or {}).get("display_name") or "")
                city, state = _split_location(location)
                candidates.append(
                    _make_candidate(
                        company=company,
                        title=title,
                        url=url,
                        date_posted=date_posted,
                        site="adzuna",
                        description=str(item.get("description") or ""),
                        location=location,
                        city=city,
                        state=state,
                        headcount=None,  # Adzuna doesn't expose a size field.
                        captured_at=captured_at,
                        signal_type=sig_type,
                    )
                )
            if len(results) < _ADZUNA_RESULTS_PER_PAGE:
                break  # last page for this query
    return candidates, disqualifiers


def fetch(
    *, since: datetime, limit: int | None = None
) -> tuple[list[LeadCandidate], list[Disqualifier]]:
    """Returns (job-posting candidates across all seven buckets, CFO
    disqualifiers).

    Two-return signature: the jobs source is the only one that produces
    disqualifiers (an open full-time CFO posting). The daily_run runner
    branches on the return shape.
    """
    candidates: list[LeadCandidate] = []
    disqualifiers: list[Disqualifier] = []

    fetchers: tuple[
        tuple[str, Any, tuple[str, ...], int], ...
    ] = (
        ("jobspy_fractional_cfo", _fetch_from_jobspy, _FRACTIONAL_CFO_QUERIES, _FRACTIONAL_MAX_POSTING_AGE_DAYS),
        ("jobspy_finance_leads", _fetch_from_jobspy, _FINANCE_LEAD_QUERIES, _MAX_POSTING_AGE_DAYS),
        ("jobspy_cfo_disqualifiers", _fetch_from_jobspy, _CFO_QUERIES, _MAX_POSTING_AGE_DAYS),
        ("jobspy_junior_finance", _fetch_from_jobspy, _JUNIOR_FINANCE_QUERIES, _MAX_POSTING_AGE_DAYS),
        ("jobspy_it", _fetch_from_jobspy, _IT_QUERIES, _MAX_POSTING_AGE_DAYS),
        ("adzuna_fractional_cfo", _fetch_from_adzuna, _FRACTIONAL_CFO_QUERIES, _FRACTIONAL_MAX_POSTING_AGE_DAYS),
        ("adzuna_finance_leads", _fetch_from_adzuna, _FINANCE_LEAD_QUERIES, _MAX_POSTING_AGE_DAYS),
        ("adzuna_cfo_disqualifiers", _fetch_from_adzuna, _CFO_QUERIES, _MAX_POSTING_AGE_DAYS),
        ("adzuna_junior_finance", _fetch_from_adzuna, _JUNIOR_FINANCE_QUERIES, _MAX_POSTING_AGE_DAYS),
        ("adzuna_it", _fetch_from_adzuna, _IT_QUERIES, _MAX_POSTING_AGE_DAYS),
    )
    for name, fetcher, queries, max_age in fetchers:
        try:
            c, d = fetcher(since, queries=queries, max_age_days=max_age)
            candidates.extend(c)
            disqualifiers.extend(d)
        except Exception:
            _log.exception("fetcher %s failed entirely", name)

    if limit is not None:
        candidates = candidates[:limit]
    return candidates, disqualifiers

import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
from pydantic import BaseModel

from msp_pipeline import llm
from msp_pipeline.models import (
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)

_log = logging.getLogger(__name__)

_TECHCRUNCH_FEED = "https://techcrunch.com/category/startups/feed/"
_PRNEWSWIRE_FEED = (
    "https://www.prnewswire.com/rss/financial-services-latest-news/"
    "financial-services-latest-news-list.rss"
)

# Title-pattern filter for RSS sources. The PR Newswire financial-services
# feed mixes funding announcements with class-action notices, regulatory
# filings, M&A, etc. — only let through titles that look like actual
# funding events.
_FUNDING_TITLE_PATTERN = re.compile(
    r"\b(raise[sd]?|secur(?:es|ed)|clos(?:es|ed)|"
    r"funding|series\s+[a-h]\b|seed(?:\s+round)?\b|"
    r"investment|round|backed|capital\s+from)\b",
    re.IGNORECASE,
)
_NON_FUNDING_PATTERN = re.compile(
    r"\b(class\s+action|lawsuit|investigation|encourag(?:es|ing)|"
    r"shareholder|inquire|complaint|fraud|securities\s+fraud|"
    r"recall(?:ed|s)?|reminder|"
    # Acquisitions / sales — "X sells for $Ym", "sold to", "acquired by"
    r"sells?\s+for|sold\s+(?:for|to)|acquir(?:es|ed|ition|er)|"
    # Lawsuits & valuation milestones
    r"sues?|suing|hits?\s+\$[\d.]+\s*(?:b|m|billion|million)?\s+valuation|"
    # VC / fund-of-funds raises (the entity raising IS itself a fund)
    r"venture\s+(?:capital|fund|funds)|growth-stage\s+funds?|"
    r"for\s+(?:new\s+|two\s+|three\s+)?(?:growth-stage\s+|venture\s+)?funds?\b|"
    # REIT investment announcements ("NHI Announces $X SHOP Investment")
    r"announces.*\binvestment\b|REIT\b|"
    # Stock-price-movement headlines are not funding rounds — a falling (or
    # spiking) share price is not a buying signal. Catches the "Kodiak AI
    # stock tumbling 37%" misclassification.
    r"\b(?:tumbl(?:es|ed|ing)?|plunge[sd]?|plunging|plummet(?:s|ed|ing)?|"
    r"slump(?:s|ed|ing)?|nosedive[sd]?|crater(?:s|ed|ing)?)\b|"
    r"\b(?:stock|shares?)\s+(?:tumbl|plung|plummet|slump|soar|surg|jump|drop|"
    r"fall|rise|sink|crash|dip)|"
    # Promotional / pump-and-dump hyperbole — "World's Largest IPO" on a
    # micro-cap is PR noise, not a real round.
    r"world'?s\s+largest)",
    re.IGNORECASE,
)

# Headline-as-name from RSS — extract the company part (everything before
# the funding-action verb). "Altara secures $7M to..." -> "Altara".
_HEADLINE_VERB_RE = re.compile(
    r"\s+(?:raises?|raised|secur(?:es|ed)|clos(?:es|ed)|announces?|"
    r"hits?|sells?|sold|backed)\s+",
    re.IGNORECASE,
)

# Strip "(CIK 0001234567)" suffixes that some upstream feeds attach to
# company names. Defensive — the SEC EDGAR fetcher that historically emitted
# these has been removed, but the helper costs nothing and protects against
# any future feed leaking similar markers.
_CIK_SUFFIX = re.compile(r"\s*\(CIK\s+\d+\)\s*$", re.IGNORECASE)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_rss_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _clean_company_name(name: str) -> str:
    return _CIK_SUFFIX.sub("", name).strip()


def _company_from_headline(title: str) -> str:
    """Extract the company name from a funding-announcement headline by
    splitting on the action verb. 'Altara secures $7M to ...' -> 'Altara'.

    Used as a fallback when the LLM extractor below is unavailable or fails."""
    m = _HEADLINE_VERB_RE.search(title)
    if m is None:
        return _clean_company_name(title)
    candidate = title[: m.start()].strip().rstrip(",").strip()
    return _clean_company_name(candidate) or _clean_company_name(title)


# --- LLM-driven extraction ---------------------------------------------------
#
# Funding headlines are messy: "Khosla-backed robotics startup Genesis AI has
# gone full stack, demo shows" should yield "Genesis AI", but a plain split-on-
# verb gives "Khosla" (because "backed" is an action verb). One small OpenAI
# call per headline gets us reliable extraction AND a flag for routine IR
# noise (earnings calls, conference call schedules) we should skip entirely.


class _HeadlineExtraction(BaseModel):
    company_name: str | None = None
    is_real_buying_signal: bool = True
    is_vc_or_fund: bool = False


_EXTRACT_PROMPT = """\
This is a headline from a startup funding / investment RSS feed. Extract
the actual company that just raised money or had a real funding event,
and flag noise we should skip.

Headline: "{headline}"

Rules for company_name:
- Return ONLY the operating company that is the subject of the funding.
  Not a VC firm. Not a person's name. Not a journalist's framing.
- "Khosla-backed robotics startup Genesis AI has gone full stack" -> "Genesis AI"
- "Y Combinator alum Skio sells for $105M cash" -> "Skio"
- "SpaceX backer 137 Ventures raises $700M for two growth-stage funds"
  -> set company_name to null AND is_vc_or_fund=true (137 Ventures is
  the fund itself, not a portfolio company)
- "Katie Haun raises $1B for new venture funds" -> null, is_vc_or_fund=true
- "Altara secures $7M to bridge the data gap" -> "Altara"
- If you can't confidently identify a real operating company, set
  company_name to null.

Rules for is_real_buying_signal:
- TRUE: Series A/B/C/D/etc., seed rounds, growth funding, real
  acquisitions where the acquired company will keep operating.
- FALSE: routine investor relations (earnings calls, conference call
  schedules, dividend announcements), class actions, lawsuits,
  regulatory inquiries, "encouraged to inquire" shareholder notices,
  share price milestones / valuation hits, stock-price movements (a
  share price tumbling, plunging, or surging), and promotional /
  pump-and-dump hyperbole on micro-caps.
- Example FALSE: "Stellus Capital Schedules First Quarter Conference Call"
- Example FALSE: "X Inc Hits $5B Valuation"
- Example FALSE: "Kodiak AI stock tumbling 37% after going public"
- Example FALSE: "Dominari Securities Raises $200,000,000 in World's Largest IPO"

Rules for is_vc_or_fund:
- TRUE if the entity raising money IS a VC firm, growth fund, or
  investment fund raising LP money. They don't buy IT services.
- FALSE for normal operating companies.
"""


def _extract_company_via_llm(headline: str) -> _HeadlineExtraction | None:
    """One small OpenAI call per headline. Returns None on any failure so
    the caller can fall back to the regex extractor."""
    try:
        return llm.call_openai(
            _EXTRACT_PROMPT.format(headline=headline),
            response_model=_HeadlineExtraction,
        )
    except Exception:
        _log.warning("funding: LLM headline extraction failed for %r", headline)
        return None


def _is_funding_title(title: str) -> bool:
    """True if a feed-entry title looks like a funding announcement."""
    if not title:
        return False
    if _NON_FUNDING_PATTERN.search(title):
        return False
    return bool(_FUNDING_TITLE_PATTERN.search(title))


def is_buying_signal_title(title: str) -> bool:
    """Public predicate: True when a funding headline reads like a real
    buying signal (a genuine raise / round), and False for IR noise, share-
    price moves, acquisitions, lawsuits, or pump-and-dump hype.

    The enrichment purge reuses this to drop leads ingested *before* this
    guard existed — their stored headline still renders on the card as if it
    were a real raise (e.g. "...World's Largest IPO", "...stock tumbling 37%")."""
    return _is_funding_title(title)


def _fetch_from_rss(feed_url: str, since: datetime) -> list[LeadCandidate]:
    captured_at = _utcnow()
    candidates: list[LeadCandidate] = []
    try:
        feed = feedparser.parse(feed_url)
    except Exception:
        _log.exception("rss fetch failed: %s", feed_url)
        return []
    for entry in feed.entries:
        title = str(entry.get("title") or "").strip()
        if not _is_funding_title(title):
            continue
        published = entry.get("published")
        published_dt = _parse_rss_date(published)
        if published_dt and published_dt < since:
            continue

        # LLM extraction first; regex fallback if the call errors. The
        # extraction also tells us when to skip (routine IR, VC fund raises).
        extraction = _extract_company_via_llm(title)
        if extraction is not None:
            if not extraction.is_real_buying_signal:
                _log.info("funding: skipping non-buying-signal headline: %r", title)
                continue
            if extraction.is_vc_or_fund:
                _log.info("funding: skipping VC/fund headline: %r", title)
                continue
            company = (extraction.company_name or "").strip()
            if not company:
                _log.info("funding: LLM couldn't identify a company in: %r", title)
                continue
        else:
            company = _company_from_headline(title)

        candidates.append(
            LeadCandidate(
                name=company,
                domain=None,
                initial_signal=Signal(
                    type=SignalType.FUNDING_RAISED,
                    source=SourceName.FUNDING,
                    captured_at=captured_at,
                    payload={
                        "feed_title": title,
                        "feed_url": feed_url,
                        "link": str(entry.get("link") or ""),
                        "published": published or "",
                    },
                ),
            )
        )
    return candidates


def _fetch_from_techcrunch(since: datetime) -> list[LeadCandidate]:
    return _fetch_from_rss(_TECHCRUNCH_FEED, since)


def _fetch_from_prnewswire(since: datetime) -> list[LeadCandidate]:
    return _fetch_from_rss(_PRNEWSWIRE_FEED, since)


def fetch(*, since: datetime, limit: int | None = None) -> list[LeadCandidate]:
    candidates: list[LeadCandidate] = []
    fetchers = (
        ("techcrunch", _fetch_from_techcrunch),
        ("prnewswire", _fetch_from_prnewswire),
    )
    for name, fetcher in fetchers:
        try:
            candidates.extend(fetcher(since))
        except Exception:
            _log.exception("fetcher %s failed entirely", name)
    if limit is not None:
        candidates = candidates[:limit]
    return candidates

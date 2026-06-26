"""Funding RSS source — ported from msp_pipeline/sources/funding.py.

Same fetcher shape (TechCrunch + PR Newswire RSS, LLM headline
extraction, regex pre-filter) but emits ``FUNDING_RAISED`` under the
insurance pipeline's enum. A fresh Series A/B/C round at a US SMB is
a real D&O / group benefits / EPLI trigger — exactly what an
independent insurance agent prospects on.

Independent from msp_pipeline.sources.funding. Mirror code, mirror
fixtures; no cross-imports.
"""

import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
from pydantic import BaseModel

from insurance_pipeline import llm
from insurance_pipeline.models import (
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
    r"sells?\s+for|sold\s+(?:for|to)|acquir(?:es|ed|ition|er)|"
    r"sues?|suing|hits?\s+\$[\d.]+\s*(?:b|m|billion|million)?\s+valuation|"
    r"venture\s+(?:capital|fund|funds)|growth-stage\s+funds?|"
    r"for\s+(?:new\s+|two\s+|three\s+)?(?:growth-stage\s+|venture\s+)?funds?\b|"
    r"announces.*\binvestment\b|REIT\b)",
    re.IGNORECASE,
)

_HEADLINE_VERB_RE = re.compile(
    r"\s+(?:raises?|raised|secur(?:es|ed)|clos(?:es|ed)|announces?|"
    r"hits?|sells?|sold|backed)\s+",
    re.IGNORECASE,
)

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
    m = _HEADLINE_VERB_RE.search(title)
    if m is None:
        return _clean_company_name(title)
    candidate = title[: m.start()].strip().rstrip(",").strip()
    return _clean_company_name(candidate) or _clean_company_name(title)


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
  -> set company_name to null AND is_vc_or_fund=true
- "Katie Haun raises $1B for new venture funds" -> null, is_vc_or_fund=true
- "Altara secures $7M to bridge the data gap" -> "Altara"
- If you can't confidently identify a real operating company, set
  company_name to null.

Rules for is_real_buying_signal:
- TRUE: Series A/B/C/D/etc., seed rounds, growth funding, real
  acquisitions where the acquired company will keep operating.
- FALSE: routine investor relations (earnings calls, conference call
  schedules, dividend announcements), class actions, lawsuits,
  regulatory inquiries, share price milestones / valuation hits.

Rules for is_vc_or_fund:
- TRUE if the entity raising money IS a VC firm, growth fund, or
  investment fund raising LP money.
- FALSE for normal operating companies.
"""


def _extract_company_via_llm(headline: str) -> _HeadlineExtraction | None:
    try:
        return llm.call_openai(
            _EXTRACT_PROMPT.format(headline=headline),
            response_model=_HeadlineExtraction,
        )
    except Exception:
        _log.warning("funding: LLM headline extraction failed for %r", headline)
        return None


def _is_funding_title(title: str) -> bool:
    if not title:
        return False
    if _NON_FUNDING_PATTERN.search(title):
        return False
    return bool(_FUNDING_TITLE_PATTERN.search(title))


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

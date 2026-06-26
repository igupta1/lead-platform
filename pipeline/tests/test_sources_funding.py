from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import feedparser

from msp_pipeline.models import (
    LeadCandidate,
    Signal,
    SignalType,
    SourceName,
)
from msp_pipeline.sources import funding as funding_module

_FIXTURES = Path(__file__).parent / "fixtures" / "funding"


def _make_candidate(name: str) -> LeadCandidate:
    return LeadCandidate(
        name=name,
        initial_signal=Signal(
            type=SignalType.FUNDING_RAISED,
            source=SourceName.FUNDING,
            captured_at=datetime.now(timezone.utc).replace(tzinfo=None),
            payload={},
        ),
    )


def test_funding_sec_edgar_fetcher_is_removed() -> None:
    """SEC EDGAR Form D was almost exclusively private fund formations, not
    operating businesses. Fetcher dropped; no replacement."""
    assert not hasattr(funding_module, "_fetch_from_sec_edgar")
    assert not hasattr(funding_module, "_SEC_EDGAR_API")


def test_funding_techcrunch_parses_fixture() -> None:
    xml = (_FIXTURES / "techcrunch.xml").read_text()
    parsed = feedparser.parse(xml)

    since = datetime(2020, 1, 1)
    with patch.object(funding_module.feedparser, "parse", return_value=parsed):
        candidates = funding_module._fetch_from_techcrunch(since)

    titles = {c.name for c in candidates}
    # Headline-as-name extraction: "Lima Robotics raises $25M Series B" -> "Lima Robotics"
    assert titles == {"Lima Robotics", "Mike's AI"}
    assert all(c.initial_signal.type == SignalType.FUNDING_RAISED for c in candidates)


def test_funding_prnewswire_parses_fixture() -> None:
    xml = (_FIXTURES / "prnewswire.xml").read_text()
    parsed = feedparser.parse(xml)

    since = datetime(2020, 1, 1)
    with patch.object(funding_module.feedparser, "parse", return_value=parsed):
        candidates = funding_module._fetch_from_prnewswire(since)

    titles = {c.name for c in candidates}
    assert titles == {"November Bank", "Oscar Insurance"}


def test_funding_fetch_aggregates_and_continues_on_failure() -> None:
    b, c = _make_candidate("TC Co"), _make_candidate("PR Co")

    with patch.object(funding_module, "_fetch_from_techcrunch", return_value=[b]), \
         patch.object(funding_module, "_fetch_from_prnewswire", return_value=[c]):
        names = {x.name for x in funding_module.fetch(since=datetime(2020, 1, 1))}
    assert names == {"TC Co", "PR Co"}

    def boom(_since: datetime) -> list[LeadCandidate]:
        raise RuntimeError("boom")

    with patch.object(funding_module, "_fetch_from_techcrunch", side_effect=boom), \
         patch.object(funding_module, "_fetch_from_prnewswire", return_value=[c]):
        names = {x.name for x in funding_module.fetch(since=datetime(2020, 1, 1))}
    assert names == {"PR Co"}


def test_is_funding_title_filters_non_funding_pr_newswire_noise() -> None:
    # Funding announcements pass.
    assert funding_module._is_funding_title("Acme Corp raises $25M Series B")
    assert funding_module._is_funding_title("Beta Inc secures $10M seed funding")
    assert funding_module._is_funding_title("Gamma closes $5M round")

    # PR Newswire class-action / regulatory noise is filtered.
    assert not funding_module._is_funding_title(
        "Rosen Law Firm Encourages Zillow Group, Inc. Investors to Inquire "
        "About Securities Class Action Investigation - Z, ZG"
    )
    assert not funding_module._is_funding_title(
        "ACME Pharma Recall Notice - Dietary Supplement"
    )
    assert not funding_module._is_funding_title(
        "Reminder: Shareholder Investigation in XYZ Corp"
    )
    # Empty / blank.
    assert not funding_module._is_funding_title("")
    assert not funding_module._is_funding_title("   ")


def test_clean_company_name_strips_cik_suffix() -> None:
    assert funding_module._clean_company_name(
        "Foo Corp  (CIK 0001234567)"
    ) == "Foo Corp"
    assert funding_module._clean_company_name(
        "Bar LLC (CIK 12345)"
    ) == "Bar LLC"
    # Names without the suffix are unchanged.
    assert funding_module._clean_company_name("Bare Co") == "Bare Co"


def test_is_funding_title_blocks_vc_fund_raises() -> None:
    assert not funding_module._is_funding_title(
        "Katie Haun raises $1B for new venture funds"
    )
    assert not funding_module._is_funding_title(
        "SpaceX backer 137 Ventures raises $700M for two growth-stage funds"
    )


def test_is_funding_title_blocks_acquisitions_lawsuits_valuations() -> None:
    assert not funding_module._is_funding_title(
        "Y Combinator alum Skio sells for $105M cash"
    )
    assert not funding_module._is_funding_title(
        "Founder of Shark Tank-backed startup Scholly sues his acquirer Sallie Mae"
    )
    assert not funding_module._is_funding_title(
        "Parallel Web Systems hits $2B valuation five months after its last big raise"
    )
    assert not funding_module._is_funding_title(
        "NHI Announces $106.9 Million SHOP Investment"
    )


def test_is_funding_title_blocks_stock_moves_and_pump_hype() -> None:
    # Stock-price-movement headlines are not funding rounds.
    assert not funding_module._is_funding_title(
        "Kodiak AI stock tumbling 37% after going public"
    )
    assert not funding_module._is_funding_title("XYZ Corp shares plunge after earnings")
    assert not funding_module._is_funding_title("Acme Inc stock plummets on weak guidance")
    # Promotional / pump-and-dump hyperbole.
    assert not funding_module._is_funding_title(
        "Dominari Securities Raises $200,000,000 in World's Largest IPO"
    )
    # A genuine round with no price-move language still passes.
    assert funding_module._is_funding_title("Acme raises $25M Series B")
    assert funding_module._is_funding_title("Beta Inc secures $10M seed funding")


def test_is_buying_signal_title_public_wrapper() -> None:
    # Public predicate reused by the enrichment purge — mirrors _is_funding_title.
    assert funding_module.is_buying_signal_title("Acme raises $25M Series B")
    assert not funding_module.is_buying_signal_title(
        "Dominari Securities Raises $200,000,000 in World's Largest IPO"
    )
    assert not funding_module.is_buying_signal_title(
        "Kodiak AI raises $100M at a steep discount, sending its stock tumbling 37%"
    )


def test_company_from_headline_extracts_company() -> None:
    assert funding_module._company_from_headline(
        "Altara secures $7M to bridge the data gap"
    ) == "Altara"
    assert funding_module._company_from_headline(
        "Firestorm Labs raises $82M to take drone factories into the field"
    ) == "Firestorm Labs"
    assert funding_module._company_from_headline(
        "Lima Robotics raises $25M Series B"
    ) == "Lima Robotics"
    # Fallback when no verb matches: return cleaned full title.
    assert funding_module._company_from_headline("Mystery Co") == "Mystery Co"

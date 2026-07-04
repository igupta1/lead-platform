"""Form D source — operating-company filter regression suite.

The patterns here are intentionally narrow: dropping a real operating
company because the regex over-fires is a worse failure than letting
a fund slip through (the enrichment + scoring layers will deal with it).
"""

from __future__ import annotations

from cfo_pipeline.sources import edgar_form_d


def test_extract_company_name_from_atom_title():
    title = "D - Acme Robotics Inc (1234567) (Filer)"
    assert edgar_form_d._extract_company_name(title) == "Acme Robotics Inc"


def test_extract_returns_none_for_non_form_d_title():
    assert edgar_form_d._extract_company_name("10-K - Acme (1234) (Filer)") is None


def test_drops_obvious_fund_filers():
    not_operating = [
        "Acme Ventures III LP",
        "Big Tech Capital Partners",
        "Series 2024 Holdings",
        "Family Office Investments LLC",
        "Bay Capital Management, L.P.",
        "Bancorp Holdings",
    ]
    for name in not_operating:
        assert not edgar_form_d._is_operating_company(name), name


def test_keeps_operating_company_names():
    operating = [
        "Acme Robotics Inc",
        "Genesis AI",
        "Altara Health Sciences",
        "Pioneer Legal",
        "Stripe Inc",
    ]
    for name in operating:
        assert edgar_form_d._is_operating_company(name), name


def test_drops_vintage_year_spv():
    assert not edgar_form_d._is_operating_company("Summit Ridge 2024 LLC")
    assert not edgar_form_d._is_operating_company("Pinecrest 1986 Inc")


def test_drops_street_spv():
    assert not edgar_form_d._is_operating_company("JR Hyde Park Blvd LLC")
    assert not edgar_form_d._is_operating_company("Marina Bay Avenue LLC")


def test_drops_tranche_suffix_spv():
    """The AQR-style hedge-fund SPV name pattern. Two formats:
    dash-separated "...LLC - Series B9" and bare "... Series B9"."""
    assert not edgar_form_d._is_operating_company("AQR Flex 1 Series LLC - Series B9")
    assert not edgar_form_d._is_operating_company("Some Fund - Series 2024")
    assert not edgar_form_d._is_operating_company("Acme Vehicle - Series A1")
    # Bare suffix
    assert not edgar_form_d._is_operating_company("Bridgewater Pure Alpha Series II")
    assert not edgar_form_d._is_operating_company("Whatever Series B9")


def test_tranche_filter_preserves_real_companies():
    """The regex must not flag a real company that happens to have
    'Series' in its name in a non-tranche context."""
    # No tranche suffix — these are real operating companies.
    assert edgar_form_d._is_operating_company("Series A Productions LLC")
    assert edgar_form_d._is_operating_company("Time Series Analytics Inc")


_FORM_D_XML = """<?xml version="1.0"?>
<edgarSubmission>
  <relatedPersonsList>
    <relatedPersonInfo>
      <relatedPersonName><firstName>Jane</firstName><lastName>Doe</lastName></relatedPersonName>
      <relatedPersonRelationshipList><relationship>Executive Officer</relationship><relationship>Director</relationship></relatedPersonRelationshipList>
      <relationshipClarification>CEO</relationshipClarification>
    </relatedPersonInfo>
    <relatedPersonInfo>
      <relatedPersonName><firstName>John</firstName><lastName>Smith</lastName></relatedPersonName>
      <relatedPersonRelationshipList><relationship>Director</relationship></relatedPersonRelationshipList>
      <relationshipClarification></relationshipClarification>
    </relatedPersonInfo>
  </relatedPersonsList>
  <offeringData>
    <industryGroup><industryGroupType>Other Technology</industryGroupType></industryGroup>
    <issuerSize><revenueRange>$1 - $1,000,000</revenueRange></issuerSize>
    <offeringSalesAmounts>
      <totalOfferingAmount>2500000</totalOfferingAmount>
      <totalAmountSold>1750000</totalAmountSold>
    </offeringSalesAmounts>
  </offeringData>
</edgarSubmission>
"""


def test_parse_form_d_xml_mines_offering_and_officers():
    d = edgar_form_d._parse_form_d_xml(_FORM_D_XML)
    assert d["is_pooled_fund"] is False
    assert d["offering_amount"] == 2500000.0
    assert d["amount_sold"] == 1750000.0
    assert d["industry_group"] == "Other Technology"
    assert d["revenue_range"] == "$1 - $1,000,000"
    assert d["officers"] == [
        {"name": "Jane Doe", "title": "CEO"},
        {"name": "John Smith", "title": "Director"},
    ]
    assert d["has_cfo_officer"] is False


def test_parse_form_d_xml_flags_cfo_officer():
    xml = _FORM_D_XML.replace(
        "<relationshipClarification>CEO</relationshipClarification>",
        "<relationshipClarification>Chief Financial Officer</relationshipClarification>",
    )
    d = edgar_form_d._parse_form_d_xml(xml)
    assert d["has_cfo_officer"] is True
    # Word-boundary: "CFO" as a bare token also matches.
    xml2 = _FORM_D_XML.replace(
        "<relationshipClarification>CEO</relationshipClarification>",
        "<relationshipClarification>CFO</relationshipClarification>",
    )
    assert edgar_form_d._parse_form_d_xml(xml2)["has_cfo_officer"] is True


def test_parse_form_d_xml_indefinite_amount_is_none():
    xml = _FORM_D_XML.replace(
        "<totalOfferingAmount>2500000</totalOfferingAmount>",
        "<totalOfferingAmount>Indefinite</totalOfferingAmount>",
    )
    d = edgar_form_d._parse_form_d_xml(xml)
    assert d["offering_amount"] is None
    assert d["amount_sold"] == 1750000.0


def test_parse_form_d_xml_pooled_fund_flag():
    xml = _FORM_D_XML.replace(
        "<industryGroupType>Other Technology</industryGroupType>",
        "<industryGroupType>Pooled Investment Fund</industryGroupType>",
    )
    assert edgar_form_d._parse_form_d_xml(xml)["is_pooled_fund"] is True


def test_parse_form_d_xml_malformed_degrades_gracefully():
    d = edgar_form_d._parse_form_d_xml("<edgarSubmission><broken")
    assert d["offering_amount"] is None
    assert d["officers"] == []
    assert d["has_cfo_officer"] is False

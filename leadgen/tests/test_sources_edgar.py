"""Fixture-based parser tests for the SEC EDGAR Form D / Form C sources.

No network: the pure XML parsers are exercised directly, and the fetch
wrappers (``_fetch_efts_page`` / ``requests.get`` for the primary_doc.xml)
are monkeypatched to inject small, real-shape fixture payloads. These guard
the parsers against silent breakage and assert the emitted Signal /
LeadCandidate honors the models.py contract (non-empty evidence_text +
source_url, parsed event_date, correct SignalType / SourceName).
"""

from __future__ import annotations

from datetime import datetime

from leadgen.models import SignalType, SourceName
from leadgen.sources import edgar_form_c, edgar_form_d

# --------------------------------------------------------------------------
# Fixtures — trimmed to the fields the parsers actually read, but shaped like
# a real primary_doc.xml (Form D carries no namespaces; Form C does).
# --------------------------------------------------------------------------

_FORM_D_XML = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission>
  <primaryIssuer>
    <entityName>Acme Robotics Inc</entityName>
    <issuerAddress>
      <stateOrCountry>CA</stateOrCountry>
    </issuerAddress>
    <issuerSize>
      <revenueRange>$1,000,000 - $4,999,999</revenueRange>
    </issuerSize>
  </primaryIssuer>
  <offeringData>
    <industryGroup>
      <industryGroupType>Other Technology</industryGroupType>
    </industryGroup>
    <offeringSalesAmounts>
      <totalOfferingAmount>5000000</totalOfferingAmount>
      <totalAmountSold>1500000</totalAmountSold>
    </offeringSalesAmounts>
    <relatedPersonsList>
      <relatedPersonInfo>
        <relatedPersonName>
          <firstName>Jane</firstName>
          <lastName>Doe</lastName>
        </relatedPersonName>
        <relatedPersonRelationshipList>
          <relationship>Executive Officer</relationship>
          <relationship>Director</relationship>
        </relatedPersonRelationshipList>
        <relationshipClarification>Chief Executive Officer</relationshipClarification>
      </relatedPersonInfo>
    </relatedPersonsList>
  </offeringData>
</edgarSubmission>
"""

_FORM_D_XML_CFO = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission>
  <offeringData>
    <offeringSalesAmounts>
      <totalOfferingAmount>2000000</totalOfferingAmount>
    </offeringSalesAmounts>
    <relatedPersonsList>
      <relatedPersonInfo>
        <relatedPersonName>
          <firstName>Sam</firstName>
          <lastName>Smith</lastName>
        </relatedPersonName>
        <relatedPersonRelationshipList>
          <relationship>Executive Officer</relationship>
        </relatedPersonRelationshipList>
        <relationshipClarification>Chief Financial Officer</relationshipClarification>
      </relatedPersonInfo>
    </relatedPersonsList>
  </offeringData>
</edgarSubmission>
"""

_FORM_D_XML_POOLED = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission>
  <offeringData>
    <industryGroup>
      <industryGroupType>Pooled Investment Fund</industryGroupType>
    </industryGroup>
    <isPooledInvestmentFundType>true</isPooledInvestmentFundType>
    <offeringSalesAmounts>
      <totalOfferingAmount>10000000</totalOfferingAmount>
    </offeringSalesAmounts>
  </offeringData>
</edgarSubmission>
"""

# Form C mirrors the real filing's default-namespace shape so the
# namespace-agnostic ``_local`` lookup is exercised.
_FORM_C_XML = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="https://www.sec.gov/edgar/formc">
  <issuerInformation>
    <issuer>
      <nameOfIssuer>Bright Widgets LLC</nameOfIssuer>
      <issuerWebsite>https://www.brightwidgets.io/home</issuerWebsite>
      <issuerAddress>
        <city>Austin</city>
        <stateOrCountry>TX</stateOrCountry>
      </issuerAddress>
    </issuer>
  </issuerInformation>
  <offeringInformation>
    <currentEmployees>12</currentEmployees>
    <revenueMostRecentFiscalYear>750000.00</revenueMostRecentFiscalYear>
    <maximumOfferingAmount>1070000.00</maximumOfferingAmount>
    <deadlineDate>2026-12-31</deadlineDate>
  </offeringInformation>
</edgarSubmission>
"""

_FORM_C_XML_OVERSIZED = _FORM_C_XML.replace(
    "<currentEmployees>12</currentEmployees>",
    "<currentEmployees>250</currentEmployees>",
)


class _FakeResp:
    """Minimal stand-in for a requests.Response carrying fixture text."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.status_code = 200

    def raise_for_status(self) -> None:  # pragma: no cover - no-op
        return None

    def json(self) -> dict:  # pragma: no cover - unused here
        raise AssertionError("json() not expected on the XML fetch")


def _xml_getter(xml: str):
    """Return a fake ``requests.get`` that always serves ``xml`` (the only
    remaining network call once ``_fetch_efts_page`` is stubbed is the
    primary_doc.xml fetch)."""

    def _get(url, *args, **kwargs):
        assert "primary_doc.xml" in url, f"unexpected fetch: {url}"
        return _FakeResp(xml)

    return _get


# --------------------------------------------------------------------------
# Form D — pure XML parser (_parse_form_d_xml)
# --------------------------------------------------------------------------


def test_parse_form_d_xml_extracts_offering_and_officers():
    d = edgar_form_d._parse_form_d_xml(_FORM_D_XML)
    assert d["offering_amount"] == 5_000_000.0
    assert d["amount_sold"] == 1_500_000.0
    assert d["industry_group"] == "Other Technology"
    assert d["revenue_range"] == "$1,000,000 - $4,999,999"
    assert d["is_pooled_fund"] is False
    assert d["has_cfo_officer"] is False
    assert d["officers"] == [{"name": "Jane Doe", "title": "Chief Executive Officer"}]


def test_parse_form_d_xml_flags_cfo_officer():
    d = edgar_form_d._parse_form_d_xml(_FORM_D_XML_CFO)
    assert d["has_cfo_officer"] is True
    assert d["officers"][0]["name"] == "Sam Smith"


def test_parse_form_d_xml_flags_pooled_fund():
    d = edgar_form_d._parse_form_d_xml(_FORM_D_XML_POOLED)
    assert d["is_pooled_fund"] is True


def test_parse_form_d_xml_bad_xml_degrades_gracefully():
    # A parse failure must not raise; it degrades to the regex pooled check.
    d = edgar_form_d._parse_form_d_xml("<not-valid-xml")
    assert d["offering_amount"] is None
    assert d["is_pooled_fund"] is False
    assert d["officers"] == []


def test_form_d_amount_handles_indefinite():
    xml = (
        "<edgarSubmission><offeringData><offeringSalesAmounts>"
        "<totalOfferingAmount>Indefinite</totalOfferingAmount>"
        "</offeringSalesAmounts></offeringData></edgarSubmission>"
    )
    d = edgar_form_d._parse_form_d_xml(xml)
    assert d["offering_amount"] is None  # non-numeric -> None, no crash


# --------------------------------------------------------------------------
# Form D — helper parsers
# --------------------------------------------------------------------------


def test_form_d_parse_iso_date():
    assert edgar_form_d._parse_iso_date("2026-07-15") == datetime(2026, 7, 15)
    assert edgar_form_d._parse_iso_date("") is None
    assert edgar_form_d._parse_iso_date(None) is None
    assert edgar_form_d._parse_iso_date("garbage") is None


def test_form_d_extract_name_from_display():
    assert (
        edgar_form_d._extract_name_from_display("Acme Robotics Inc  (CIK 0001234567)")
        == "Acme Robotics Inc"
    )


def test_form_d_evidence_never_empty():
    ev = edgar_form_d._form_d_evidence(
        company="Acme Robotics Inc",
        filed_on="2026-07-15",
        offering_amount=5_000_000.0,
        industry_group="Other Technology",
    )
    assert "Acme Robotics Inc" in ev
    assert "$5,000,000 offering" in ev
    # Always non-empty even with no extras.
    assert edgar_form_d._form_d_evidence(company="X", filed_on="").strip()


# --------------------------------------------------------------------------
# Form D — full EFTS path (Signal / LeadCandidate contract)
# --------------------------------------------------------------------------


def _form_d_efts_page(**_kwargs):
    return {
        "hits": {
            "total": {"value": 1},
            "hits": [
                {
                    "_source": {
                        "display_names": ["Acme Robotics Inc  (CIK 0001234567)"],
                        "file_date": "2026-07-15",
                        "adsh": "0001234567-26-000123",
                        "ciks": ["0001234567"],
                        "biz_states": ["CA"],
                        "biz_locations": ["San Francisco, California"],
                    }
                }
            ],
        }
    }


def test_form_d_efts_emits_contract_compliant_candidate(monkeypatch):
    monkeypatch.setattr(edgar_form_d, "_fetch_efts_page", _form_d_efts_page)
    monkeypatch.setattr(edgar_form_d.requests, "get", _xml_getter(_FORM_D_XML))

    candidates, disqualifiers = edgar_form_d._fetch_from_efts(datetime(2026, 1, 1))

    assert disqualifiers == []
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.name == "Acme Robotics Inc"
    assert cand.state == "CA"
    assert cand.city == "San Francisco, California"

    sig = cand.initial_signal
    assert sig.type is SignalType.FUNDING_FORM_D
    assert sig.source is SourceName.EDGAR_FORM_D
    assert sig.event_date == datetime(2026, 7, 15)
    assert sig.evidence_text  # non-empty (model enforces, but assert intent)
    assert "Acme Robotics Inc" in sig.evidence_text
    assert sig.source_url.startswith("https://www.sec.gov/Archives/edgar/data/1234567/")
    assert sig.payload["offering_amount"] == 5_000_000.0
    assert sig.payload["industry_group"] == "Other Technology"
    assert sig.payload["biz_state"] == "CA"


def test_form_d_efts_cfo_officer_becomes_disqualifier(monkeypatch):
    monkeypatch.setattr(edgar_form_d, "_fetch_efts_page", _form_d_efts_page)
    monkeypatch.setattr(edgar_form_d.requests, "get", _xml_getter(_FORM_D_XML_CFO))

    candidates, disqualifiers = edgar_form_d._fetch_from_efts(datetime(2026, 1, 1))

    assert candidates == []
    assert len(disqualifiers) == 1
    assert disqualifiers[0].reason == "cfo_listed_on_form_d"
    assert disqualifiers[0].source is SourceName.EDGAR_FORM_D


def test_form_d_efts_pooled_fund_skipped(monkeypatch):
    monkeypatch.setattr(edgar_form_d, "_fetch_efts_page", _form_d_efts_page)
    monkeypatch.setattr(edgar_form_d.requests, "get", _xml_getter(_FORM_D_XML_POOLED))

    candidates, disqualifiers = edgar_form_d._fetch_from_efts(datetime(2026, 1, 1))
    assert candidates == []
    assert disqualifiers == []


def test_form_d_efts_drops_hit_without_filing_link(monkeypatch):
    def _page_no_link(**_kwargs):
        return {
            "hits": {
                "total": {"value": 1},
                "hits": [
                    {
                        "_source": {
                            "display_names": ["Acme Robotics Inc  (CIK 0001234567)"],
                            "file_date": "2026-07-15",
                            "adsh": "",  # no accession -> no link -> dropped
                            "ciks": [],
                        }
                    }
                ],
            }
        }

    monkeypatch.setattr(edgar_form_d, "_fetch_efts_page", _page_no_link)
    # No XML fetch should occur (no ciks/adsh), so a getter that asserts.
    monkeypatch.setattr(edgar_form_d.requests, "get", _xml_getter(_FORM_D_XML))

    candidates, disqualifiers = edgar_form_d._fetch_from_efts(datetime(2026, 1, 1))
    assert candidates == []


# --------------------------------------------------------------------------
# Form C — pure XML parser (_parse_form_c_xml)
# --------------------------------------------------------------------------


def test_parse_form_c_xml_extracts_all_fields():
    d = edgar_form_c._parse_form_c_xml(_FORM_C_XML)
    assert d is not None
    assert d["name"] == "Bright Widgets LLC"
    assert d["domain"] == "brightwidgets.io"  # scheme/www/path stripped
    assert d["city"] == "Austin"
    assert d["state"] == "TX"
    assert d["current_employees"] == 12
    assert isinstance(d["current_employees"], int)
    assert d["revenue"] == 750_000.0
    assert d["offering_amount"] == 1_070_000.0
    assert d["deadline"] == "2026-12-31"


def test_parse_form_c_xml_requires_issuer_name():
    xml = (
        '<edgarSubmission xmlns="https://www.sec.gov/edgar/formc">'
        "<offeringInformation><currentEmployees>5</currentEmployees>"
        "</offeringInformation></edgarSubmission>"
    )
    assert edgar_form_c._parse_form_c_xml(xml) is None


def test_parse_form_c_xml_bad_xml_returns_none():
    assert edgar_form_c._parse_form_c_xml("<broken") is None


def test_form_c_clean_domain_edge_cases():
    assert edgar_form_c._clean_domain("https://www.example.com/path") == "example.com"
    assert edgar_form_c._clean_domain("HTTP://Example.COM") == "example.com"
    assert edgar_form_c._clean_domain("no-dot-here") is None
    assert edgar_form_c._clean_domain("has space.com") is None
    assert edgar_form_c._clean_domain("") is None
    assert edgar_form_c._clean_domain(None) is None


def test_form_c_to_float_and_int():
    assert edgar_form_c._to_float("$1,070,000.00") == 1_070_000.0
    assert edgar_form_c._to_float("nope") is None
    assert edgar_form_c._to_float(None) is None
    assert edgar_form_c._to_int("12") == 12
    assert edgar_form_c._to_int("") is None


# --------------------------------------------------------------------------
# Form C — full EFTS path (Signal / LeadCandidate contract)
# --------------------------------------------------------------------------


def _form_c_efts_page(**_kwargs):
    return {
        "hits": {
            "total": {"value": 1},
            "hits": [
                {
                    "_source": {
                        "display_names": ["Bright Widgets LLC (CIK 0009998887)"],
                        "file_date": "2026-07-10",
                        "adsh": "0009998887-26-000045",
                        "ciks": ["0009998887"],
                    }
                }
            ],
        }
    }


def test_form_c_efts_emits_contract_compliant_candidate(monkeypatch):
    monkeypatch.setattr(edgar_form_c, "_fetch_efts_page", _form_c_efts_page)
    monkeypatch.setattr(edgar_form_c.requests, "get", _xml_getter(_FORM_C_XML))

    candidates = edgar_form_c._fetch_from_efts(datetime(2026, 1, 1))

    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.name == "Bright Widgets LLC"
    assert cand.domain == "brightwidgets.io"
    assert cand.headcount == 12
    assert cand.state == "TX"
    assert cand.city == "Austin"

    sig = cand.initial_signal
    assert sig.type is SignalType.FUNDING_FORM_C
    assert sig.source is SourceName.EDGAR_FORM_C
    assert sig.event_date == datetime(2026, 7, 10)
    assert "Bright Widgets LLC" in sig.evidence_text
    assert sig.source_url.startswith("https://www.sec.gov/Archives/edgar/data/9998887/")
    assert sig.payload["offering_amount"] == 1_070_000.0
    assert sig.payload["revenue_amount"] == 750_000.0
    assert sig.payload["current_employees"] == 12


def test_form_c_efts_drops_oversized_issuer(monkeypatch):
    monkeypatch.setattr(edgar_form_c, "_fetch_efts_page", _form_c_efts_page)
    monkeypatch.setattr(edgar_form_c.requests, "get", _xml_getter(_FORM_C_XML_OVERSIZED))

    candidates = edgar_form_c._fetch_from_efts(datetime(2026, 1, 1))
    assert candidates == []  # headcount 250 > _SMB_HEADCOUNT_CAP

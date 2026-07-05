"""Form C (Reg Crowdfunding) source — XML parsing + field extraction.

No live HTTP: exercises the pure parser against a representative,
namespaced Form C primary_doc.xml.
"""

from __future__ import annotations

from cfo_pipeline.sources import edgar_form_c as fc

_SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/formc" xmlns:com="http://www.sec.gov/edgar/common">
  <formData>
    <issuerInformation>
      <issuerInfo>
        <nameOfIssuer>Acme Widgets Inc.</nameOfIssuer>
        <issuerAddress>
          <com:city>Austin</com:city>
          <com:stateOrCountry>TX</com:stateOrCountry>
        </issuerAddress>
        <issuerWebsite>https://www.acmewidgets.com/invest</issuerWebsite>
      </issuerInfo>
      <companyName>StartEngine Capital LLC</companyName>
    </issuerInformation>
    <offeringInformation>
      <offeringAmount>50000.00</offeringAmount>
      <maximumOfferingAmount>1235000.00</maximumOfferingAmount>
      <deadlineDate>09-15-2026</deadlineDate>
    </offeringInformation>
    <annualReportDisclosureRequirements>
      <currentEmployees>18</currentEmployees>
      <revenueMostRecentFiscalYear>2400000.00</revenueMostRecentFiscalYear>
    </annualReportDisclosureRequirements>
  </formData>
</edgarSubmission>"""


def test_parses_core_fields():
    d = fc._parse_form_c_xml(_SAMPLE_XML)
    assert d is not None
    # Uses nameOfIssuer, NOT the funding-portal companyName.
    assert d["name"] == "Acme Widgets Inc."
    assert d["domain"] == "acmewidgets.com"
    assert d["city"] == "Austin"
    assert d["state"] == "TX"
    assert d["current_employees"] == 18
    assert d["revenue"] == 2400000.0
    assert d["offering_amount"] == 1235000.0


def test_clean_domain_strips_scheme_www_path():
    assert fc._clean_domain("https://www.acme.com/invest") == "acme.com"
    assert fc._clean_domain("www.acme.com") == "acme.com"
    assert fc._clean_domain("acme.com") == "acme.com"
    assert fc._clean_domain("") is None
    assert fc._clean_domain("not a domain") is None


def test_numeric_helpers():
    assert fc._to_float("1,235,000.00") == 1235000.0
    assert fc._to_int("18") == 18
    assert fc._to_float(None) is None
    assert fc._to_int("n/a") is None


def test_missing_name_returns_none():
    assert fc._parse_form_c_xml("<edgarSubmission></edgarSubmission>") is None
    assert fc._parse_form_c_xml("not xml <") is None

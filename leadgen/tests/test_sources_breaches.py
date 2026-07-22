"""Fixture-based parser tests for the public data-breach source.

No network: the pure record/date/entity parsers and the state-AG row
parsers are exercised directly on small BeautifulSoup fixtures, and the HHS
OCR / WA-AG paths are driven by monkeypatching ``breaches._get`` to return a
trimmed real-shape HTML fixture. Asserts field extraction and that every
emitted breach Signal honors the models.py contract (non-empty evidence_text
+ source_url, parsed event_date, BREACH_DISCLOSED / BREACHES).
"""

from __future__ import annotations

from datetime import datetime

from bs4 import BeautifulSoup

from leadgen.models import SignalType, SourceName
from leadgen.sources import breaches


def _tds(row_html: str) -> list:
    """Parse a ``<tr>...</tr>`` fragment and return its <td> tags."""
    soup = BeautifulSoup(f"<table>{row_html}</table>", "html.parser")
    return soup.find("tr").find_all("td")


# --------------------------------------------------------------------------
# _parse_record_count — the explicitly-called-out edge cases (req #3)
# --------------------------------------------------------------------------


def test_parse_record_count_strips_commas():
    assert breaches._parse_record_count("12,400") == 12400


def test_parse_record_count_placeholders_return_none():
    assert breaches._parse_record_count("Unknown") is None
    assert breaches._parse_record_count("") is None
    assert breaches._parse_record_count(None) is None
    assert breaches._parse_record_count("N/A") is None


def test_parse_record_count_zero_is_none():
    # A zero count carries no signal -> None (n > 0 guard).
    assert breaches._parse_record_count("0") is None


def test_parse_record_count_embedded_digits():
    assert breaches._parse_record_count("approx. 3,210 individuals") == 3210


# --------------------------------------------------------------------------
# _parse_us_date / agency_display_name
# --------------------------------------------------------------------------


def test_parse_us_date_formats():
    assert breaches._parse_us_date("07/01/2026") == datetime(2026, 7, 1)
    assert breaches._parse_us_date("2026-07-01") == datetime(2026, 7, 1)
    assert breaches._parse_us_date("07-01-2026") == datetime(2026, 7, 1)
    assert breaches._parse_us_date("") is None
    assert breaches._parse_us_date("not a date") is None


def test_agency_display_name():
    assert breaches.agency_display_name("hhs_ocr") == "HHS OCR"
    assert breaches.agency_display_name("wa_ag") == "Washington State AG"
    assert breaches.agency_display_name(None) == "a state regulator"
    assert breaches.agency_display_name("mystery_code") == "a state regulator"


# --------------------------------------------------------------------------
# _clean_breach_entity — "on behalf of" -> the breached company
# --------------------------------------------------------------------------


def test_clean_breach_entity_plain():
    assert breaches._clean_breach_entity("Beta Corp") == "Beta Corp"


def test_clean_breach_entity_on_behalf_of():
    assert (
        breaches._clean_breach_entity("Big Law LLP on behalf of Beta Corp")
        == "Beta Corp"
    )


def test_clean_breach_entity_generic_client_dropped():
    # No identifiable breached company (reporting agent only) -> None.
    assert breaches._clean_breach_entity("Big Law LLP on behalf of its clients") is None


# --------------------------------------------------------------------------
# _evidence_text — factual, always non-empty
# --------------------------------------------------------------------------


def test_evidence_text_includes_count_and_agency():
    text = breaches._evidence_text("Acme Health Clinic", 12400, "07/01/2026", "hhs_ocr")
    assert "Acme Health Clinic" in text
    assert "12,400 individuals" in text
    assert "07/01/2026" in text
    assert "HHS OCR" in text


def test_evidence_text_without_count():
    text = breaches._evidence_text("Acme Health Clinic", None, "", "hhs_ocr")
    assert text.startswith("Acme Health Clinic reported a data breach")
    assert "individuals" not in text


# --------------------------------------------------------------------------
# _make_candidate — Signal contract + drop-on-missing-url
# --------------------------------------------------------------------------


def test_make_candidate_builds_contract_compliant_signal():
    cand = breaches._make_candidate(
        company="Acme Health Clinic",
        agency_code="hhs_ocr",
        source_url=breaches._HHS_PORTAL_URL,
        reported_date="07/01/2026",
        record_count=12400,
        breach_type="Hacking/IT Incident",
        state="TX",
        event_date=datetime(2026, 7, 1),
        captured_at=datetime(2026, 7, 21),
    )
    assert cand is not None
    assert cand.name == "Acme Health Clinic"
    assert cand.state == "TX"
    sig = cand.initial_signal
    assert sig.type is SignalType.BREACH_DISCLOSED
    assert sig.source is SourceName.BREACHES
    assert sig.event_date == datetime(2026, 7, 1)
    assert sig.source_url == breaches._HHS_PORTAL_URL
    assert sig.evidence_text
    assert sig.payload["record_count"] == 12400
    assert sig.payload["non_exclusive"] is True


def test_make_candidate_drops_row_without_source_url():
    cand = breaches._make_candidate(
        company="Acme Health Clinic",
        agency_code="hhs_ocr",
        source_url="",  # no URL -> Signal cannot be built -> row dropped
        reported_date="07/01/2026",
        record_count=12400,
        breach_type=None,
        state="TX",
        event_date=datetime(2026, 7, 1),
        captured_at=datetime(2026, 7, 21),
    )
    assert cand is None


# --------------------------------------------------------------------------
# _hhs_parse_rows — the HHS OCR table row extraction
# --------------------------------------------------------------------------

_HHS_TABLE_HTML = """
<tbody id="ocrForm:reportResultTable_data">
  <tr>
    <td></td>
    <td>Acme Health Clinic</td>
    <td>TX</td>
    <td>Healthcare Provider</td>
    <td>12,400</td>
    <td>07/01/2026</td>
    <td>Hacking/IT Incident</td>
    <td>Network Server</td>
  </tr>
  <tr>
    <td></td>
    <td>Beacon Dental Group</td>
    <td>OR</td>
    <td>Healthcare Provider</td>
    <td>Unknown</td>
    <td>06/28/2026</td>
    <td>Unauthorized Access/Disclosure</td>
    <td>Email</td>
  </tr>
</tbody>
"""


def test_hhs_parse_rows_extracts_fields():
    soup = BeautifulSoup(_HHS_TABLE_HTML, "html.parser")
    container = soup.find(id="ocrForm:reportResultTable_data")
    rows = breaches._hhs_parse_rows(container)
    assert len(rows) == 2
    first = rows[0]
    assert first["name"] == "Acme Health Clinic"
    assert first["state"] == "TX"
    assert first["individuals"] == "12,400"
    assert first["submitted"] == "07/01/2026"
    assert first["breach_type"] == "Hacking/IT Incident"
    assert first["location"] == "Network Server"


def test_hhs_parse_rows_none_container():
    assert breaches._hhs_parse_rows(None) == []


def test_fetch_from_hhs_ocr_emits_candidates(monkeypatch):
    # A full report page (no ViewState -> pagination loop is skipped) so the
    # source parses the seeded table and emits candidates, no network.
    page = f"<html><body>{_HHS_TABLE_HTML}</body></html>".encode()
    monkeypatch.setattr(breaches, "_get", lambda url, session=None: page)

    # since far in the past -> cutoff old -> both fixture rows survive.
    candidates = breaches._fetch_from_hhs_ocr(datetime(2000, 1, 1))
    assert len(candidates) == 2

    by_name = {c.name: c for c in candidates}
    acme = by_name["Acme Health Clinic"]
    assert acme.state == "TX"
    sig = acme.initial_signal
    assert sig.type is SignalType.BREACH_DISCLOSED
    assert sig.source is SourceName.BREACHES
    assert sig.event_date == datetime(2026, 7, 1)
    assert sig.source_url == breaches._HHS_PORTAL_URL
    assert sig.payload["record_count"] == 12400
    assert sig.payload["breach_type"] == "Hacking/IT Incident"
    assert sig.payload["covered_entity_type"] == "Healthcare Provider"

    # "Unknown" affected count parses to None but the row is still kept.
    beacon = by_name["Beacon Dental Group"]
    assert beacon.initial_signal.payload["record_count"] is None


# --------------------------------------------------------------------------
# State-AG row parsers
# --------------------------------------------------------------------------


def test_ca_ag_row_parser():
    tds = _tds(
        '<tr>'
        '<td><a href="https://oag.ca.gov/notice/beta">Beta Corp</a></td>'
        '<td>06/01/2026</td>'
        '<td>07/05/2026</td>'
        '</tr>'
    )
    name, reported, count, url = breaches._ca_ag_row_parser(tds)
    assert name == "Beta Corp"
    assert reported == "07/05/2026"  # last column
    assert count is None  # CA list page carries no affected-count
    assert url == "https://oag.ca.gov/notice/beta"


def test_wa_ag_row_parser_extracts_count_and_notice_url():
    tds = _tds(
        '<tr>'
        '<td>07/05/2026</td>'
        '<td><a href="https://www.atg.wa.gov/notice/beta.pdf">Beta Corp</a></td>'
        '<td>06/01/2026</td>'
        '<td>3,210</td>'
        '<td>Names, SSNs</td>'
        '</tr>'
    )
    name, reported, count, url = breaches._wa_ag_row_parser(tds)
    assert name == "Beta Corp"
    assert reported == "07/05/2026"
    assert count == 3210
    assert url == "https://www.atg.wa.gov/notice/beta.pdf"


def test_or_doj_row_parser():
    tds = _tds(
        '<tr>'
        '<td>Gamma Inc</td>'
        '<td>07/03/2026</td>'
        '<td>06/01/2026</td>'
        '<td>06/15/2026</td>'
        '<td>06/20/2026</td>'
        '<td>1,024</td>'
        '</tr>'
    )
    name, reported, count, url = breaches._or_doj_row_parser(tds)
    assert name == "Gamma Inc"
    assert reported == "07/03/2026"
    assert count == 1024
    assert url is None  # OR has no per-row link


def test_fetch_from_wa_ag_uses_notice_url_as_source(monkeypatch):
    html = (
        "<html><body><table>"
        "<tr><th>Reported</th><th>Org</th><th>Breach</th><th>Affected</th><th>Info</th></tr>"
        "<tr>"
        "<td>07/05/2026</td>"
        '<td><a href="https://www.atg.wa.gov/notice/beta.pdf">Beta Corp</a></td>'
        "<td>06/01/2026</td>"
        "<td>3,210</td>"
        "<td>Names, SSNs</td>"
        "</tr>"
        "</table></body></html>"
    ).encode()
    monkeypatch.setattr(breaches, "_get", lambda url, session=None: html)

    candidates = breaches._fetch_from_wa_ag(datetime(2000, 1, 1))
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.name == "Beta Corp"
    assert cand.state == "WA"
    sig = cand.initial_signal
    assert sig.type is SignalType.BREACH_DISCLOSED
    assert sig.source_url == "https://www.atg.wa.gov/notice/beta.pdf"
    assert sig.event_date == datetime(2026, 7, 5)
    assert sig.payload["record_count"] == 3210
    assert sig.payload["agency"] == "wa_ag"

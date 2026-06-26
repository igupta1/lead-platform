"""Smoke tests for the FL SunBiz source.

Tests parser behavior against a synthetic HTML fragment shaped like
what we expect SunBiz to return. The real HTML may differ — these
tests are a starting point, not a final spec.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from insurance_pipeline.models import SignalType, SourceName
from insurance_pipeline.sources import sos_fl


_SAMPLE_RESULTS_HTML = """
<html><body>
<table>
  <tr><th>Name</th><th>Type</th><th>Filed</th></tr>
  <tr>
    <td><a href="/Inquiry/CorporationSearch/SearchResultDetail?inquirytype=EntityName&document_number=L25000001">PIONEER LOGISTICS LLC</a></td>
    <td>Florida Limited Liability</td>
    <td>05/14/2026</td>
  </tr>
  <tr>
    <td><a href="/Inquiry/CorporationSearch/SearchResultDetail?inquirytype=EntityName&document_number=P25000002">SUNSHINE DINER INC</a></td>
    <td>Florida Profit Corporation</td>
    <td>05/13/2026</td>
  </tr>
</table>
</body></html>
"""


def test_parse_results_page_extracts_candidates() -> None:
    captured_at = datetime.now(timezone.utc).replace(tzinfo=None)
    cands = sos_fl._parse_results_page(_SAMPLE_RESULTS_HTML, captured_at)

    names = {c.name for c in cands}
    assert names == {"PIONEER LOGISTICS LLC", "SUNSHINE DINER INC"}

    by_name = {c.name: c.initial_signal for c in cands}
    sig = by_name["PIONEER LOGISTICS LLC"]
    assert sig.type == SignalType.NEW_BUSINESS_FILED
    assert sig.source == SourceName.SOS_FL
    assert sig.payload["state"] == "FL"
    assert "Limited" in sig.payload["filing_type"]
    assert sig.payload["filed_on"] == "05/14/2026"


def test_parse_results_page_ignores_unrelated_rows() -> None:
    html = """<table>
      <tr><td>Some marketing copy</td></tr>
      <tr><td><a href="/other/page">Not a filing</a></td><td>Some text</td></tr>
    </table>"""
    captured_at = datetime.now(timezone.utc).replace(tzinfo=None)
    assert sos_fl._parse_results_page(html, captured_at) == []


def test_fetch_clamps_since_to_max_age() -> None:
    """A very old `since` must be clamped — the date sent to SunBiz
    should be no older than _MAX_FILING_AGE_DAYS."""
    captured_params: list[dict[str, object]] = []

    def fake_get(url: str, **kwargs: object) -> MagicMock:
        captured_params.append(dict(kwargs.get("params") or {}))
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.text = "<html></html>"  # empty results → loop breaks
        return resp

    with patch.object(sos_fl.requests, "get", side_effect=fake_get):
        sos_fl.fetch(since=datetime(2020, 1, 1))

    assert captured_params, "expected at least one HTTP call"
    start = str(captured_params[0]["filedDateStart"])
    # Parse MM/DD/YYYY
    month, day, year = start.split("/")
    start_dt = datetime(int(year), int(month), int(day))
    now = datetime.now()
    age_days = (now - start_dt).days
    # Within a day of the cap.
    assert sos_fl._MAX_FILING_AGE_DAYS - 1 <= age_days <= sos_fl._MAX_FILING_AGE_DAYS + 1


def test_fetch_handles_http_failure(monkeypatch) -> None:
    def fake_get(url: str, **kwargs: object) -> MagicMock:
        raise sos_fl.requests.ConnectionError("network down")

    monkeypatch.setattr(sos_fl.requests, "get", fake_get)
    # Must not raise.
    assert sos_fl.fetch(since=datetime(2020, 1, 1)) == []

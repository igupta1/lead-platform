"""The signal evidence contract: no evidence -> not storable."""

from datetime import datetime, timezone

import pytest

from leadgen.models import Signal, SignalType, SourceName


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.mark.parametrize("field", ["evidence_text", "source_url"])
def test_empty_evidence_or_url_rejected(field):
    kwargs = dict(
        type=SignalType.FUNDING_FORM_D,
        source=SourceName.EDGAR_FORM_D,
        captured_at=_now(),
        evidence_text="Form D filed",
        source_url="https://sec.gov/x",
    )
    kwargs[field] = "   "
    with pytest.raises(Exception):
        Signal(**kwargs)


def test_evidence_is_stripped():
    s = Signal(
        type=SignalType.FUNDING_FORM_D,
        source=SourceName.EDGAR_FORM_D,
        captured_at=_now(),
        evidence_text="  Form D filed  ",
        source_url="  https://sec.gov/x  ",
    )
    assert s.evidence_text == "Form D filed"
    assert s.source_url == "https://sec.gov/x"


def test_disqualifier_marker_not_storable_as_signal():
    with pytest.raises(Exception):
        Signal(
            type=SignalType.CFO_ROLE_OPEN,
            source=SourceName.JOBS,
            captured_at=_now(),
            evidence_text="open full-time CFO",
            source_url="https://jobs/x",
        )

"""Shared test fixtures/factories for leadgen."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from leadgen import db
from leadgen.models import Signal, SignalType, SourceName


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


_JOB_TYPES = {
    SignalType.JOB_FRACTIONAL_CFO,
    SignalType.JOB_FINANCE_LEAD,
    SignalType.JOB_JUNIOR_FINANCE,
    SignalType.JOB_IT_SUPPORT,
    SignalType.JOB_IT_LEADERSHIP,
    SignalType.JOB_SECURITY,
    SignalType.JOB_CLOUD_DEVOPS,
}


def make_signal(
    sig_type: SignalType,
    *,
    url: str = "https://example.com/evidence",
    evidence: str = "verbatim evidence",
    days_ago: float = 1.0,
) -> Signal:
    now = _now()
    if sig_type in _JOB_TYPES:
        source = SourceName.JOBS
    elif sig_type is SignalType.BREACH_DISCLOSED:
        source = SourceName.BREACHES
    else:
        source = SourceName.EDGAR_FORM_D
    return Signal(
        type=sig_type,
        source=source,
        captured_at=now,
        event_date=now - timedelta(days=days_ago),
        evidence_text=evidence,
        source_url=url,
    )


@pytest.fixture
def make_sig():
    return make_signal


@pytest.fixture
def conn():
    c = db.init_db(":memory:")
    yield c
    c.close()

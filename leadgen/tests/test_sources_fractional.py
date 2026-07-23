"""Fractional boards route each posting to a niche via the shared classifier,
so one board feeds cfo / mssp / msp / cloud instead of only fractional-CFO."""

from __future__ import annotations

from leadgen.models import SignalType
from leadgen.sources.fractional_boards import _signal_type_for


# FractionalJobs.io: the board is fractional-only, so a bare exec title is
# treated as a fractional role (assume_fractional=True).
def test_fractional_only_board_routes_every_niche():
    st = SignalType
    assert _signal_type_for("Chief Financial Officer", assume_fractional=True) == st.JOB_FRACTIONAL_CFO
    assert _signal_type_for("Controller", assume_fractional=True) == st.JOB_FRACTIONAL_CFO
    assert _signal_type_for("Chief Information Security Officer", assume_fractional=True) == st.JOB_SECURITY
    assert _signal_type_for("CISO", assume_fractional=True) == st.JOB_SECURITY
    assert _signal_type_for("IT Director", assume_fractional=True) == st.JOB_IT_LEADERSHIP
    assert _signal_type_for("DevOps Engineer", assume_fractional=True) == st.JOB_CLOUD_DEVOPS
    assert _signal_type_for("Cloud Engineer", assume_fractional=True) == st.JOB_CLOUD_DEVOPS
    # Off-niche exec roles are dropped.
    assert _signal_type_for("Chief Marketing Officer", assume_fractional=True) is None
    assert _signal_type_for("Head of Sales", assume_fractional=True) is None


# We Work Remotely: a GENERAL board, so a title must read fractional/interim
# itself, or it's a full-time hire and not this signal (assume_fractional=False).
def test_general_board_requires_explicit_fractional_qualifier():
    st = SignalType
    assert _signal_type_for("Fractional CISO", assume_fractional=False) == st.JOB_SECURITY
    assert _signal_type_for("Interim CFO", assume_fractional=False) == st.JOB_FRACTIONAL_CFO
    # A plain full-time title on a general board is NOT a fractional signal.
    assert _signal_type_for("Security Engineer", assume_fractional=False) is None
    assert _signal_type_for("Chief Financial Officer", assume_fractional=False) is None

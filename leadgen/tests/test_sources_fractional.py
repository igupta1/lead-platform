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


# No general board is wired up any more (We Work Remotely was dropped — it
# yielded zero signals), but the assume_fractional=False contract is kept for
# the next one: the title must read fractional/interim itself, or the role is
# a full-time hire and not this signal.
def test_general_board_requires_explicit_fractional_qualifier():
    st = SignalType
    assert _signal_type_for("Fractional CISO", assume_fractional=False) == st.JOB_SECURITY
    assert _signal_type_for("Interim CFO", assume_fractional=False) == st.JOB_FRACTIONAL_CFO
    # A plain full-time title on a general board is NOT a fractional signal.
    assert _signal_type_for("Security Engineer", assume_fractional=False) is None
    assert _signal_type_for("Chief Financial Officer", assume_fractional=False) is None


# --- company name from the page title, not the slug -------------------------

from leadgen.sources.fractional_boards import _fj_page_company  # noqa: E402


def _page(title: str) -> str:
    return f"<html><head><title>{title}</title></head><body>x</body></html>"


def test_page_title_recovers_the_real_casing():
    # Every one of these is a live listing the slug mangled.
    cases = {
        "New Job | Fractional Chief Financial Officer at 3DT Holdings": "3DT Holdings",
        "New Job | Fractional Chief Financial Officer at EmpowerHCP": "EmpowerHCP",
        "New Job | Fractional Chief Financial Officer at co:census": "co:census",
        "New Job | Fractional Chief Financial Officer at LifeSiteNews": "LifeSiteNews",
    }
    for title, expected in cases.items():
        assert _fj_page_company(_page(title)) == expected


def test_hashed_slugs_collapse_to_one_name():
    """The three lifesitenews slugs differ only by a content hash; their pages
    all title to the same company, so they dedup to ONE lead in the store."""
    t = "New Job | Fractional Chief Financial Officer at LifeSiteNews"
    assert len({_fj_page_company(_page(t)) for _ in range(3)}) == 1


def test_html_entities_are_decoded():
    assert _fj_page_company(
        _page("New Job | Fractional CFO at Smith &amp; Sons")
    ) == "Smith & Sons"


def test_missing_or_unparseable_title_falls_back_to_none():
    assert _fj_page_company("<html><body>no title</body></html>") is None
    assert _fj_page_company(_page("New Job | Fractional CFO")) is None   # no " at "
    assert _fj_page_company(_page("Fractional CFO at ")) is None          # empty tail
    assert _fj_page_company("") is None


def test_company_containing_at_keeps_the_last_segment():
    # "at" inside the role must not win over the real separator.
    assert _fj_page_company(
        _page("New Job | Fractional Head of Data at Acme")
    ) == "Acme"

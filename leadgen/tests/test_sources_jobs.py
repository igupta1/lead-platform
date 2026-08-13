"""The unified jobs classifier maps each title to exactly one signal type,
covers all niches, and never resurrects exec_hired."""

import pytest

from leadgen.models import SignalType
from leadgen.sources.jobs import classify


@pytest.mark.parametrize("title,expected", [
    ("Fractional CFO", SignalType.JOB_FRACTIONAL_CFO),
    ("Interim Chief Financial Officer", SignalType.JOB_FRACTIONAL_CFO),
    ("Controller", SignalType.JOB_FINANCE_LEAD),
    ("VP of Finance", SignalType.JOB_FINANCE_LEAD),
    ("Bookkeeper", SignalType.JOB_JUNIOR_FINANCE),
    ("Accounts Payable Specialist", SignalType.JOB_JUNIOR_FINANCE),
    ("Help Desk Technician", SignalType.JOB_IT_SUPPORT),
    ("IT Support Specialist", SignalType.JOB_IT_SUPPORT),
    ("Director of IT", SignalType.JOB_IT_LEADERSHIP),
    ("Security Engineer", SignalType.JOB_SECURITY),
    ("CISO", SignalType.JOB_SECURITY),
    ("Site Reliability Engineer", SignalType.JOB_CLOUD_DEVOPS),
    ("DevOps Engineer", SignalType.JOB_CLOUD_DEVOPS),
])
def test_classify_maps_titles(title, expected):
    assert classify(title) == expected


def test_full_time_cfo_is_not_a_signal():
    # a plain full-time CFO posting classifies into no bucket (it needs a
    # part-time qualifier to reach JOB_FRACTIONAL_CFO), so it is dropped
    assert classify("Chief Financial Officer") is None


def test_exec_hired_is_gone():
    # a generic exec title yields no buying signal (no exec_hired proxy)
    assert not hasattr(SignalType, "EXEC_HIRED")
    assert classify("Chief Executive Officer") is None


# --- Body gate --------------------------------------------------------------
#
# Requiring the part-time qualifier in the TITLE dropped every genuinely
# fractional role posted as a plain "CFO" / "Head of Finance" with the terms
# only in the description. That title-only rule is most of why the fractional
# bucket was thin (138 of 185 signals came from one board). The qualifier is
# now accepted from the title OR the body.


@pytest.mark.parametrize("title,body,expected", [
    # qualifier in the body promotes a plain finance-leadership title
    ("Chief Financial Officer", "This is a fractional engagement, ~10 hrs/week",
     SignalType.JOB_FRACTIONAL_CFO),
    ("Head of Finance", "We are hiring on a part-time basis",
     SignalType.JOB_FRACTIONAL_CFO),
    # controller LEVEL routes to the accounting niche's lead-first signal, not
    # the CFO one — a fractional controller is what an outsourced accounting
    # firm sells, and the niche can only select on the type.
    ("Controller", "Interim coverage during a parental leave",
     SignalType.JOB_FRACTIONAL_CONTROLLER),
    # ...but a CFO title anywhere in it is a CFO search
    ("Fractional CFO / Controller", "", SignalType.JOB_FRACTIONAL_CFO),
    # strategy titles stay CFO-level
    ("Fractional VP of Finance", "", SignalType.JOB_FRACTIONAL_CFO),
    # qualifier in the title still works with no body at all
    ("Fractional CFO", "", SignalType.JOB_FRACTIONAL_CFO),
    # a full-time posting stays dropped -- no qualifier in either place
    ("Chief Financial Officer", "Full time, permanent, on-site role", None),
    # a non-leadership finance title is NOT promoted by a body qualifier
    ("Staff Accountant", "This is a part-time contract role",
     SignalType.JOB_JUNIOR_FINANCE),
])
def test_body_qualifier_promotes_only_finance_leadership(title, body, expected):
    assert classify(title, body) == expected


def test_classify_body_is_optional():
    # every existing caller passes a title only; that must keep working
    assert classify("Fractional CFO") is SignalType.JOB_FRACTIONAL_CFO
    assert classify("Chief Financial Officer") is None


def test_stringified_null_company_is_untargetable():
    """pandas turns an empty company cell into NaN, and str(nan) == "nan" —
    that is how a lead literally named `nan` reached a customer-facing email."""
    from leadgen.filters import is_untargetable_name
    for junk in ("nan", "NaN", " none ", "NULL", "n/a", "-", "unknown"):
        assert is_untargetable_name(junk), junk
    # a real company that merely starts with one of these is untouched
    assert not is_untargetable_name("NAN, Inc.")
    assert not is_untargetable_name("Nantucket Provisions")

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

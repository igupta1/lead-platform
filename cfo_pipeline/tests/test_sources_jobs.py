"""Jobs source — title classification, CFO disqualifier path, headcount band parser.

No live HTTP: we exercise the pure helpers directly.
"""

from __future__ import annotations

from cfo_pipeline.sources import jobs


def test_classifies_controller_title():
    assert jobs._is_finance_lead_title("Controller")
    assert jobs._is_finance_lead_title("Senior Controller")
    assert jobs._is_finance_lead_title("Assistant Controller, Manufacturing")
    assert jobs._is_finance_lead_title("Comptroller")  # variant spelling


def test_classifies_vp_finance_titles():
    assert jobs._is_finance_lead_title("VP Finance")
    assert jobs._is_finance_lead_title("VP of Finance")
    assert jobs._is_finance_lead_title("Vice President of Finance")
    assert jobs._is_finance_lead_title("Head of Finance")
    assert jobs._is_finance_lead_title("Director of Finance")
    assert jobs._is_finance_lead_title("Finance Director")


def test_classifies_accounting_manager_titles():
    assert jobs._is_finance_lead_title("Accounting Manager")
    assert jobs._is_finance_lead_title("Finance Manager")
    assert jobs._is_finance_lead_title("Head of Accounting")
    assert jobs._is_finance_lead_title("Director of Accounting")


def test_classifies_fpa_leadership_titles():
    assert jobs._is_finance_lead_title("FP&A Manager")
    assert jobs._is_finance_lead_title("FP&A Director")
    assert jobs._is_finance_lead_title("Senior FP&A Manager")
    assert jobs._is_finance_lead_title("VP FP&A")
    assert jobs._is_finance_lead_title("Head of FP&A")
    # FPA without ampersand
    assert jobs._is_finance_lead_title("FPA Manager")
    # FP&A Analyst is IC-level — not a buying signal.
    assert not jobs._is_finance_lead_title("FP&A Analyst")
    assert not jobs._is_finance_lead_title("Senior FP&A Analyst")


def test_classifies_senior_accountant_with_specialist_exclusion():
    assert jobs._is_finance_lead_title("Senior Accountant")
    assert jobs._is_finance_lead_title("Sr. Accountant")
    assert jobs._is_finance_lead_title("Senior Staff Accountant")
    # Specialist IC tracks aren't the buying signal:
    assert not jobs._is_finance_lead_title("Senior Tax Accountant")
    assert not jobs._is_finance_lead_title("Senior Audit Accountant")
    assert not jobs._is_finance_lead_title("Senior Cost Accountant")
    assert not jobs._is_finance_lead_title("Senior Payroll Accountant")


def test_unrelated_titles_dont_match():
    assert not jobs._is_finance_lead_title("Senior Software Engineer")
    assert not jobs._is_finance_lead_title("Director of Marketing")
    assert not jobs._is_finance_lead_title("Customer Success Manager")
    assert not jobs._is_finance_lead_title("")


def test_cfo_disqualifier_recognizes_full_time_titles():
    assert jobs._is_cfo_disqualifier_title("Chief Financial Officer")
    assert jobs._is_cfo_disqualifier_title("CFO")
    assert jobs._is_cfo_disqualifier_title("Chief Financial Officer (Hybrid)")


def test_cfo_disqualifier_excludes_part_time_variants():
    """A 'Fractional CFO' posting is the OPPOSITE of disqualifying —
    that company is in market for what's being sold. These must not
    write to the disqualifier table."""
    assert not jobs._is_cfo_disqualifier_title("Fractional CFO")
    assert not jobs._is_cfo_disqualifier_title("Interim CFO")
    assert not jobs._is_cfo_disqualifier_title("Part-Time CFO")
    assert not jobs._is_cfo_disqualifier_title("Outsourced CFO")
    assert not jobs._is_cfo_disqualifier_title("Contract CFO")
    assert not jobs._is_cfo_disqualifier_title("CFO Consultant")
    assert not jobs._is_cfo_disqualifier_title("Advisory CFO")


def test_cfo_disqualifier_doesnt_match_non_cfo_titles():
    assert not jobs._is_cfo_disqualifier_title("Controller")
    assert not jobs._is_cfo_disqualifier_title("CMO")  # chief marketing
    assert not jobs._is_cfo_disqualifier_title("CTO")
    assert not jobs._is_cfo_disqualifier_title("CFO services consultant for hire")  # has 'consultant'


def test_recruiter_names_filtered():
    assert jobs._is_recruiter_name("Robert Half")
    assert jobs._is_recruiter_name("Aerotek")
    assert jobs._is_recruiter_name("Acme Staffing Solutions")
    assert jobs._is_recruiter_name("XYZ Recruiting Group")
    assert not jobs._is_recruiter_name("Acme Robotics Inc")


def test_headcount_label_band_parsing():
    assert jobs._parse_headcount_label("1 to 10") == 10
    assert jobs._parse_headcount_label("11 to 50") == 50
    assert jobs._parse_headcount_label("201 to 500") == 500
    assert jobs._parse_headcount_label("1,001 to 5,000") == 5000


def test_headcount_label_plus_parsing():
    assert jobs._parse_headcount_label("10,001+") == 10001
    assert jobs._parse_headcount_label("5000+") == 5000


def test_headcount_label_invalid_returns_none():
    assert jobs._parse_headcount_label(None) is None
    assert jobs._parse_headcount_label("") is None
    assert jobs._parse_headcount_label("unknown") is None
    assert jobs._parse_headcount_label("N/A") is None


def test_cfo_posting_classified_as_disqualifier_not_signal():
    """Order matters: a 'Chief Financial Officer' job posting must
    not also classify as a finance-lead signal — daily_run branches
    on which classifier hits first."""
    title = "Chief Financial Officer"
    assert jobs._is_cfo_disqualifier_title(title)
    # The runner branches on CFO check first, then finance-lead check —
    # so a CFO posting never enters the finance-lead branch even though
    # the regex would also match other patterns. Confirm both classifiers
    # behave independently and the runner's branching is what enforces
    # the priority.
    assert not jobs._is_finance_lead_title(title)

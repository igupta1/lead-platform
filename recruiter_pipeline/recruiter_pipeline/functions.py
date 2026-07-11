"""Classify a job title into a hiring *function*.

The recruiter niche match keys off this: a finance recruiter wants companies
hiring finance roles, a security recruiter wants security roles, etc. Kept
deliberately coarse and deterministic (regex, no LLM) — good enough to route a
company's role mix to the recruiter specialties that exist in practice.
"""

from __future__ import annotations

import re

# Ordered most-specific → least. First match wins, so put narrow buckets
# (security, data) before the broad ones (engineering) they'd otherwise fall into.
_FUNCTION_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("security", re.compile(
        r"\b(security|infosec|appsec|soc analyst|penetration|pentest|ciso|"
        r"grc|threat|vulnerability)\b", re.IGNORECASE)),
    ("data", re.compile(
        r"\b(data scientist|data engineer|data analyst|analytics|machine learning|"
        r"\bml\b|\bai\b engineer|data platform)\b", re.IGNORECASE)),
    ("finance", re.compile(
        r"\b(accountant|accounting|bookkeeper|controller|cfo|finance|financial|"
        r"fp&a|payroll|accounts payable|accounts receivable|treasury|auditor|tax)\b",
        re.IGNORECASE)),
    ("it", re.compile(
        r"\b(help ?desk|it support|system administrator|sysadmin|network engineer|"
        r"it technician|desktop support|it manager)\b", re.IGNORECASE)),
    ("sales", re.compile(
        r"\b(sales|account executive|\bae\b|account manager|business development|"
        r"\bbdr\b|\bsdr\b|revenue|quota)\b", re.IGNORECASE)),
    ("marketing", re.compile(
        r"\b(marketing|growth|seo|content|brand|demand gen|social media|"
        r"communications|pr manager)\b", re.IGNORECASE)),
    ("product", re.compile(r"\b(product manager|product owner|\bpm\b|product lead)\b", re.IGNORECASE)),
    ("design", re.compile(r"\b(designer|design|ux|ui|creative director)\b", re.IGNORECASE)),
    ("hr", re.compile(
        r"\b(recruiter|talent|people ops|human resources|\bhr\b|hris|"
        r"people partner)\b", re.IGNORECASE)),
    ("operations", re.compile(
        r"\b(operations|logistics|supply chain|warehouse|fulfillment|"
        r"office manager|administrative)\b", re.IGNORECASE)),
    ("customer_success", re.compile(
        r"\b(customer success|customer support|support engineer|customer experience|"
        r"account support)\b", re.IGNORECASE)),
    ("legal", re.compile(r"\b(legal|counsel|attorney|paralegal|compliance)\b", re.IGNORECASE)),
    ("engineering", re.compile(
        r"\b(engineer|developer|software|programmer|devops|sre|architect|"
        r"full stack|backend|frontend|mobile|qa)\b", re.IGNORECASE)),
)


def classify_function(title: str) -> str:
    """Coarse function bucket for a role title; 'other' when nothing matches."""
    for name, rx in _FUNCTION_RES:
        if rx.search(title or ""):
            return name
    return "other"

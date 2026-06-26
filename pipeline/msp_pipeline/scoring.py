"""Per-niche signal-only scorer.

Score = clamp(sum(signal_weight × recency_decay), 0, 100), per niche, using
ONLY the signal types that actually fit that niche's service. A lead with no
qualifying signal for a niche scores ``None`` (not 0.0) and is dropped from
that niche's slice in ``daily_run._build_output``. This is what makes each
dashboard show only leads whose buying signal matches its stated scope —
help-desk/IT-leadership/growth for IT MSP, security/breach for MSSP,
cloud-DevOps/funding for Cloud.

No LLM. Pure deterministic. Industry and headcount live on the lead row
(written by ``enrichment.py``) but are UI filters, not score inputs.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from msp_pipeline.models import Lead, NicheName, Signal, SignalType

HALF_LIFE_DAYS = 30.0
SCORE_MIN = 0.0
SCORE_MAX = 100.0

# Per-niche QUALIFYING signal weights. A signal type absent from a niche's map
# does not count toward that niche's score at all (previously every type
# carried a small non-zero weight in every niche, which let a breach-only or
# helpdesk-only lead leak into all three dashboards). The matrix below is the
# stated scope of each page expressed in code:
#
#   IT MSP — IT support, IT leadership, growth (funding), exec hires
#   MSSP   — breach disclosures, security postings, security-exec hires
#   Cloud  — cloud/DevOps postings, growth (funding), cloud-exec hires
#
# Breach is intentionally NOT an IT-MSP signal: a breach-only lead belongs in
# MSSP. A breach lead that ALSO has a help-desk/IT-leadership posting still
# qualifies for IT MSP via that posting.
#
# EXEC_HIRED appears in all three maps but is gated by title (see
# ``_exec_niche``): a CISO hire counts only for MSSP, a CIO/VP-IT hire only for
# IT MSP, a cloud/DevOps exec only for Cloud — never all three at once.
SIGNAL_WEIGHTS: dict[NicheName, dict[SignalType, float]] = {
    NicheName.IT_MSP: {
        SignalType.JOB_IT_LEADERSHIP: 40,  # strongest MSP-buying signal
        SignalType.JOB_IT_SUPPORT: 35,     # growing IT team — MSP supplements
        SignalType.FUNDING_RAISED: 35,     # new cash, IT scale-up
        SignalType.EXEC_HIRED: 35,         # CIO/CTO/VP-IT triggers vendor eval
    },
    NicheName.MSSP: {
        SignalType.BREACH_DISCLOSED: 45,
        SignalType.JOB_SECURITY: 40,
        SignalType.EXEC_HIRED: 30,         # CISO / head-of-security hire
    },
    NicheName.CLOUD: {
        SignalType.JOB_CLOUD_DEVOPS: 40,
        SignalType.FUNDING_RAISED: 28,
        SignalType.EXEC_HIRED: 30,         # VP DevOps / cloud-infra exec
    },
}

# Title patterns that route an EXEC_HIRED signal to a single niche. Checked in
# order: a security exec wins over cloud wins over the IT-leadership default.
_EXEC_SECURITY_RE = re.compile(
    r"\b(ciso|infosec|information security|security officer|head of security)\b",
    re.IGNORECASE,
)
_EXEC_CLOUD_RE = re.compile(
    r"\b(devops|cloud|sre|site reliability|infrastructure|platform)\b",
    re.IGNORECASE,
)

# Date formats seen in breach payloads ("reported_date") and elsewhere.
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%Y/%m/%d")


def _exec_niche(sig: Signal) -> NicheName:
    """Which niche an exec hire fits, from its job title."""
    title = str(sig.payload.get("title") or "")
    if _EXEC_SECURITY_RE.search(title):
        return NicheName.MSSP
    if _EXEC_CLOUD_RE.search(title):
        return NicheName.CLOUD
    return NicheName.IT_MSP


def _parse_date(raw: object) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    return None


def effective_date(sig: Signal) -> datetime:
    """Date used for recency. For breaches this is the disclosure date
    (``reported_date``), not when our scraper happened to capture the row —
    a months-old breach should decay like a months-old breach, and the card
    should read "12d ago", never "0 days ago"."""
    if sig.type == SignalType.BREACH_DISCLOSED:
        disclosed = _parse_date(sig.payload.get("reported_date"))
        if disclosed is not None:
            return disclosed
    return sig.captured_at


def signal_matches_niche(sig: Signal, niche: NicheName) -> bool:
    """True when this signal is a qualifying buying signal for ``niche`` —
    used both for scoring and to decide which signals render on a niche's
    cards / appear in its insight copy."""
    weights = SIGNAL_WEIGHTS[niche]
    if sig.type not in weights:
        return False
    if sig.type == SignalType.EXEC_HIRED:
        return _exec_niche(sig) == niche
    return True


def score(lead: Lead, *, now: datetime | None = None) -> dict[NicheName, float | None]:
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    return {niche: _score_niche(lead, niche, now) for niche in NicheName}


def _score_niche(lead: Lead, niche: NicheName, now: datetime) -> float | None:
    weights = SIGNAL_WEIGHTS[niche]
    total = 0.0
    matched = False
    for sig in lead.signals:
        w = weights.get(sig.type)
        if w is None:
            continue
        if sig.type == SignalType.EXEC_HIRED and _exec_niche(sig) != niche:
            continue
        matched = True
        total += w * _decay(effective_date(sig), now)
    if not matched:
        return None
    return max(SCORE_MIN, min(SCORE_MAX, total))


def _decay(captured_at: datetime, now: datetime) -> float:
    days = max(0.0, (now - captured_at).total_seconds() / 86400.0)
    return 0.5 ** (days / HALF_LIFE_DAYS)

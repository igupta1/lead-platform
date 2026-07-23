"""Enrichment purge helpers — the pure-code CFO-competitor checks that keep
service providers (the competition) out of the buyer inventory."""

from __future__ import annotations

from leadgen.enrichment import _insight_is_service_provider, _is_cfo_competitor


def test_cfo_competitor_name_catches_vcfo_brand():
    # The bare "vCFO" / "vcfo" virtual-CFO brand slips past the "Virtual CFO"
    # (space-separated) pattern, so it needs its own catch.
    assert _is_cfo_competitor("vcfo") is True
    assert _is_cfo_competitor("v-CFO Partners") is True
    # Existing provider shapes still match; a normal company does not.
    assert _is_cfo_competitor("Acme Bookkeeping Services LLC") is True
    assert _is_cfo_competitor("Redwood Robotics") is False


def test_insight_backstop_flags_service_providers():
    # A provider's own description names the service it sells — this backstops
    # the Gemini yes/no flag, which missed "vcfo" despite this exact insight.
    assert _insight_is_service_provider(
        "Provides outsourced finance, HR, and recruiting solutions, "
        "including fractional CFO services."
    ) is True
    assert _insight_is_service_provider("A bookkeeping services firm for SMBs.") is True
    # A real buyer's description does NOT contain the provider phrases.
    assert _insight_is_service_provider("Operates a family dental practice in Ohio.") is False
    assert _insight_is_service_provider("Builds warehouse robotics.") is False
    assert _insight_is_service_provider(None) is False

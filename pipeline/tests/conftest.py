"""Test-wide setup. The pipeline's daily-run path consults
``APOLLO_API_KEY`` to decide whether to call Apollo. We unset it by default
here so test environments that happen to have a real key in ``.env`` don't
accidentally hit Apollo's API during tests. Tests that exercise the Apollo
path explicitly set the env var via monkeypatch."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _disable_apollo_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    # Belt and suspenders — also clear from os.environ in case a prior test
    # set it and forgot to clean up.
    os.environ.pop("APOLLO_API_KEY", None)

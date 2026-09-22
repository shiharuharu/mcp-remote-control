"""Service-test defaults: one owner for the global-registry reset rule.

The endpoint registry is process-global state, so every service test must run
against a fresh registry with ``MRC_HOME`` pinned to the fixture home. That
rule lives here once; per-file copies are what let it drift.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_remote_control.endpoint import reset_registry

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin MRC_HOME and reset the endpoint registry around each test."""
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_registry()
    yield
    reset_registry()

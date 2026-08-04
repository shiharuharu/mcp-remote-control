"""Integration tests: skipped unless MRC_INTEGRATION=1 and live profiles exist."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _integration_enabled() -> bool:
    val = os.environ.get("MRC_INTEGRATION", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: live host tests (require MRC_INTEGRATION=1)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if _integration_enabled():
        return
    skip = pytest.mark.skip(
        reason="set MRC_INTEGRATION=1 to run live integration tests"
    )
    for item in items:
        if "integration" in item.keywords or "/integration/" in str(
            item.fspath
        ):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def integration_home() -> Path:
    """MRC_HOME for live profiles (env or default user config)."""
    raw = (
        os.environ.get("MRC_HOME")
        or os.environ.get("MCP_REMOTE_CONTROL_HOME")
        or ""
    ).strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".config" / "mcp-remote-control"


@pytest.fixture(scope="session")
def ssh_profile() -> str:
    return os.environ.get("MRC_SSH_PROFILE", "lab-ssh").strip() or "lab-ssh"


@pytest.fixture(scope="session")
def winrm_profile() -> str:
    return os.environ.get("MRC_WINRM_PROFILE", "lab-win").strip() or "lab-win"

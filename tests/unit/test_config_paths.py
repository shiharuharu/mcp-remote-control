"""Unit tests for config path resolution and public_path_for_msg."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_remote_control.config import resolve_home



# ---------------------------------------------------------------------------
# resolve_home
# ---------------------------------------------------------------------------

def test_resolve_home_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MRC_HOME", raising=False)
    monkeypatch.delenv("MCP_REMOTE_CONTROL_HOME", raising=False)
    # Point HOME at tmp so we do not touch the real user config dir.
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() on some platforms uses pwd, not HOME - also patch if needed.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    home = resolve_home()
    assert home == (tmp_path / ".config" / "mcp-remote-control").resolve()


def test_resolve_home_mrc_home_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "mrc-root"
    target.mkdir()
    monkeypatch.setenv("MRC_HOME", str(target))
    monkeypatch.setenv("MCP_REMOTE_CONTROL_HOME", str(tmp_path / "legacy"))

    home = resolve_home()
    assert home == target.resolve()


def test_resolve_home_legacy_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "legacy-root"
    target.mkdir()
    monkeypatch.delenv("MRC_HOME", raising=False)
    monkeypatch.setenv("MCP_REMOTE_CONTROL_HOME", str(target))

    home = resolve_home()
    assert home == target.resolve()


def test_resolve_home_explicit_env_mapping(tmp_path: Path) -> None:
    target = tmp_path / "explicit"
    target.mkdir()
    home = resolve_home(env={"MRC_HOME": str(target)})
    assert home == target.resolve()


def test_resolve_home_expands_dollar_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Host JSON often sets MRC_HOME=$HOME/... without shell expansion."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("MRC_HOME", "$HOME/.config/mcp-remote-control")
    home = resolve_home()
    assert home == (tmp_path / ".config" / "mcp-remote-control").resolve()


def test_resolve_home_expands_tilde(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("MRC_HOME", "~/.config/mcp-remote-control")
    home = resolve_home()
    assert home == (tmp_path / ".config" / "mcp-remote-control").resolve()


def test_resolve_home_ignores_doc_placeholder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Copy-pasted /Users/<you>/... must not become a real mkdir root."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("MRC_HOME", "/Users/<you>/.config/mcp-remote-control")
    home = resolve_home()
    assert home == (tmp_path / ".config" / "mcp-remote-control").resolve()
    assert "<you>" not in str(home)


def test_resolve_home_ignores_stacked_tilde_users(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """~/Users/<you>/... is unusable; fall back to Path.home() join."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("MRC_HOME", "~/Users/<you>/.config/mcp-remote-control")
    home = resolve_home()
    assert home == (tmp_path / ".config" / "mcp-remote-control").resolve()


def test_default_home_joins_absolute_parts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from mcp_remote_control.config.paths import default_home

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    h = default_home()
    assert h.is_absolute()
    assert h == (tmp_path.resolve() / ".config" / "mcp-remote-control")


def test_public_path_for_msg_under_and_outside_home(tmp_path: Path) -> None:
    """Helper: under-home -> relative; outside-home absolute kept."""
    from mcp_remote_control.config.errors import public_path_for_msg

    home = tmp_path / "mrc"
    home.mkdir()
    under = home / "profiles" / "x.toml"
    under.parent.mkdir()
    under.write_text("x", encoding="utf-8")
    assert public_path_for_msg(home, under) == "profiles/x.toml"
    outside = tmp_path / "elsewhere" / "key.pem"
    outside.parent.mkdir()
    outside.write_text("k", encoding="utf-8")
    out = public_path_for_msg(home, outside)
    assert Path(out).is_absolute()
    assert str(outside.resolve()) in out or out == str(outside)
    assert public_path_for_msg(home, None) == ""


def test_no_secret_rel_path_or_public_path_thin_wrappers() -> None:
    """Path layers collapsed - single public_path_for_msg, no dual thin forwards."""
    import mcp_remote_control.config.store as store_mod
    from mcp_remote_control.config.errors import public_path_for_msg

    assert not hasattr(store_mod, "secret_rel_path")
    assert not hasattr(store_mod, "_public_path")
    # store reuses errors.public_path_for_msg (imported name in module ns).
    assert store_mod.public_path_for_msg is public_path_for_msg

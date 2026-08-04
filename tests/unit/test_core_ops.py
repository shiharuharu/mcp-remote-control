"""Core ops unit tests: endpoint, exec, fs, screen, and ps OpResult dispatch."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

import mcp_remote_control.fs.service as fs_service
from mcp_remote_control.core import endpoint_ops, exec_ops, fs_ops, ps_ops, screen_ops
from mcp_remote_control.core.result import OpResult, render_result
from mcp_remote_control.endpoint import reset_registry

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    reset_registry()
    yield
    reset_registry()


def test_endpoint_ops_list_open_close(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    r = endpoint_ops.run(op="list", home=FIXTURES)
    assert isinstance(r, OpResult)
    assert r.kind == "endpoint"
    assert r.status == "ok"

    r_open = endpoint_ops.run(op="open", profile="local", home=FIXTURES)
    assert r_open.status == "ok"
    assert r_open.fields.get("transport") == "local"

    r_close = endpoint_ops.run(op="close", ep="local")
    assert r_close.status == "ok"


def test_endpoint_invalid_op() -> None:
    r = endpoint_ops.run(op="explode")
    assert r.status == "error"
    assert r.code == "INVALID_OP"


def test_exec_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    r = exec_ops.run(ep="local", command="echo hi", home=FIXTURES)
    assert r.kind == "exec"
    assert r.status == "ok"
    assert r.code is None
    assert r.fields.get("exit") == 0
    assert "hi" in (r.body or "")
    assert "@exec ok" in r.render_text()
    assert r.cwd is not None


def test_fs_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    r = fs_ops.run("list", ep="local", path="/tmp", home=FIXTURES)
    assert r.kind == "fs"
    assert r.status == "ok"
    assert r.fields.get("op") == "list"
    assert Path(r.fields["path"]).is_absolute()
    text = r.render_text()
    # fs embeds op in header: @fs list ok …
    assert text.startswith("@fs list ok")


def test_fs_service_has_no_run_dispatch() -> None:
    """Public fs op dispatch is core.fs_ops only; fs.service is backend factory."""
    assert not hasattr(fs_service, "run")
    assert callable(fs_ops.run)
    assert callable(fs_service.backend_for_endpoint)


def test_screen_ops_open_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    from mcp_remote_control.screen.registry import reset_screen_registry

    reset_screen_registry()
    try:
        # Real local PTY — keep geometry small and shell simple.
        shell = "/bin/bash" if Path("/bin/bash").is_file() else "/bin/sh"
        r = screen_ops.run(
            "open",
            ep="local",
            home=FIXTURES,
            shell=shell,
            settle_s=0.4,
            cols=100,
            rows=30,
        )
        assert r.kind == "screen"
        assert r.status == "ok"
        assert r.code is None
        assert r.fields.get("id")
        assert r.fields.get("cur")
    finally:
        reset_screen_registry()


def test_screen_ops_send_empty_shot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    from mcp_remote_control.screen.registry import reset_screen_registry

    reset_screen_registry()
    try:
        shell = "/bin/bash" if Path("/bin/bash").is_file() else "/bin/sh"
        opened = screen_ops.run(
            "open",
            ep="local",
            home=FIXTURES,
            shell=shell,
            settle_s=0.3,
            cols=100,
            rows=30,
        )
        assert opened.status == "ok"
        sid = opened.fields["id"]
        r = screen_ops.run(
            "send",
            id=sid,
            actions=[],
            wait={"until": "deadline", "timeout_ms": 50},
        )
        assert r.kind == "screen"
        assert r.status in ("ok", "unchanged")
        assert r.code is None
        assert r.fields.get("id") == sid
        assert r.fields.get("cur")
    finally:
        reset_screen_registry()


def test_ps_ops_not_found_without_open() -> None:
    """invoke on unknown session → PS_NOT_FOUND."""
    r = ps_ops.run("invoke", id="s1", script="Get-Host")
    assert r.kind == "ps"
    assert r.status == "error"
    assert r.code == "PS_NOT_FOUND"


def test_ps_ops_local_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    r = ps_ops.run("open", ep="local", home=FIXTURES)
    assert r.kind == "ps"
    assert r.code == "UNSUPPORTED"


def test_render_result_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    r = endpoint_ops.list_endpoints(home=FIXTURES)
    raw = render_result(r, as_json=True)
    data = json.loads(raw)
    assert data["kind"] == "endpoint"
    assert data["status"] == "ok"

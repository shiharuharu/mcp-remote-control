"""Core ops unit tests: endpoint, exec, fs, screen, and ps OpResult dispatch."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

import mcp_remote_control.fs.service as fs_service
from mcp_remote_control.core import endpoint_ops, exec_ops, fs_ops, ps_ops, screen_ops
from mcp_remote_control.core.result import OpResult, render_result
from mcp_remote_control.endpoint import reset_registry
from mcp_remote_control.fs.types import FsError
from mcp_remote_control.transport.base import TransportError, normalize_timeout_s

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


def test_endpoint_list_open_notes_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_remote_control.config.store import (
        ensure_home_layout,
        put_profile,
        write_notes,
    )

    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    ensure_home_layout(home)
    put_profile(home, name="box", transport="local")

    listed0 = endpoint_ops.run(op="list", home=home)
    assert listed0.status == "ok"
    assert "notes=1" not in (listed0.body or "")

    marker = "UNIQUE-CORE-OPS-NOTES-BODY"
    write_notes(home, "box", marker)
    listed = endpoint_ops.run(op="list", home=home)
    assert listed.status == "ok"
    assert "notes=1" in (listed.body or "")
    assert marker not in (listed.body or "")

    opened = endpoint_ops.run(op="open", profile="box", home=home)
    assert opened.status == "ok"
    assert opened.fields.get("notes") == 1
    text = opened.render_text()
    assert "notes=1" in text
    assert marker not in text


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
    # fs embeds op in header: @fs list ok ...
    assert text.startswith("@fs list ok")


def test_fs_ops_whitespace_path_names_a_file_not_a_missing_arg(
    tmp_path: Path,
) -> None:
    """A whitespace-only path is a name: the service layer must not strip it.

    The backend keeps the caller's spelling, whitespace included, so a strip
    at the ops gate would make the relative spelling of such a file
    unreachable while its absolute spelling still worked.
    """
    from mcp_remote_control.fs.backends.local import LocalFs

    blank = tmp_path / " "
    blank.write_text("blank name\n")
    backend = LocalFs(cwd=str(tmp_path))

    stat = fs_ops.run("stat", path=" ", backend=backend)
    assert stat.status == "ok", stat.render_text()
    assert stat.fields.get("type") == "file"

    read = fs_ops.run("read", path=" ", backend=backend)
    assert read.status == "ok", read.render_text()
    assert read.body == "blank name\n"

    rm = fs_ops.run("rm", path=" ", backend=backend)
    assert rm.status == "ok", rm.render_text()
    assert not blank.exists()


def test_fs_ops_omitted_and_empty_path_still_missing_arg(tmp_path: Path) -> None:
    """Only an omitted or empty path is MISSING_ARG, not a whitespace name."""
    from mcp_remote_control.fs.backends.local import LocalFs

    backend = LocalFs(cwd=str(tmp_path))
    for missing in (None, ""):
        r = fs_ops.run("read", path=missing, backend=backend)
        assert r.status == "error"
        assert r.code == "MISSING_ARG"


def test_fs_ops_connect_failure_row_keeps_a_whitespace_only_path(
    tmp_path: Path,
) -> None:
    """A row for a failed op names the caller's whitespace-only path.

    The row's ``path`` is the caller's own spelling, so a whitespace-only one
    must survive into the fields an Agent reads: without it the row names no
    path at all and the failure cannot be tied to what was asked for.
    """
    r = fs_ops.run("read", ep="no-such-profile", path=" ", home=tmp_path / "mrc")

    assert r.status == "error"
    assert r.code == "PROFILE_NOT_FOUND"
    assert r.fields.get("path") == " "
    assert "| path= " in r.render_text()

    # An empty path stays out of the row: there is no name to report.
    empty = fs_ops.run("read", ep="no-such-profile", path="", home=tmp_path / "mrc")
    assert empty.code == "MISSING_ARG"
    assert empty.fields.get("path") is None


def test_fs_ops_backend_failure_names_caller_path_beside_the_node(
    tmp_path: Path,
) -> None:
    """A backend failure keeps the caller's spelling and the node it reached.

    LocalFs resolves ``" "`` against its cwd, so the row carries ``path=" "``
    (what was asked for) beside ``node_path`` (the absolute name the backend
    failed at). The node is not a repeat of the caller's string, so it stays.
    """
    from mcp_remote_control.fs.backends.local import LocalFs

    backend = LocalFs(cwd=str(tmp_path))
    r = fs_ops.run("stat", path=" ", backend=backend)

    assert r.status == "error"
    assert r.code == "NOT_FOUND"
    assert r.fields.get("path") == " "
    node = os.path.join(str(tmp_path), " ")
    assert r.fields.get("node_path") == node
    assert node in str(r.fields.get("msg") or "")


class _EchoPathBackend:
    """Backend stand-in that fails while naming the string it was handed."""

    via = None

    def stat(self, path: str) -> object:
        raise FsError("NOT_FOUND", f"path not found: {path}", details={"path": path})


def test_fs_ops_node_path_is_not_repeated_when_it_equals_the_caller_path() -> None:
    """One name is reported once: an identical node adds no second field."""
    r = fs_ops.run("stat", path=" ", backend=_EchoPathBackend())

    assert r.status == "error"
    assert r.fields.get("path") == " "
    assert "node_path" not in r.fields


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
        # Real local PTY - keep geometry small and shell simple.
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
    """invoke on unknown session -> PS_NOT_FOUND."""
    r = ps_ops.run("invoke", id="s1", script="Get-Host")
    assert r.kind == "ps"
    assert r.status == "error"
    assert r.code == "PS_NOT_FOUND"


def test_ps_ops_local_cap_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    r = ps_ops.run("open", ep="local", home=FIXTURES)
    assert r.kind == "ps"
    assert r.code == "CAP_DENIED"
    assert "lacks ps capability" in (r.fields.get("msg") or "").lower()


def test_render_result_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    r = endpoint_ops.list_endpoints(home=FIXTURES)
    raw = render_result(r, as_json=True)
    data = json.loads(raw)
    assert data["kind"] == "endpoint"
    assert data["status"] == "ok"


def test_shared_home_short_helpers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """endpoint/exec/fs/ps/screen share one _home/_short from core.result."""
    from mcp_remote_control.core import result as result_mod

    modules = (endpoint_ops, exec_ops, fs_ops, ps_ops, screen_ops)
    for mod in modules:
        assert mod._home is result_mod._home, f"{mod.__name__}._home not shared"
        assert mod._short is result_mod._short, f"{mod.__name__}._short not shared"

    # Home resolve semantics: None -> resolve_home; path expands + resolves.
    monkeypatch.setenv("MRC_HOME", str(tmp_path))
    assert result_mod._home(None) == tmp_path.resolve()
    nested = tmp_path / "sub"
    nested.mkdir()
    assert result_mod._home(nested) == nested.resolve()
    assert result_mod._home(str(nested)) == nested.resolve()

    # Truncation: collapse whitespace, ellipsis at limit.
    short = result_mod._short("  a   b  \n c  ", limit=200)
    assert short == "a b c"
    long = "x" * 250
    out = result_mod._short(long, limit=20)
    assert len(out) == 20
    assert out.endswith("...")
    assert out.startswith("x")


def test_normalize_timeout_s_none_is_unlimited() -> None:
    """None stays unlimited (same as the former exec/ps _normalize_timeout)."""
    assert normalize_timeout_s(None) is None


def test_normalize_timeout_s_positive_passthrough() -> None:
    assert normalize_timeout_s(1.5) == 1.5
    assert normalize_timeout_s(3) == 3.0
    assert normalize_timeout_s("2.5") == 2.5  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad",
    [0, 0.0, -5, -0.1],
    ids=["zero-int", "zero-float", "neg-int", "neg-float"],
)
def test_normalize_timeout_s_non_positive_raises(bad: float) -> None:
    """0 and negatives are INVALID_ARG (pre-change _normalize_timeout)."""
    with pytest.raises(TransportError) as ei:
        normalize_timeout_s(bad)
    assert ei.value.code == "INVALID_ARG"
    assert ei.value.msg == f"timeout must be > 0, got {bad!r}"


@pytest.mark.parametrize(
    "bad",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        "nan",
        "inf",
        "-inf",
    ],
    ids=["nan", "inf", "-inf", "str-nan", "str-inf", "str--inf"],
)
def test_normalize_timeout_s_non_finite_raises(bad: float | str) -> None:
    """NaN / +/-Inf (and their string forms) are INVALID_ARG."""
    with pytest.raises(TransportError) as ei:
        normalize_timeout_s(bad)  # type: ignore[arg-type]
    assert ei.value.code == "INVALID_ARG"
    assert ei.value.msg == f"timeout must be a finite number, got {bad!r}"


def test_normalize_timeout_s_unparseable_raises() -> None:
    with pytest.raises(TransportError) as ei:
        normalize_timeout_s("abc")  # type: ignore[arg-type]
    assert ei.value.code == "INVALID_ARG"
    assert ei.value.msg == "timeout must be a finite number, got 'abc'"


def test_normalize_timeout_s_single_body_reexported() -> None:
    """exec_ops / ps_ops keep the old name as an alias of the shared function."""
    assert exec_ops._normalize_timeout is normalize_timeout_s
    assert ps_ops._normalize_timeout is normalize_timeout_s


_MISSING_CWD = "/no/such/dir/mrc-missing"
_JUNK_CWDS: tuple[object, ...] = ("", True, "True", "${HOME:-}", "%CD%")


@pytest.mark.parametrize("junk", _JUNK_CWDS, ids=["empty", "bool-true", "str-true", "home-probe", "cd-probe"])
def test_resolve_local_cwd_non_path_falls_back(junk: object) -> None:
    """Empty / True / unexpanded probe tokens are not explicit missing dirs."""
    got = exec_ops._resolve_local_cwd(
        requested=junk,  # type: ignore[arg-type]
        endpoint_cwd=_MISSING_CWD,
        transport_cwd=None,
        strict_requested=True,
    )
    assert Path(got).is_dir()
    assert Path(got).resolve() == Path(os.getcwd()).resolve()


def test_resolve_local_cwd_explicit_missing_raises() -> None:
    with pytest.raises(TransportError) as ei:
        exec_ops._resolve_local_cwd(
            requested=_MISSING_CWD,
            endpoint_cwd=None,
            transport_cwd=None,
            strict_requested=True,
        )
    assert ei.value.code == "INVALID_CWD"
    assert "does not exist" in ei.value.msg or "not a directory" in ei.value.msg


def test_resolve_cwd_non_path_requested_uses_fallback() -> None:
    """_resolve_cwd must not treat non-path requested as strict-missing."""

    class _Local:
        name = "local"
        cwd = os.getcwd()
        home = os.getcwd()

    transport = _Local()
    for junk in _JUNK_CWDS:
        got = exec_ops._resolve_cwd(
            requested=junk,  # type: ignore[arg-type]
            endpoint_cwd=_MISSING_CWD,
            transport=transport,  # type: ignore[arg-type]
        )
        assert got is not None
        assert Path(got).is_dir()


def test_resolve_cwd_explicit_missing_raises() -> None:
    class _Local:
        name = "local"
        cwd = os.getcwd()
        home = os.getcwd()

    with pytest.raises(TransportError) as ei:
        exec_ops._resolve_cwd(
            requested=_MISSING_CWD,
            endpoint_cwd=None,
            transport=_Local(),  # type: ignore[arg-type]
        )
    assert ei.value.code == "INVALID_CWD"

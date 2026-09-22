"""CLI isomorphic tool subcommands."""

from __future__ import annotations

import io
import json
import os
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_remote_control.cli import main
from mcp_remote_control.cli_cmds import (
    EXIT_OK,
    EXIT_TRANSPORT,
    EXIT_USAGE,
    EXIT_VALIDATION,
)
from mcp_remote_control.core import console_ops, endpoint_ops, fs_ops
from mcp_remote_control.core.result import OpResult
from mcp_remote_control.endpoint import reset_registry
from mcp_remote_control.screen.registry import reset_screen_registry
from mcp_remote_control.serial.handle import SerialConsole
from mcp_remote_control.serial.ports import SerialConsoleInfo
from mcp_remote_control.serial.registry import reset_serial_registry

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"

TOOLS = ("endpoint", "exec", "fs", "screen", "ps", "console", "config")


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_registry()
    reset_screen_registry()
    yield
    reset_screen_registry()
    reset_registry()


@pytest.mark.parametrize("tool", TOOLS)
def test_tool_help_exits_zero(tool: str) -> None:
    with pytest.raises(SystemExit) as ei:
        main([tool, "--help"])
    assert ei.value.code == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["endpoint", "list", "--help"],
        ["endpoint", "open", "--help"],
        ["endpoint", "close", "--help"],
        ["fs", "list", "--help"],
        ["screen", "open", "--help"],
        ["screen", "send", "--help"],
        ["screen", "close", "--help"],
        ["screen", "list", "--help"],
        ["ps", "open", "--help"],
        ["ps", "invoke", "--help"],
        ["ps", "close", "--help"],
        ["console", "list", "--help"],
        ["console", "open", "--help"],
        ["console", "views", "--help"],
        ["config", "ensure-home", "--help"],
        ["config", "put-profile", "--help"],
        ["config", "put-secret", "--help"],
        ["config", "notes", "--help"],
    ],
)
def test_nested_op_help(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as ei:
        main(argv)
    assert ei.value.code == 0


def test_endpoint_list_agent_text(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["endpoint", "list"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("@endpoint ok")
    assert not out.lstrip().startswith("{")


def test_endpoint_list_json_local_flag(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["endpoint", "list", "--json"])
    assert code == EXIT_OK
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["kind"] == "endpoint"
    assert data["status"] == "ok"


def test_endpoint_list_json_global_flag(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--json", "endpoint", "list"])
    assert code == EXIT_OK
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["kind"] == "endpoint"
    assert data["status"] == "ok"


def test_exec_local_command(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["exec", "--ep", "local", "--command", "echo hi"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("@exec ok")
    assert "hi" in out
    assert "cwd=" in out


def test_fs_list_local(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["fs", "list", "--ep", "local", "--path", "/tmp"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("@fs list ok")
    assert "path=" in out


def test_fs_read_max_bytes_forwarded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``fs read --max-bytes N`` reaches fs_ops.run(max_bytes=N).

    Without CLI exposure, read budget stays at Core default (1MiB) even when
    callers need a smaller or larger budget (MCP already exposes max_bytes).
    """
    captured: dict[str, object] = {}

    def _capture(*, op: str, **kwargs: object) -> OpResult:
        captured["op"] = op
        captured["kwargs"] = kwargs
        return OpResult(
            kind="fs",
            status="ok",
            fields={"op": "read", "path": "/tmp/x", "content": "ok"},
        )

    monkeypatch.setattr(fs_ops, "run", _capture)
    code = main(
        [
            "fs",
            "read",
            "--ep",
            "local",
            "--path",
            "/tmp/x",
            "--max-bytes",
            "64",
        ]
    )
    assert code == EXIT_OK
    assert captured["op"] == "read"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("max_bytes") == 64
    assert kwargs.get("ep") == "local"
    assert kwargs.get("path") == "/tmp/x"


def test_fs_read_max_bytes_default_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Omitting --max-bytes leaves max_bytes=None for Core default read budget."""
    captured: dict[str, object] = {}

    def _capture(*, op: str, **kwargs: object) -> OpResult:
        captured["op"] = op
        captured["kwargs"] = kwargs
        return OpResult(
            kind="fs",
            status="ok",
            fields={"op": "read", "path": "/tmp/x", "content": "ok"},
        )

    monkeypatch.setattr(fs_ops, "run", _capture)
    code = main(["fs", "read", "--ep", "local", "--path", "/tmp/x"])
    assert code == EXIT_OK
    assert captured["op"] == "read"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("max_bytes") is None


def test_screen_list_and_ps_local_unsupported(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # screen list is implemented (empty registry -> ok n=0).
    assert main(["screen", "list"]) == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("@screen ok")
    assert "n=0" in out

    # ps on local -> CAP_DENIED (caps.ps=false; winrm-only).
    assert main(["ps", "open", "--ep", "local"]) == EXIT_VALIDATION
    out = capsys.readouterr().out
    assert "@ps" in out
    assert "CAP_DENIED" in out
    assert "lacks ps capability" in out.lower()


def test_endpoint_missing_op_usage(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["endpoint"])
    assert code == EXIT_USAGE
    err = capsys.readouterr().err
    assert "list" in err or "OP" in err or "usage" in err.lower()


def test_main_help_lists_tools(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as ei:
        main(["--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    for tool in TOOLS:
        assert tool in out


def test_exec_timeout_help_wall_clock_semantics(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI ``exec --help`` states timeout = local wait wall-clock.

    Searchable cues: wall-clock, remote cancel (not guaranteed), close+reopen.
    Must not steer toward security hardening.
    """
    with pytest.raises(SystemExit) as ei:
        main(["exec", "--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    lower = out.lower()
    assert "--timeout" in out
    assert "wall-clock" in lower
    assert (
        "remote cancel" in lower
        or "cannot guarantee" in lower
        or "not remote kill" in lower
        or "not a remote kill" in lower
    )
    assert (
        "close+reopen" in lower
        or "close then open" in lower
        or ("close" in lower and "reopen" in lower)
    )
    assert "hardeni" not in lower
    assert "redact" not in lower
    assert "0/omit" not in out
    assert "0 = unlimited" not in lower
    assert "INVALID_ARG" in out
    assert "unlimited" in lower


def test_ps_invoke_timeout_help_wall_clock_semantics(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI ``ps invoke --help`` keeps wall-clock + close+reopen cues."""
    with pytest.raises(SystemExit) as ei:
        main(["ps", "invoke", "--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    lower = out.lower()
    assert "--timeout" in out
    assert "wall-clock" in lower
    assert "remote cancel" in lower or "cannot guarantee" in lower
    assert "close+reopen" in lower or "close then open" in lower
    assert "0/omit" not in out
    assert "0 = unlimited" not in lower
    assert "INVALID_ARG" in out
    assert "unlimited" in lower


def test_ps_help_process_local_wording(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``ps`` / ``ps open`` / ``ps invoke`` help state process-local sessions.

    Cross-process CLI chains (open in one process, invoke in another) always
    hit PS_NOT_FOUND; help must say so so agents do not treat it as a bug.

    Argparse may soft-wrap ``process-local`` as ``process-\\nlocal``; normalize
    before matching so the cue is not lost to help formatter line breaks.
    """
    for argv in (
        ["ps", "--help"],
        ["ps", "open", "--help"],
        ["ps", "invoke", "--help"],
    ):
        with pytest.raises(SystemExit) as ei:
            main(argv)
        assert ei.value.code == 0
        out = capsys.readouterr().out
        # Argparse soft-wraps ``process-local`` as ``process-\\nlocal``;
        # rejoin the hyphenated break, then collapse whitespace.
        norm = " ".join(out.lower().replace("-\n", "-").split())
        assert "process-local" in norm or "process local" in norm, (argv, out)
        assert (
            "same process" in norm
            or "\u540c\u8fdb\u7a0b" in out
            or "one process" in norm
            or "this same process" in norm
        ), (argv, out)
        assert "ps_not_found" in norm or "ps-not-found" in norm, (argv, out)


def test_fs_help_timeout_semantics(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI ``fs --help`` mentions WinRM oneshot wall-clock / close+reopen."""
    with pytest.raises(SystemExit) as ei:
        main(["fs", "--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    lower = out.lower()
    assert "wall-clock" in lower
    assert "remote cancel" in lower or "not guaranteed" in lower
    assert "close+reopen" in lower or ("close" in lower and "reopen" in lower)


def test_exec_ops_timeout_hint_wall_clock_close_reopen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core exec timeout OpResult carries wall-clock / close+reopen hint."""
    from mcp_remote_control.core import exec_ops
    from mcp_remote_control.endpoint.registry import ensure_endpoint
    from mcp_remote_control.transport.base import ExecResult

    ep = ensure_endpoint("local", home=FIXTURES)
    assert ep.transport is not None

    class _TimeoutTransport:
        def __init__(self, real: object) -> None:
            self._real = real

        def __getattr__(self, name: str) -> object:
            return getattr(self._real, name)

        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            del command, timeout_s, env
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr="",
                cwd=cwd,
                timed_out=True,
            )

    ep.transport = _TimeoutTransport(ep.transport)  # type: ignore[assignment]
    # Keep registry clean if something else re-opens.
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))

    r = exec_ops.run(
        ep="local",
        command="sleep 99",
        timeout=0.1,
        home=FIXTURES,
    )
    assert r.status == "timeout"
    hint = (r.hint or "").lower()
    assert "wall-clock" in hint
    assert "remote" in hint and ("cancel" in hint or "kill" in hint)
    assert "close" in hint and ("reopen" in hint or "open" in hint)


# ---------------------------------------------------------------------------
# console + config CLI surface (param pass-through, hyphenated help,
# put-secret stdin, dead transport code removed)
# ---------------------------------------------------------------------------


class _FakeSer:
    """Minimal thread-safe serial mock for the CapturePump background thread."""

    def __init__(self) -> None:
        self.is_open = True
        self._lock = threading.Lock()
        self._rx = bytearray()

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._rx)

    def read(self, n: int) -> bytes:
        with self._lock:
            chunk = bytes(self._rx[:n])
            del self._rx[:n]
            return chunk

    def write(self, data: bytes) -> int:
        return len(data)

    def close(self) -> None:
        self.is_open = False


def _patch_console_open(monkeypatch: pytest.MonkeyPatch, fake: _FakeSer) -> None:
    """Redirect ``console_ops.SerialConsole`` to wrap *fake*."""

    def _open(port: str, baudrate: int = 115200, **_kw: object) -> SerialConsole:
        return SerialConsole(port, baudrate=baudrate, serial_factory=lambda: fake)

    monkeypatch.setattr(console_ops, "SerialConsole", _open)


def test_console_list_happy_mocked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reset_serial_registry()
    fake_ports = [
        SerialConsoleInfo(device="COM9", name="COM9", description="USB", link="usb"),
    ]
    monkeypatch.setattr(
        console_ops, "list_serial_consoles", lambda **_kw: fake_ports
    )
    assert main(["console", "list"]) == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("@console ok")
    assert "COM9" in out


def test_console_open_happy_mocked_serial(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reset_serial_registry()
    _patch_console_open(monkeypatch, _FakeSer())
    code = main(["console", "open", "--path", "COM9", "--baud", "115200"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("@console ok")
    # Core must render ``path=COM9``; a weak ``or "COM9"`` fallback would
    # hide a missing path= field.
    assert "path=COM9" in out
    # sessions shows the open console
    assert main(["console", "sessions"]) == EXIT_OK
    out2 = capsys.readouterr().out
    assert out2.startswith("@console ok")


def test_console_open_device_alias_works_like_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--device`` is an argparse alias for ``--path`` (console open).

    The parser registers ``--path``/``--device`` on the same dest, so a caller
    using ``--device COM9`` must reach Core with ``path=COM9`` and render the
    same Agent track as ``--path COM9``. Pins the alias so a future parser
    refactor that drops ``--device`` fails here rather than silently at the
    host.
    """
    reset_serial_registry()
    _patch_console_open(monkeypatch, _FakeSer())
    code = main(["console", "open", "--device", "COM9", "--baud", "115200"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("@console ok")
    assert "path=COM9" in out


def test_console_open_error_path_mocked_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reset_serial_registry()

    def _boom(port: str, baudrate: int = 115200, **_kw: object) -> SerialConsole:
        raise OSError("device busy")

    monkeypatch.setattr(console_ops, "SerialConsole", _boom)
    code = main(["console", "open", "--path", "COM9"])
    assert code == EXIT_VALIDATION
    out = capsys.readouterr().out
    assert "@console" in out
    assert "CONSOLE_OPEN_FAILED" in out or "error" in out.lower()


def test_console_missing_op_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["console"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "list" in err or "OP" in err or "usage" in err.lower()


def test_console_views_max_lines_forwarded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``console views --max-lines N`` reaches console_ops.run(max_lines=N).

    Without CLI exposure, views hard_cap stays at DEFAULT_VIEWS_MAX_LINES
    (2000) even when callers need a larger body.
    """
    captured: dict[str, object] = {}

    def _capture(*, op: str, **kwargs: object) -> OpResult:
        captured["op"] = op
        captured["kwargs"] = kwargs
        return OpResult(
            kind="console",
            status="ok",
            fields={"op": "views", "id": "con_x", "n": 0, "body": ""},
        )

    monkeypatch.setattr(console_ops, "run", _capture)
    code = main(
        [
            "console",
            "views",
            "--id",
            "con_x",
            "--mode",
            "tail",
            "--n",
            "50",
            "--max-lines",
            "5000",
        ]
    )
    assert code == EXIT_OK
    assert captured["op"] == "views"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("max_lines") == 5000
    assert kwargs.get("id") == "con_x"
    assert kwargs.get("mode") == "tail"
    assert kwargs.get("n") == 50


def test_console_views_max_lines_default_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Omitting --max-lines leaves max_lines=None for Core default (2000)."""
    captured: dict[str, object] = {}

    def _capture(*, op: str, **kwargs: object) -> OpResult:
        captured["op"] = op
        captured["kwargs"] = kwargs
        return OpResult(
            kind="console",
            status="ok",
            fields={"op": "views", "id": "con_x", "n": 0, "body": ""},
        )

    monkeypatch.setattr(console_ops, "run", _capture)
    code = main(["console", "views", "--id", "con_x", "--mode", "tail"])
    assert code == EXIT_OK
    assert captured["op"] == "views"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("max_lines") is None


def test_console_views_settle_ms_argparse_default_none() -> None:
    """--settle-ms has no implicit 50; argparse default is None."""
    from mcp_remote_control.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["console", "views", "--id", "con_x"])
    assert getattr(args, "settle_ms", "missing") is None


def test_console_views_settle_ms_omit_forwards_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting --settle-ms forwards None so Core uses 0 (same as MCP)."""
    captured: dict[str, object] = {}

    def _capture(*, op: str, **kwargs: object) -> OpResult:
        captured["op"] = op
        captured["kwargs"] = kwargs
        return OpResult(
            kind="console",
            status="ok",
            fields={"op": "views", "id": "con_x", "n": 0, "body": ""},
        )

    monkeypatch.setattr(console_ops, "run", _capture)
    code = main(["console", "views", "--id", "con_x", "--mode", "tail"])
    assert code == EXIT_OK
    assert captured["op"] == "views"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("settle_ms") is None


def test_console_views_settle_ms_explicit_50_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--settle-ms 50 is forwarded; it is not an implicit default."""
    captured: dict[str, object] = {}

    def _capture(*, op: str, **kwargs: object) -> OpResult:
        captured["op"] = op
        captured["kwargs"] = kwargs
        return OpResult(
            kind="console",
            status="ok",
            fields={"op": "views", "id": "con_x", "n": 0, "body": ""},
        )

    monkeypatch.setattr(console_ops, "run", _capture)
    code = main(
        [
            "console",
            "views",
            "--id",
            "con_x",
            "--mode",
            "tail",
            "--settle-ms",
            "50",
        ]
    )
    assert code == EXIT_OK
    assert captured["op"] == "views"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("settle_ms") == 50


def test_config_put_profile_roundtrip_with_winrm_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI put-profile forwards --winrm-json/--defaults-json/--caps-json to Core."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    reset_registry()

    assert main(["config", "ensure-home"]) == EXIT_OK

    code = main(
        [
            "config", "put-profile",
            "--name", "win",
            "--transport", "winrm",
            "--host", "h",
            "--username", "u",
            "--auth-json", '{"method":"password","password_path":"secrets/p"}',
            "--winrm-json", '{"scheme":"https"}',
            "--defaults-json", '{"cwd":"C:/x"}',
            "--caps-json", '{"ps":true}',
        ]
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "@config ok" in out
    assert "name=win" in out

    # Verify the profile TOML was written with the winrm/defaults/caps blocks.
    assert (home / "profiles" / "win.toml").is_file()
    toml_text = (home / "profiles" / "win.toml").read_text()
    assert "[winrm]" in toml_text
    assert "scheme = \"https\"" in toml_text
    assert "[defaults]" in toml_text
    # Defaults block must round-trip the actual value, not just be present
    # (regression: a [defaults] header with a wrong/missing cwd key would
    # satisfy the old ``"[defaults]" in toml_text`` assertion while silently
    # dropping the value the caller passed).
    assert "cwd = \"C:/x\"" in toml_text
    assert "[caps]" in toml_text
    assert "ps = true" in toml_text

    # get-profile surfaces the winrm/caps blocks on the Agent track.
    assert main(["config", "get-profile", "--name", "win"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "winrm.scheme" in out
    assert "caps.ps" in out
    # Defaults block must round-trip on the Agent track too: the parsed
    # ``defaults.cwd`` line carries the value we wrote, not just the key.
    assert "defaults.cwd" in out, out
    assert "defaults.cwd=C:/x" in out, out


def test_config_put_profile_bad_json_error_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed --winrm-json -> Core PROFILE_INVALID -> EXIT_VALIDATION."""
    monkeypatch.setenv("MRC_HOME", str(tmp_path / "mrc"))
    reset_registry()
    assert main(["config", "ensure-home"]) == EXIT_OK
    code = main(
        [
            "config", "put-profile",
            "--name", "bad",
            "--transport", "winrm",
            "--host", "h",
            "--username", "u",
            "--winrm-json", "{not valid json",
        ]
    )
    assert code == EXIT_VALIDATION
    out = capsys.readouterr().out
    assert "@config" in out
    assert "error" in out.lower() or "PROFILE_INVALID" in out


def test_config_put_secret_content_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """put-secret --content-stdin reads body from stdin (not argv)."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    reset_registry()
    assert main(["config", "ensure-home"]) == EXIT_OK

    monkeypatch.setattr(sys, "stdin", io.StringIO("TOPSECRET"))
    code = main(["config", "put-secret", "--name", "k", "--content-stdin"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "@config ok" in out
    assert "path=secrets/k" in out
    # Body never echoed in the response ...
    assert "TOPSECRET" not in out
    # ... but is written verbatim to the secret file.
    assert (home / "secrets" / "k").read_text() == "TOPSECRET"


def test_config_put_secret_content_file_happy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """put-secret --content-file <existing> reads body from file (not argv/stdin).

    Mirrors ``test_config_put_secret_content_stdin``'s write-and-assert pattern
    on the ``--content-file`` happy path: a temp file with secret content is
    written, the CLI reads it verbatim, and the secret file under MRC_HOME
    matches the temp file byte-for-byte (utf-8). Pins the path that was
    previously entirely untested.
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    reset_registry()
    assert main(["config", "ensure-home"]) == EXIT_OK

    payload = "FILESECRET_BODY\nline2\n"
    secret_src = tmp_path / "secret_payload.txt"
    secret_src.write_text(payload)

    code = main(
        [
            "config", "put-secret",
            "--name", "k",
            "--content-file", str(secret_src),
        ]
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "@config ok" in out
    assert "path=secrets/k" in out
    # Body never echoed in the response ...
    assert "FILESECRET_BODY" not in out
    # ... but is written verbatim to the secret file (round-trip equality).
    assert (home / "secrets" / "k").read_text() == payload


def test_config_put_secret_content_file_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """put-secret --content-file <missing> -> INVALID_ARG + EXIT_VALIDATION, no traceback.

    ``_resolve_secret_content`` must not leak a raw ``FileNotFoundError``
    through ``cli.main`` as an unhandled traceback (exit 1). The CLI surfaces
    a clean ``@config error code=INVALID_ARG`` OpResult and returns
    ``EXIT_VALIDATION`` (3) - matching how ``--winrm-json`` bad-JSON is handled.
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    reset_registry()
    assert main(["config", "ensure-home"]) == EXIT_OK

    missing = tmp_path / "does_not_exist.txt"
    assert not missing.exists()
    code = main(
        [
            "config", "put-secret",
            "--name", "k",
            "--content-file", str(missing),
        ]
    )
    assert code == EXIT_VALIDATION
    out = capsys.readouterr().out
    assert "@config" in out
    assert "error" in out.lower()
    assert "INVALID_ARG" in out
    # The error message names the offending path (mirrors config_ops'
    # CONFIG_WRITE_FAILED ``{type(exc).__name__}: {exc}`` style).
    assert "content-file" in out
    # Unreadable --content-file must surface as a structured error, not a
    # Python traceback from main (exception type name in the message is fine).
    assert "Traceback" not in out
    assert " most recent" not in out.lower()


def test_config_put_secret_no_content_source_missing_arg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """put-secret with no --content/--content-stdin/--content-file -> MISSING_ARG + EXIT_VALIDATION.

    Argparse's mutually exclusive group allows none to be set, so
    ``_resolve_secret_content`` returns None and Core's ``op_put_secret``
    reports ``MISSING_ARG`` (name and content required). Pins the none-given
    path so a future refactor that silently defaults content cannot pass.
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    reset_registry()
    assert main(["config", "ensure-home"]) == EXIT_OK

    code = main(["config", "put-secret", "--name", "k"])
    assert code == EXIT_VALIDATION
    out = capsys.readouterr().out
    assert "@config" in out
    assert "MISSING_ARG" in out
    assert "content" in out.lower()


def test_config_put_secret_content_mutex_argparse_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--content + --content-stdin together -> argparse mutex error -> SystemExit(EXIT_USAGE=2).

    The put-secret source group is mutually exclusive; argparse prints usage to
    stderr and exits 2 (EXIT_USAGE) when two sources are given. Pins the mutex
    so a future parser change that drops the group would fail here.
    """
    monkeypatch.setenv("MRC_HOME", str(tmp_path / "mrc"))
    reset_registry()
    with pytest.raises(SystemExit) as ei:
        main(
            [
                "config", "put-secret",
                "--name", "k",
                "--content", "X",
                "--content-stdin",
            ]
        )
    assert ei.value.code == EXIT_USAGE


def test_config_help_uses_hyphenated_subcommands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """config --help must list hyphenated subcommand names (not underscores)."""
    with pytest.raises(SystemExit) as ei:
        main(["config", "--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "ensure-home" in out
    assert "put-profile" in out
    assert "list-profiles" in out
    assert "put-secret" in out
    assert "notes" in out
    # Underscored forms must NOT appear as subcommand names.
    assert "ensure_home" not in out
    assert "list_profiles" not in out
    # Do not steer agents into shell-editing the config home.
    lower = out.lower()
    assert "cat ~/.config" not in lower
    assert "edit ~/.config" not in lower
    assert "vim " not in lower
    assert "nano " not in lower


def test_config_missing_op_usage_uses_hyphenated_names(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bare ``config`` (no OP) usage lists hyphenated ops, not underscores."""
    assert main(["config"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "ensure-home" in err
    assert "put-profile" in err
    assert "list-profiles" in err
    assert "put-secret" in err
    assert "notes" in err
    assert "ensure_home" not in err
    assert "list_profiles" not in err
    assert "missing OP" in err or "OP" in err


def test_config_notes_help_lists_actions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``config notes --help`` lists action verbs; does not point at ~/.config."""
    with pytest.raises(SystemExit) as ei:
        main(["config", "notes", "--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    lower = out.lower()
    assert "--action" in out
    assert "--name" in out
    assert "--content" in out
    for action in ("read", "write", "append", "prepend", "stat", "rm"):
        assert action in lower
    assert "cat ~/.config" not in lower
    assert "edit ~/.config" not in lower


def test_config_notes_write_read_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI ``config notes`` write/stat/read is isomorphic to MCP config op=notes."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    reset_registry()
    assert main(["config", "ensure-home"]) == EXIT_OK
    assert main(["config", "put-profile", "--name", "box", "--transport", "local"]) == (
        EXIT_OK
    )
    capsys.readouterr()

    body = "## dirs\n/opt/app\n"
    code = main(
        [
            "config",
            "notes",
            "--action",
            "write",
            "--name",
            "box",
            "--content",
            body,
        ]
    )
    assert code == EXIT_OK
    write_out = capsys.readouterr().out
    assert "@config ok" in write_out
    assert "action=write" in write_out
    assert "name=box" in write_out
    assert (home / "notes" / "box.md").read_text(encoding="utf-8") == body
    # Successful write is status fields only; the notes body is not echoed.
    assert "/opt/app" not in write_out
    assert "## dirs" not in write_out
    assert body not in write_out

    assert main(["config", "notes", "--action", "stat", "--name", "box"]) == EXIT_OK
    stat_out = capsys.readouterr().out
    assert "@config ok" in stat_out
    assert "action=stat" in stat_out
    assert "bytes=" in stat_out
    assert "/opt/app" not in stat_out
    assert "## dirs" not in stat_out

    assert main(["config", "notes", "--action", "read", "--name", "box"]) == EXIT_OK
    read_out = capsys.readouterr().out
    assert "@config ok" in read_out
    assert "action=read" in read_out
    assert "/opt/app" in read_out
    assert "## dirs" in read_out


def test_config_notes_action_prepend_parse_args() -> None:
    """``--action prepend`` is an argparse choice, not merely a help substring."""
    from mcp_remote_control.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        ["config", "notes", "--action", "prepend", "--name", "x"]
    )
    assert args.action == "prepend"
    assert args.name == "x"
    assert args.config_op == "notes"


def test_config_notes_unknown_action_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown ``--action`` is argparse usage (EXIT_USAGE), not a Core op."""
    with pytest.raises(SystemExit) as ei:
        main(
            [
                "config",
                "notes",
                "--action",
                "splice",
                "--name",
                "x",
            ]
        )
    assert ei.value.code == EXIT_USAGE
    err = capsys.readouterr().err.lower()
    assert "invalid choice" in err or "choose from" in err


def test_config_notes_append_prepend_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI append/prepend concat in place; stdout does not echo the fragment."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    reset_registry()
    assert main(["config", "ensure-home"]) == EXIT_OK
    assert main(["config", "put-profile", "--name", "box", "--transport", "local"]) == (
        EXIT_OK
    )
    capsys.readouterr()

    mid = "CLI-NOTES-MID"
    head = "CLI-PREPEND-HEAD"
    tail = "CLI-APPEND-TAIL"
    assert main(
        [
            "config",
            "notes",
            "--action",
            "write",
            "--name",
            "box",
            "--content",
            mid,
        ]
    ) == EXIT_OK
    write_out = capsys.readouterr().out
    assert mid not in write_out

    assert main(
        [
            "config",
            "notes",
            "--action",
            "prepend",
            "--name",
            "box",
            "--content",
            head,
        ]
    ) == EXIT_OK
    prepend_out = capsys.readouterr().out
    assert "@config ok" in prepend_out
    assert "action=prepend" in prepend_out
    assert head not in prepend_out
    assert tail not in prepend_out

    assert main(
        [
            "config",
            "notes",
            "--action",
            "append",
            "--name",
            "box",
            "--content",
            tail,
        ]
    ) == EXIT_OK
    append_out = capsys.readouterr().out
    assert "@config ok" in append_out
    assert "action=append" in append_out
    assert tail not in append_out
    assert head not in append_out

    notes_path = home / "notes" / "box.md"
    assert notes_path.read_text(encoding="utf-8") == head + mid + tail
    assert main(["config", "notes", "--action", "read", "--name", "box"]) == EXIT_OK
    read_out = capsys.readouterr().out
    assert head in read_out
    assert mid in read_out
    assert tail in read_out


def test_config_notes_write_empty_content_truncates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``write --content ''`` truncates to a zero-byte notes file."""
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    reset_registry()
    assert main(["config", "ensure-home"]) == EXIT_OK
    assert main(["config", "put-profile", "--name", "box", "--transport", "local"]) == (
        EXIT_OK
    )
    assert main(
        [
            "config",
            "notes",
            "--action",
            "write",
            "--name",
            "box",
            "--content",
            "keep-until-truncate",
        ]
    ) == EXIT_OK
    capsys.readouterr()

    assert main(
        [
            "config",
            "notes",
            "--action",
            "write",
            "--name",
            "box",
            "--content",
            "",
        ]
    ) == EXIT_OK
    empty_out = capsys.readouterr().out
    assert "@config ok" in empty_out
    assert "action=write" in empty_out
    assert "keep-until-truncate" not in empty_out

    notes_path = home / "notes" / "box.md"
    assert notes_path.is_file()
    assert notes_path.stat().st_size == 0
    assert notes_path.read_bytes() == b""

    assert main(["config", "notes", "--action", "read", "--name", "box"]) == EXIT_OK
    read_out = capsys.readouterr().out
    assert "@config ok" in read_out
    assert "keep-until-truncate" not in read_out


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission bits gate unlink only for a non-root POSIX user",
)
def test_config_delete_profile_notes_cleanup_failure_no_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unremovable notes file must not turn the CLI into a traceback.

    Core reports the failed notes cleanup as an error OpResult, so the CLI
    exits non-zero with a rendered message and the abs config path stays out
    of it.
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    reset_registry()
    assert main(["config", "ensure-home"]) == EXIT_OK
    assert main(["config", "put-profile", "--name", "box", "--transport", "local"]) == (
        EXIT_OK
    )
    assert main(
        [
            "config",
            "notes",
            "--action",
            "write",
            "--name",
            "box",
            "--content",
            "keep-me",
        ]
    ) == EXIT_OK
    capsys.readouterr()

    os.chmod(home / "notes", 0o500)
    try:
        code = main(["config", "delete-profile", "--name", "box"])
    finally:
        os.chmod(home / "notes", 0o700)

    captured = capsys.readouterr()
    both = captured.out + captured.err
    assert code != EXIT_OK, both
    assert code == EXIT_VALIDATION, both
    assert "Traceback" not in both, both
    assert "notes/box.md" in both, both
    assert str(home.resolve()) not in both, both
    # The profile was deleted; only the notes cleanup failed.
    assert not (home / "profiles" / "box.toml").exists()
    assert (home / "notes" / "box.md").is_file()


def test_endpoint_connect_failed_removed_from_transport_codes() -> None:
    """Dead ENDPOINT_CONNECT_FAILED code must be gone from _TRANSPORT_CODES."""
    from mcp_remote_control.cli_cmds.tools import _TRANSPORT_CODES

    assert "ENDPOINT_CONNECT_FAILED" not in _TRANSPORT_CODES
    assert "CONNECT_FAILED" in _TRANSPORT_CODES
    assert "NOT_CONNECTED" in _TRANSPORT_CODES


# ---------------------------------------------------------------------------
# EXIT_TRANSPORT=4 mapping for transport-code OpResults (cli_cmds/tools._exit_for)
# ---------------------------------------------------------------------------


def test_transport_code_opresult_maps_to_exit_transport(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A Core OpResult with a transport code (CONNECT_FAILED) must surface as
    EXIT_TRANSPORT (4), NOT EXIT_VALIDATION (3). ``_exit_for`` checks
    ``_TRANSPORT_CODES`` after the ``is_ok`` branch - pin the distinction.
    """
    from mcp_remote_control.cli_cmds.tools import _TRANSPORT_CODES

    # Sanity: CONNECT_FAILED is in the transport set the CLI uses.
    assert "CONNECT_FAILED" in _TRANSPORT_CODES

    def _boom(**_kw: object) -> OpResult:
        return OpResult(
            kind="endpoint",
            status="error",
            code="CONNECT_FAILED",
            fields={
                "op": "open",
                "profile": "lab-ssh",
                "msg": "Connection refused",
            },
        )

    monkeypatch.setattr(endpoint_ops, "run", _boom)
    code = main(["endpoint", "open", "--profile", "lab-ssh"])
    assert code == EXIT_TRANSPORT, (
        f"transport-code OpResult must map to EXIT_TRANSPORT ({EXIT_TRANSPORT}), "
        f"got {code}"
    )
    out = capsys.readouterr().out
    # Rendered Agent track must surface the kind and the transport code.
    assert out.startswith("@endpoint error")
    assert "CONNECT_FAILED" in out


def test_non_transport_error_opresult_maps_to_exit_validation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-transport error code (MISSING_ARG) must surface as
    EXIT_VALIDATION (3), NOT EXIT_TRANSPORT (4). Pins the validation/transport
    distinction in ``_exit_for``.
    """
    from mcp_remote_control.cli_cmds.tools import _TRANSPORT_CODES

    # Sanity: MISSING_ARG is NOT in the transport set.
    assert "MISSING_ARG" not in _TRANSPORT_CODES

    def _validation_fail(**_kw: object) -> OpResult:
        return OpResult(
            kind="endpoint",
            status="error",
            code="MISSING_ARG",
            fields={"op": "open", "msg": "profile name required"},
        )

    monkeypatch.setattr(endpoint_ops, "run", _validation_fail)
    code = main(["endpoint", "open", "--profile", "lab-ssh"])
    assert code == EXIT_VALIDATION, (
        f"non-transport error code must map to EXIT_VALIDATION ({EXIT_VALIDATION}), "
        f"got {code}"
    )
    out = capsys.readouterr().out
    assert out.startswith("@endpoint error")
    assert "MISSING_ARG" in out


@pytest.mark.parametrize(
    "code,expected",
    [
        ("CONNECT_FAILED", EXIT_TRANSPORT),
        ("HOSTKEY_MISMATCH", EXIT_TRANSPORT),
        ("AUTH_FAILED", EXIT_TRANSPORT),
        ("NOT_CONNECTED", EXIT_TRANSPORT),
        ("INVALID_ARG", EXIT_VALIDATION),
        ("PROFILE_NOT_FOUND", EXIT_VALIDATION),
        ("PROFILE_INVALID", EXIT_VALIDATION),
        ("MISSING_ARG", EXIT_VALIDATION),
    ],
)
def test_exit_for_dispatch_table(
    code: str, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the full transport/validation dispatch for every error code we map.

    Guards against a future refactor that drops a transport code or moves a
    validation code into the transport bucket. Each row asserts the exact
    exit-code for an OpResult carrying that code.
    """
    from mcp_remote_control.cli_cmds.tools import _exit_for

    result = OpResult(
        kind="endpoint",
        status="error",
        code=code,
        fields={"op": "open", "msg": f"forced {code}"},
    )
    assert _exit_for(result) == expected, (
        f"_exit_for(code={code!r}) returned {_exit_for(result)}, expected {expected}"
    )
    # Also exercise end-to-end via main() so the wiring stays covered.
    monkeypatch.setattr(
        endpoint_ops,
        "run",
        lambda **_kw: OpResult(
            kind="endpoint",
            status="error",
            code=code,
            fields={"op": "open", "msg": f"forced {code}"},
        ),
    )
    assert main(["endpoint", "open", "--profile", "lab-ssh"]) == expected

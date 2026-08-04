"""Unit tests for local + ssh + winrm transports (T06/T07/T12)."""

from __future__ import annotations

import os
import shlex
import time
from pathlib import Path

import pytest

from mcp_remote_control.transport import (
    ExecResult,
    LocalTransport,
    SSHTransport,
    TransportError,
)
from mcp_remote_control.transport.winrm import WinRMTransport


def test_local_connect_close() -> None:
    t = LocalTransport()
    assert t.name == "local"
    assert t.is_connected() is False
    t.connect()
    assert t.is_connected() is True
    assert t.cwd is not None
    t.close()
    assert t.is_connected() is False


def test_local_run_command_and_argv() -> None:
    t = LocalTransport()
    t.connect()
    r = t.run_command("echo unit-cmd")
    assert r.exit_code == 0
    assert "unit-cmd" in r.stdout
    assert r.cwd is not None
    r2 = t.run_argv(["/bin/echo", "unit-argv"])
    assert r2.exit_code == 0
    assert "unit-argv" in r2.stdout


def test_local_run_timeout() -> None:
    t = LocalTransport()
    t.connect()
    r = t.run_command("sleep 5", timeout_s=0.15)
    assert r.timed_out is True
    assert r.exit_code == -1


def test_ssh_mock_connector_success() -> None:
    class Conn:
        pass

    seen: dict[str, object] = {}

    def connector(**kwargs: object) -> Conn:
        seen.update(kwargs)
        return Conn()

    t = SSHTransport(
        host="10.0.0.5",
        port=22,
        username="deploy",
        client_keys=["/tmp/fake"],
        connector=connector,
    )
    t.connect()
    assert t.is_connected()
    assert seen["host"] == "10.0.0.5"
    assert seen["username"] == "deploy"
    t.close()
    assert t.is_connected() is False


def test_ssh_connector_failure_is_connect_failed() -> None:
    def connector(**_kwargs: object) -> None:
        raise ConnectionRefusedError("nope")

    t = SSHTransport(host="h", username="u", connector=connector)
    with pytest.raises(TransportError) as ei:
        t.connect()
    assert ei.value.code == "CONNECT_FAILED"
    assert t.is_connected() is False


def test_ssh_transport_error_passthrough() -> None:
    def connector(**_kwargs: object) -> None:
        raise TransportError("AUTH_FAILED", "keys rejected")

    t = SSHTransport(host="h", username="u", connector=connector)
    with pytest.raises(TransportError) as ei:
        t.connect()
    assert ei.value.code == "AUTH_FAILED"


def test_ssh_mock_run_command() -> None:
    class Conn:
        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            return ExecResult(exit_code=0, stdout=f"out:{command}", cwd=cwd or "/r")

    def connector(**_kwargs: object) -> Conn:
        return Conn()

    t = SSHTransport(host="h", username="u", connector=connector)
    t.connect()
    r = t.run_command("uname -a", cwd="/opt")
    assert r.exit_code == 0
    assert r.stdout == "out:uname -a"
    assert r.cwd == "/opt"


# ---------------------------------------------------------------------------
# WinRM (T12)
# ---------------------------------------------------------------------------


def test_winrm_mock_connector_success() -> None:
    class Sess:
        cwd = r"C:\Users\Admin"
        home = r"C:\Users\Admin"
        os = "windows"
        shell = "powershell"
        ps_version = "5.1"

        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            return ExecResult(exit_code=0, stdout=f"ps:{command}", cwd=cwd or self.cwd)

    seen: dict[str, object] = {}

    def connector(**kwargs: object) -> Sess:
        seen.update({k: v for k, v in kwargs.items() if k != "password"})
        return Sess()

    t = WinRMTransport(
        host="10.0.0.20",
        port=5985,
        username="Administrator",
        password="secret-should-not-appear",
        connector=connector,
    )
    t.connect()
    assert t.is_connected()
    assert seen["host"] == "10.0.0.20"
    assert seen["username"] == "Administrator"
    assert t.cwd == r"C:\Users\Admin"
    assert "password" not in repr(t)
    assert "secret" not in repr(t)
    r = t.run_command("Get-Date", cwd=r"C:\Temp")
    assert r.exit_code == 0
    assert r.stdout == "ps:Get-Date"
    assert r.cwd == r"C:\Temp"
    probe = t.collect_probe()
    assert probe.get("os") == "windows"
    assert probe.get("status") == "ok"
    t.close()
    assert t.is_connected() is False


def test_winrm_connector_failure_is_connect_failed() -> None:
    def connector(**_kwargs: object) -> None:
        raise ConnectionRefusedError("winrm port closed")

    t = WinRMTransport(host="h", username="u", connector=connector)
    with pytest.raises(TransportError) as ei:
        t.connect()
    assert ei.value.code == "CONNECT_FAILED"
    assert t.is_connected() is False


def test_winrm_auth_failed_passthrough() -> None:
    def connector(**_kwargs: object) -> None:
        raise TransportError("AUTH_FAILED", "bad password")

    t = WinRMTransport(host="h", username="u", connector=connector)
    with pytest.raises(TransportError) as ei:
        t.connect()
    assert ei.value.code == "AUTH_FAILED"


def test_winrm_auth_heuristic_from_generic_exc() -> None:
    def connector(**_kwargs: object) -> None:
        raise RuntimeError("401 Unauthorized: credentials rejected")

    t = WinRMTransport(host="h", username="u", connector=connector)
    with pytest.raises(TransportError) as ei:
        t.connect()
    assert ei.value.code == "AUTH_FAILED"


def test_winrm_mock_run_argv() -> None:
    class Sess:
        def run_argv(
            self,
            argv: list[str],
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            return ExecResult(exit_code=0, stdout="|".join(argv), cwd=cwd or "C:\\")

    t = WinRMTransport(
        host="h",
        username="u",
        connector=lambda **_k: Sess(),
    )
    t.connect()
    r = t.run_argv(["ipconfig", "/all"])
    assert r.exit_code == 0
    assert r.stdout == "ipconfig|/all"


def test_winrm_not_connected_exec() -> None:
    t = WinRMTransport(host="h", username="u", connector=lambda **_k: object())
    with pytest.raises(TransportError) as ei:
        t.run_command("x")
    assert ei.value.code == "NOT_CONNECTED"


# ---------------------------------------------------------------------------
# O5: PowerShell run_argv call operator, collect_probe Windows retry,
#     bridge-level timeout pass-through, local process-group kill.
# ---------------------------------------------------------------------------


class _RecordingBridge:
    """Duck-typed AsyncLoopBridge that records timeout_s and never runs a loop.

    First ``run`` call returns a fresh ``Conn`` (for ``connect``); later calls
    return an ``ExecResult`` (for ``run``/``run_argv``). Coroutines are closed
    so no "coroutine never awaited" warning leaks.
    """

    def __init__(self, conn_factory: type) -> None:
        self.calls: list[float | None] = []
        self._n = 0
        self._conn_factory = conn_factory

    def start(self) -> None:
        pass

    def run(self, coro: object, timeout_s: float | None = None) -> object:  # type: ignore[no-untyped-def]
        self.calls.append(timeout_s)
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        self._n += 1
        if self._n == 1:
            return self._conn_factory()
        return ExecResult(exit_code=0, stdout="", cwd=None)


def _async_connector(conn_factory: type) -> object:
    """Return an async connector whose result is a coroutine (exercises bridge)."""

    async def connector(**_kwargs: object) -> object:
        return conn_factory()

    return connector


class _RaisingBridge:
    """Like _RecordingBridge but raises *exc* on the Nth run() call (1-based).

    First call returns a fresh Conn (for connect); calls before the failure
    return an ExecResult. The coroutine is always closed so no "coroutine
    never awaited" warning leaks. Used to simulate AsyncLoopBridge's own
    TimeoutError vs asyncssh's internal timeout.
    """

    def __init__(self, conn_factory: type, fail_on_n: int, exc: BaseException) -> None:
        self.calls: list[float | None] = []
        self._n = 0
        self._conn_factory = conn_factory
        self._fail_on_n = fail_on_n
        self._exc = exc

    def start(self) -> None:
        pass

    def run(self, coro: object, timeout_s: float | None = None) -> object:  # type: ignore[no-untyped-def]
        self.calls.append(timeout_s)
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        self._n += 1
        if self._n == self._fail_on_n:
            raise self._exc
        if self._n == 1:
            return self._conn_factory()
        return ExecResult(exit_code=0, stdout="", cwd=None)


def test_ssh_run_argv_powershell_uses_call_operator() -> None:
    """run_argv fallback must prefix `&` so PowerShell invokes the command."""
    captured: dict[str, str] = {}

    class Conn:
        def run(self, command: str, **_kwargs: object) -> object:
            captured["command"] = command

            async def coro() -> ExecResult:
                return ExecResult(exit_code=0, stdout="", cwd=None)

            return coro()

    t = SSHTransport(
        host="h",
        username="u",
        remote_shell_family="powershell",
        connector=_async_connector(Conn),  # type: ignore[arg-type]
        bridge=_RecordingBridge(Conn),  # type: ignore[arg-type]
    )
    t.connect()
    t.run_argv(["echo", "hello"])
    # Adjacent single-quoted strings are separate expressions in PowerShell;
    # the call operator `&` is required to invoke the command.
    assert captured["command"] == "& 'echo' 'hello'"


def test_ssh_run_argv_powershell_script_form_invokable() -> None:
    """The pwsh -Command script argv stays invokable via the call operator."""
    captured: dict[str, str] = {}

    class Conn:
        def run(self, command: str, **_kwargs: object) -> object:
            captured["command"] = command

            async def coro() -> ExecResult:
                return ExecResult(exit_code=0, stdout="", cwd=None)

            return coro()

    t = SSHTransport(
        host="h",
        username="u",
        remote_shell_family="powershell",
        connector=_async_connector(Conn),  # type: ignore[arg-type]
        bridge=_RecordingBridge(Conn),  # type: ignore[arg-type]
    )
    t.connect()
    # build_script_argv produces this for runtime="pwsh".
    t.run_argv(["pwsh", "-NoProfile", "-NonInteractive", "-Command", "Get-Date"])
    assert captured["command"] == (
        "& 'pwsh' '-NoProfile' '-NonInteractive' '-Command' 'Get-Date'"
    )


def test_ssh_run_argv_cmd_family_unchanged() -> None:
    """cmd family still uses double-quoted form (no call operator)."""
    captured: dict[str, str] = {}

    class Conn:
        def run(self, command: str, **_kwargs: object) -> object:
            captured["command"] = command

            async def coro() -> ExecResult:
                return ExecResult(exit_code=0, stdout="", cwd=None)

            return coro()

    t = SSHTransport(
        host="h",
        username="u",
        remote_shell_family="cmd",
        connector=_async_connector(Conn),  # type: ignore[arg-type]
        bridge=_RecordingBridge(Conn),  # type: ignore[arg-type]
    )
    t.connect()
    t.run_argv(["echo", "hello world"])
    assert captured["command"] == '"echo" "hello world"'


def test_ssh_collect_probe_windows_retry_on_cmd_echoed_posix() -> None:
    """cmd.exe echoing the POSIX probe verbatim (exit 0, os=posix) must still
    trigger the Windows retry and set remote_shell_family to a windows family."""
    calls: list[str] = []

    class Conn:
        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            calls.append(command)
            if "uname=" in command:
                # cmd.exe echoes POSIX syntax verbatim, including `os=posix`.
                return ExecResult(
                    exit_code=0,
                    stdout=(
                        "uname=$(uname -s 2>/dev/null)-$(uname -m 2>/dev/null)\n"
                        "shell_path=\n"
                        "os=posix\n"
                    ),
                    cwd=None,
                )
            # WINDOWS_PROBE_SCRIPT — credible windows output.
            return ExecResult(
                exit_code=0,
                stdout=(
                    "os=windows\n"
                    "comspec=C:\\Windows\\system32\\cmd.exe\n"
                    "home=C:\\Users\\admin\n"
                    "pwd=C:\\Users\\admin\n"
                    "Active code page: 936\n"
                ),
                cwd=None,
            )

    t = SSHTransport(
        host="h",
        username="u",
        remote_shell_family="posix",
        connector=lambda **_k: Conn(),
    )
    t.connect()
    out = t.collect_probe()
    assert out.get("os") == "windows"
    assert out.get("status") == "ok"
    # remote_shell_family must be a windows family, not posix.
    assert t.remote_shell_family in ("cmd", "powershell")
    assert t.remote_shell_family != "posix"
    # Both probes actually ran.
    assert any("uname=" in c for c in calls)
    assert any("os=windows" in c for c in calls)


def test_ssh_collect_probe_posix_host_not_misclassified_as_windows() -> None:
    """A genuine POSIX host must not be misclassified when the Windows probe
    is echoed back with literal %COMSPEC% placeholders."""
    calls: list[str] = []

    class Conn:
        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            calls.append(command)
            if "uname=" in command:
                # Real POSIX probe output.
                return ExecResult(
                    exit_code=0,
                    stdout=(
                        "uname=Linux-x86_64\n"
                        "shell_path=/bin/bash\n"
                        "home=/home/u\n"
                        "pwd=/home/u\n"
                        "os=posix\n"
                    ),
                    cwd=None,
                )
            # Windows probe run on a POSIX shell: echoes os=windows but with
            # unexpanded %COMSPEC% and no chcp.
            return ExecResult(
                exit_code=0,
                stdout=(
                    "os=windows\n"
                    "comspec=%COMSPEC%\n"
                    "home=%USERPROFILE%\n"
                    "pwd=%CD%\n"
                ),
                stderr="chcp: command not found\n",
                cwd=None,
            )

    t = SSHTransport(
        host="h",
        username="u",
        remote_shell_family="posix",
        connector=lambda **_k: Conn(),
    )
    t.connect()
    out = t.collect_probe()
    # The echoed windows parse is not credible → keep posix.
    assert out.get("os") == "posix"
    assert t.remote_shell_family == "posix"


def test_ssh_collect_probe_hardfail_posix_fish_not_misclassified_as_cmd() -> None:
    """A POSIX host whose login shell uses non-POSIX command substitution
    (fish/csh/tcsh) hard-fails the POSIX probe (non-zero exit, no uname) and
    then echoes the Windows probe's `os=windows`/`comspec=%COMSPEC%` literals
    verbatim (no `%` expansion, no chcp). The hard-fail branch must NOT adopt
    that non-credible Windows parse — remote_shell_family must stay posix-ish,
    not cmd/powershell — so cwd wrap + run_argv remain valid on that shell.
    """
    calls: list[str] = []

    class Conn:
        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            calls.append(command)
            if "uname=" in command:
                # fish/csh/tcsh: `$(uname ...)` is a syntax error → abort,
                # empty output, non-zero exit (hard-fail first branch).
                return ExecResult(
                    exit_code=1,
                    stdout="",
                    stderr="Syntax error: '(' unexpected",
                    cwd=None,
                )
            # WINDOWS_PROBE_SCRIPT on fish/csh: `&` is background, the
            # literals echo verbatim with no `%` expansion and no chcp.
            return ExecResult(
                exit_code=0,
                stdout=(
                    "os=windows\n"
                    "comspec=%COMSPEC%\n"
                    "home=%USERPROFILE%\n"
                    "pwd=%CD%\n"
                ),
                cwd=None,
            )

    t = SSHTransport(
        host="h",
        username="u",
        remote_shell_family="posix",
        connector=lambda **_k: Conn(),
    )
    t.connect()
    out = t.collect_probe()
    # Both probes ran (hard-fail first branch).
    assert any("uname=" in c for c in calls)
    assert any("os=windows" in c for c in calls)
    # The echoed Windows parse is non-credible → NOT adopted. The host is
    # NOT misclassified as windows/cmd. (status stays "ok" — parse_probe_output
    # always populates caps/dialect/shell_family via _enrich_dialect whenever
    # text is non-empty, so `not parsed` is False and the partial marker does
    # not fire; this mirrors the elif-branch test, which also does not assert
    # partial. The load-bearing assertion is that remote_shell_family is NOT
    # a windows family.)
    assert out.get("os") != "windows"
    assert out.get("shell_base") not in ("cmd", "powershell", "pwsh")
    assert out.get("comspec") != "%COMSPEC%"
    # remote_shell_family must NOT be a windows family — stays posix-ish.
    assert t.remote_shell_family not in ("cmd", "powershell")
    assert t.remote_shell_family == "posix"


def test_ssh_collect_probe_hardfail_windows_credible_adopts_cmd() -> None:
    """A genuine Windows host whose POSIX probe hard-fails (non-zero exit,
    no uname) but whose Windows probe returns a credible COMSPEC path + real
    chcp must still adopt the windows family. The hard-fail branch's new
    credible-windows gate must not reject genuine Windows hosts.
    """
    calls: list[str] = []

    class Conn:
        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            calls.append(command)
            if "uname=" in command:
                # POSIX probe on cmd.exe: `$(...)` not recognized → non-zero
                # exit, no uname (hard-fail first branch).
                return ExecResult(
                    exit_code=1,
                    stdout="",
                    stderr="'$(' is not recognized as an internal command.",
                    cwd=None,
                )
            # WINDOWS_PROBE_SCRIPT on real cmd.exe: expands %COMSPEC%, real chcp.
            return ExecResult(
                exit_code=0,
                stdout=(
                    "os=windows\n"
                    "comspec=C:\\Windows\\system32\\cmd.exe\n"
                    "home=C:\\Users\\admin\n"
                    "pwd=C:\\Users\\admin\n"
                    "Active code page: 936\n"
                ),
                cwd=None,
            )

    t = SSHTransport(
        host="h",
        username="u",
        remote_shell_family="posix",
        connector=lambda **_k: Conn(),
    )
    t.connect()
    out = t.collect_probe()
    assert any("uname=" in c for c in calls)
    assert any("os=windows" in c for c in calls)
    # Credible Windows parse → adopted.
    assert out.get("os") == "windows"
    assert out.get("status") == "ok"
    assert out.get("comspec") == "C:\\Windows\\system32\\cmd.exe"
    assert out.get("chcp") == 936
    # remote_shell_family must be a windows family.
    assert t.remote_shell_family in ("cmd", "powershell")
    assert t.remote_shell_family != "posix"


def test_ssh_connect_passes_bridge_timeout() -> None:
    """connect() must forward a belt-and-suspenders deadline to the bridge."""

    class Conn:
        pass

    bridge = _RecordingBridge(Conn)  # type: ignore[arg-type]
    t = SSHTransport(
        host="h",
        username="u",
        connector=_async_connector(Conn),  # type: ignore[arg-type]
        bridge=bridge,  # type: ignore[arg-type]
    )
    t.connect()
    # default connect_timeout_ms=15000 → 15.0s + 5.0s grace = 20.0s
    assert bridge.calls == [20.0]


def test_default_ssh_connector_returns_awaitable() -> None:
    """Production default connector must hand back a coroutine, not a sync result.

    Pre-running via run_coro would finish connect before SSHTransport.connect
    can apply bridge_timeout, so the wall-clock deadline would never fire.
    """
    import inspect

    from mcp_remote_control.transport.ssh import default_ssh_connector

    result = default_ssh_connector(
        host="127.0.0.1",
        port=22,
        username="u",
        client_keys=None,
        connect_timeout=1.0,
        known_hosts=None,
    )
    try:
        assert inspect.isawaitable(result)
        assert inspect.iscoroutine(result)
    finally:
        close = getattr(result, "close", None)
        if callable(close):
            close()


def test_default_connector_shape_passes_bridge_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default production connector path: bridge_timeout reaches the bridge."""

    class Conn:
        pass

    async def fake_asyncssh_connect(**_kwargs: object) -> Conn:
        return Conn()

    monkeypatch.setattr(
        "mcp_remote_control.transport.ssh._default_asyncssh_connect",
        fake_asyncssh_connect,
    )
    bridge = _RecordingBridge(Conn)  # type: ignore[arg-type]
    # No connector= → uses default_ssh_connector (production shape).
    t = SSHTransport(
        host="h",
        username="u",
        bridge=bridge,  # type: ignore[arg-type]
    )
    t.connect()
    assert t.is_connected()
    # connect_timeout_ms=15000 → 15.0s + 5.0s grace = 20.0s
    assert bridge.calls == [20.0]
    t.close()


def test_default_connector_bridge_timeout_maps_to_connect_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge TimeoutError on connect maps to CONNECT_FAILED (not silent success)."""

    class Conn:
        pass

    async def fake_asyncssh_connect(**_kwargs: object) -> Conn:
        return Conn()

    monkeypatch.setattr(
        "mcp_remote_control.transport.ssh._default_asyncssh_connect",
        fake_asyncssh_connect,
    )
    exc = TimeoutError("AsyncLoopBridge.run timed out after 20.0s")
    bridge = _RaisingBridge(Conn, fail_on_n=1, exc=exc)  # type: ignore[arg-type]
    t = SSHTransport(
        host="h",
        username="u",
        bridge=bridge,  # type: ignore[arg-type]
    )
    with pytest.raises(TransportError) as ei:
        t.connect()
    assert ei.value.code == "CONNECT_FAILED"
    assert "AsyncLoopBridge" in ei.value.msg or "timed out" in ei.value.msg.lower()
    assert t.is_connected() is False


def test_ssh_run_command_passes_bridge_timeout() -> None:
    """run_command must forward (exec_timeout + grace) to the bridge."""
    captured: dict[str, str] = {}

    class Conn:
        def run(self, command: str, **_kwargs: object) -> object:
            captured["command"] = command

            async def coro() -> ExecResult:
                return ExecResult(exit_code=0, stdout="ok", cwd="/r")

            return coro()

    bridge = _RecordingBridge(Conn)  # type: ignore[arg-type]
    t = SSHTransport(
        host="h",
        username="u",
        remote_shell_family="posix",
        connector=_async_connector(Conn),  # type: ignore[arg-type]
        bridge=bridge,  # type: ignore[arg-type]
    )
    t.connect()
    assert bridge.calls == [20.0]  # connect deadline only
    r = t.run_command("echo ok", timeout_s=10.0)
    assert r.exit_code == 0
    # connect (20.0) + run (10.0 + 5.0 grace = 15.0)
    assert bridge.calls == [20.0, 15.0]


def test_local_timeout_kills_process_group(tmp_path: Path) -> None:
    """A timeout must kill the whole process group so backgrounded children
    (grandchildren of the shell) do not survive the call."""
    if os.name != "posix":
        pytest.skip("process-group kill is POSIX-only")
    pidfile = tmp_path / "child.pid"
    t = LocalTransport()
    t.connect()
    # Background a long-lived sleep, publish its PID, then block the shell in
    # `wait` so the exec timeout fires while the shell is still alive.
    cmd = f"sleep 30 & echo $! > {pidfile}; wait"
    r = t.run_command(cmd, timeout_s=1.0)
    assert r.timed_out is True
    assert r.exit_code == -1
    # The backgrounded child must not survive the process-group kill.
    assert pidfile.exists()
    child_pid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 2.0
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            alive = False
            break
        except PermissionError:
            alive = False  # reaped / no longer ours
            break
        time.sleep(0.05)
    assert not alive, f"child sleep pid {child_pid} survived timeout"


def test_local_resolve_cwd_idempotent_fast_path(tmp_path: Path) -> None:
    """An already-absolute existing dir is returned without re-resolution."""
    if os.name != "posix":
        pytest.skip("symlink fast-path check is POSIX-only")
    from mcp_remote_control.transport.local import _resolve_local_cwd

    real = str(tmp_path)
    # tmp_path is already absolute and exists → returned verbatim.
    assert _resolve_local_cwd(real) == real
    # None → getcwd (still absolute + existing).
    resolved = Path(_resolve_local_cwd(None))
    assert resolved.is_absolute() and resolved.is_dir()
    # Nonexistent absolute path still raises INVALID_CWD.
    with pytest.raises(TransportError) as ei:
        _resolve_local_cwd("/definitely/not/here/mrc-o5")
    assert ei.value.code == "INVALID_CWD"


# ---------------------------------------------------------------------------
# C3: _resolve_local_cwd fast/slow-path consistency (both use os.path.abspath,
#     neither calls Path.resolve) + Windows taskkill tree-kill.
# ---------------------------------------------------------------------------


def test_local_resolve_cwd_fast_slow_paths_use_abspath_no_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fast and slow paths of _resolve_local_cwd must both return the lexical
    os.path.abspath form and never call Path.resolve (no symlink-following).

    This keeps them consistent: an already-absolute existing dir returned by
    exec_ops._resolve_cwd is a no-op when handed back through run_command,
    and a relative-path resolution does not silently canonicalize through
    a symlinked parent (e.g. /var → /private/var on macOS).
    """
    if os.name != "posix":
        pytest.skip("symlink semantics checked on POSIX only")
    from mcp_remote_control.transport import local as local_mod
    from mcp_remote_control.transport.local import _resolve_local_cwd

    resolve_calls = [0]
    orig_resolve = Path.resolve

    def counting_resolve(self: Path, *args: object, **kwargs: object) -> Path:
        resolve_calls[0] += 1
        return orig_resolve(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "resolve", counting_resolve)

    real = str(tmp_path)
    # Fast path: already-absolute existing dir.
    fast = _resolve_local_cwd(real)
    assert fast == os.path.abspath(real)
    # Slow path: a relative "." after chdir into tmp_path forces the slow
    # branch (the input itself is not absolute, so the fast path's
    # is_absolute() check fails).
    cwd_prev = os.getcwd()
    try:
        os.chdir(real)
        slow = _resolve_local_cwd(".")
    finally:
        os.chdir(cwd_prev)
    assert Path(slow).is_absolute() and Path(slow).is_dir()
    # On macOS where /var → /private/var, the lexical abspath of `real` is
    # /var/... (NOT the resolved /private/var/...). The slow path must
    # produce the same form as the fast path for the same dir.
    assert fast == os.path.abspath(real)
    assert resolve_calls[0] == 0, (
        f"_resolve_local_cwd called Path.resolve {resolve_calls[0]} times; "
        "both paths should use os.path.abspath (no symlink-following)"
    )
    # _kill_process_tree is unaffected by this monkeypatch; sanity reference
    # to keep the import meaningful even when the assertion above is trivial.
    assert hasattr(local_mod.LocalTransport, "_kill_process_tree")


def test_local_kill_tree_windows_uses_taskkill(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows (os.name != 'posix'), _kill_process_tree must invoke
    `taskkill /F /T /PID` to kill the whole process tree. A plain
    proc.kill() only kills the immediate child shell — grandchildren of
    the shell (e.g. `sleep 3600 &`) survive. /T walks the tree.
    """
    from mcp_remote_control.transport import local as local_mod

    calls: list[list[str]] = []

    class _FakeRunResult:
        returncode = 0

    def fake_run(args: list[str], **_kwargs: object) -> _FakeRunResult:
        calls.append(list(args))
        return _FakeRunResult()

    class FakeProc:
        pid = 4242

    monkeypatch.setattr(local_mod.os, "name", "nt")
    monkeypatch.setattr(local_mod.subprocess, "run", fake_run)

    t = local_mod.LocalTransport()
    t._kill_process_tree(FakeProc())  # type: ignore[arg-type]

    assert len(calls) == 1, f"taskkill should be called once, got {calls}"
    args = calls[0]
    assert args[0] == "taskkill"
    assert "/F" in args
    assert "/T" in args
    assert "/PID" in args
    assert "4242" in args


def test_local_kill_tree_windows_taskkill_failure_falls_back_to_proc_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If taskkill itself can't be invoked (FileNotFoundError / OSError),
    _kill_process_tree falls back to proc.kill() — best-effort tree-kill so
    at least the shell dies when taskkill is missing from PATH."""
    from mcp_remote_control.transport import local as local_mod

    killed: list[int] = []

    def fake_run(args: list[str], **_kwargs: object) -> object:
        raise FileNotFoundError("taskkill not on PATH")

    class FakeProc:
        pid = 4242

        def kill(self) -> None:
            killed.append(self.pid)

    monkeypatch.setattr(local_mod.os, "name", "nt")
    monkeypatch.setattr(local_mod.subprocess, "run", fake_run)

    t = local_mod.LocalTransport()
    t._kill_process_tree(FakeProc())  # type: ignore[arg-type]

    assert killed == [4242], (
        "proc.kill() should be invoked when taskkill raises OSError"
    )


# ---------------------------------------------------------------------------
# O5 review follow-up: bridge-timeout → mark_dead heuristic is load-bearing;
# pin it so a stale connection is not reused after a bridge deadline, and so
# asyncssh's internal timeout does NOT kill the connection.
# ---------------------------------------------------------------------------


def test_ssh_bridge_timeout_marks_connection_dead() -> None:
    """A bridge-level TimeoutError must mark the connection dead and cause the
    next run_command to raise NOT_CONNECTED (no stale reuse)."""

    class Conn:
        def run(self, command: str, **_kwargs: object) -> object:
            async def coro() -> ExecResult:
                return ExecResult(exit_code=0, stdout="", cwd=None)

            return coro()

    # fail_on_n=2 → connect (n=1) succeeds, run_command (n=2) hits the bridge
    # deadline. Message matches AsyncLoopBridge.run's real format.
    exc = TimeoutError("AsyncLoopBridge.run timed out after 15.0s")
    bridge = _RaisingBridge(Conn, fail_on_n=2, exc=exc)  # type: ignore[arg-type]
    t = SSHTransport(
        host="h",
        username="u",
        remote_shell_family="posix",
        connector=_async_connector(Conn),  # type: ignore[arg-type]
        bridge=bridge,  # type: ignore[arg-type]
    )
    t.connect()
    assert t.is_alive() is True
    r = t.run_command("echo ok", timeout_s=10.0)
    # Bridge timeout → timed_out ExecResult, non-zero exit.
    assert r.timed_out is True
    assert r.exit_code == -1
    assert "AsyncLoopBridge" in (r.stderr or "")
    # mark_dead fired: connection no longer alive, reason recorded.
    assert t.is_alive() is False
    assert t.meta.get("dead_reason") == "bridge timeout"
    # Next run_command must NOT reuse the stale handle.
    with pytest.raises(TransportError) as ei:
        t.run_command("echo again")
    assert ei.value.code == "NOT_CONNECTED"


def test_ssh_internal_timeout_does_not_mark_dead() -> None:
    """asyncssh's own command timeout (no 'AsyncLoopBridge' marker) must NOT
    mark the connection dead — asyncssh handles its own timeout cleanly, so
    the session stays usable for the next command."""

    class Conn:
        def run(self, command: str, **_kwargs: object) -> object:
            async def coro() -> ExecResult:
                return ExecResult(exit_code=0, stdout="", cwd=None)

            return coro()

    # Message has NO bridge marker → must be treated as asyncssh-internal.
    exc = TimeoutError("Channel command timed out")
    bridge = _RaisingBridge(Conn, fail_on_n=2, exc=exc)  # type: ignore[arg-type]
    t = SSHTransport(
        host="h",
        username="u",
        remote_shell_family="posix",
        connector=_async_connector(Conn),  # type: ignore[arg-type]
        bridge=bridge,  # type: ignore[arg-type]
    )
    t.connect()
    assert t.is_alive() is True
    r = t.run_command("echo slow", timeout_s=10.0)
    assert r.timed_out is True
    assert r.exit_code == -1
    # Internal timeout → connection stays alive (no mark_dead).
    assert t.is_alive() is True
    assert t.is_connected() is True
    assert "dead_reason" not in (t.meta or {})


def test_ssh_run_argv_posix_family_unchanged() -> None:
    """POSIX-family run_argv fallback uses shlex.quote and must NOT prefix the
    call operator `&` (that is PowerShell-only)."""
    captured: dict[str, str] = {}

    class Conn:
        def run(self, command: str, **_kwargs: object) -> object:
            captured["command"] = command

            async def coro() -> ExecResult:
                return ExecResult(exit_code=0, stdout="", cwd=None)

            return coro()

    t = SSHTransport(
        host="h",
        username="u",
        remote_shell_family="posix",
        connector=_async_connector(Conn),  # type: ignore[arg-type]
        bridge=_RecordingBridge(Conn),  # type: ignore[arg-type]
    )
    t.connect()
    argv = ["echo", "hello world"]
    t.run_argv(argv)
    cmd = captured["command"]
    # No call operator (PowerShell-only).
    assert not cmd.lstrip().startswith("& ")
    # shlex.quote form, not single-quoted PS literals.
    expected = " ".join(shlex.quote(str(a)) for a in argv)
    assert cmd == expected

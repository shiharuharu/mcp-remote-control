"""Unit tests for SSHTransport run/argv/probe/bridge/dispose/env/helpers."""

from __future__ import annotations

import shlex
import time

import pytest

from mcp_remote_control.transport import (
    ExecResult,
    SSHTransport,
    TransportError,
)
from mcp_remote_control.transport.ssh import (
    _has_trusted_probe_identity,
    _is_trusted_identity_value,
    _looks_credibly_windows,
)


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
# PowerShell run_argv call operator, collect_probe Windows retry,
# bridge-level timeout pass-through, local process-group kill.
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
            # WINDOWS_PROBE_SCRIPT - credible windows output.
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


def test_is_trusted_identity_value_rejects_probe_echoes() -> None:
    """Unexpanded ${...} and literal posix/windows probe labels are not identity."""
    assert _is_trusted_identity_value("${HOME:-}") is False
    assert _is_trusted_identity_value("${SHELL:-}") is False
    assert _is_trusted_identity_value("posix") is False
    assert _is_trusted_identity_value("windows") is False
    assert _is_trusted_identity_value("POSIX") is False
    assert _is_trusted_identity_value("Windows") is False
    assert _is_trusted_identity_value("%COMSPEC%") is False
    assert _is_trusted_identity_value("%USERPROFILE%") is False
    assert _is_trusted_identity_value("%CD%") is False
    assert _is_trusted_identity_value("$()") is False
    assert _is_trusted_identity_value("$(uname -s 2>/dev/null)") is False
    assert _is_trusted_identity_value("powershell") is False
    assert _is_trusted_identity_value("pwsh") is False
    assert _is_trusted_identity_value("cmd") is False
    assert _is_trusted_identity_value("command") is False
    assert _is_trusted_identity_value("Linux-x86_64") is True
    assert _is_trusted_identity_value("/home/u") is True
    assert _is_trusted_identity_value(r"C:\Users\admin") is True
    assert _is_trusted_identity_value("/bin/bash") is True


def test_has_trusted_probe_identity_echo_posix_not_identity() -> None:
    """Hardcoded os=posix plus unexpanded ${HOME:-}/${SHELL:-} is not identity."""
    assert (
        _has_trusted_probe_identity(
            {
                "os": "posix",
                "uname": "$(uname -s 2>/dev/null)-$(uname -m 2>/dev/null)",
                "home": "${HOME:-}",
                "shell_path": "${SHELL:-}",
                "pwd": "$(pwd 2>/dev/null)",
            }
        )
        is False
    )
    assert (
        _has_trusted_probe_identity(
            {
                "os": "windows",
                "home": "%USERPROFILE%",
                "pwd": "%CD%",
                "comspec": "%COMSPEC%",
            }
        )
        is False
    )
    assert (
        _has_trusted_probe_identity(
            {"os": "posix", "uname": "Linux-x86_64", "home": "/home/u"}
        )
        is True
    )
    assert (
        _has_trusted_probe_identity(
            {"os": "windows", "home": r"C:\Users\admin", "pwd": r"C:\Users\admin"}
        )
        is True
    )
    # Hardcoded PS/cmd probe labels are wrap hints, not identity.
    assert (
        _has_trusted_probe_identity(
            {"os": "windows", "shell_base": "powershell"}
        )
        is False
    )
    assert _has_trusted_probe_identity({"shell_base": "pwsh"}) is False
    assert _has_trusted_probe_identity({"shell_base": "cmd"}) is False


def test_ssh_collect_probe_cmd_echoed_posix_failed_retry_is_partial() -> None:
    """cmd.exe echoing POSIX_PROBE_SCRIPT then failing the Windows retry
    has no real identity: status=partial and remote_shell_family is not
    forced to posix (cwd wrap would use POSIX ``cd`` on cmd).
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
                # cmd.exe echoes POSIX ${...} / $(...) and `os=posix` verbatim.
                return ExecResult(
                    exit_code=0,
                    stdout=(
                        "uname=$(uname -s 2>/dev/null)-$(uname -m 2>/dev/null)\n"
                        "shell_path=${SHELL:-}\n"
                        "home=${HOME:-}\n"
                        "pwd=$(pwd 2>/dev/null)\n"
                        "os=posix\n"
                    ),
                    cwd=None,
                )
            # PowerShell / cmd retry: not a Windows shell.
            return ExecResult(
                exit_code=1,
                stdout="",
                stderr="'Write-Output' is not recognized as an internal command.",
                cwd=None,
            )

    t = SSHTransport(
        host="h",
        username="u",
        remote_shell_family="cmd",
        connector=lambda **_k: Conn(),
    )
    t.connect()
    out = t.collect_probe()
    assert any("uname=" in c for c in calls)
    assert any("os=windows" in c for c in calls)
    assert out.get("status") == "partial"
    assert _has_trusted_probe_identity(out) is False
    # Do not overwrite the existing family with posix wrap.
    assert t.remote_shell_family != "posix"
    assert t.remote_shell_family == "cmd"


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
    # The echoed windows parse is not credible -> keep posix.
    assert out.get("os") == "posix"
    assert out.get("status") == "ok"
    assert t.remote_shell_family == "posix"


def test_ssh_collect_probe_hardfail_posix_fish_not_misclassified_as_cmd() -> None:
    """A POSIX host whose login shell uses non-POSIX command substitution
    (fish/csh/tcsh) hard-fails the POSIX probe (non-zero exit, no uname) and
    then echoes the Windows probe's `os=windows`/`comspec=%COMSPEC%` literals
    verbatim (no `%` expansion, no chcp). The hard-fail branch must NOT adopt
    that non-credible Windows parse - remote_shell_family must stay posix-ish,
    not cmd/powershell - so cwd wrap + run_argv remain valid on that shell.
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
                # fish/csh/tcsh: `$(uname ...)` is a syntax error -> abort,
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
    # Echoed Windows parse is not adopted. No trusted identity field remains,
    # so status is partial (dialect/caps/shell_family are not identity).
    assert out.get("status") == "partial"
    assert out.get("os") != "windows"
    assert out.get("shell_base") not in ("cmd", "powershell", "pwsh")
    assert out.get("comspec") != "%COMSPEC%"
    # remote_shell_family must NOT be a windows family - stays posix-ish.
    assert t.remote_shell_family not in ("cmd", "powershell")
    assert t.remote_shell_family == "posix"


def test_ssh_collect_probe_empty_streams_status_partial() -> None:
    """Empty stdout+stderr still concatenates to a newline; enrich writes
    dialect/caps, but that is not identity - status must be partial."""

    class Conn:
        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            return ExecResult(exit_code=0, stdout="", stderr="", cwd=None)

    t = SSHTransport(
        host="h",
        username="u",
        remote_shell_family="posix",
        connector=lambda **_k: Conn(),
    )
    t.connect()
    out = t.collect_probe()
    assert out.get("status") == "partial"
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
                # POSIX probe on cmd.exe: `$(...)` not recognized -> non-zero
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
    # Credible Windows parse -> adopted.
    assert out.get("os") == "windows"
    assert out.get("status") == "ok"
    assert out.get("comspec") == "C:\\Windows\\system32\\cmd.exe"
    assert out.get("chcp") == 936
    # remote_shell_family must be a windows family.
    assert t.remote_shell_family in ("cmd", "powershell")
    assert t.remote_shell_family != "posix"


def test_looks_credibly_windows_requires_comspec_or_chcp() -> None:
    """shell_base / os=windows labels are not Windows evidence."""
    assert (
        _looks_credibly_windows({"os": "windows", "shell_base": "powershell"})
        is False
    )
    assert (
        _looks_credibly_windows({"os": "windows", "comspec": "%COMSPEC%"})
        is False
    )
    assert (
        _looks_credibly_windows({"os": "windows", "comspec": "cmd.exe"}) is False
    )
    assert (
        _looks_credibly_windows(
            {"os": "windows", "comspec": r"C:\Windows\system32\cmd.exe"}
        )
        is True
    )
    assert (
        _looks_credibly_windows(
            {"os": "windows", "comspec": "C:/Windows/system32/cmd.exe"}
        )
        is True
    )
    assert _looks_credibly_windows({"os": "windows", "chcp": 437}) is True
    assert (
        _looks_credibly_windows(
            {"os": "posix", "comspec": r"C:\Windows\system32\cmd.exe"}
        )
        is False
    )


def test_ssh_collect_probe_echoed_ps_shell_base_not_windows() -> None:
    """POSIX echo then PS stdout of only os=windows / shell_base=powershell
    is untrusted: no COMSPEC/chcp -> partial, not windows, not powershell wrap.
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
                return ExecResult(
                    exit_code=0,
                    stdout=(
                        "uname=$(uname -s 2>/dev/null)-$(uname -m 2>/dev/null)\n"
                        "shell_path=${SHELL:-}\n"
                        "home=${HOME:-}\n"
                        "pwd=$(pwd 2>/dev/null)\n"
                    ),
                    cwd=None,
                )
            if "Write-Output" in command:
                # Linux pwsh / POSIX host echoing the PS probe literals.
                return ExecResult(
                    exit_code=0,
                    stdout="os=windows\nshell_base=powershell\n",
                    cwd=None,
                )
            return ExecResult(
                exit_code=1,
                stdout="",
                stderr="'cmd' is not recognized",
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
    assert any("Write-Output" in c for c in calls)
    assert out.get("status") == "partial"
    assert out.get("os") != "windows"
    assert _has_trusted_probe_identity(out) is False
    assert t.remote_shell_family != "powershell"
    assert t.remote_shell_family != "pwsh"
    assert t.remote_shell_family == "posix"


def test_ssh_collect_probe_linux_pwsh_stays_posix() -> None:
    """A Linux host whose login shell is pwsh still has a real POSIX uname."""
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
                return ExecResult(
                    exit_code=0,
                    stdout=(
                        "uname=Linux-x86_64\n"
                        "shell_path=/usr/bin/pwsh\n"
                        "home=/home/u\n"
                        "pwd=/home/u\n"
                    ),
                    cwd=None,
                )
            return ExecResult(
                exit_code=0,
                stdout="os=windows\nshell_base=powershell\n",
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
    # Credible POSIX uname: do not adopt a later PS echo as Windows.
    assert not any("Write-Output" in c for c in calls)
    assert out.get("os") == "posix"
    assert out.get("status") == "ok"
    assert out.get("uname") == "Linux-x86_64"
    assert t.remote_shell_family == "posix"
    assert t.remote_shell_family != "powershell"


def test_ssh_collect_probe_ps_real_comspec_adopts_powershell() -> None:
    """PS probe with a real COMSPEC path still proves Windows + powershell wrap."""
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
                return ExecResult(
                    exit_code=0,
                    stdout=(
                        "uname=$(uname -s 2>/dev/null)-$(uname -m 2>/dev/null)\n"
                    ),
                    cwd=None,
                )
            if "Write-Output" in command:
                return ExecResult(
                    exit_code=0,
                    stdout=(
                        "os=windows\n"
                        "shell_base=powershell\n"
                        "comspec=C:\\Windows\\system32\\cmd.exe\n"
                        "home=C:\\Users\\admin\n"
                        "pwd=C:\\Users\\admin\n"
                    ),
                    cwd=None,
                )
            return ExecResult(exit_code=1, stdout="", stderr="", cwd=None)

    t = SSHTransport(
        host="h",
        username="u",
        remote_shell_family="posix",
        connector=lambda **_k: Conn(),
    )
    t.connect()
    out = t.collect_probe()
    assert any("uname=" in c for c in calls)
    assert any("Write-Output" in c for c in calls)
    assert out.get("os") == "windows"
    assert out.get("status") == "ok"
    assert out.get("comspec") == r"C:\Windows\system32\cmd.exe"
    assert out.get("shell_base") == "powershell"
    assert t.remote_shell_family == "powershell"


def test_ssh_collect_probe_ps_chcp_adopts_windows() -> None:
    """Active code page (chcp) still proves Windows when COMSPEC is absent."""
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
                return ExecResult(exit_code=1, stdout="", stderr="", cwd=None)
            if "Write-Output" in command:
                return ExecResult(
                    exit_code=0,
                    stdout=(
                        "os=windows\n"
                        "shell_base=powershell\n"
                        "home=C:\\Users\\admin\n"
                        "pwd=C:\\Users\\admin\n"
                        "Active code page: 437\n"
                    ),
                    cwd=None,
                )
            return ExecResult(exit_code=1, stdout="", stderr="", cwd=None)

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
    assert out.get("chcp") == 437
    assert t.remote_shell_family == "powershell"


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
    # default connect_timeout_ms=15000 -> 15.0s + 5.0s grace = 20.0s
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
    # No connector= -> uses default_ssh_connector (production shape).
    t = SSHTransport(
        host="h",
        username="u",
        bridge=bridge,  # type: ignore[arg-type]
    )
    t.connect()
    assert t.is_connected()
    # connect_timeout_ms=15000 -> 15.0s + 5.0s grace = 20.0s
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


# ---------------------------------------------------------------------------
# Bridge-timeout -> mark_dead is load-bearing: a stale connection must not be
# reused after a bridge deadline, and asyncssh's internal timeout must not
# kill the connection.
# ---------------------------------------------------------------------------


def test_ssh_bridge_timeout_marks_connection_dead() -> None:
    """A bridge-level TimeoutError must mark the connection dead and cause the
    next run_command to raise NOT_CONNECTED (no stale reuse)."""

    class Conn:
        def run(self, command: str, **_kwargs: object) -> object:
            async def coro() -> ExecResult:
                return ExecResult(exit_code=0, stdout="", cwd=None)

            return coro()

    # fail_on_n=2 -> connect (n=1) succeeds, run_command (n=2) hits the bridge
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
    # Bridge timeout -> timed_out ExecResult, non-zero exit.
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
    mark the connection dead - asyncssh handles its own timeout cleanly, so
    the session stays usable for the next command."""

    class Conn:
        def run(self, command: str, **_kwargs: object) -> object:
            async def coro() -> ExecResult:
                return ExecResult(exit_code=0, stdout="", cwd=None)

            return coro()

    # Message has NO bridge marker -> must be treated as asyncssh-internal.
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
    # Internal timeout -> connection stays alive (no mark_dead).
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


# ---------------------------------------------------------------------------
# dispose / SFTP teardown pass finite bridge timeout_s
# ---------------------------------------------------------------------------


def test_ssh_dispose_wait_closed_passes_finite_timeout() -> None:
    """close() -> wait_closed must forward _DISPOSE_TIMEOUT_S (not None)."""
    from mcp_remote_control.transport.ssh import _DISPOSE_TIMEOUT_S

    class Conn:
        def close(self) -> None:
            pass

        def wait_closed(self) -> object:
            async def done() -> None:
                return None

            return done()

    class DisposeBridge:
        def __init__(self) -> None:
            self.calls: list[float | None] = []
            self._n = 0

        def start(self) -> None:
            pass

        def run(self, coro: object, timeout_s: float | None = None) -> object:
            self.calls.append(timeout_s)
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            self._n += 1
            if self._n == 1:
                return Conn()
            return None

    bridge = DisposeBridge()
    t = SSHTransport(
        host="h",
        username="u",
        connector=_async_connector(Conn),  # type: ignore[arg-type]
        bridge=bridge,  # type: ignore[arg-type]
    )
    t.connect()
    # connect budget first; dispose path is the next awaitable.
    assert bridge.calls == [20.0]
    t.close()
    assert len(bridge.calls) >= 2
    assert bridge.calls[-1] == _DISPOSE_TIMEOUT_S
    assert bridge.calls[-1] is not None


def test_ssh_sftp_clear_passes_finite_timeout() -> None:
    """invalidate_sftp -> SFTP exit/close must forward _DISPOSE_TIMEOUT_S."""
    from mcp_remote_control.transport.ssh import _DISPOSE_TIMEOUT_S

    class Conn:
        pass

    class HangishSftp:
        def exit(self) -> object:
            async def done() -> None:
                return None

            return done()

    class SftpBridge:
        def __init__(self) -> None:
            self.calls: list[float | None] = []
            self._n = 0

        def start(self) -> None:
            pass

        def run(self, coro: object, timeout_s: float | None = None) -> object:
            self.calls.append(timeout_s)
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            self._n += 1
            if self._n == 1:
                return Conn()
            return None

    bridge = SftpBridge()
    t = SSHTransport(
        host="h",
        username="u",
        connector=_async_connector(Conn),  # type: ignore[arg-type]
        bridge=bridge,  # type: ignore[arg-type]
    )
    t.connect()
    t._sftp = HangishSftp()
    t.invalidate_sftp()
    assert t._sftp is None
    assert bridge.calls[-1] == _DISPOSE_TIMEOUT_S
    assert bridge.calls[-1] is not None


def test_ssh_mark_dead_sftp_clear_passes_finite_timeout() -> None:
    """mark_dead -> _clear_sftp_cache exit/close forwards _DISPOSE_TIMEOUT_S."""
    from mcp_remote_control.transport.ssh import _DISPOSE_TIMEOUT_S

    class Conn:
        pass

    class HangishSftp:
        def exit(self) -> object:
            async def done() -> None:
                return None

            return done()

    class SftpBridge:
        def __init__(self) -> None:
            self.calls: list[float | None] = []
            self._n = 0

        def start(self) -> None:
            pass

        def run(self, coro: object, timeout_s: float | None = None) -> object:
            self.calls.append(timeout_s)
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            self._n += 1
            if self._n == 1:
                return Conn()
            return None

    bridge = SftpBridge()
    t = SSHTransport(
        host="h",
        username="u",
        connector=_async_connector(Conn),  # type: ignore[arg-type]
        bridge=bridge,  # type: ignore[arg-type]
    )
    t.connect()
    first_conn = t.connection
    t._sftp = HangishSftp()
    t.mark_dead("peer_reset")
    assert t._sftp is None
    assert t.is_connected() is False
    assert bridge.calls[-1] == _DISPOSE_TIMEOUT_S
    assert bridge.calls[-1] is not None
    # _conn retained until connect/close dispose.
    assert t.connection is first_conn


# ---------------------------------------------------------------------------
# SSH exec timeout terminates remote process/channel (LocalTransport parity)
# ---------------------------------------------------------------------------


def test_ssh_exec_timeout_closes_remote_process() -> None:
    """Process wait timeout must best-effort terminate/close the remote process.

    Aligns with LocalTransport TimeoutExpired -> process-tree kill. Uses a real
    AsyncLoopBridge so create_process + wait actually run.
    """
    import asyncio

    from mcp_remote_control.transport.async_bridge import AsyncLoopBridge

    closed: list[str] = []

    class Proc:
        def __init__(self) -> None:
            self.exit_status: int | None = None
            self.stdout = b""
            self.stderr = b""

        async def wait(
            self, check: bool = False, timeout: float | None = None
        ) -> object:
            del check
            if timeout is not None:
                try:
                    await asyncio.wait_for(asyncio.sleep(30.0), timeout=timeout)
                except TimeoutError as exc:
                    raise TimeoutError("Channel command timed out") from exc
            await asyncio.sleep(30.0)
            self.exit_status = 0
            return self

        def terminate(self) -> None:
            closed.append("terminate")

        def close(self) -> None:
            closed.append("close")

    class Conn:
        async def create_process(self, command: str, **_kwargs: object) -> Proc:
            del command
            return Proc()

    bridge = AsyncLoopBridge()
    try:

        async def connector(**_kwargs: object) -> Conn:
            return Conn()

        t = SSHTransport(
            host="h",
            username="u",
            remote_shell_family="posix",
            connector=connector,  # type: ignore[arg-type]
            bridge=bridge,
        )
        t.connect()
        r = t.run_command("sleep 3600", timeout_s=0.1)
        assert r.timed_out is True
        assert r.exit_code == -1
        # Process handle must be torn down (terminate and/or close).
        assert "terminate" in closed or "close" in closed, f"closed={closed}"
        # Internal wait timeout (no AsyncLoopBridge marker) keeps session alive.
        assert t.is_alive() is True
        assert "dead_reason" not in (t.meta or {})
    finally:
        bridge.stop()


def test_ssh_bridge_timeout_closes_process_when_wait_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge wall-clock timeout must close a process whose wait ignores budget.

    Also asserts mark_dead when the TimeoutError message is from AsyncLoopBridge
    (connection may be stale after mid-flight cancel).
    """
    import asyncio

    from mcp_remote_control.transport import ssh as ssh_mod
    from mcp_remote_control.transport.async_bridge import AsyncLoopBridge

    # Shrink grace so the test does not wait full 5s belt-and-suspenders.
    monkeypatch.setattr(ssh_mod, "_BRIDGE_TIMEOUT_GRACE_S", 0.08)

    closed: list[str] = []

    class HangProc:
        def __init__(self) -> None:
            self.exit_status: int | None = None
            self.stdout = b""
            self.stderr = b""

        async def wait(
            self, check: bool = False, timeout: float | None = None
        ) -> object:
            # Ignore timeout= - simulate a stuck cancel / non-cooperative wait.
            del check, timeout
            await asyncio.sleep(30.0)
            return self

        def terminate(self) -> None:
            closed.append("terminate")

        def close(self) -> None:
            closed.append("close")

    class Conn:
        async def create_process(self, command: str, **_kwargs: object) -> HangProc:
            del command
            return HangProc()

    bridge = AsyncLoopBridge()
    try:

        async def connector(**_kwargs: object) -> Conn:
            return Conn()

        t = SSHTransport(
            host="h",
            username="u",
            remote_shell_family="posix",
            connector=connector,  # type: ignore[arg-type]
            bridge=bridge,
        )
        t.connect()
        r = t.run_command("sleep 3600", timeout_s=0.05)
        assert r.timed_out is True
        assert r.exit_code == -1
        assert "AsyncLoopBridge" in (r.stderr or "")
        assert "terminate" in closed or "close" in closed, f"closed={closed}"
        # Bridge cancel may leave the session stale -> mark_dead.
        assert t.is_alive() is False
        assert t.meta.get("dead_reason") == "bridge timeout"
    finally:
        bridge.stop()


def test_ssh_exec_success_does_not_close_process() -> None:
    """Non-timeout success path must not invoke terminate/close on the process."""
    import asyncio

    from mcp_remote_control.transport.async_bridge import AsyncLoopBridge

    closed: list[str] = []

    class QuickProc:
        def __init__(self) -> None:
            self.exit_status = 0
            self.stdout = b"ok\n"
            self.stderr = b""

        async def wait(
            self, check: bool = False, timeout: float | None = None
        ) -> object:
            del check, timeout
            await asyncio.sleep(0)
            return self

        def terminate(self) -> None:
            closed.append("terminate")

        def close(self) -> None:
            closed.append("close")

    class Conn:
        async def create_process(self, command: str, **_kwargs: object) -> QuickProc:
            del command
            return QuickProc()

    bridge = AsyncLoopBridge()
    try:

        async def connector(**_kwargs: object) -> Conn:
            return Conn()

        t = SSHTransport(
            host="h",
            username="u",
            remote_shell_family="posix",
            connector=connector,  # type: ignore[arg-type]
            bridge=bridge,
        )
        t.connect()
        r = t.run_command("echo ok", timeout_s=5.0)
        assert r.timed_out is False
        assert r.exit_code == 0
        assert "ok" in r.stdout
        assert closed == [], f"success path must not close process; got {closed}"
        assert t.is_alive() is True
    finally:
        bridge.stop()


def test_ssh_run_fallback_bridge_timeout_still_timed_out() -> None:
    """Regression: run()-only conn still maps bridge timeout -> timed_out + mark_dead."""

    class Conn:
        def run(self, command: str, **_kwargs: object) -> object:
            async def coro() -> ExecResult:
                return ExecResult(exit_code=0, stdout="", cwd=None)

            return coro()

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
    r = t.run_command("echo ok", timeout_s=10.0)
    assert r.timed_out is True
    assert r.exit_code == -1
    assert t.is_alive() is False
    assert t.meta.get("dead_reason") == "bridge timeout"


# ---------------------------------------------------------------------------
# SSH env must reach conn.run / create_process (or UNSUPPORTED) - never drop
# ---------------------------------------------------------------------------


def test_ssh_run_kwargs_env_via_varkw_asyncssh_shape() -> None:
    """asyncssh-like run(*args, check=, timeout=, **kwargs) must forward env.

    Real asyncssh.SSHClientConnection.run has no named ``env`` param; env is
    accepted only via **kwargs. Gating on ``'env' in sig_params`` would
    silently discard the dict.
    """
    from mcp_remote_control.transport.ssh import _ssh_run_kwargs

    def run(
        *args: object,
        check: bool = False,
        timeout: float | None = None,
        **kwargs: object,
    ) -> None:
        del args, check, timeout, kwargs

    kw = _ssh_run_kwargs(run, timeout_s=1.0, env={"FOO": "bar"})
    assert kw.get("env") == {"FOO": "bar"}
    assert kw.get("check") is False
    assert kw.get("timeout") == 1.0


def test_ssh_run_kwargs_env_none_unchanged() -> None:
    """env=None must not inject an env key (behavior preserved)."""
    from mcp_remote_control.transport.ssh import _ssh_run_kwargs

    def run(
        *args: object,
        check: bool = False,
        timeout: float | None = None,
        **kwargs: object,
    ) -> None:
        del args, check, timeout, kwargs

    kw = _ssh_run_kwargs(run, timeout_s=None, env=None)
    assert "env" not in kw


def test_ssh_run_kwargs_env_unsupported_without_accept() -> None:
    """Fixed signature without env / **kwargs -> UNSUPPORTED (fail-closed)."""
    from mcp_remote_control.transport.ssh import _ssh_run_kwargs

    def run(command: str, *, check: bool = False, timeout: float | None = None) -> None:
        del command, check, timeout

    with pytest.raises(TransportError) as ei:
        _ssh_run_kwargs(run, timeout_s=1.0, env={"FOO": "bar"})
    assert ei.value.code == "UNSUPPORTED"
    assert "env" in ei.value.msg.lower()


def test_ssh_create_process_kwargs_env_via_varkw() -> None:
    """create_process **kwargs path must also forward env (timeout exec path)."""
    from mcp_remote_control.transport.ssh import _ssh_create_process_kwargs

    def create_process(command: str, **kwargs: object) -> None:
        del command, kwargs

    kw = _ssh_create_process_kwargs(create_process, env={"A": "1"})
    assert kw == {"env": {"A": "1"}}

    # env=None -> no key
    assert _ssh_create_process_kwargs(create_process, env=None) == {}


def test_ssh_create_process_kwargs_env_unsupported() -> None:
    """create_process fixed signature without env -> UNSUPPORTED."""
    from mcp_remote_control.transport.ssh import _ssh_create_process_kwargs

    def create_process(command: str, *, term_type: str | None = None) -> None:
        del command, term_type

    with pytest.raises(TransportError) as ei:
        _ssh_create_process_kwargs(create_process, env={"A": "1"})
    assert ei.value.code == "UNSUPPORTED"


def test_ssh_run_command_forwards_env_to_conn_run() -> None:
    """run_command(env=...) must put env in conn.run kwargs (asyncssh shape)."""
    captured: dict[str, object] = {}

    class Conn:
        # Mirror asyncssh: named check/timeout, env only via **kwargs.
        def run(
            self,
            command: str,
            *,
            check: bool = False,
            timeout: float | None = None,
            **kwargs: object,
        ) -> object:
            del check, timeout
            captured["command"] = command
            captured["kwargs"] = dict(kwargs)

            async def coro() -> ExecResult:
                return ExecResult(exit_code=0, stdout="ok", cwd="/r")

            return coro()

    t = SSHTransport(
        host="h",
        username="u",
        remote_shell_family="posix",
        connector=_async_connector(Conn),  # type: ignore[arg-type]
        bridge=_RecordingBridge(Conn),  # type: ignore[arg-type]
    )
    t.connect()
    r = t.run_command("echo ok", env={"MRC_TEST": "1"})
    assert r.exit_code == 0
    assert captured.get("kwargs", {}).get("env") == {"MRC_TEST": "1"}  # type: ignore[union-attr]


def test_ssh_run_command_env_unsupported_not_fake_success() -> None:
    """conn.run that cannot accept env must raise UNSUPPORTED (not success)."""

    class Conn:
        def run(
            self,
            command: str,
            *,
            check: bool = False,
            timeout: float | None = None,
        ) -> object:
            del command, check, timeout

            async def coro() -> ExecResult:
                return ExecResult(exit_code=0, stdout="should-not-run", cwd=None)

            return coro()

    t = SSHTransport(
        host="h",
        username="u",
        remote_shell_family="posix",
        connector=_async_connector(Conn),  # type: ignore[arg-type]
        bridge=_RecordingBridge(Conn),  # type: ignore[arg-type]
    )
    t.connect()
    with pytest.raises(TransportError) as ei:
        t.run_command("echo ok", env={"MRC_TEST": "1"})
    assert ei.value.code == "UNSUPPORTED"
    assert "env" in ei.value.msg.lower()


def test_ssh_run_command_helper_still_receives_env() -> None:
    """Explicit conn.run_command helper path must keep receiving env=."""
    seen: dict[str, object] = {}

    class Conn:
        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            seen["command"] = command
            seen["env"] = env
            del cwd, timeout_s
            return ExecResult(exit_code=0, stdout="helper", cwd=None)

    t = SSHTransport(
        host="h",
        username="u",
        remote_shell_family="posix",
        connector=lambda **_k: Conn(),
    )
    t.connect()
    r = t.run_command("echo helper", env={"H": "v"})
    assert r.exit_code == 0
    assert "helper" in r.stdout
    assert seen.get("env") == {"H": "v"}


def test_ssh_create_process_path_forwards_env() -> None:
    """When timeout uses create_process, env must reach create_process kwargs."""
    import asyncio

    from mcp_remote_control.transport.async_bridge import AsyncLoopBridge

    captured: dict[str, object] = {}

    class Proc:
        def __init__(self) -> None:
            self.exit_status = 0
            self.stdout = b"ok\n"
            self.stderr = b""

        async def wait(
            self, check: bool = False, timeout: float | None = None
        ) -> object:
            del check, timeout
            await asyncio.sleep(0)
            return self

    class Conn:
        async def create_process(
            self,
            command: str,
            **kwargs: object,
        ) -> Proc:
            del command
            captured["kwargs"] = dict(kwargs)
            return Proc()

    bridge = AsyncLoopBridge()
    try:

        async def connector(**_kwargs: object) -> Conn:
            return Conn()

        t = SSHTransport(
            host="h",
            username="u",
            remote_shell_family="posix",
            connector=connector,  # type: ignore[arg-type]
            bridge=bridge,
        )
        t.connect()
        r = t.run_command("echo ok", timeout_s=5.0, env={"CP": "1"})
        assert r.timed_out is False
        assert r.exit_code == 0
        assert captured.get("kwargs", {}).get("env") == {"CP": "1"}  # type: ignore[union-attr]
    finally:
        bridge.stop()


# ---------------------------------------------------------------------------
# run_command / run_argv helper path bridge wall-clock deadline
# ---------------------------------------------------------------------------


def test_ssh_helper_run_command_passes_bridge_timeout() -> None:
    """conn.run_command helper must forward (exec_timeout + grace) to the bridge."""
    from mcp_remote_control.transport.ssh import _BRIDGE_TIMEOUT_GRACE_S

    class Conn:
        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> object:
            del command, cwd, timeout_s, env

            async def coro() -> ExecResult:
                return ExecResult(exit_code=0, stdout="helper-ok", cwd="/r")

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
    # RecordingBridge closes the coro and returns a stub ExecResult; the
    # assertion under test is the timeout_s forwarded on the second call.
    assert bridge.calls == [20.0, 10.0 + _BRIDGE_TIMEOUT_GRACE_S]


def test_ssh_helper_run_argv_passes_bridge_timeout() -> None:
    """conn.run_argv helper must forward (exec_timeout + grace) to the bridge."""
    from mcp_remote_control.transport.ssh import _BRIDGE_TIMEOUT_GRACE_S

    class Conn:
        def run_argv(
            self,
            argv: list[str],
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> object:
            del argv, cwd, timeout_s, env

            async def coro() -> ExecResult:
                return ExecResult(exit_code=0, stdout="argv-ok", cwd="/r")

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
    r = t.run_argv(["echo", "ok"], timeout_s=3.0)
    assert r.exit_code == 0
    assert bridge.calls == [20.0, 3.0 + _BRIDGE_TIMEOUT_GRACE_S]


def test_ssh_helper_run_command_timeout_none_unbounded_bridge() -> None:
    """timeout_s=None on helper path must leave bridge deadline open (parity shell)."""

    class Conn:
        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> object:
            del command, cwd, timeout_s, env

            async def coro() -> ExecResult:
                return ExecResult(exit_code=0, stdout="nb", cwd=None)

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
    r = t.run_command("echo ok")  # timeout_s defaults to None
    assert r.exit_code == 0
    # connect has finite budget; helper await must be None (unbounded).
    assert bridge.calls == [20.0, None]


def test_ssh_helper_hung_run_command_returns_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hung async conn.run_command must not block forever when timeout_s is set."""
    import asyncio

    from mcp_remote_control.transport.async_bridge import AsyncLoopBridge
    from mcp_remote_control.transport import ssh as ssh_mod

    # Shrink grace so the wall-clock budget is short for the test.
    monkeypatch.setattr(ssh_mod, "_BRIDGE_TIMEOUT_GRACE_S", 0.08)

    class Conn:
        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> object:
            del command, cwd, timeout_s, env

            async def hang() -> ExecResult:
                await asyncio.sleep(3600.0)
                return ExecResult(exit_code=0, stdout="never", cwd=None)

            return hang()

    bridge = AsyncLoopBridge()
    try:

        async def connector(**_kwargs: object) -> Conn:
            return Conn()

        t = SSHTransport(
            host="h",
            username="u",
            remote_shell_family="posix",
            connector=connector,  # type: ignore[arg-type]
            bridge=bridge,
        )
        t.connect()
        t0 = time.monotonic()
        r = t.run_command("sleep forever", timeout_s=0.1)
        elapsed = time.monotonic() - t0
        assert r.timed_out is True
        assert r.exit_code == -1
        assert "AsyncLoopBridge" in (r.stderr or "")
        # Budget ~= 0.1 + 0.08 grace; allow generous slack for CI.
        assert elapsed < 2.0, f"helper hung past budget: elapsed={elapsed}"
        assert t.is_alive() is False
        assert t.meta.get("dead_reason") == "bridge timeout"
    finally:
        bridge.stop()


def test_ssh_helper_hung_run_argv_returns_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hung async conn.run_argv must surface timed_out within bridge budget."""
    import asyncio

    from mcp_remote_control.transport.async_bridge import AsyncLoopBridge
    from mcp_remote_control.transport import ssh as ssh_mod

    monkeypatch.setattr(ssh_mod, "_BRIDGE_TIMEOUT_GRACE_S", 0.08)

    class Conn:
        def run_argv(
            self,
            argv: list[str],
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> object:
            del argv, cwd, timeout_s, env

            async def hang() -> ExecResult:
                await asyncio.sleep(3600.0)
                return ExecResult(exit_code=0, stdout="never", cwd=None)

            return hang()

    bridge = AsyncLoopBridge()
    try:

        async def connector(**_kwargs: object) -> Conn:
            return Conn()

        t = SSHTransport(
            host="h",
            username="u",
            remote_shell_family="posix",
            connector=connector,  # type: ignore[arg-type]
            bridge=bridge,
        )
        t.connect()
        t0 = time.monotonic()
        r = t.run_argv(["sleep", "forever"], timeout_s=0.1)
        elapsed = time.monotonic() - t0
        assert r.timed_out is True
        assert r.exit_code == -1
        assert "AsyncLoopBridge" in (r.stderr or "")
        assert elapsed < 2.0, f"helper hung past budget: elapsed={elapsed}"
        assert t.is_alive() is False
        assert t.meta.get("dead_reason") == "bridge timeout"
    finally:
        bridge.stop()


def test_ssh_helper_bridge_timeout_marks_dead_via_raising_bridge() -> None:
    """Helper path: simulated AsyncLoopBridge TimeoutError -> timed_out + mark_dead."""

    class Conn:
        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> object:
            del command, cwd, timeout_s, env

            async def coro() -> ExecResult:
                return ExecResult(exit_code=0, stdout="", cwd=None)

            return coro()

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
    r = t.run_command("echo ok", timeout_s=10.0)
    assert r.timed_out is True
    assert r.exit_code == -1
    assert t.is_alive() is False
    assert t.meta.get("dead_reason") == "bridge timeout"
    with pytest.raises(TransportError) as ei:
        t.run_command("echo again")
    assert ei.value.code == "NOT_CONNECTED"


def test_ssh_helper_success_with_timeout_unchanged() -> None:
    """Normal helper success still coerces exit/stdout and keeps env."""
    seen: dict[str, object] = {}

    class Conn:
        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            seen["command"] = command
            seen["timeout_s"] = timeout_s
            seen["env"] = env
            del cwd
            return ExecResult(exit_code=0, stdout="helper-ok", cwd="/work")

    t = SSHTransport(
        host="h",
        username="u",
        remote_shell_family="posix",
        connector=lambda **_k: Conn(),
    )
    t.connect()
    r = t.run_command("echo helper", timeout_s=2.5, env={"H": "v"})
    assert r.exit_code == 0
    assert r.timed_out is False
    assert "helper-ok" in r.stdout
    assert seen.get("timeout_s") == 2.5
    assert seen.get("env") == {"H": "v"}

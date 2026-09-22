"""Service tests: exec three forms local + mock SSH."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from mcp_remote_control.cli import main
from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_USAGE, EXIT_VALIDATION
from mcp_remote_control.core import exec_ops
from mcp_remote_control.endpoint import get_registry, reset_registry
from mcp_remote_control.exec import run_script_on_transport
from mcp_remote_control.transport.base import ExecResult, TransportError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_registry()
    yield
    reset_registry()


# ---------------------------------------------------------------------------
# local command / argv / script
# ---------------------------------------------------------------------------


def test_local_command_echo_hello() -> None:
    r = exec_ops.run(ep="local", command="echo hello", home=FIXTURES)
    assert r.kind == "exec"
    assert r.status == "ok"
    assert r.fields.get("exit") == 0
    assert r.fields.get("form") == "command"
    assert r.fields.get("ep") == "local"
    assert r.cwd is not None
    assert Path(r.cwd).is_absolute()
    assert r.body is not None
    assert "hello" in r.body
    text = r.render_text()
    assert text.startswith("@exec ok")
    assert "cwd=" in text
    assert "exit=0" in text
    assert "hello" in text


def test_local_argv_echo() -> None:
    r = exec_ops.run(ep="local", argv=["/bin/echo", "hello-argv"], home=FIXTURES)
    assert r.status == "ok"
    assert r.fields.get("form") == "argv"
    assert r.fields.get("exit") == 0
    assert r.cwd is not None
    assert Path(r.cwd).is_absolute()
    assert r.body is not None
    assert "hello-argv" in r.body


def test_local_script_body_bash() -> None:
    r = exec_ops.run(
        ep="local",
        script="echo script-body-ok",
        runtime="bash",
        home=FIXTURES,
    )
    assert r.status == "ok"
    assert r.fields.get("form") == "script"
    assert r.fields.get("exit") == 0
    assert r.body is not None
    assert "script-body-ok" in r.body


def test_local_script_body_python() -> None:
    r = exec_ops.run(
        ep="local",
        script="print('py-body')",
        runtime="python",
        home=FIXTURES,
    )
    assert r.status == "ok"
    assert r.fields.get("exit") == 0
    assert r.body is not None
    assert "py-body" in r.body


def test_run_script_python_argv_not_controller_sys_executable() -> None:
    """runtime=python never embeds controller sys.executable in argv.

    Remote-ish transports (SSH/WinRM) would fail if the controller venv
    path were shipped; local also uses PATH-discoverable names (python3/py).
    """
    import sys

    from mcp_remote_control.transport.local import LocalTransport

    controller = sys.executable or ""
    assert controller

    captured: list[list[str]] = []

    class _CaptureLocal(LocalTransport):
        def run_argv(
            self,
            argv: list[str],
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            captured.append(list(argv))
            return ExecResult(
                exit_code=0,
                stdout="py-ok\n",
                stderr="",
                cwd=cwd or self.cwd,
            )

    # Local posix-ish: python3, never controller venv path.
    t = _CaptureLocal()
    t.connect()
    run_script_on_transport(t, body="print(1)", runtime="python")
    assert captured, "run_argv not called"
    assert captured[0][0] in ("python3", "py")
    assert captured[0][0] != controller
    assert captured[0][1:3] == ["-c", "print(1)"]

    # Path form
    captured.clear()
    run_script_on_transport(t, path="/tmp/job.py", runtime="python")
    assert captured[0][0] in ("python3", "py")
    assert captured[0][0] != controller

    # Remote-like: name != local, meta.os=windows -> py
    captured.clear()
    remote = _CaptureLocal()
    remote.cwd = "/tmp"
    remote.home = "/tmp"
    remote.meta = {"os": "windows", "shell_family": "powershell"}
    remote.remote_shell_family = "powershell"
    remote._connected = True  # noqa: SLF001
    # Pretend SSH so controller platform is not used as primary signal.
    remote.name = "ssh"  # type: ignore[misc]
    run_script_on_transport(remote, body="print(1)", runtime="python")
    assert captured[0][0] == "py"
    assert captured[0][0] != controller

    # Remote linux meta -> python3
    captured.clear()
    remote.meta = {"os": "linux"}
    remote.remote_shell_family = "posix"
    run_script_on_transport(remote, body="print(1)", runtime="python")
    assert captured[0][0] == "python3"
    assert captured[0][0] != controller


def test_local_script_path(tmp_path: Path) -> None:
    script = tmp_path / "hi.sh"
    script.write_text("#!/bin/sh\necho from-path\n", encoding="utf-8")
    r = exec_ops.run(
        ep="local",
        script_path=str(script),
        runtime="bash",
        home=FIXTURES,
    )
    assert r.status == "ok"
    assert r.fields.get("exit") == 0
    assert r.body is not None
    assert "from-path" in r.body


def test_local_script_path_relative_uses_exec_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative script_path names the file the child will actually read.

    The control process stands in an unrelated directory and ``job.sh`` exists
    only under the run's cwd, which is also the directory the interpreter is
    launched in. The equivalent argv form already reads it from there, so the
    script form must too: a pre-check against the control cwd rejects a script
    that is present. The rendered record must name the file the child opened,
    not the caller's spelling of it.
    """
    control = tmp_path / "control"
    work = tmp_path / "work"
    control.mkdir()
    work.mkdir()
    (work / "job.sh").write_text('echo "$0"\necho rel-ok\n', encoding="utf-8")
    monkeypatch.chdir(control)

    argv_form = exec_ops.run(
        ep="local",
        argv=["/bin/sh", "job.sh"],
        cwd=str(work),
        home=FIXTURES,
    )
    assert argv_form.status == "ok", argv_form.render_text()
    assert "job.sh" in (argv_form.body or "")

    r = exec_ops.run(
        ep="local",
        script_path="job.sh",
        runtime="bash",
        cwd=str(work),
        home=FIXTURES,
    )
    assert r.status == "ok", r.render_text()
    assert r.fields.get("exit") == 0
    assert "rel-ok" in (r.body or "")
    # $0 is the path the interpreter handed the child; the echo line must not
    # name a different file.
    lines = [ln for ln in (r.body or "").splitlines() if ln.strip()]
    assert lines[0] == f"$ script runtime=bash path={lines[1]}", r.body
    assert lines[1].endswith("/job.sh"), lines[1]


def test_local_script_path_tilde_runs_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``~/job.sh`` is checked and dispatched as the expanded path.

    An expanded pre-check plus a literal dispatched argv lets the call pass
    validation and then fail opening a directory named ``~``; it also leaves
    the rendered record naming a path the interpreter cannot resolve.
    """
    fake_home = tmp_path / "home"
    work = tmp_path / "work"
    fake_home.mkdir()
    work.mkdir()
    (fake_home / "job.sh").write_text('echo "$0"\necho tilde-ok\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.chdir(work)

    r = exec_ops.run(
        ep="local",
        script_path="~/job.sh",
        runtime="bash",
        cwd=str(work),
        home=FIXTURES,
    )
    assert r.status == "ok", r.render_text()
    assert r.fields.get("exit") == 0
    assert "tilde-ok" in (r.body or "")
    lines = [ln for ln in (r.body or "").splitlines() if ln.strip()]
    assert lines[0] == f"$ script runtime=bash path={lines[1]}", r.body
    assert lines[1] == str(fake_home / "job.sh"), lines[1]


@pytest.mark.parametrize("as_absolute", [False, True], ids=["relative", "absolute"])
def test_local_script_path_missing_is_invalid_arg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    as_absolute: bool,
) -> None:
    """A script that is absent from the run's cwd is refused before dispatch."""
    control = tmp_path / "control"
    work = tmp_path / "work"
    control.mkdir()
    work.mkdir()
    monkeypatch.chdir(control)

    missing = work / "nope.sh"
    r = exec_ops.run(
        ep="local",
        script_path=str(missing) if as_absolute else missing.name,
        runtime="bash",
        cwd=str(work),
        home=FIXTURES,
    )
    assert r.status == "error", r.render_text()
    assert r.code == "INVALID_ARG", r.render_text()
    msg = str((r.fields or {}).get("msg") or "")
    assert "script path not found" in msg, msg


def test_local_script_path_unresolvable_tilde_is_invalid_arg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``~user`` with no home directory here is refused as a path fault.

    ``pathlib`` raises for that spelling. Escaping as an interpreter error
    would report a dispatch attempt for an argument that never named a file;
    the caller gets the argument diagnosis instead, before anything runs.
    """
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    r = exec_ops.run(
        ep="local",
        script_path="~mrc-no-such-user/job.sh",
        runtime="bash",
        cwd=str(work),
        home=FIXTURES,
    )
    assert r.status == "error", r.render_text()
    assert r.code == "INVALID_ARG", r.render_text()
    msg = str((r.fields or {}).get("msg") or "")
    assert "home directory" in msg, msg


@pytest.mark.parametrize("as_absolute", [False, True], ids=["absolute", "relative"])
@pytest.mark.parametrize("suffix", ["/", "/."], ids=["slash", "dot"])
def test_local_script_path_trailing_separator_does_not_execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    as_absolute: bool,
    suffix: str,
) -> None:
    """A spelling that names no file must not run the file it looks like.

    ``<dir>/job.sh/`` and ``<dir>/job.sh/.`` are not paths to a regular file:
    an interpreter refuses both with ``Not a directory`` and executes nothing.
    Collapsing the spelling first turns it into the valid ``<dir>/job.sh`` and
    runs a file the caller did not name, so an invalid argument becomes a
    successful execution. The refusal is asserted on the run's side effect,
    since a dispatched script would produce it whatever the reported status.
    """
    control = tmp_path / "control"
    work = tmp_path / "work"
    control.mkdir()
    work.mkdir()
    marker = tmp_path / "side-effect"
    (work / "job.sh").write_text(
        f"#!/bin/sh\ntouch '{marker}'\necho UNEXPECTED_EXECUTION\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(control)

    spelling = (str(work / "job.sh") if as_absolute else "job.sh") + suffix
    r = exec_ops.run(
        ep="local",
        script_path=spelling,
        runtime="bash",
        cwd=str(work),
        home=FIXTURES,
    )
    assert not marker.exists(), r.render_text()
    assert "UNEXPECTED_EXECUTION" not in (r.body or ""), r.render_text()
    assert r.status == "error", r.render_text()
    assert r.code == "INVALID_ARG", r.render_text()


def test_ssh_script_path_stays_remote_unchecked() -> None:
    """A remote script path is dispatched as given.

    An SSH path addresses the remote filesystem: the controller must neither
    require the file to exist locally nor rewrite a relative path against a
    local cwd.
    """
    captured: list[list[str]] = []

    class Conn:
        cwd = "/var/www"
        home = "/home/deploy"

        def run_argv(
            self,
            argv: list[str],
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            captured.append(list(argv))
            return ExecResult(
                exit_code=0,
                stdout="remote-ok\n",
                stderr="",
                cwd=cwd or self.cwd,
            )

    def connector(**_kwargs: object) -> Conn:
        return Conn()

    r = exec_ops.run(
        ep="lab-ssh",
        script_path="scripts/job.sh",
        runtime="bash",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok", r.render_text()
    assert captured, "script never reached run_argv"
    assert captured[0][1] == "scripts/job.sh", captured


def test_local_timeout() -> None:
    r = exec_ops.run(
        ep="local",
        command="sleep 10",
        timeout=0.2,
        home=FIXTURES,
    )
    assert r.status == "timeout"
    assert r.fields.get("form") == "command"
    # Machine-readable timeout fields (no WinRM dispose noise on local).
    assert r.fields.get("timed_out") is True
    assert r.fields.get("exit") == -1
    assert r.fields.get("session_disposed") is None
    assert r.fields.get("marked_dead") is None
    assert r.fields.get("reopen_hint") is None
    assert r.cwd is not None
    assert Path(r.cwd).is_absolute()
    text = r.render_text()
    assert text.startswith("@exec timeout")


@pytest.mark.parametrize(
    "bad_timeout",
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
def test_local_non_finite_timeout_rejected(bad_timeout: float | str) -> None:
    """NaN and +/-Inf must be INVALID_ARG (no crash, no orphan child)."""
    r = exec_ops.run(
        ep="local",
        command="sleep 30",
        timeout=bad_timeout,  # type: ignore[arg-type]
        home=FIXTURES,
    )
    assert r.status == "error", r.render_text()
    assert r.code == "INVALID_ARG", r.render_text()
    msg = str((r.fields or {}).get("msg") or "")
    assert "finite" in msg.lower() or "timeout" in msg.lower(), msg
    text = r.render_text()
    assert text.startswith("@exec error") or "INVALID_ARG" in text


@pytest.mark.parametrize(
    "bad_timeout",
    [0, 0.0, -5, -0.1, "abc"],
    ids=["zero-int", "zero-float", "neg-int", "neg-float", "unparseable"],
)
def test_local_non_positive_or_unparseable_timeout_rejected(
    bad_timeout: float | str,
) -> None:
    """timeout<=0 and unparseable must be INVALID_ARG (no silent unlimited)."""
    r = exec_ops.run(
        ep="local",
        command="sleep 30",
        timeout=bad_timeout,  # type: ignore[arg-type]
        home=FIXTURES,
    )
    assert r.status == "error", r.render_text()
    assert r.code == "INVALID_ARG", r.render_text()
    msg = str((r.fields or {}).get("msg") or "")
    assert "timeout" in msg.lower(), msg
    text = r.render_text()
    assert text.startswith("@exec error") or "INVALID_ARG" in text


def test_timeout_zero_rejected_before_ensure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """timeout=0 is INVALID_ARG without attempting a lazy connect."""
    calls: list[object] = []

    def _boom(*_args: object, **_kwargs: object) -> object:
        calls.append((_args, _kwargs))
        raise TransportError("CONNECT_FAILED", "sentinel connect")

    monkeypatch.setattr(exec_ops, "ensure_endpoint", _boom)
    r = exec_ops.run(
        ep="probe-target",
        command="true",
        timeout=0,
        home=FIXTURES,
    )
    assert r.status == "error", r.render_text()
    assert r.code == "INVALID_ARG", r.render_text()
    assert r.code != "CONNECT_FAILED"
    msg = str((r.fields or {}).get("msg") or "")
    assert "timeout" in msg.lower(), msg
    assert calls == []


def test_positive_timeout_still_calls_ensure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legal timeout still lazy-connects; validation does not skip ensure."""
    calls: list[object] = []

    def _boom(*_args: object, **_kwargs: object) -> object:
        calls.append((_args, _kwargs))
        raise TransportError("CONNECT_FAILED", "sentinel connect")

    monkeypatch.setattr(exec_ops, "ensure_endpoint", _boom)
    r = exec_ops.run(
        ep="probe-target",
        command="true",
        timeout=1,
        home=FIXTURES,
    )
    assert r.status == "error", r.render_text()
    assert r.code == "CONNECT_FAILED", r.render_text()
    assert len(calls) == 1


def test_local_timeout_none_still_unlimited() -> None:
    """timeout=None remains unlimited (behavior unchanged)."""
    r = exec_ops.run(
        ep="local",
        command="echo none-ok",
        timeout=None,
        home=FIXTURES,
    )
    assert r.status == "ok"
    assert r.fields.get("exit") == 0
    assert "none-ok" in (r.body or "")


def test_local_fail_nonzero_exit() -> None:
    r = exec_ops.run(ep="local", command="exit 7", home=FIXTURES)
    assert r.status == "fail"
    assert r.fields.get("exit") == 7
    assert r.cwd is not None


def test_failed_exec_does_not_overwrite_endpoint_cwd(tmp_path: Path) -> None:
    """Fail/timeout must keep prior endpoint.cwd (no pollution).

    After a successful run seeds endpoint.cwd, a failing command that
    reports a different result.cwd must not advance the tracked default.
    """
    good = tmp_path / "good"
    good.mkdir()
    bad_reported = tmp_path / "bad-reported"
    bad_reported.mkdir()

    # Seed tracked cwd via a successful local exec.
    r0 = exec_ops.run(
        ep="local",
        command="pwd",
        cwd=str(good),
        home=FIXTURES,
    )
    assert r0.status == "ok"
    ep = get_registry().get("local")
    assert ep is not None
    seeded = ep.cwd
    assert seeded is not None
    assert Path(seeded).resolve() == good.resolve()

    # Nonzero exit with a different result.cwd (simulates remote cd fail
    # still returning a probe path that should not stick).
    class _FailTransport:
        """Wrap real transport: force fail + foreign cwd on run_command."""

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
                exit_code=1,
                stdout="",
                stderr="cd: no such file or directory\n",
                cwd=str(bad_reported),
                timed_out=False,
            )

    assert ep.transport is not None
    ep.transport = _FailTransport(ep.transport)  # type: ignore[assignment]

    r_fail = exec_ops.run(
        ep="local",
        command="cd /nonexistent",
        home=FIXTURES,
    )
    assert r_fail.status == "fail"
    assert r_fail.fields.get("exit") == 1
    # endpoint.cwd must remain the seeded success path.
    assert ep.cwd == seeded
    assert Path(ep.cwd).resolve() == good.resolve()


def test_timeout_exec_does_not_overwrite_endpoint_cwd(tmp_path: Path) -> None:
    """Timeout path also preserves prior endpoint.cwd."""
    good = tmp_path / "good-to"
    good.mkdir()
    foreign = tmp_path / "foreign-to"
    foreign.mkdir()

    r0 = exec_ops.run(
        ep="local",
        command="pwd",
        cwd=str(good),
        home=FIXTURES,
    )
    assert r0.status == "ok"
    ep = get_registry().get("local")
    assert ep is not None
    seeded = ep.cwd
    assert seeded is not None

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
                cwd=str(foreign),
                timed_out=True,
            )

    assert ep.transport is not None
    ep.transport = _TimeoutTransport(ep.transport)  # type: ignore[assignment]

    r_to = exec_ops.run(
        ep="local",
        command="sleep 99",
        timeout=0.1,
        home=FIXTURES,
    )
    assert r_to.status == "timeout"
    assert ep.cwd == seeded


def test_success_exec_tracks_endpoint_cwd(tmp_path: Path) -> None:
    """Successful exec still advances endpoint.cwd for later defaults."""
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()

    r1 = exec_ops.run(
        ep="local",
        command="pwd",
        cwd=str(d1),
        home=FIXTURES,
    )
    assert r1.status == "ok"
    ep = get_registry().get("local")
    assert ep is not None
    assert Path(ep.cwd or "").resolve() == d1.resolve()

    r2 = exec_ops.run(
        ep="local",
        command="pwd",
        cwd=str(d2),
        home=FIXTURES,
    )
    assert r2.status == "ok"
    assert Path(ep.cwd or "").resolve() == d2.resolve()

    # Default (no cwd arg) should resolve via tracked endpoint.cwd -> d2.
    r3 = exec_ops.run(ep="local", command="pwd", home=FIXTURES)
    assert r3.status == "ok"
    assert Path(r3.cwd or "").resolve() == d2.resolve()


def test_ssh_mock_failed_exec_preserves_endpoint_cwd() -> None:
    """SSH mock fail with foreign result.cwd does not overwrite track."""
    class Conn:
        cwd = "/var/www"
        home = "/home/deploy"

        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            del timeout_s, env
            # Fail only the intentional user command; probes / seed cmds stay ok.
            if "cd /nope" in command or command.strip() == "cd /nope":
                return ExecResult(
                    exit_code=1,
                    stdout="",
                    stderr="cd: /nope: No such file or directory\n",
                    cwd="/nope",
                    timed_out=False,
                )
            return ExecResult(
                exit_code=0,
                stdout="ok\n",
                stderr="",
                cwd=cwd or self.cwd,
            )

    conn = Conn()

    def connector(**_kwargs: object) -> Conn:
        return conn

    reg = get_registry()
    reg.ssh_connector = connector  # type: ignore[assignment]

    r0 = exec_ops.run(
        ep="lab-ssh",
        command="echo ok",
        home=FIXTURES,
        connector=connector,
    )
    assert r0.status == "ok"
    ep = reg.get("lab-ssh")
    assert ep is not None
    seeded = ep.cwd
    assert seeded is not None

    r_fail = exec_ops.run(
        ep="lab-ssh",
        command="cd /nope",
        home=FIXTURES,
        connector=connector,
    )
    assert r_fail.status == "fail"
    assert ep.cwd == seeded
    assert ep.cwd != "/nope"


def test_local_lazy_connect_without_prior_open() -> None:
    reg = get_registry()
    assert reg.get("local") is None
    r = exec_ops.run(ep="local", command="echo lazy", home=FIXTURES)
    assert r.status == "ok"
    assert "lazy" in (r.body or "")
    ep = reg.get("local")
    assert ep is not None
    assert ep.connected is True


def test_local_cwd_absolute_when_requested(tmp_path: Path) -> None:
    r = exec_ops.run(
        ep="local",
        command="pwd",
        cwd=str(tmp_path),
        home=FIXTURES,
    )
    assert r.status == "ok"
    assert r.cwd == str(tmp_path.resolve())
    # pwd body should match
    assert r.body is not None
    assert str(tmp_path.resolve()) in r.body or tmp_path.name in r.body


@pytest.mark.parametrize(
    "junk",
    ["", True, "${HOME:-}", "%CD%"],
    ids=["empty", "bool-true", "home-probe", "cd-probe"],
)
def test_local_empty_or_garbage_cwd_uses_fallback(junk: object) -> None:
    """Non-path cwd is not an explicit missing directory; default cwd still works."""
    r = exec_ops.run(
        ep="local",
        command="echo fallback-cwd",
        cwd=junk,  # type: ignore[arg-type]
        home=FIXTURES,
    )
    assert r.status == "ok"
    assert r.code != "INVALID_CWD"
    assert r.cwd is not None
    assert Path(r.cwd).is_dir()
    assert "fallback-cwd" in (r.body or "")


def test_local_explicit_missing_cwd_is_invalid() -> None:
    r = exec_ops.run(
        ep="local",
        command="echo should-not-run",
        cwd="/no/such/dir/mrc-missing",
        home=FIXTURES,
    )
    assert r.status == "error"
    assert r.code == "INVALID_CWD"


def test_missing_ep() -> None:
    r = exec_ops.run(command="echo x", home=FIXTURES)
    assert r.status == "error"
    assert r.code == "MISSING_ARG"


def test_missing_form() -> None:
    r = exec_ops.run(ep="local", home=FIXTURES)
    assert r.status == "error"
    assert r.code == "MISSING_ARG"


def test_mutual_exclusion() -> None:
    r = exec_ops.run(
        ep="local",
        command="echo a",
        argv=["echo", "b"],
        home=FIXTURES,
    )
    assert r.status == "error"
    assert r.code == "INVALID_ARG"


# ---------------------------------------------------------------------------
# SSH mock
# ---------------------------------------------------------------------------


def test_ssh_mock_exec_command() -> None:
    class Conn:
        cwd = "/var/www"
        home = "/home/deploy"

        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            return ExecResult(
                exit_code=0,
                stdout=f"remote:{command}\n",
                stderr="",
                cwd=cwd or self.cwd,
            )

    def connector(**_kwargs: object) -> Conn:
        return Conn()

    reg = get_registry()
    reg.ssh_connector = connector  # type: ignore[assignment]

    r = exec_ops.run(
        ep="lab-ssh",
        command="echo hello",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok"
    assert r.fields.get("ep") == "lab-ssh"
    assert r.fields.get("exit") == 0
    assert r.body is not None
    assert "remote:echo hello" in r.body
    assert r.cwd is not None
    # profile defaults.cwd = /var/www
    assert r.cwd == "/var/www" or r.cwd.startswith("/")


def test_ssh_mock_exec_argv() -> None:
    class Conn:
        def run_argv(
            self,
            argv: list[str],
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            return ExecResult(
                exit_code=0,
                stdout=" ".join(argv) + "\n",
                cwd=cwd or "/tmp",
            )

    def connector(**_kwargs: object) -> Conn:
        return Conn()

    r = exec_ops.run(
        ep="lab-ssh",
        argv=["/bin/echo", "ssh-argv"],
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok"
    assert "ssh-argv" in (r.body or "")


# ---------------------------------------------------------------------------
# Connect-fault (AUTH / HOSTKEY / CONNECT) -> exec OpResult mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "msg"),
    [
        ("AUTH_FAILED", "permission denied (publickey,password)"),
        ("HOSTKEY_MISMATCH", "host key does not match known_hosts"),
        ("CONNECT_FAILED", "connection refused"),
    ],
)
def test_exec_connect_fault_maps_transport_code(code: str, msg: str) -> None:
    """Lazy open failures surface as exec error with the transport code.

    ensure_endpoint raises TransportError(AUTH_FAILED|HOSTKEY_MISMATCH|
    CONNECT_FAILED); exec_ops must map those 1:1 onto OpResult (not
    EXEC_FAILED / CONNECT_FAILED collapse).
    """

    def connector(**_kwargs: object) -> object:
        raise TransportError(code, msg, details={"host": "lab.example"})

    reg = get_registry()
    reg.ssh_connector = connector  # type: ignore[assignment]

    r = exec_ops.run(
        ep="lab-ssh",
        command="echo never",
        home=FIXTURES,
        connector=connector,
    )
    assert r.kind == "exec"
    assert r.status == "error"
    assert r.code == code, f"expected {code}, got {r.code} fields={r.fields}"
    assert r.fields.get("ep") == "lab-ssh"
    assert r.fields.get("form") == "command"
    assert r.fields.get("msg") == msg
    assert r.fields.get("host") == "lab.example"
    text = r.render_text()
    assert code in text
    # Endpoint must not be left half-open under the name.
    assert reg.get("lab-ssh") is None or not getattr(
        reg.get("lab-ssh"), "connected", False
    )


def test_exec_connect_fault_generic_exception_is_connect_failed() -> None:
    """Non-TransportError on open maps to CONNECT_FAILED (not silent ok).

    SSHTransport wraps bare OSError into TransportError(CONNECT_FAILED, ...);
    exec_ops must surface that code (not EXEC_FAILED / uncaught).
    """

    def connector(**_kwargs: object) -> object:
        raise OSError("network unreachable")

    reg = get_registry()
    reg.ssh_connector = connector  # type: ignore[assignment]

    r = exec_ops.run(
        ep="lab-ssh",
        command="echo never",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "error"
    assert r.code == "CONNECT_FAILED"
    assert r.fields.get("ep") == "lab-ssh"
    assert r.fields.get("form") == "command"
    assert "network unreachable" in (r.fields.get("msg") or "")


# ---------------------------------------------------------------------------
# Concurrent run_command on one endpoint (no intermittent NOT_CONNECTED)
# ---------------------------------------------------------------------------


def test_concurrent_local_run_command_no_not_connected() -> None:
    """Dual-thread stress: local exec never returns intermittent NOT_CONNECTED.

    FastMCP runs sync tools in a thread pool; transport op_lock serializes
    same-endpoint run_command so concurrent callers cannot tear liveness.
    """
    import threading

    from mcp_remote_control.core import exec_ops as _exec_ops

    # Warm open so both threads share one transport.
    warm = _exec_ops.run(ep="local", command="echo warm", home=FIXTURES)
    assert warm.status == "ok", f"warm exec failed: {warm.code} {warm.fields}"

    N = 2
    PER = 20
    results: list[object] = []
    res_lock = threading.Lock()
    errors: list[BaseException] = []
    err_lock = threading.Lock()

    def worker() -> None:
        local_results: list[object] = []
        local_errs: list[BaseException] = []
        for i in range(PER):
            try:
                r = _exec_ops.run(
                    ep="local",
                    command=f"echo concurrent-{i}",
                    home=FIXTURES,
                )
                local_results.append(r)
            except BaseException as exc:  # noqa: BLE001
                local_errs.append(exc)
        with res_lock:
            results.extend(local_results)
        with err_lock:
            errors.extend(local_errs)

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"workers raised: {errors[:3]}"
    assert len(results) == N * PER
    not_connected = [
        r
        for r in results
        if getattr(r, "code", None) == "NOT_CONNECTED"
        or (
            getattr(r, "status", None) == "error"
            and getattr(r, "code", None) == "NOT_CONNECTED"
        )
    ]
    assert not not_connected, (
        f"intermittent NOT_CONNECTED under concurrent run_command: "
        f"{not_connected[:3]}"
    )
    for r in results:
        assert getattr(r, "status", None) in ("ok", "fail", "timeout"), (
            f"unexpected status {getattr(r, 'status', None)} "
            f"code={getattr(r, 'code', None)}"
        )
        # Local echo should always succeed.
        assert getattr(r, "status", None) == "ok"
        assert getattr(r, "fields", {}).get("exit") == 0


def test_concurrent_ssh_mock_run_command_serialized() -> None:
    """Mock SSH: concurrent exec_ops.run never sees NOT_CONNECTED."""
    import threading
    import time

    class Conn:
        cwd = "/var/www"
        home = "/home/deploy"

        def __init__(self) -> None:
            self._depth = 0
            self._lock = threading.Lock()
            self.max_depth = 0

        def run_command(
            self,
            command: str,
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            del timeout_s, env
            with self._lock:
                self._depth += 1
                self.max_depth = max(self.max_depth, self._depth)
                depth = self._depth
            if depth > 1:
                # SSHTransport.run_command is serialized by op_lock, so the
                # underlying conn.run_command must never see concurrent entry.
                raise RuntimeError("conn.run_command overlapped without lock")
            try:
                time.sleep(0.01)
                return ExecResult(
                    exit_code=0,
                    stdout=f"remote:{command}\n",
                    stderr="",
                    cwd=cwd or self.cwd,
                )
            finally:
                with self._lock:
                    self._depth -= 1

    conn = Conn()

    def connector(**_kwargs: object) -> Conn:
        return conn

    reg = get_registry()
    reg.ssh_connector = connector  # type: ignore[assignment]

    warm = exec_ops.run(
        ep="lab-ssh",
        command="echo warm",
        home=FIXTURES,
        connector=connector,
    )
    assert warm.status == "ok", f"warm: {warm.code} {warm.fields}"

    results: list[object] = []
    res_lock = threading.Lock()
    errors: list[BaseException] = []
    err_lock = threading.Lock()
    N = 4
    PER = 8

    def worker() -> None:
        local_r: list[object] = []
        local_e: list[BaseException] = []
        for i in range(PER):
            try:
                r = exec_ops.run(
                    ep="lab-ssh",
                    command=f"echo n{i}",
                    home=FIXTURES,
                    connector=connector,
                )
                local_r.append(r)
            except BaseException as exc:  # noqa: BLE001
                local_e.append(exc)
        with res_lock:
            results.extend(local_r)
        with err_lock:
            errors.extend(local_e)

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"workers raised: {errors[:3]}"
    assert len(results) == N * PER
    for r in results:
        assert getattr(r, "status", None) == "ok", (
            f"status={getattr(r, 'status', None)} code={getattr(r, 'code', None)} "
            f"fields={getattr(r, 'fields', None)}"
        )
        assert getattr(r, "code", None) != "NOT_CONNECTED"
    assert conn.max_depth == 1, (
        f"SSHTransport op_lock failed: conn saw max_depth={conn.max_depth}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_exec_command_form(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["exec", "--ep", "local", "--", "command", "echo hello"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "@exec ok" in out
    assert "cwd=" in out
    assert "hello" in out
    assert "exit=0" in out


def test_cli_exec_command_flag(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["exec", "--ep", "local", "--command", "echo via-flag"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "via-flag" in out


def test_cli_exec_flag_with_trailing_positionals(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Form flag + leftover positionals must error, not silently drop them.

    ``--command 'echo hi' /important`` used to ignore ``/important`` and
    exit 0. Critical path args must surface as usage.
    """
    code = main(
        ["exec", "--ep", "local", "--command", "echo hi", "/important"]
    )
    assert code == EXIT_USAGE
    captured = capsys.readouterr()
    err = captured.err
    low = err.lower()
    assert (
        "trailing" in low
        or "positional" in low
        or "leftover" in low
    ), f"expected leftover/positional/trailing in stderr, got: {err!r}"
    assert "/important" in err or "important" in err
    # Must not have run the command successfully on stdout.
    assert "@exec ok" not in captured.out


def test_cli_exec_argv_form(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["exec", "--ep", "local", "--", "argv", "/bin/echo", "cli-argv"]
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "cli-argv" in out


def test_cli_exec_json(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["exec", "--ep", "local", "--json", "--command", "echo json-hi"]
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["kind"] == "exec"
    assert data["status"] == "ok"
    assert data["exit"] == 0
    assert "json-hi" in (data.get("body") or "")
    assert data.get("cwd")
    assert Path(data["cwd"]).is_absolute()


def test_cli_exec_timeout(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "exec",
            "--ep",
            "local",
            "--timeout",
            "0.2",
            "--command",
            "sleep 10",
        ]
    )
    assert code != EXIT_OK
    out = capsys.readouterr().out
    assert "@exec timeout" in out


def test_cli_exec_missing_form(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["exec", "--ep", "local"])
    assert code == EXIT_VALIDATION
    out = capsys.readouterr().out
    assert "@exec error" in out


# ---------------------------------------------------------------------------
# Dead local_path_read SSH-local-file-upload feature fully removed
# ---------------------------------------------------------------------------


def test_run_script_on_transport_no_local_path_read_param() -> None:
    """The dead local_path_read kwarg is removed from run_script_on_transport.

    It was always False at the only dispatch site (exec_ops._dispatch), so
    the `if path is not None and local_path_read and body is None:` branch
    was unreachable. The kwarg and the dead branch are deleted together.
    """
    sig = inspect.signature(run_script_on_transport)
    assert "local_path_read" not in sig.parameters
    # The remaining params are unchanged.
    expected = {
        "transport",
        "body",
        "path",
        "runtime",
        "args",
        "cwd",
        "timeout_s",
        "env",
        "dialect",
    }
    assert set(sig.parameters) == expected


def test_run_script_on_transport_rejects_local_path_read_kwarg() -> None:
    """Passing local_path_read= must raise TypeError now that the kwarg is
    gone (no silent acceptance of stale caller code)."""
    with pytest.raises(TypeError):
        run_script_on_transport(
            object(),  # transport unused: TypeError fires before body
            body="echo hi",
            local_path_read=True,  # pyright: ignore[reportCallIssue]
        )


# ---------------------------------------------------------------------------
# WinRM ps_oneshot gate: NoLanguage hosts fail pre-flight with UNSUPPORTED;
# ConstrainedLanguage (ps_oneshot=true) and probe-less sessions still run.
# ---------------------------------------------------------------------------


class _MockWinRMSession:
    """Injectable winrm session: identity seeds only, no network sockets."""

    def __init__(self) -> None:
        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1.19041"
        self.closed = False
        self.commands: list[str] = []
        self.argvs: list[list[str]] = []

    def close(self) -> None:
        self.closed = True

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        self.commands.append(command)
        return ExecResult(
            exit_code=0,
            stdout=f"winrm-out:{command}\n",
            stderr="",
            cwd=cwd or self.cwd,
        )

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        self.argvs.append(list(argv))
        return ExecResult(
            exit_code=0,
            stdout=" ".join(argv) + "\n",
            cwd=cwd or self.cwd,
        )


class _NoLanguageWinRMSession(_MockWinRMSession):
    """Identity + NoLanguage (JEA) capability seeds -> ps_oneshot=false."""

    def __init__(self) -> None:
        super().__init__()
        self.language_mode = "NoLanguage"
        self.has_convertto_json = True
        self.can_get_item = True
        self.can_file_io = True
        self.ps_edition = "Desktop"


class _ConstrainedWinRMSession(_MockWinRMSession):
    """ConstrainedLanguage seeds -> ps_oneshot=true but ps_script_fs=false."""

    def __init__(self) -> None:
        super().__init__()
        self.language_mode = "ConstrainedLanguage"
        self.has_convertto_json = True
        self.can_get_item = True
        self.can_file_io = True
        self.ps_edition = "Desktop"


def _winrm_connector(
    session: _MockWinRMSession,
) -> Callable[..., _MockWinRMSession]:
    def connector(**_kwargs: object) -> _MockWinRMSession:
        return session

    return connector


def test_winrm_exec_ps_oneshot_false_nolanguage_unsupported() -> None:
    """winrm_ps ps_oneshot=false gates exec before dispatch.

    On a NoLanguage (JEA) host the user's PowerShell would hard-fail
    mid-exec; exec must instead return a pre-flight UNSUPPORTED carrying
    the reported lang_mode, and never touch the transport exec surface.
    """
    sess = _NoLanguageWinRMSession()
    r = exec_ops.run(
        ep="lab-win",
        command="Get-Date",
        home=FIXTURES,
        connector=_winrm_connector(sess),
    )
    assert r.kind == "exec"
    assert r.status == "error"
    assert r.code == "UNSUPPORTED"
    assert r.fields.get("ep") == "lab-win"
    assert r.fields.get("form") == "command"
    assert r.fields.get("lang_mode") == "NoLanguage"
    hint = r.hint or ""
    assert "language mode" in hint
    assert "ps_oneshot" in hint
    text = r.render_text()
    assert "UNSUPPORTED" in text
    assert "lang_mode=NoLanguage" in text
    # The transport's exec surface was NOT called.
    assert sess.commands == []
    assert sess.argvs == []
    # Pin the gate input: the probe-derived flag is present and false.
    ep = get_registry().get("lab-win")
    assert ep is not None and ep.transport is not None
    winrm_ps = ep.transport.meta.get("winrm_ps")
    assert isinstance(winrm_ps, dict)
    assert winrm_ps.get("ps_oneshot") is False
    assert winrm_ps.get("language_mode") == "NoLanguage"


@pytest.mark.parametrize("form", ["command", "argv", "script"])
def test_winrm_exec_ps_oneshot_false_gates_all_forms(form: str) -> None:
    """The gate sits before dispatch, so all three exec forms are gated."""
    sess = _NoLanguageWinRMSession()
    connector = _winrm_connector(sess)
    if form == "command":
        r = exec_ops.run(
            ep="lab-win", command="Get-Date", home=FIXTURES, connector=connector
        )
    elif form == "argv":
        r = exec_ops.run(
            ep="lab-win",
            argv=["ipconfig", "/all"],
            home=FIXTURES,
            connector=connector,
        )
    else:
        r = exec_ops.run(
            ep="lab-win",
            script="Write-Output hi",
            runtime="powershell",
            home=FIXTURES,
            connector=connector,
        )
    assert r.status == "error"
    assert r.code == "UNSUPPORTED"
    assert r.fields.get("form") == form
    assert r.fields.get("lang_mode") == "NoLanguage"
    assert sess.commands == []
    assert sess.argvs == []


def test_winrm_exec_ps_oneshot_true_constrained_runs() -> None:
    """ConstrainedLanguage reports ps_oneshot=true with ps_script_fs=false.

    Exec is explicitly not gated on ps_script_fs, so the command runs.
    """
    sess = _ConstrainedWinRMSession()
    r = exec_ops.run(
        ep="lab-win",
        command="Get-Date",
        home=FIXTURES,
        connector=_winrm_connector(sess),
    )
    assert r.status == "ok"
    assert r.fields.get("form") == "command"
    assert r.fields.get("exit") == 0
    assert "winrm-out:Get-Date" in (r.body or "")
    assert sess.commands == ["Get-Date"]
    ep = get_registry().get("lab-win")
    assert ep is not None and ep.transport is not None
    winrm_ps = ep.transport.meta.get("winrm_ps")
    assert isinstance(winrm_ps, dict)
    assert winrm_ps.get("ps_oneshot") is True
    assert winrm_ps.get("ps_script_fs") is False


def test_winrm_exec_winrm_ps_absent_legacy_allow() -> None:
    """Identity-only seeds leave meta without winrm_ps (lab / probe off):
    exec keeps the historical allow path and runs."""
    sess = _MockWinRMSession()
    r = exec_ops.run(
        ep="lab-win",
        command="Get-Date",
        home=FIXTURES,
        connector=_winrm_connector(sess),
    )
    assert r.status == "ok"
    assert r.fields.get("exit") == 0
    assert "winrm-out:Get-Date" in (r.body or "")
    assert sess.commands == ["Get-Date"]
    ep = get_registry().get("lab-win")
    assert ep is not None and ep.transport is not None
    assert ep.transport.meta.get("winrm_ps") is None


def test_winrm_exec_incomplete_probe_still_allows_oneshot() -> None:
    """Incomplete winrm_ps (ps_oneshot true, probe failed) must not gate exec.

    A hung/unparseable capability probe marks script_fs/runspace closed but
    keeps oneshot allowed - inject that shape via transport.meta after open.
    """
    from mcp_remote_control.transport.winrm import _incomplete_winrm_ps

    sess = _MockWinRMSession()
    # First open with identity-only seeds (no winrm_ps), then inject incomplete.
    r0 = exec_ops.run(
        ep="lab-win",
        command="hostname",
        home=FIXTURES,
        connector=_winrm_connector(sess),
    )
    assert r0.status == "ok"
    ep = get_registry().get("lab-win")
    assert ep is not None and ep.transport is not None
    incomplete = _incomplete_winrm_ps(error="capability probe timed out")
    ep.transport.meta["winrm_ps"] = incomplete
    assert incomplete["ps_oneshot"] is True
    assert incomplete["ps_script_fs"] is False
    assert incomplete["ps_runspace"] is False

    r = exec_ops.run(
        ep="lab-win",
        command="Get-Date",
        home=FIXTURES,
        connector=_winrm_connector(sess),
    )
    assert r.status == "ok"
    assert r.code is None or r.code != "UNSUPPORTED"
    assert r.fields.get("exit") == 0
    assert "Get-Date" in sess.commands


# ---------------------------------------------------------------------------
# WinRM execute_ps LASTEXITCODE (false-success fix) via exec_ops
# ---------------------------------------------------------------------------


class _ExecutePsNativeExitSession:
    """WinRM session with only execute_ps; echoes LASTEXITCODE probe.

    Prefer path for run_command/run_argv when run_command/run_argv are absent.
    """

    def __init__(self, *, native_rc: int, had_errors: bool = False) -> None:
        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1"
        self.closed = False
        self.scripts: list[str] = []
        self.native_rc = native_rc
        self.had_errors = had_errors

    def close(self) -> None:
        self.closed = True

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, object, bool]:
        del environment
        self.scripts.append(script)
        from mcp_remote_control.transport.winrm import _EXIT_MARKER

        out = f"{_EXIT_MARKER}{self.native_rc}\n"
        return (out, None, self.had_errors)


def test_winrm_exec_ops_run_argv_exit_7_not_false_zero() -> None:
    """exec_ops argv cmd.exe /c exit 7 -> exit field 7 (not false 0)."""
    sess = _ExecutePsNativeExitSession(native_rc=7)

    def connector(**_kwargs: object) -> _ExecutePsNativeExitSession:
        return sess

    r = exec_ops.run(
        ep="lab-win",
        argv=["cmd.exe", "/c", "exit", "7"],
        home=FIXTURES,
        connector=connector,
    )
    assert r.fields.get("exit") == 7
    assert r.fields.get("exit") != 0
    assert r.status != "ok"
    assert sess.scripts, "must use execute_ps preferred path"
    assert "LASTEXITCODE" in sess.scripts[0]


def test_winrm_exec_ops_pure_ps_success_exit_0() -> None:
    """Pure PS success via execute_ps stays exit 0 / status ok."""
    sess = _ExecutePsNativeExitSession(native_rc=0)

    def connector(**_kwargs: object) -> _ExecutePsNativeExitSession:
        return sess

    r = exec_ops.run(
        ep="lab-win",
        command="Write-Output 'hi'",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok"
    assert r.fields.get("exit") == 0
    assert sess.scripts and "LASTEXITCODE" in sess.scripts[0]


def test_winrm_script_path_stays_remote_unchecked() -> None:
    """A WinRM script path is dispatched to the remote host as given.

    The path addresses the remote filesystem, so the controller must not
    require it to exist locally nor rewrite it.
    """
    sess = _ExecutePsNativeExitSession(native_rc=0)

    def connector(**_kwargs: object) -> _ExecutePsNativeExitSession:
        return sess

    remote = r"C:\scripts\job.ps1"
    r = exec_ops.run(
        ep="lab-win",
        script_path=remote,
        runtime="pwsh",
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok", r.render_text()
    assert sess.scripts, "script never reached the winrm exec surface"
    assert remote in sess.scripts[0], sess.scripts[0]


# ---------------------------------------------------------------------------
# Hard-timeout dispose -> ensure reconnect on next exec
# ---------------------------------------------------------------------------


def test_winrm_exec_hard_timeout_disposes_then_ensure_reconnects() -> None:
    """After wall-clock timeout, session is closed; next exec_ops reopens.

    Hard-timeout path close >=1; subsequent ensure_connected path
    succeeds without leaving NOT_CONNECTED permanently.
    """
    import threading
    import time

    from mcp_remote_control.transport.winrm import _EXIT_MARKER

    opens = {"n": 0}
    closes = {"n": 0}
    hang = {"on": True}

    class _Sess:
        def __init__(self) -> None:
            self.cwd = r"C:\Users\Administrator"
            self.home = r"C:\Users\Administrator"
            self.os = "windows"
            self.shell = "powershell"
            self.ps_version = "5.1"
            self.closed = False
            self.block = threading.Event()

        def close(self) -> None:
            closes["n"] += 1
            self.closed = True
            self.block.set()

        def execute_ps(
            self,
            script: str,
            *,
            environment: dict[str, str] | None = None,
        ) -> tuple[str, object, bool]:
            del environment
            if hang["on"]:
                self.block.wait(timeout=30.0)
                return ("", None, False)
            return (f"whoami-ok\n{_EXIT_MARKER}0\n", None, False)

    def connector(**_kwargs: object) -> _Sess:
        opens["n"] += 1
        return _Sess()

    t0 = time.monotonic()
    r1 = exec_ops.run(
        ep="lab-win",
        command="Start-Sleep 30",
        timeout=0.5,
        home=FIXTURES,
        connector=connector,
    )
    elapsed = time.monotonic() - t0
    assert r1.status == "timeout"
    assert r1.fields.get("exit") == -1
    # Machine-readable fields (not English prose alone).
    assert r1.fields.get("timed_out") is True
    assert r1.fields.get("marked_dead") is True
    assert r1.fields.get("session_disposed") is True
    assert r1.fields.get("reopen_hint") == "endpoint close then open"
    assert elapsed < 3.0
    assert closes["n"] >= 1, "hard timeout must dispose WinRM session"
    assert opens["n"] >= 1

    hang["on"] = False
    r2 = exec_ops.run(
        ep="lab-win",
        command="whoami",
        home=FIXTURES,
        connector=connector,
    )
    assert r2.status == "ok", f"ensure after dispose must reconnect, got {r2!r}"
    assert r2.fields.get("exit") == 0
    # Success path must not carry timeout dispose noise.
    assert r2.fields.get("timed_out") is None
    assert r2.fields.get("marked_dead") is None
    assert r2.fields.get("session_disposed") is None
    assert r2.fields.get("reopen_hint") is None
    assert r2.fields.get("partial") is None
    assert opens["n"] >= 2, "next exec must trigger reconnect connect"
    # Success path: no extra dispose beyond the timeout close.
    closes_after = closes["n"]
    r3 = exec_ops.run(
        ep="lab-win",
        command="whoami",
        home=FIXTURES,
        connector=connector,
    )
    assert r3.status == "ok"
    assert closes["n"] == closes_after
    assert r3.fields.get("timed_out") is None
    assert r3.fields.get("session_disposed") is None


def test_winrm_exec_timeout_fields_machine_readable() -> None:
    """Hard-timeout OpResult exposes timed_out/dispose/reopen_hint fields.

    Agents must not parse free-text body/hint alone. Prose hint is still
    present and consistent, but fields are the stable contract.
    """
    import threading

    from mcp_remote_control.transport.winrm import _EXIT_MARKER

    hang = {"on": True}

    class _Sess:
        def __init__(self) -> None:
            self.cwd = r"C:\Users\Administrator"
            self.home = r"C:\Users\Administrator"
            self.os = "windows"
            self.shell = "powershell"
            self.ps_version = "5.1"
            self.closed = False
            self.block = threading.Event()

        def close(self) -> None:
            self.closed = True
            self.block.set()

        def execute_ps(
            self,
            script: str,
            *,
            environment: dict[str, str] | None = None,
        ) -> tuple[str, object, bool]:
            del environment
            if hang["on"]:
                self.block.wait(timeout=30.0)
                return ("", None, False)
            return (f"ok\n{_EXIT_MARKER}0\n", None, False)

    r = exec_ops.run(
        ep="lab-win",
        command="Start-Sleep 30",
        timeout=0.4,
        home=FIXTURES,
        connector=lambda **_k: _Sess(),
    )
    assert r.status == "timeout", r.render_text()
    f = r.fields or {}
    assert f.get("timed_out") is True
    assert f.get("exit") == -1
    assert f.get("marked_dead") is True
    assert f.get("session_disposed") is True
    assert f.get("reopen_hint") == "endpoint close then open"
    # Prose hint still present (supplemental; fields are primary).
    assert r.hint is not None
    assert "wall-clock" in r.hint.lower()
    assert "close" in r.hint.lower()
    # Do not claim remote pipeline Stopped in body/hint.
    blob = f"{r.hint or ''}\n{r.body or ''}".lower()
    assert "stopped" not in blob or "not guaranteed" in blob


def test_winrm_exec_timeout_5_aligns_op_read_timeouts() -> None:
    """Exec timeout=5 -> call path carries >=5s op/read (ceil seconds)."""
    from mcp_remote_control.transport.winrm import (
        WinRMTransport,
        _EXIT_MARKER,
    )
    from mcp_remote_control.transport.winrm_timeouts import PYPSRP_HTTP_TIMEOUT_SLACK_S

    class _FakeTransport:
        def __init__(self) -> None:
            self.read_timeout = 30

    class _FakeWsman:
        def __init__(self) -> None:
            self.operation_timeout = 20
            self.transport = _FakeTransport()

    class _Sess:
        def __init__(self) -> None:
            self.cwd = r"C:\Users\Administrator"
            self.home = r"C:\Users\Administrator"
            self.os = "windows"
            self.shell = "powershell"
            self.ps_version = "5.1"
            self.closed = False
            self.wsman = _FakeWsman()
            self.captured: list[tuple[int, int]] = []

        def close(self) -> None:
            self.closed = True

        def execute_ps(
            self,
            script: str,
            *,
            environment: dict[str, str] | None = None,
        ) -> tuple[str, object, bool]:
            del environment, script
            self.captured.append(
                (
                    int(self.wsman.operation_timeout),
                    int(self.wsman.transport.read_timeout),
                )
            )
            return (f"ok\n{_EXIT_MARKER}0\n", None, False)

    sess = _Sess()

    def connector(**_kwargs: object) -> _Sess:
        return sess

    r = exec_ops.run(
        ep="lab-win",
        command="whoami",
        timeout=5,
        home=FIXTURES,
        connector=connector,
    )
    assert r.status == "ok", r.render_text()
    assert sess.captured, "oneshot must run under applied op/read"
    op, rd = sess.captured[0]
    assert op >= 5
    assert rd >= 5
    assert op == 5
    # HTTP read timeout keeps ahead of the WSMan operation timeout.
    assert rd == 5 + PYPSRP_HTTP_TIMEOUT_SLACK_S

    ep = get_registry().get("lab-win")
    assert ep is not None and isinstance(ep.transport, WinRMTransport)
    assert ep.transport._last_applied_operation_timeout == 5  # noqa: SLF001
    assert (  # noqa: SLF001
        ep.transport._last_applied_read_timeout == 5 + PYPSRP_HTTP_TIMEOUT_SLACK_S
    )


def test_winrm_exec_no_timeout_does_not_force_short_op_read() -> None:
    """Exec without timeout leaves library-magnitude op/read (not forced short)."""
    from mcp_remote_control.transport.winrm import _EXIT_MARKER

    class _FakeTransport:
        def __init__(self) -> None:
            self.read_timeout = 30

    class _FakeWsman:
        def __init__(self) -> None:
            self.operation_timeout = 20
            self.transport = _FakeTransport()

    class _Sess:
        def __init__(self) -> None:
            self.cwd = r"C:\Users\Administrator"
            self.home = r"C:\Users\Administrator"
            self.os = "windows"
            self.shell = "powershell"
            self.ps_version = "5.1"
            self.closed = False
            self.wsman = _FakeWsman()
            self.captured: list[tuple[int, int]] = []

        def close(self) -> None:
            self.closed = True

        def execute_ps(
            self,
            script: str,
            *,
            environment: dict[str, str] | None = None,
        ) -> tuple[str, object, bool]:
            del environment, script
            self.captured.append(
                (
                    int(self.wsman.operation_timeout),
                    int(self.wsman.transport.read_timeout),
                )
            )
            return (f"ok\n{_EXIT_MARKER}0\n", None, False)

    sess = _Sess()
    r = exec_ops.run(
        ep="lab-win",
        command="whoami",
        home=FIXTURES,
        connector=lambda **_k: sess,
    )
    assert r.status == "ok", r.render_text()
    assert sess.captured
    # No derivation -> session keeps library defaults during the call.
    assert sess.captured[0] == (20, 30)


# ---------------------------------------------------------------------------
# runtime=auto without dialect must not hardcode bash on win32
# ---------------------------------------------------------------------------


def test_run_script_auto_win32_local_not_bash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local transport on win32 mock builds pwsh argv, not bash.

    LocalTransport.connect seeds powershell dialect/family on win32 so
    runtime=auto body scripts do not require bash.exe.
    """
    import sys

    from mcp_remote_control.transport.local import LocalTransport

    monkeypatch.setattr(sys, "platform", "win32")

    captured: list[list[str]] = []

    class _CaptureLocal(LocalTransport):
        def run_argv(
            self,
            argv: list[str],
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            captured.append(list(argv))
            return ExecResult(
                exit_code=0,
                stdout="ok\n",
                stderr="",
                cwd=cwd or self.cwd,
            )

    t = _CaptureLocal()
    t.connect()
    assert t.meta.get("dialect") == "powershell"
    assert getattr(t, "remote_shell_family", None) == "powershell"

    result = run_script_on_transport(
        t,
        body="Write-Output hi",
        runtime="auto",
    )
    assert result.exit_code == 0
    assert captured, "run_argv was not called"
    assert captured[0][0] == "pwsh"
    assert captured[0][0] != "bash"

    # Path with unknown suffix also avoids bash when dialect is powershell.
    captured.clear()
    run_script_on_transport(
        t,
        path=r"C:\tools\run",
        runtime="auto",
    )
    assert captured[0][0] == "pwsh"

    # Explicit runtime=bash still honored.
    captured.clear()
    run_script_on_transport(
        t,
        body="echo x",
        runtime="bash",
    )
    assert captured[0][0] == "bash"

    # No dialect / no family: local name + win32 platform still not bash.
    t2 = _CaptureLocal()
    t2.cwd = str(Path.cwd())
    t2.home = t2.cwd
    t2.meta = {}
    t2._connected = True  # noqa: SLF001
    if hasattr(t2, "remote_shell_family"):
        del t2.remote_shell_family
    captured.clear()
    run_script_on_transport(t2, body="Write-Output x", runtime="auto")
    assert captured, "run_argv was not called under win32 mock"
    assert captured[0][0] == "pwsh"
    assert captured[0][0] != "bash"


def test_run_script_auto_posix_local_reasonable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Posix local without dialect keeps bash-friendly body default."""
    import sys

    from mcp_remote_control.transport.local import LocalTransport

    monkeypatch.setattr(sys, "platform", "linux")

    captured: list[list[str]] = []

    class _CaptureLocal(LocalTransport):
        def run_argv(
            self,
            argv: list[str],
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            captured.append(list(argv))
            return ExecResult(
                exit_code=0,
                stdout="ok\n",
                stderr="",
                cwd=cwd or self.cwd,
            )

    t = _CaptureLocal()
    t.connect()  # posix path: no dialect seed
    assert t.meta.get("dialect") is None

    run_script_on_transport(t, body="echo ok", runtime="auto")
    assert captured[0][0] == "bash"

    # busybox dialect still maps to sh
    captured.clear()
    t.meta["dialect"] = "posix-busybox"
    run_script_on_transport(t, body="echo ok", runtime="auto")
    assert captured[0][0] == "sh"


def test_exec_ops_script_auto_uses_transport_dialect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exec_ops script form with runtime=auto follows transport dialect."""
    from mcp_remote_control.transport.local import LocalTransport

    captured: list[list[str]] = []

    class _CaptureLocal(LocalTransport):
        def connect(self) -> None:
            super().connect()
            # Force Windows-like seeds even on macOS/Linux CI hosts.
            self.remote_shell_family = "powershell"
            self.meta["dialect"] = "powershell"
            self.meta["shell_family"] = "powershell"

        def run_argv(
            self,
            argv: list[str],
            *,
            cwd: str | None = None,
            timeout_s: float | None = None,
            env: dict[str, str] | None = None,
        ) -> ExecResult:
            captured.append(list(argv))
            return ExecResult(
                exit_code=0,
                stdout="pwsh-ok\n",
                stderr="",
                cwd=cwd or self.cwd,
            )

    monkeypatch.setattr(
        "mcp_remote_control.endpoint.registry.LocalTransport",
        _CaptureLocal,
    )
    # ensure_endpoint imports LocalTransport at call site via transport package
    monkeypatch.setattr(
        "mcp_remote_control.transport.local.LocalTransport",
        _CaptureLocal,
    )
    monkeypatch.setattr(
        "mcp_remote_control.transport.LocalTransport",
        _CaptureLocal,
    )

    reset_registry()
    r = exec_ops.run(
        ep="local",
        script="Write-Output hi",
        runtime="auto",
        home=FIXTURES,
    )
    assert r.status == "ok", r.render_text()
    assert captured, "script never reached run_argv"
    assert captured[0][0] == "pwsh"
    assert captured[0][0] != "bash"

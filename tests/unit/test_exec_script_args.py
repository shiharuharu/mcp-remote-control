"""Script-form argument binding, and stream attribution in the exec body.

Pins the ``script_args`` contract of :func:`build_script_argv`: arguments are
*bound* to the script rather than pasted into its text, and the PowerShell body
form - the one runtime whose arguments must appear inside the command text at
all, because ``-Command`` appends trailing tokens to that text - places each
argument as a single-quoted literal inside a script-block invocation. The same
contract is pinned one level up, through :func:`exec_ops.run`, so a refactor
that stops forwarding ``script_args`` from ``_dispatch`` cannot ship green; the
same level pins that an argument belonging to one form (``script_args``, and a
``runtime`` naming an interpreter) is refused rather than dropped when another
form is chosen. Also pins that the runtime is matched by basename (``pwsh.exe``,
``C:\\...\\pwsh.exe``, ``python3.12``) and that every inline-body runtime without
a binding form (``cmd``, and any unrecognised interpreter) refuses arguments
instead of splicing them. Finally pins the ``[stderr]`` label that keeps a
stderr-only exec body distinguishable from program data on stdout.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mcp_remote_control.core import exec_ops
from mcp_remote_control.core.result import OpResult
from mcp_remote_control.endpoint import get_registry, reset_registry
from mcp_remote_control.exec import (
    build_script_argv,
    normalize_runtime,
    run_script_on_transport,
)
from mcp_remote_control.transport.base import (
    ExecResult,
    TransportError,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


class _CaptureTransport:
    """Minimal transport that records the argv handed to ``run_argv``."""

    name = "capture"
    meta: dict[str, Any] = {}
    remote_shell_family: str | None = None
    cwd: str | None = None

    def __init__(self) -> None:
        self.argv: list[str] | None = None

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del cwd, timeout_s, env
        self.argv = [str(a) for a in argv]
        return ExecResult(exit_code=0, stdout="", stderr="")


# --- PowerShell body: arguments bind to the script -------------------------


def test_powershell_body_binds_args_inside_script_block() -> None:
    """Args reach the script as a script block invocation, not as extra text."""
    argv = build_script_argv(
        body="Write-Output $args[0]",
        runtime="pwsh",
        args=["hello world"],
    )
    assert argv[:4] == ["pwsh", "-NoProfile", "-NonInteractive", "-Command"]
    # One command-text element: nothing follows it as a separate argv token,
    # because PowerShell appends such tokens to the command text itself.
    assert len(argv) == 5, argv
    assert argv[4] == "& {\nWrite-Output $args[0]\n} 'hello world'"
    # The space-bearing argument is one quoted literal, so `$args[0]` receives
    # the whole value instead of the parser splitting it into two tokens.
    assert "'hello world'" in argv[4]


def test_powershell_body_quotes_metacharacter_args() -> None:
    """Quotes, ``$``, ``;`` and spaces cannot escape their argument slot."""
    argv = build_script_argv(
        body="Write-Output $args",
        runtime="powershell",
        args=["a b", "it's", "$(Get-Date)", "x; Remove-Item /"],
    )
    assert argv[0] == "powershell"
    script = argv[-1]
    # Each argument is exactly one PowerShell single-quoted literal; a lone
    # quote is doubled (the only escape a single-quoted string has).
    assert script.endswith("'a b' 'it''s' '$(Get-Date)' 'x; Remove-Item /'")
    assert "$(Get-Date)" in script  # literal in the text, not an evaluated call
    assert len(argv) == 5


def test_powershell_body_closing_brace_own_line() -> None:
    """A trailing body comment cannot swallow the closing brace."""
    argv = build_script_argv(
        body="Write-Output hi # tail comment",
        runtime="pwsh",
        args=["x"],
    )
    script = argv[-1]
    assert script == "& {\nWrite-Output hi # tail comment\n} 'x'"


def test_powershell_body_without_args_keeps_plain_command_form() -> None:
    """No arguments means no wrapper: the body is the command text verbatim."""
    for args in (None, []):
        argv = build_script_argv(
            body="Get-Date",
            runtime="pwsh",
            args=args,
        )
        assert argv == [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-Date",
        ], argv


def test_powershell_path_script_keeps_file_argument_binding() -> None:
    """``-File`` binds trailing arguments natively; the shape stays as-is."""
    argv = build_script_argv(
        path="C:/s.ps1",
        runtime="pwsh",
        args=["hello world"],
    )
    assert argv == [
        "pwsh",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        "C:/s.ps1",
        "hello world",
    ]


def test_run_script_on_transport_hands_powershell_body_command_text() -> None:
    """The argv the transport receives carries the bound arguments."""
    t = _CaptureTransport()
    run_script_on_transport(
        t,  # type: ignore[arg-type]
        body="Write-Output $args[0]",
        runtime="pwsh",
        args=["hello world", "second"],
    )
    assert t.argv is not None
    assert t.argv[:4] == ["pwsh", "-NoProfile", "-NonInteractive", "-Command"]
    assert len(t.argv) == 5
    assert t.argv[4] == (
        "& {\nWrite-Output $args[0]\n} 'hello world' 'second'"
    )


# --- Other runtimes: arguments stay out of the body text --------------------


def test_posix_and_python_body_args_are_not_in_the_body() -> None:
    """Posix/Python bind positionally after the code, so the body is untouched."""
    bash = build_script_argv(body="echo $1", runtime="bash", args=["a b"])
    assert bash == ["bash", "-c", "echo $1", "bash", "a b"]
    assert bash[2] == "echo $1"

    python = build_script_argv(body="print(1)", runtime="python", args=["a b"])
    assert python[1] == "-c"
    assert python[2] == "print(1)"
    assert python[3:] == ["a b"]


# --- Runtime spellings: the basename decides the binding form ---------------


def test_exe_spelling_selects_the_same_binding_form() -> None:
    """``pwsh.exe`` / ``powershell.exe`` bind like their bare names.

    Without basename matching these fall through to the raw-interpreter shape
    ``<exe> -c <body> <args...>``, where PowerShell's ``-c`` appends the argument
    to the command text instead of binding it.
    """
    for runtime in ("pwsh.exe", "PWSH.EXE", "powershell.exe"):
        argv = build_script_argv(
            body="Write-Output $args[0]",
            runtime=runtime,
            args=["hello world"],
        )
        assert argv[0] == runtime.lower()
        assert argv[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
        assert argv[4] == "& {\nWrite-Output $args[0]\n} 'hello world'"


def test_windows_interpreter_path_keeps_binding_on_any_controller() -> None:
    """A backslash interpreter path is matched by basename, not by ``Path.name``.

    ``pathlib`` does not split ``\\`` on POSIX, so the path spelling must still
    reach the PowerShell branch - while ``argv[0]`` stays the caller's path
    rather than collapsing to ``pwsh``.
    """
    exe = r"C:\Program Files\PowerShell\7\pwsh.exe"
    body_argv = build_script_argv(
        body="Write-Output $args[0]",
        runtime=exe,
        args=["hello world"],
    )
    assert body_argv[0].lower() == exe.lower()
    assert body_argv[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
    assert body_argv[4] == "& {\nWrite-Output $args[0]\n} 'hello world'"

    path_argv = build_script_argv(
        path=r"C:\jobs\s.ps1",
        runtime=exe,
        args=["a b"],
    )
    assert path_argv[0].lower() == exe.lower()
    assert path_argv[1:4] == ["-NoProfile", "-NonInteractive", "-File"]
    assert path_argv[4:] == [r"C:\jobs\s.ps1", "a b"]


def test_bash_exe_body_does_not_shift_arguments_into_dollar_zero() -> None:
    """``bash.exe`` must bind like ``bash``, not via the raw fallback.

    The fallback omits the ``$0`` slot, so ``bash -c 'echo $1' a b`` would
    hand ``a`` to ``$0`` and print ``b``.
    """
    argv = build_script_argv(body="echo $1", runtime="bash.exe", args=["a b"])
    assert argv == ["bash.exe", "-c", "echo $1", "bash", "a b"]


def test_explicit_python_spelling_keeps_the_callers_executable() -> None:
    """Only the canonical ``python`` token is rewritten to a PATH launcher."""
    argv = build_script_argv(
        body="print(1)", runtime=r"C:\Python312\python.exe", args=["a b"]
    )
    assert argv[0].lower() == r"c:\python312\python.exe"
    assert argv[1:3] == ["-c", "print(1)"]
    assert argv[3:] == ["a b"]


def test_versioned_python_spelling_binds_arguments_to_argv() -> None:
    """``python3.12`` is python: its ``-c`` arguments reach ``sys.argv``.

    Without this the versioned spelling falls out of the python family into
    the unrecognised-interpreter branch, which binds nothing - so the same
    body and arguments would work under ``python`` and be refused (or, before
    the fallback refused, be spliced) under ``python3.12``.
    """
    for runtime in ("python3.12", "/usr/local/bin/python3.11", "pythonw3.11"):
        argv = build_script_argv(body="pass", runtime=runtime, args=["a b"])
        assert argv == [runtime, "-c", "pass", "a b"], argv


def test_bare_windowed_python_launcher_binds_arguments() -> None:
    """``pythonw`` is python: it reads ``-c`` arguments into ``sys.argv`` too.

    Its versioned sibling ``pythonw3.11`` already bound, so the bare windowed
    spelling must not be the one spelling that falls into the
    unrecognised-interpreter branch and is refused.
    """
    for runtime in ("pythonw", "pythonw3.11"):
        argv = build_script_argv(body="pass", runtime=runtime, args=["a b"])
        assert argv == [runtime, "-c", "pass", "a b"], argv
    # A windowed *path* is still the caller's executable, not the bare token.
    argv = build_script_argv(
        body="pass", runtime=r"C:\Python312\pythonw.exe", args=["a b"]
    )
    assert argv[0].lower() == r"c:\python312\pythonw.exe"
    assert argv[1:] == ["-c", "pass", "a b"]


def test_explicit_interpreter_path_is_never_collapsed_to_its_alias() -> None:
    """A runtime that names a path stays that path in ``argv[0]``.

    Collapsing ``/usr/bin/python3`` to the ``python`` alias turns the caller's
    chosen interpreter into a PATH lookup, so a different program runs with no
    warning; only a *bare* token may be normalised to its alias.
    """
    for runtime in ("/usr/bin/python3", "/opt/homebrew/bin/bash", "/usr/bin/pwsh"):
        argv = build_script_argv(body="print(1)", runtime=runtime, args=["a b"])
        assert argv[0] == runtime, argv
    # Bare tokens keep normalising (the alias table is unchanged by path-ness).
    assert normalize_runtime("python3") == "python"
    assert normalize_runtime("bash") == "bash"
    assert normalize_runtime("/usr/bin/python3") == "/usr/bin/python3"


def test_explicit_interpreter_path_runs_that_file(tmp_path: Path) -> None:
    """End to end: the file named by ``runtime`` is what executes.

    The probe is named ``python3`` - an alias basename - so collapsing the
    path to its alias would resolve ``python`` on PATH and lose the marker.
    """
    probe = tmp_path / "python3"
    probe.write_text(
        "#!/bin/sh\nprintf 'CUSTOM-INTERPRETER-RAN\\n'\n", encoding="utf-8"
    )
    probe.chmod(0o755)

    result = exec_ops.run(
        ep="local",
        script="print(1)",
        runtime=str(probe),
        home=FIXTURES,
    )
    assert result.status == "ok", result.render_text()
    assert "CUSTOM-INTERPRETER-RAN" in (result.body or "")


# --- cmd: no inline-body binding form, so arguments are refused -------------


def test_cmd_inline_body_rejects_arguments() -> None:
    """``cmd /c`` reads the rest of its line as command text, not as ``%1``.

    Splicing would run ``& del /f /q /`` as a second command, so the shape is
    rejected for the bare token and the ``.exe`` spelling alike.
    """
    for runtime in ("cmd", "cmd.exe"):
        with pytest.raises(TransportError) as ei:
            build_script_argv(
                body="echo hi", runtime=runtime, args=["& del /f /q /"]
            )
        assert ei.value.code == "INVALID_ARG"
    # No arguments: the plain inline form is unchanged.
    assert build_script_argv(body="echo hi", runtime="cmd") == [
        "cmd",
        "/c",
        "echo hi",
    ]


def test_cmd_script_path_still_binds_arguments() -> None:
    """A batch file is the supported way to pass arguments through cmd."""
    assert build_script_argv(
        path=r"C:\jobs\s.cmd", runtime="cmd", args=["a b"]
    ) == ["cmd", "/c", r"C:\jobs\s.cmd", "a b"]


def test_cmd_inline_batch_file_body_binds_arguments() -> None:
    """An inline body that *is* a batch file binds like the path form.

    ``cmd /c deploy.cmd prod`` lands ``prod`` in the batch file's ``%1``, so
    refusing it would reject a shape cmd handles natively. Only a body that is
    command text (more than one token, or one carrying cmd syntax) has no
    positional slot and stays refused.
    """
    assert build_script_argv(
        body="deploy.cmd", runtime="auto", args=["prod"], dialect="cmd"
    ) == ["cmd", "/c", "deploy.cmd", "prod"]
    assert build_script_argv(
        body=r"C:\jobs\deploy.bat", runtime="cmd", args=["a b"]
    ) == ["cmd", "/c", r"C:\jobs\deploy.bat", "a b"]

    for body in ("echo hi", "deploy.cmd extra", "deploy.cmd & del /f /q /", "call"):
        with pytest.raises(TransportError) as ei:
            build_script_argv(body=body, runtime="cmd", args=["prod"])
        assert ei.value.code == "INVALID_ARG", body
        assert "script_args" in str(ei.value), body


def test_cmd_quoted_batch_file_body_binds_arguments() -> None:
    r"""A fully quoted batch-file reference is one token to cmd, spaces and all.

    Quoting is how a path with spaces stays a single file name through cmd's
    split: ``cmd /c "C:\Program Files\jobs\deploy.cmd" prod`` keeps the quotes
    and binds ``prod`` as ``%1``. Refusing it would reject the one spelling a
    spaced path has. Content inside the quotes that is not a plain file
    reference - command text, further quoting, cmd syntax - stays refused.
    """
    body = '"C:\\Program Files\\jobs\\deploy.cmd"'
    assert build_script_argv(body=body, runtime="cmd", args=["prod"]) == [
        "cmd",
        "/c",
        body,
        "prod",
    ]
    assert build_script_argv(body='"deploy.cmd"', runtime="cmd", args=["a b"]) == [
        "cmd",
        "/c",
        '"deploy.cmd"',
        "a b",
    ]

    for bad in ('"echo hi"', '"deploy.cmd" extra', '"deploy"x.cmd"', '"deploy.cmd'):
        with pytest.raises(TransportError) as ei:
            build_script_argv(body=bad, runtime="cmd", args=["prod"])
        assert ei.value.code == "INVALID_ARG", bad
        assert "script_args" in str(ei.value), bad


# --- Unrecognised interpreter: also no inline-body binding form -------------


def test_unknown_interpreter_body_rejects_arguments() -> None:
    """A body form with no known ``-c`` binding refuses instead of splicing.

    The fallback used to append the values as trailing tokens, which is not a
    binding for any of these interpreters: ``ruby -c 'puts 1' "a b"`` exits 1
    with ``No such file or directory -- puts 1`` (``-c`` is a syntax check and
    the argument is read as a file name), and a bare posix shell takes the
    first trailing token as ``$0`` - measured with ``/bin/dash``: ``sh -c
    'echo 0=[$0] 1=[$1]' 'a b'`` prints ``0=[a b] 1=[]`` while the handled
    ``sh`` branch prints ``0=[sh] 1=[a b]``. The caller gets a structured
    error rather than a run whose parameters silently differ.
    """
    for runtime in ("ruby", "perl", "/usr/bin/ruby", "dash", "/bin/dash"):
        with pytest.raises(TransportError) as ei:
            build_script_argv(body="puts 1", runtime=runtime, args=["a b"])
        assert ei.value.code == "INVALID_ARG", runtime
        assert "script_args" in str(ei.value), runtime
    # No arguments: the plain inline form is unchanged.
    assert build_script_argv(body="puts 1", runtime="ruby") == [
        "ruby",
        "-c",
        "puts 1",
    ]


def test_unknown_interpreter_path_still_binds_arguments() -> None:
    """A script *file* binds through any interpreter: ``interp file arg1...``."""
    assert build_script_argv(
        body=None, path="job.rb", runtime="ruby", args=["a b"]
    ) == ["ruby", "job.rb", "a b"]


# --- exec_ops.run: script_args cross the public seam ------------------------


class _SshArgvCapture:
    """SSH connection double that records the argv the transport hands over."""

    def __init__(self) -> None:
        self.argv_calls: list[list[str]] = []

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout_s, env
        self.argv_calls.append([str(a) for a in argv])
        return ExecResult(exit_code=0, stdout="", stderr="", cwd=cwd or "/var/www")

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del command, timeout_s, env
        return ExecResult(exit_code=0, stdout="", stderr="", cwd=cwd or "/var/www")


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate the endpoint registry so ``run()`` resolves lab-ssh per test."""
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    reset_registry()
    yield
    reset_registry()


def _run_script(**kwargs: Any) -> tuple[OpResult, list[list[str]]]:
    """Call the public ``exec_ops.run`` seam against a recording SSH double."""
    conn = _SshArgvCapture()

    def connector(**_kwargs: object) -> _SshArgvCapture:
        return conn

    get_registry().ssh_connector = connector  # type: ignore[assignment]
    result = exec_ops.run(
        ep="lab-ssh",
        home=FIXTURES,
        connector=connector,
        **kwargs,
    )
    return result, conn.argv_calls


def test_exec_ops_run_forwards_script_args_to_the_transport() -> None:
    """Deleting the ``args=script_args`` forwarding must break this test."""
    result, calls = _run_script(
        script="echo $1", runtime="bash", script_args=["hello world"]
    )
    assert result.status == "ok", result.render_text()
    assert ["bash", "-c", "echo $1", "bash", "hello world"] in calls


def test_exec_ops_run_binds_powershell_body_args_to_the_script() -> None:
    """The public seam hands PowerShell one command text with the bound args."""
    result, calls = _run_script(
        script="Write-Output $args[0]",
        runtime="pwsh",
        script_args=["hello world"],
    )
    assert result.status == "ok", result.render_text()
    assert [
        "pwsh",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "& {\nWrite-Output $args[0]\n} 'hello world'",
    ] in calls


def test_exec_ops_run_reports_cmd_body_arguments_as_invalid_arg() -> None:
    """The refusal surfaces as a structured error, not a spliced command."""
    result, calls = _run_script(
        script="echo hi", runtime="cmd", script_args=["hi"]
    )
    assert result.status == "error", result.render_text()
    assert result.code == "INVALID_ARG"
    assert calls == []


def test_exec_ops_run_reports_unknown_runtime_args_as_invalid_arg() -> None:
    """An unrecognised body runtime reaches the transport with no argv at all."""
    result, calls = _run_script(
        script="puts 1", runtime="ruby", script_args=["a b"]
    )
    assert result.status == "error", result.render_text()
    assert result.code == "INVALID_ARG"
    assert calls == []


def test_exec_ops_run_refuses_script_args_with_command_and_argv() -> None:
    """A dropped argument list is invisible, so the pairing is refused.

    ``script_args`` is the script form's parameter list; with ``command=`` or
    ``argv=`` there is nothing to bind it to, and the old behaviour ran the
    form with status ok / exit 0 and the values silently unused.
    """
    for kwargs in (
        {"command": "echo PROG"},
        {"argv": ["/bin/echo", "PROG"]},
    ):
        result, calls = _run_script(script_args=["SHOULD_HAVE_APPEARED"], **kwargs)
        assert result.status == "error", result.render_text()
        assert result.code == "INVALID_ARG", kwargs
        assert "script_args" in str(result.fields.get("msg")), kwargs
        assert calls == []

    # The same forms without script_args are untouched.
    result, _ = _run_script(command="echo PROG")
    assert result.status == "ok", result.render_text()


def test_exec_ops_run_refuses_runtime_with_command_and_argv() -> None:
    """``runtime=`` names a script interpreter; command=/argv= never consult it.

    A dropped name is invisible in the result (status ok, exit 0, the runtime
    in no field), so ``exec runtime=python command='job.py'`` would report
    success while the shell, not python, ran the job. ``auto`` names no
    interpreter, so it stays accepted as the no-op it is.
    """
    for kwargs in (
        {"command": "echo PROG", "runtime": "python"},
        {"argv": ["/bin/echo", "PROG"], "runtime": "/usr/bin/python3"},
    ):
        result, calls = _run_script(**kwargs)
        assert result.status == "error", result.render_text()
        assert result.code == "INVALID_ARG", kwargs
        assert "runtime" in str(result.fields.get("msg")), kwargs
        assert calls == []

    # The script form is the one runtime= is meant for, and it still carries
    # the caller's interpreter path into argv[0]. Recorded first because the
    # run that opens the endpoint is the one holding the recording double.
    result, calls = _run_script(
        script="print(1)", runtime="/usr/bin/python3", script_args=["a"]
    )
    assert result.status == "ok", result.render_text()
    assert calls == [["/usr/bin/python3", "-c", "print(1)", "a"]]

    # ``auto`` names no interpreter, so command= is not refused.
    result, _ = _run_script(command="echo PROG", runtime="auto")
    assert result.status == "ok", result.render_text()


# --- exec body: stdout / stderr attribution --------------------------------


def _exec_body(
    *,
    stdout: str,
    stderr: str,
    command: str = "run",
) -> str | None:
    return exec_ops._build_body(
        form="command",
        command=command,
        argv=None,
        script=None,
        script_path=None,
        runtime=None,
        script_args=None,
        result=ExecResult(exit_code=0, stdout=stdout, stderr=stderr),
    )


def test_stderr_only_body_carries_the_marker() -> None:
    """stderr-only output is labelled, so it cannot read as stdout data."""
    body = _exec_body(stdout="", stderr="ONLYERR\n")
    assert body == "$ run\n[stderr]\nONLYERR", repr(body)


def test_marker_present_on_both_tracks_for_stderr_only() -> None:
    """The label reaches the Agent track; the JSON body matches it verbatim."""
    body = _exec_body(stdout="", stderr="boom\n")
    rendered = OpResult(kind="exec", status="ok", fields={"exit": 0}, body=body)
    assert "[stderr]\nboom" in rendered.render_text()
    assert "[stderr]\nboom" in str(json.loads(rendered.render_json())["body"])


def test_stdout_is_not_labelled_and_both_streams_keep_their_order() -> None:
    """stdout stays bare; stderr follows it behind the marker."""
    assert _exec_body(stdout="OUT\n", stderr="") == "$ run\nOUT"
    assert _exec_body(stdout="OUT\n", stderr="ERR\n") == (
        "$ run\nOUT\n[stderr]\nERR"
    )


def test_empty_stdout_and_stderr_keep_the_bare_echo() -> None:
    """No streams -> the echo line is the whole body, with no stray marker."""
    assert _exec_body(stdout="", stderr="") == "$ run"
    assert _exec_body(stdout="", stderr="   \n") == "$ run"


def test_whitespace_only_stdout_with_stderr_is_labelled() -> None:
    """A whitespace-only stdout does not suppress the marker."""
    assert _exec_body(stdout="  \n", stderr="ERR") == "$ run\n  \n[stderr]\nERR"

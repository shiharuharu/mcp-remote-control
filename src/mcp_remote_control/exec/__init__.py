"""Exec helpers: script form normalization and runtime -> argv mapping.

Builds argv for script body/path forms without shelling the body, and
formats the ``$ ...`` echo line used on the Agent track.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from mcp_remote_control.transport.base import BaseTransport, ExecResult, TransportError

# Canonical runtime tokens accepted by script form.
RUNTIME_ALIASES: dict[str, str] = {
    "auto": "auto",
    "bash": "bash",
    "sh": "sh",
    "zsh": "zsh",
    "python": "python",
    "python3": "python",
    "py": "python",
    "pwsh": "pwsh",
    "powershell": "powershell",
    "cmd": "cmd",
}


def _is_path_spelling(token: str) -> bool:
    """Does *token* name a location rather than a bare interpreter name?

    A token carrying a separator (``/usr/bin/python3``,
    ``C:\\Python312\\python.exe``) is a path the caller chose; a bare token
    (``python3``, ``pwsh.exe``) is a name to be resolved on the target PATH.
    Windows spellings are checked by separator, not by ``Path``, because
    ``pathlib`` does not split ``\\`` on POSIX.
    """
    return "/" in token or "\\" in token


def normalize_runtime(runtime: str | None) -> str:
    """Map free-form runtime string to a canonical token.

    Only a *bare* token is rewritten to its alias: collapsing a path spelling
    (``/usr/bin/python3`` -> ``python3``) would turn the caller's chosen
    interpreter into a PATH lookup and run a different program, so a path is
    returned exactly as given - case included, since the target filesystem
    decides which file that is. The binding form still keys off the basename;
    see ``_runtime_family``.
    """
    if runtime is None or not str(runtime).strip():
        return "auto"
    key = str(runtime).strip()
    if _is_path_spelling(key):
        return key
    return RUNTIME_ALIASES.get(key.lower(), key.lower())


def _is_python_launcher(base: str) -> bool:
    """True for the python family's versioned and windowed spellings.

    ``python3.12``, ``pythonw`` and ``pythonw3.11`` all read their ``-c``
    arguments into ``sys.argv`` exactly like the bare token, so they must not
    fall out of the python family: doing so would send them to the
    unrecognised-interpreter branch, which has no binding form to offer. The
    only suffix forms that qualify are an all-digit/dotted version, an
    optional ``w`` (the windowed launcher - same interpreter, no console), or
    that ``w`` plus a version; ``pythonww`` and ``python3w`` do not.
    """
    if not base.startswith("python"):
        return False
    rest = base[len("python") :]
    if rest[:1] == "w":
        rest = rest[1:]
    return all(ch.isdigit() or ch == "." for ch in rest)


def _runtime_family(token: str) -> str | None:
    """Canonical runtime for an interpreter name *or path*, or ``None``.

    Only the last path segment and a trailing ``.exe`` take part, so
    ``pwsh``, ``pwsh.exe`` and ``C:\\...\\pwsh.exe`` all select the same family
    on any controller OS - ``pathlib`` does not split backslash separators on
    POSIX, which would otherwise leave a Windows spelling unrecognised. A
    version-suffixed python launcher (``python3.12``) is the python family for
    the same reason: the caller meant python, and its ``-c`` binds arguments
    to ``sys.argv``. The caller's own token still becomes ``argv[0]``, so an
    explicit interpreter path is never replaced by the bare name. ``None``
    means the token is not a known interpreter (a plain interpreter name such
    as ``ruby``).
    """
    base = str(token).replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    if base.endswith(".exe"):
        base = base[: -len(".exe")]
    fam = RUNTIME_ALIASES.get(base)
    if fam is None and _is_python_launcher(base):
        return "python"
    return fam


def _python_exe(*, platform: str | None = None) -> str:
    """Return a *target-host* discoverable Python launcher name.

    Never returns the controller ``sys.executable`` absolute path. Shipping a
    controller venv path (e.g. ``/Users/me/.venv/bin/python``) into SSH/WinRM
    argv fails on the remote host; Agents must use PATH names instead.

    Strategy (searchable: ``python3`` / ``python`` / ``py``):
    - win32 / windows / cygwin / msys* -> ``py`` (Windows Python launcher)
    - otherwise (posix / linux / darwin / unknown) -> ``python3``

    Explicit interpreter paths still work when the caller passes
    ``runtime=/path/to/python3`` - ``normalize_runtime`` leaves a path spelling
    untouched, so the alias in its basename never reaches this helper.
    Local endpoints may still resolve ``python3``/``py`` via the target PATH;
    this helper intentionally does not prefer the controller interpreter.
    """
    plat = (platform or "").strip().lower()
    if (
        plat.startswith("win")
        or plat in ("windows", "cygwin", "msys", "msys2")
    ):
        return "py"
    return "python3"


def resolve_script_path_runtime(
    path: str,
    runtime: str,
    *,
    dialect: str | None = None,
    platform: str | None = None,
    shell_family: str | None = None,
) -> str:
    """Pick runtime for a script path when runtime=auto.

    Known suffixes win (``.ps1`` -> pwsh, ``.sh`` -> bash, ...). Unknown
    suffixes fall back to dialect, then platform / shell_family - never
    hardcode bash on Windows when dialect is missing.
    """
    if runtime != "auto":
        return runtime
    suffix = Path(path).suffix.lower()
    if suffix in {".py", ".pyw"}:
        return "python"
    if suffix in {".ps1"}:
        return "pwsh"
    if suffix in {".cmd", ".bat"}:
        return "cmd"
    if suffix in {".sh", ".bash"}:
        return "bash"
    return resolve_body_runtime(
        "auto",
        dialect=dialect,
        platform=platform,
        shell_family=shell_family,
    )


def resolve_body_runtime(
    runtime: str,
    *,
    dialect: str | None = None,
    platform: str | None = None,
    shell_family: str | None = None,
) -> str:
    """Map runtime token; ``auto`` follows dialect, then platform.

    With dialect: busybox/posix-sh -> ``sh``, powershell -> ``pwsh``, ...
    Without dialect: win32 -> ``pwsh`` (not bash); posix -> ``bash``.
    Explicit runtime tokens (including ``bash``) are returned unchanged.
    """
    if runtime != "auto":
        return runtime
    if dialect:
        from mcp_remote_control.shell.dialect import default_runtime_for_dialect

        return default_runtime_for_dialect(dialect)
    from mcp_remote_control.shell.dialect import platform_default_runtime

    return platform_default_runtime(
        platform=platform,
        shell_family=shell_family,
    )


def build_script_argv(
    *,
    body: str | None = None,
    path: str | None = None,
    runtime: str | None = None,
    args: list[str] | None = None,
    dialect: str | None = None,
    platform: str | None = None,
    shell_family: str | None = None,
) -> list[str]:
    """Build argv for a script form without using a shell for the body.

    body: interpreted via ``bash -c`` / ``python -c`` / etc.
    path: invoked as ``runtime path args...`` (path is on the *target* host).
    args:
        Bound to the script, never pasted in as free text: every form either
        binds the values or rejects them with ``INVALID_ARG``, so a runtime
        whose ``-c``/inline form cannot carry them is never silently
        reinterpreted. Binding forms, by family: shells and python via
        ``-c body arg1...`` (``$0`` is the name for shells, ``sys.argv`` for
        python); pwsh as a block, ``& { body } arg1...``, reaching ``$args``
        / ``param()``; the path form as ``runtime path arg1...`` - the file's
        own ``$1...``, ``-File`` for pwsh, ``/c file.cmd`` for cmd with ``%1``
        inside the batch file. cmd binds a body only when it is a single
        ``.cmd``/``.bat`` reference: any other body is command text, where a
        trailing token would run as a second command. Any other interpreter is
        rejected - ``-c`` is not a binding form in general. The runtime is
        matched by basename whatever the spelling, so an ``.exe`` spelling
        still binds; an explicit interpreter path stays ``argv[0]`` and only
        the bare ``python`` token becomes the PATH-discoverable launcher.
    dialect: when runtime is auto, selects sh vs bash / pwsh for body scripts.
    platform / shell_family:
        Fallback when dialect is missing (local win32 must not pick bash).
        Also selects the runtime=python launcher: win32 -> ``py``, else
        ``python3`` (never controller ``sys.executable``).
    """
    if body is not None and path is not None:
        raise TransportError(
            "INVALID_ARG",
            "script body and path are mutually exclusive",
        )
    if body is None and path is None:
        raise TransportError(
            "INVALID_ARG",
            "script requires body or path",
        )

    rt = normalize_runtime(runtime)
    extra = [str(a) for a in (args or [])]

    if path is not None:
        path_s = str(path)
        rt = resolve_script_path_runtime(
            path_s,
            rt,
            dialect=dialect,
            platform=platform,
            shell_family=shell_family,
        )
        return _argv_for_path(rt, path_s, extra, platform=platform)

    body_s = body if body is not None else ""
    rt = resolve_body_runtime(
        rt,
        dialect=dialect,
        platform=platform,
        shell_family=shell_family,
    )
    return _argv_for_body(rt, body_s, extra, platform=platform)


def _argv_for_path(
    runtime: str,
    path: str,
    args: list[str],
    *,
    platform: str | None = None,
) -> list[str]:
    # ``runtime`` is the caller's spelling of the interpreter and stays
    # argv[0] for every family below except the python launcher, so an
    # explicit interpreter path is never replaced by the bare name.
    fam = _runtime_family(runtime)
    if runtime == "python":
        # Canonical token (also reached from ``python3`` / ``py``): pick a
        # PATH-discoverable launcher. A python *spelling* such as
        # ``C:\\Python312\\python.exe`` is the caller's executable and takes
        # the generic branch, which binds ``interpreter script arg1...`` too.
        return [_python_exe(platform=platform), path, *args]
    if fam in ("bash", "sh", "zsh"):
        # ``interpreter script arg1...`` - the script's own $1... are bound.
        return [runtime, path, *args]
    if fam in ("pwsh", "powershell"):
        return [
            runtime,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            path,
            *args,
        ]
    if fam == "cmd":
        # ``cmd /c file.cmd a`` binds %1... inside the batch file.
        return [runtime, "/c", path, *args]
    # Treat runtime as an interpreter executable path/name.
    return [runtime, path, *args]


# Characters cmd treats as syntax inside a command line. A body carrying one
# outside its quoting is command text, not a file name: cmd splits it and runs
# the pieces.
_CMD_SYNTAX_CHARS = frozenset('&|<>^()%"!;,=')


def _cmd_body_binds_args(body: str) -> bool:
    """Can ``cmd /c <body> <args...>`` bind *args* positionally?

    Only when the inline body is a single token naming a ``.cmd``/``.bat``
    file: cmd then executes it as ``/c file.cmd arg1...`` and the arguments land
    in the batch file's ``%1...`` - the same shape the ``path`` form and the
    pre-refusal splice produced. Any other body is a command line whose
    trailing tokens cmd appends to that line, where they either extend the
    command or start a second one, never become a positional parameter.

    A body that is one fully quoted token counts as a single token: quoting is
    how a path with spaces (``"C:\\Program Files\\jobs\\deploy.cmd"``) stays one
    file name through cmd's split, so the reference is read inside the quotes.
    """
    token = str(body).strip()
    if not token or token != str(body):  # padded body is not one clean token
        return False
    if len(token) > 1 and token.startswith('"') and token.endswith('"'):
        # The quotes are the whole reason a spaced path survives; the text
        # inside them must carry no quoting or command syntax of its own.
        token = token[1:-1]
    elif any(ch.isspace() for ch in token):
        # Unquoted: whitespace means more than one token, i.e. command text.
        return False
    if not token.lower().endswith((".cmd", ".bat")):
        return False
    return not any(ch in _CMD_SYNTAX_CHARS for ch in token)


def _argv_for_body(
    runtime: str,
    body: str,
    args: list[str],
    *,
    platform: str | None = None,
) -> list[str]:
    if runtime == "python":
        # python -c code [args...] - args land in sys.argv after the '-c' slot.
        # See ``_argv_for_path`` for why only the canonical token is rewritten.
        return [_python_exe(platform=platform), "-c", body, *args]
    fam = _runtime_family(runtime)
    if fam == "python":
        # A python *spelling* (``python3.12``, ``C:\\...\\python.exe``) keeps the
        # caller's executable and binds like the canonical token.
        return [runtime, "-c", body, *args]
    if fam == "bash":
        # bash -c 'body' name arg1... -> $0=name, $1=arg1
        return [runtime, "-c", body, "bash", *args]
    if fam == "sh":
        return [runtime, "-c", body, "sh", *args]
    if fam == "zsh":
        return [runtime, "-c", body, "zsh", *args]
    if fam in ("pwsh", "powershell"):
        return _argv_for_powershell_body(runtime, body, args)
    if fam == "cmd":
        # cmd /c consumes the rest of its command line as command text, so only
        # a bare batch-file reference has a positional slot: cmd runs
        # ``/c deploy.cmd arg1...`` and the arguments land in the file's ``%1...``.
        # Anything else (``echo hi & del x``) would run the "argument" as a
        # second command, so refuse rather than splice.
        if args:
            if not _cmd_body_binds_args(body):
                raise TransportError(
                    "INVALID_ARG",
                    "script_args are not supported for an inline cmd body "
                    "that is not a single .cmd/.bat file reference; pass "
                    "script_path=<.cmd/.bat> so cmd binds them as %1, or "
                    "use argv=",
                )
        return [runtime, "/c", body, *args]
    # Unrecognised interpreter: no portable binding form exists. ``-c`` is not
    # a "run this text" switch in general - ruby and perl read it as "check
    # syntax", php as "load this config file", and a bare posix shell
    # (dash/ksh) takes the first trailing token as ``$0`` - so the arguments
    # would silently change what runs. Refuse rather than splice.
    if args:
        raise TransportError(
            "INVALID_ARG",
            f"script_args are not supported for runtime={runtime!r} with an "
            "inline body: this interpreter has no known argument-binding "
            "form. Name a known runtime (bash/sh/zsh/python/pwsh), pass "
            "script_path=<file> so the file's own arguments bind, or use "
            "argv=",
        )
    return [runtime, "-c", body]


def _ps_quote(value: str) -> str:
    """PowerShell single-quoted string literal.

    A single-quoted string has no escape sequences, so doubling ``'`` is the
    whole encoding: spaces, ``"``, ``$`` and ``;`` stay literal argument text.
    """
    return "'" + str(value).replace("'", "''") + "'"


def _argv_for_powershell_body(
    exe: str,
    body: str,
    args: list[str],
) -> list[str]:
    """argv for a PowerShell script *body* (``-Command`` form).

    ``-Command`` takes the script as text and PowerShell appends argument
    tokens that follow it to that text, so trailing argv elements would be
    parsed as more script instead of reaching the script as its arguments.
    With arguments the body is therefore invoked as a script block -
    ``& { <body> } <arg>`` - which binds them positionally to the block's
    ``$args`` / ``param()`` names. Each argument is emitted as a single-quoted
    literal, so spaces and quotes cannot split it or become script text; the
    body keeps its own line and the closing brace its own line so a trailing
    body comment cannot swallow it. Without arguments the plain
    ``-Command <body>`` shape is kept.

    The block form binds; it does not append. A body that is itself a whole
    command line (``body='winget'`` with ``args=['list']``) therefore runs with
    no arguments bound - the values sit unused in the block's ``$args``. Pass
    the command and its own arguments as ``argv=`` when that is the intent.

    ``-File`` binds arguments natively, but it needs the script to exist on the
    target host as a file while ``body`` is controller-side text.
    """
    if not args:
        return [exe, "-NoProfile", "-NonInteractive", "-Command", body]
    quoted = " ".join(_ps_quote(a) for a in args)
    command = f"& {{\n{body}\n}} {quoted}"
    return [exe, "-NoProfile", "-NonInteractive", "-Command", command]


def run_script_on_transport(
    transport: BaseTransport,
    *,
    body: str | None = None,
    path: str | None = None,
    runtime: str | None = None,
    args: list[str] | None = None,
    cwd: str | None = None,
    timeout_s: float | None = None,
    env: dict[str, str] | None = None,
    dialect: str | None = None,
) -> ExecResult:
    """Execute script form via ``transport.run_argv``.

    ``path`` is interpreted by the *target* host: local filesystem for the
    local transport, remote path for SSH. When the script content lives on
    the controller, pass ``body`` instead - there is no automatic
    local-file upload to the remote host.

    dialect:
        Endpoint shell dialect for ``runtime=auto`` (busybox -> ``sh -c``).
        When missing, falls back to transport meta / remote_shell_family /
        local platform so Windows hosts do not hardcode bash.
    """
    eff_dialect = dialect
    rem = getattr(transport, "remote_shell_family", None)
    tmeta = getattr(transport, "meta", None) or {}
    if not eff_dialect and isinstance(tmeta, dict):
        eff_dialect = tmeta.get("dialect")  # type: ignore[assignment]
    if not eff_dialect and rem:
        from mcp_remote_control.shell.dialect import resolve_dialect

        eff_dialect = resolve_dialect(shell_family=str(rem))

    # Target-host platform for runtime=auto (bash vs pwsh) and for
    # runtime=python launcher selection (python3 vs py). Never use the
    # controller sys.executable - remote argv must be PATH-discoverable.
    platform: str | None = None
    shell_family: str | None = str(rem) if rem else None
    if isinstance(tmeta, dict):
        if not shell_family:
            sf = tmeta.get("shell_family") or tmeta.get("shell")
            if sf:
                shell_family = str(sf)
        # Probe OS (e.g. "windows", "linux") when present - prefer over
        # controller OS so SSH/WinRM python argv matches the remote host.
        os_hint = tmeta.get("os")
        if os_hint is not None and str(os_hint).strip():
            platform = str(os_hint).strip()
    if platform is None and getattr(transport, "name", None) == "local":
        # Local: always use this process's platform (even when dialect is
        # already known from meta) so win32 picks ``py`` not ``python3``.
        platform = sys.platform
    if platform is None and shell_family:
        # Infer Windows-ish host when only shell family is known.
        fam = str(shell_family).strip().lower()
        if fam in (
            "powershell",
            "powershell.exe",
            "pwsh",
            "pwsh.exe",
            "ps",
            "ps1",
            "cmd",
            "cmd.exe",
            "command",
            "command.com",
        ):
            platform = "win32"

    argv = build_script_argv(
        body=body,
        path=path,
        runtime=runtime,
        args=args,
        dialect=str(eff_dialect) if eff_dialect else None,
        platform=platform,
        shell_family=shell_family,
    )
    return transport.run_argv(argv, cwd=cwd, timeout_s=timeout_s, env=env)


def format_command_echo(
    *,
    form: str,
    command: str | None = None,
    argv: list[str] | None = None,
    script_summary: str | None = None,
) -> str:
    """Single-line ``$ ...`` echo for the Agent-track body."""
    if form == "command" and command is not None:
        return f"$ {command}"
    if form == "argv" and argv is not None:
        return f"$ {shlex.join(str(a) for a in argv)}"
    if form == "script":
        return f"$ script {script_summary or ''}".rstrip()
    return f"$ {form}"


def script_summary(
    *,
    body: str | None,
    path: str | None,
    runtime: str | None,
    args: list[str] | None,
) -> str:
    rt = normalize_runtime(runtime)
    parts = [f"runtime={rt}"]
    if path:
        parts.append(f"path={path}")
    elif body is not None:
        one = " ".join(body.strip().split())
        if len(one) > 60:
            one = one[:57] + "..."
        parts.append(f"body={one!r}" if one else "body=")
    if args:
        parts.append(f"args={len(args)}")
    return " ".join(parts)


__all__ = [
    "RUNTIME_ALIASES",
    "build_script_argv",
    "format_command_echo",
    "normalize_runtime",
    "resolve_body_runtime",
    "resolve_script_path_runtime",
    "run_script_on_transport",
    "script_summary",
]

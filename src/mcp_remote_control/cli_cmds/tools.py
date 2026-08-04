"""CLI shells isomorphic to the MCP tools.

Subcommands: ``endpoint|exec|fs|screen|ps|console|config``. Each path is
parse argv → Core op → render (Agent text or ``--json``). No business logic
beyond argument shaping lives here.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, TextIO

from mcp_remote_control.cli_cmds import (
    EXIT_OK,
    EXIT_TRANSPORT,
    EXIT_USAGE,
    EXIT_VALIDATION,
)
from mcp_remote_control.core import (
    config_ops,
    console_ops,
    endpoint_ops,
    exec_ops,
    fs_ops,
    ps_ops,
    screen_ops,
)
from mcp_remote_control.core.result import OpResult, render_result

# Error codes that map to process exit 4 (transport / connect failures).
_TRANSPORT_CODES: frozenset[str] = frozenset(
    {
        "CONNECT_FAILED",
        "HOSTKEY_MISMATCH",
        "AUTH_FAILED",
        "NOT_CONNECTED",
    }
)


def _want_json(args: argparse.Namespace) -> bool:
    """True when global or local ``--json`` was set on the tool parser."""
    return bool(getattr(args, "json", False))


def _exit_for(result: OpResult) -> int:
    if result.is_ok():
        return EXIT_OK
    if result.code and result.code in _TRANSPORT_CODES:
        return EXIT_TRANSPORT
    return EXIT_VALIDATION


def _emit(result: OpResult, args: argparse.Namespace, stdout: TextIO) -> int:
    text = render_result(result, as_json=_want_json(args))
    stdout.write(text)
    if not text.endswith("\n"):
        stdout.write("\n")
    return _exit_for(result)


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    # SUPPRESS so a local --json does not overwrite a parent ``--json`` already True.
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print compact machine-track JSON instead of Agent text",
    )


# ---------------------------------------------------------------------------
# endpoint
# ---------------------------------------------------------------------------


def add_endpoint_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    p = subparsers.add_parser(
        "endpoint",
        help="endpoint lifecycle: list | open | close (MCP tool isomorphic)",
    )
    _add_json_flag(p)
    sub = p.add_subparsers(dest="endpoint_op", metavar="OP")

    p_list = sub.add_parser("list", help="list open endpoints / profiles")
    _add_json_flag(p_list)
    p_list.set_defaults(_handler=_handle_endpoint, endpoint_op="list")

    p_open = sub.add_parser("open", help="open endpoint from profile")
    _add_json_flag(p_open)
    p_open.add_argument(
        "--profile",
        "--ep",
        dest="profile",
        default=None,
        help="profile name to open",
    )
    p_open.set_defaults(_handler=_handle_endpoint, endpoint_op="open")

    p_close = sub.add_parser("close", help="close a connected endpoint")
    _add_json_flag(p_close)
    p_close.add_argument(
        "--ep",
        "--profile",
        dest="ep",
        default=None,
        help="endpoint / profile id to close",
    )
    p_close.set_defaults(_handler=_handle_endpoint, endpoint_op="close")

    p.set_defaults(_handler=_handle_endpoint_root)


def _handle_endpoint_root(args: argparse.Namespace) -> int:
    if getattr(args, "endpoint_op", None) is None:
        print(
            "usage: mcp-remote-control-cli endpoint {list,open,close} ...\n"
            "mcp-remote-control-cli endpoint: missing OP "
            "(serial console: use `console` command)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    return _handle_endpoint(args)


def _handle_endpoint(args: argparse.Namespace) -> int:
    op = getattr(args, "endpoint_op", None) or "list"
    result = endpoint_ops.run(
        op=op,
        profile=getattr(args, "profile", None),
        ep=getattr(args, "ep", None),
    )
    return _emit(result, args, sys.stdout)


# ---------------------------------------------------------------------------
# serial console
# ---------------------------------------------------------------------------


def add_console_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    p = subparsers.add_parser(
        "console",
        help="serial console: list|open|send|views|close|sessions",
    )
    _add_json_flag(p)
    sub = p.add_subparsers(dest="console_op", metavar="OP")

    p_list = sub.add_parser(
        "list",
        help="list system console device names (USB/UART/BT-mapped)",
    )
    _add_json_flag(p_list)
    p_list.set_defaults(_handler=_handle_console, console_op="list")

    p_open = sub.add_parser("open", help="open console by device= from list")
    _add_json_flag(p_open)
    p_open.add_argument("--path", "--device", dest="path", required=True)
    p_open.add_argument("--baud", type=int, default=115200)
    p_open.add_argument(
        "--max-lines",
        type=int,
        default=None,
        help="capture buffer lines (default 99999)",
    )
    p_open.add_argument("--label", default=None)
    p_open.set_defaults(_handler=_handle_console, console_op="open")

    p_send = sub.add_parser("send", help="write to open console")
    _add_json_flag(p_send)
    p_send.add_argument("--id", required=True)
    p_send.add_argument("--data", default=None)
    p_send.add_argument("--data-b64", dest="data_b64", default=None)
    p_send.add_argument(
        "--newline",
        action="store_true",
        help="append \\n to --data if missing",
    )
    p_send.set_defaults(_handler=_handle_console, console_op="send")

    p_views = sub.add_parser(
        "views",
        help="query capture buffer (tail|since|contains) — not raw driver recv",
    )
    _add_json_flag(p_views)
    p_views.add_argument("--id", required=True)
    p_views.add_argument(
        "--mode",
        default="tail",
        choices=("tail", "since", "contains"),
    )
    p_views.add_argument("--n", type=int, default=100)
    p_views.add_argument("--since", type=int, default=None, help="seq exclusive")
    p_views.add_argument("--contains", default=None, help="substring filter")
    p_views.add_argument("--context", type=int, default=3)
    p_views.add_argument("--settle-ms", type=int, default=50)
    p_views.add_argument("--with-seq", action="store_true")
    p_views.set_defaults(_handler=_handle_console, console_op="views")

    p_close = sub.add_parser("close", help="close open console")
    _add_json_flag(p_close)
    p_close.add_argument("--id", required=True)
    p_close.set_defaults(_handler=_handle_console, console_op="close")

    p_sess = sub.add_parser("sessions", help="list open console sessions")
    _add_json_flag(p_sess)
    p_sess.set_defaults(_handler=_handle_console, console_op="sessions")

    p.set_defaults(_handler=_handle_console_root)


def _handle_console_root(args: argparse.Namespace) -> int:
    if getattr(args, "console_op", None) is None:
        print(
            "usage: mcp-remote-control-cli console "
            "{list,open,send,views,close,sessions} ...\n"
            "mcp-remote-control-cli console: missing OP",
            file=sys.stderr,
        )
        return EXIT_USAGE
    return _handle_console(args)


def _handle_console(args: argparse.Namespace) -> int:
    op = getattr(args, "console_op", None) or "list"
    result = console_ops.run(
        op=op,
        path=getattr(args, "path", None),
        baud=getattr(args, "baud", None),
        max_lines=getattr(args, "max_lines", None),
        label=getattr(args, "label", None),
        id=getattr(args, "id", None),
        data=getattr(args, "data", None),
        data_b64=getattr(args, "data_b64", None),
        newline=bool(getattr(args, "newline", False)),
        mode=getattr(args, "mode", None),
        n=getattr(args, "n", None),
        since=getattr(args, "since", None),
        contains=getattr(args, "contains", None),
        context=getattr(args, "context", None),
        settle_ms=getattr(args, "settle_ms", 50),
        with_seq=bool(getattr(args, "with_seq", False)),
    )
    return _emit(result, args, sys.stdout)


# ---------------------------------------------------------------------------
# exec
# ---------------------------------------------------------------------------


def add_exec_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    p = subparsers.add_parser(
        "exec",
        help="remote non-interactive exec (MCP tool isomorphic)",
    )
    _add_json_flag(p)
    p.add_argument("--ep", default=None, help="endpoint / profile name")
    p.add_argument("--cwd", default=None, help="working directory on remote")
    p.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="timeout seconds",
    )
    p.add_argument(
        "--command",
        "-c",
        dest="exec_command",
        default=None,
        help="shell command string",
    )
    p.add_argument(
        "--argv",
        dest="exec_argv",
        nargs="+",
        default=None,
        help="argv form: executable and args",
    )
    p.add_argument(
        "--script",
        dest="exec_script",
        default=None,
        help="inline script body",
    )
    p.add_argument(
        "--script-file",
        dest="script_path",
        default=None,
        help="path to local/remote script file",
    )
    p.add_argument(
        "--runtime",
        default=None,
        help="script runtime: auto|bash|sh|python|pwsh|…",
    )
    p.add_argument(
        "--script-arg",
        dest="script_args",
        action="append",
        default=None,
        help="argument for script form (repeatable)",
    )
    p.add_argument(
        "form_words",
        nargs="*",
        help=(
            "form + payload after -- : "
            "command 'echo hi' | argv /bin/echo hi | script 'echo hi'"
        ),
    )
    p.set_defaults(_handler=_handle_exec)


def _handle_exec(args: argparse.Namespace) -> int:
    command, argv, script, script_path = _parse_exec_forms(args)
    result = exec_ops.run(
        ep=args.ep,
        command=command,
        argv=argv,
        script=script,
        script_path=script_path,
        runtime=getattr(args, "runtime", None),
        script_args=getattr(args, "script_args", None),
        cwd=args.cwd,
        timeout=args.timeout,
    )
    return _emit(result, args, sys.stdout)


def _parse_exec_forms(
    args: argparse.Namespace,
) -> tuple[str | None, list[str] | None, str | None, str | None]:
    """Resolve command / argv / script from flags and trailing form words.

    Supports harness style::

        mcp-remote-control-cli exec --ep local -- command 'echo hello'
        mcp-remote-control-cli exec --ep local -- argv /bin/echo hello
        mcp-remote-control-cli exec --ep local -- script 'echo hello'
    """
    command = getattr(args, "exec_command", None)
    argv = getattr(args, "exec_argv", None)
    script = getattr(args, "exec_script", None)
    script_path = getattr(args, "script_path", None)
    words = list(getattr(args, "form_words", None) or [])

    flag_set = any(x is not None for x in (command, argv, script, script_path))

    if words and not flag_set:
        form = words[0]
        rest = words[1:]
        if form == "command":
            command = " ".join(rest) if rest else None
        elif form == "argv":
            argv = rest if rest else None
        elif form == "script":
            # `script <body…>` or `script -- path` is not special-cased;
            # body is the remaining words joined (use --script-file for paths).
            script = " ".join(rest) if rest else None
        else:
            # Bare words without form keyword → shell command string.
            command = " ".join(words)
    elif words and flag_set:
        # Flags already chose a form; leftover positionals are ignored.
        pass

    return command, argv, script, script_path


# ---------------------------------------------------------------------------
# fs
# ---------------------------------------------------------------------------


def add_fs_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    p = subparsers.add_parser(
        "fs",
        help="filesystem ops: list|stat|read|write|put|get|mkdir|rm",
    )
    _add_json_flag(p)
    sub = p.add_subparsers(dest="fs_op", metavar="OP")

    def _fs_op(name: str, help_text: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text)
        _add_json_flag(sp)
        sp.add_argument("--ep", default=None, help="endpoint / profile")
        sp.add_argument("--path", default=None, help="remote path")
        sp.add_argument("--content", default=None, help="write content")
        sp.add_argument("--local", default=None, help="local path (put/get)")
        sp.add_argument(
            "--recursive",
            action="store_true",
            default=False,
            help="recursive (list/rm)",
        )
        if name in {"put", "get"}:
            sp.add_argument(
                "--progress",
                action="store_true",
                default=False,
                help="print transfer progress lines to stderr",
            )
        sp.set_defaults(_handler=_handle_fs, fs_op=name)
        return sp

    for name, help_text in (
        ("list", "list directory"),
        ("stat", "stat path"),
        ("read", "read file text"),
        ("write", "write file text"),
        ("put", "upload local → remote"),
        ("get", "download remote → local"),
        ("mkdir", "create directory"),
        ("rm", "remove path"),
    ):
        _fs_op(name, help_text)

    p.set_defaults(_handler=_handle_fs_root)


def _handle_fs_root(args: argparse.Namespace) -> int:
    if getattr(args, "fs_op", None) is None:
        print(
            "usage: mcp-remote-control-cli fs {list,stat,read,write,put,get,mkdir,rm} ...\n"
            "mcp-remote-control-cli fs: missing OP",
            file=sys.stderr,
        )
        return EXIT_USAGE
    return _handle_fs(args)


def _handle_fs(args: argparse.Namespace) -> int:
    op = args.fs_op
    progress = None
    if op in {"put", "get"} and (
        bool(getattr(args, "progress", False))
        or int(getattr(args, "verbose", 0) or 0) > 0
    ):
        progress = _stderr_progress_cb

    result = fs_ops.run(
        op=op,
        ep=getattr(args, "ep", None),
        path=getattr(args, "path", None),
        content=getattr(args, "content", None),
        local=getattr(args, "local", None),
        recursive=bool(getattr(args, "recursive", False)) or None,
        progress=progress,
    )
    return _emit(result, args, sys.stdout)


def _stderr_progress_cb(bytes_done: int, total: int | None) -> None:
    """Emit a parseable ``progress <done>[/<total>]`` line on stderr."""
    if total is not None:
        sys.stderr.write(f"progress {bytes_done}/{total}\n")
    else:
        sys.stderr.write(f"progress {bytes_done}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# screen
# ---------------------------------------------------------------------------


def add_screen_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    p = subparsers.add_parser(
        "screen",
        help="interactive PTY: open | send | close | list",
    )
    _add_json_flag(p)
    sub = p.add_subparsers(dest="screen_op", metavar="OP")

    p_open = sub.add_parser("open", help="open interactive screen on ep")
    _add_json_flag(p_open)
    p_open.add_argument("--ep", default=None, help="endpoint / profile")
    p_open.set_defaults(_handler=_handle_screen, screen_op="open")

    p_send = sub.add_parser("send", help="send actions to screen")
    _add_json_flag(p_send)
    p_send.add_argument("--id", dest="screen_id", default=None, help="screen id")
    p_send.add_argument(
        "--json-actions",
        dest="json_actions",
        default=None,
        help='JSON array of actions, e.g. [{"type":"text","text":"pwd","submit":true}]',
    )
    p_send.add_argument(
        "--wait",
        dest="json_wait",
        default=None,
        help='JSON WaitSpec, e.g. {"until":"idle","idle_ms":200,"timeout_ms":15000}',
    )
    shot_grp = p_send.add_mutually_exclusive_group()
    shot_grp.add_argument(
        "--shot",
        dest="shot",
        action="store_true",
        default=None,
        help="return frame after send (default on)",
    )
    shot_grp.add_argument(
        "--no-shot",
        dest="shot",
        action="store_false",
        help="skip frame capture after send",
    )
    p_send.set_defaults(_handler=_handle_screen, screen_op="send", shot=None)

    p_close = sub.add_parser("close", help="close screen session")
    _add_json_flag(p_close)
    p_close.add_argument("--id", dest="screen_id", default=None, help="screen id")
    p_close.set_defaults(_handler=_handle_screen, screen_op="close")

    p_list = sub.add_parser("list", help="list open screens")
    _add_json_flag(p_list)
    p_list.set_defaults(_handler=_handle_screen, screen_op="list")

    p.set_defaults(_handler=_handle_screen_root)


def _handle_screen_root(args: argparse.Namespace) -> int:
    if getattr(args, "screen_op", None) is None:
        print(
            "usage: mcp-remote-control-cli screen {open,send,close,list} ...\n"
            "mcp-remote-control-cli screen: missing OP",
            file=sys.stderr,
        )
        return EXIT_USAGE
    return _handle_screen(args)


def _handle_screen(args: argparse.Namespace) -> int:
    op = args.screen_op
    import json

    actions = None
    raw = getattr(args, "json_actions", None)
    if raw:
        try:
            actions = json.loads(raw)
        except json.JSONDecodeError as exc:
            result = OpResult(
                kind="screen",
                status="error",
                code="INVALID_ARG",
                fields={"op": op, "msg": f"bad --json-actions: {exc}"},
            )
            return _emit(result, args, sys.stdout)

    wait = None
    raw_wait = getattr(args, "json_wait", None)
    if raw_wait:
        try:
            wait = json.loads(raw_wait)
        except json.JSONDecodeError as exc:
            result = OpResult(
                kind="screen",
                status="error",
                code="INVALID_ARG",
                fields={"op": op, "msg": f"bad --wait: {exc}"},
            )
            return _emit(result, args, sys.stdout)

    shot = getattr(args, "shot", None)

    result = screen_ops.run(
        op=op,
        ep=getattr(args, "ep", None),
        id=getattr(args, "screen_id", None),
        actions=actions,
        wait=wait,
        shot=shot,
    )
    return _emit(result, args, sys.stdout)


# ---------------------------------------------------------------------------
# ps
# ---------------------------------------------------------------------------


def add_ps_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    p = subparsers.add_parser(
        "ps",
        help="persistent PowerShell session: open | invoke | close",
    )
    _add_json_flag(p)
    sub = p.add_subparsers(dest="ps_op", metavar="OP")

    p_open = sub.add_parser("open", help="open PS runspace on ep")
    _add_json_flag(p_open)
    p_open.add_argument("--ep", default=None, help="endpoint / profile (winrm)")
    p_open.set_defaults(_handler=_handle_ps, ps_op="open")

    p_invoke = sub.add_parser("invoke", help="invoke script in session")
    _add_json_flag(p_invoke)
    p_invoke.add_argument("--id", dest="session_id", default=None, help="session id")
    p_invoke.add_argument("--script", default=None, help="PowerShell script body")
    p_invoke.set_defaults(_handler=_handle_ps, ps_op="invoke")

    p_close = sub.add_parser("close", help="close PS session")
    _add_json_flag(p_close)
    p_close.add_argument("--id", dest="session_id", default=None, help="session id")
    p_close.set_defaults(_handler=_handle_ps, ps_op="close")

    p.set_defaults(_handler=_handle_ps_root)


def _handle_ps_root(args: argparse.Namespace) -> int:
    if getattr(args, "ps_op", None) is None:
        print(
            "usage: mcp-remote-control-cli ps {open,invoke,close} ...\n"
            "mcp-remote-control-cli ps: missing OP",
            file=sys.stderr,
        )
        return EXIT_USAGE
    return _handle_ps(args)


def _handle_ps(args: argparse.Namespace) -> int:
    op = args.ps_op
    result = ps_ops.run(
        op=op,
        ep=getattr(args, "ep", None),
        id=getattr(args, "session_id", None),
        script=getattr(args, "script", None),
    )
    return _emit(result, args, sys.stdout)


def add_config_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    p = subparsers.add_parser(
        "config",
        help="self-config MRC_HOME: home|ensure-home|get|list-profiles|get-profile|put-profile|delete-profile|put-secret|list-secrets",
    )
    _add_json_flag(p)
    sub = p.add_subparsers(dest="config_op", metavar="OP")

    for op, help_t in (
        ("home", "show resolved MRC_HOME paths"),
        ("ensure_home", "create layout + default config.toml"),
        ("get", "summary of config home"),
        ("list_profiles", "list profile names"),
        ("list_secrets", "list secret filenames (no bodies)"),
    ):
        sp = sub.add_parser(op.replace("_", "-") if op != "get" else op, help=help_t)
        sp.set_defaults(_handler=_handle_config, config_op=op)
        _add_json_flag(sp)

    p_getp = sub.add_parser("get-profile", help="show one profile (paths only)")
    _add_json_flag(p_getp)
    p_getp.add_argument("--name", required=True)
    p_getp.set_defaults(_handler=_handle_config, config_op="get_profile")

    p_put = sub.add_parser("put-profile", help="create/update profile TOML")
    _add_json_flag(p_put)
    p_put.add_argument("--name", required=True)
    p_put.add_argument("--transport", default=None)
    p_put.add_argument("--host", default=None)
    p_put.add_argument("--port", type=int, default=None)
    p_put.add_argument("--username", default=None)
    p_put.add_argument("--label", default=None)
    p_put.add_argument(
        "--auth-json",
        dest="auth",
        default=None,
        help='JSON object e.g. {"method":"private_key_path","key_path":"secrets/k"}',
    )
    p_put.add_argument("--ssh-json", dest="ssh", default=None)
    p_put.add_argument(
        "--winrm-json",
        dest="winrm",
        default=None,
        help='JSON object e.g. {"scheme":"https","auth":"ntlm"}',
    )
    p_put.add_argument(
        "--defaults-json",
        dest="defaults",
        default=None,
        help='JSON object of profile defaults e.g. {"cwd":"/var/www"}',
    )
    p_put.add_argument(
        "--caps-json",
        dest="caps",
        default=None,
        help='JSON object of capability overrides e.g. {"ps":true}',
    )
    p_put.add_argument("--body", default=None, help="raw profile TOML body")
    p_put.set_defaults(_handler=_handle_config, config_op="put_profile")

    p_del = sub.add_parser("delete-profile", help="delete profile file")
    _add_json_flag(p_del)
    p_del.add_argument("--name", required=True)
    p_del.set_defaults(_handler=_handle_config, config_op="delete_profile")

    p_sec = sub.add_parser("put-secret", help="write secrets/<name> (body not echoed)")
    _add_json_flag(p_sec)
    p_sec.add_argument("--name", required=True)
    src = p_sec.add_mutually_exclusive_group()
    src.add_argument(
        "--content",
        default=None,
        help=(
            "secret body — INSECURE: visible in process argv (ps -ef) and "
            "shell history; prefer --content-stdin or --content-file"
        ),
    )
    src.add_argument(
        "--content-stdin",
        action="store_true",
        help="read secret body from stdin (preferred for secrets)",
    )
    src.add_argument(
        "--content-file",
        default=None,
        help="read secret body from a local file path",
    )
    p_sec.set_defaults(_handler=_handle_config, config_op="put_secret")

    p.set_defaults(_handler=_handle_config_root)


def _handle_config_root(args: argparse.Namespace) -> int:
    if getattr(args, "config_op", None) is None:
        print(
            "usage: mcp-remote-control-cli config "
            "{home,ensure_home,get,list_profiles,...} ...\n"
            "mcp-remote-control-cli config: missing OP",
            file=sys.stderr,
        )
        return EXIT_USAGE
    return _handle_config(args)


def _resolve_secret_content(args: argparse.Namespace) -> str | None:
    """Resolve put-secret body from --content / --content-stdin / --content-file.

    Argparse's mutually exclusive group guarantees at most one source is set.
    Returns None when none are given (Core then reports MISSING_ARG).
    """
    if getattr(args, "content", None) is not None:
        return args.content
    if bool(getattr(args, "content_stdin", False)):
        return sys.stdin.read()
    fpath = getattr(args, "content_file", None)
    if fpath is not None:
        with open(fpath, "rb") as fh:
            return fh.read().decode("utf-8", errors="replace")
    return None


def _handle_config(args: argparse.Namespace) -> int:
    op = getattr(args, "config_op", None) or "home"
    content = getattr(args, "content", None)
    if op == "put_secret":
        try:
            content = _resolve_secret_content(args)
        except (OSError, FileNotFoundError, PermissionError) as exc:
            # Surface unreadable --content-file as a structured Core error
            # (INVALID_ARG → EXIT_VALIDATION), not a traceback from main.
            result = OpResult(
                kind="config",
                status="error",
                code="INVALID_ARG",
                fields={
                    "op": "put_secret",
                    "msg": (
                        f"cannot read --content-file "
                        f"{getattr(args, 'content_file', None)!r}: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                },
            )
            return _emit(result, args, sys.stdout)
    result = config_ops.run(
        op=op,
        name=getattr(args, "name", None),
        transport=getattr(args, "transport", None),
        host=getattr(args, "host", None),
        port=getattr(args, "port", None),
        username=getattr(args, "username", None),
        label=getattr(args, "label", None),
        auth=getattr(args, "auth", None),
        ssh=getattr(args, "ssh", None),
        winrm=getattr(args, "winrm", None),
        defaults=getattr(args, "defaults", None),
        caps=getattr(args, "caps", None),
        body=getattr(args, "body", None),
        content=content,
    )
    return _emit(result, args, sys.stdout)


def register_tool_parsers(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Register CLI subcommands isomorphic to MCP tools."""
    add_endpoint_parser(subparsers)
    add_exec_parser(subparsers)
    add_fs_parser(subparsers)
    add_screen_parser(subparsers)
    add_ps_parser(subparsers)
    add_console_parser(subparsers)
    add_config_parser(subparsers)

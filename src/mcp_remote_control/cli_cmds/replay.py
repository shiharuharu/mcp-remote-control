"""``mcp-remote-control-cli replay`` — re-play recorded PTY fixtures offline.

Runs screen frame fixtures through the same buffer path without a live TUI,
for CI and local harness checks.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, TextIO

from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_USAGE, EXIT_VALIDATION
from mcp_remote_control.screen.replay import (
    format_replay_agent_text,
    format_replay_json,
    replay_fixture,
    resolve_fixture_path,
)


def add_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    p = subparsers.add_parser(
        "replay",
        help="replay recorded PTY/frame fixtures (CI harness; no live TUI)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print compact JSON instead of Agent text",
    )
    p.add_argument(
        "target",
        nargs="?",
        default="screen",
        help="replay target (default: screen)",
    )
    p.add_argument(
        "--fixture",
        "-f",
        dest="fixture",
        default=None,
        help="fixture path or name under tests/fixtures/pty",
    )
    p.add_argument(
        "--cols",
        type=int,
        default=None,
        help="override fixture columns",
    )
    p.add_argument(
        "--rows",
        type=int,
        default=None,
        help="override fixture rows",
    )
    p.add_argument(
        "--check",
        action="store_true",
        default=False,
        help="fail when meta expect_hash/substrings do not match",
    )
    p.set_defaults(_handler=run)


def run(args: argparse.Namespace, stdout: TextIO | None = None) -> int:
    out = stdout or sys.stdout
    target = (getattr(args, "target", None) or "screen").strip().lower()
    if target not in ("screen", "pty", "frame"):
        # Allow bare fixture as first positional when it looks like a path/name.
        if getattr(args, "fixture", None) is None and target not in ("",):
            args.fixture = getattr(args, "target", None)
            target = "screen"
        else:
            print(
                "usage: mcp-remote-control-cli replay [screen] --fixture <path|name> [--cols N] [--rows N]",
                file=sys.stderr,
            )
            return EXIT_USAGE

    fixture = getattr(args, "fixture", None)
    if not fixture:
        print(
            "mcp-remote-control-cli replay: --fixture is required (e.g. bash_prompt or path to .bin)",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        path = resolve_fixture_path(str(fixture))
    except FileNotFoundError as exc:
        print(f"mcp-remote-control-cli replay: {exc}", file=sys.stderr)
        return EXIT_VALIDATION

    try:
        result = replay_fixture(
            path,
            cols=getattr(args, "cols", None),
            rows=getattr(args, "rows", None),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"mcp-remote-control-cli replay: failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_VALIDATION

    want_json = bool(getattr(args, "json", False))
    text = format_replay_json(result) if want_json else format_replay_agent_text(result)
    out.write(text)
    if not text.endswith("\n"):
        out.write("\n")

    if getattr(args, "check", False) and result.expect_ok is False:
        return EXIT_VALIDATION
    return EXIT_OK

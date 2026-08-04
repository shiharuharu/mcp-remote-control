"""CLI entry point: thin argparse shell over :mod:`cli_cmds`.

Harness and operators use the same Core ops as the MCP server. Business
logic lives in Core; this module only builds the parser and dispatches.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_USAGE
from mcp_remote_control.cli_cmds import doctor as doctor_cmd
from mcp_remote_control.cli_cmds import replay as replay_cmd
from mcp_remote_control.cli_cmds import selftest as selftest_cmd
from mcp_remote_control.cli_cmds import tools as tools_cmd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-remote-control-cli",
        description="mcp-remote-control harness CLI (same Core as MCP)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="machine-track JSON output (Agent text is default)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="increase stderr verbosity",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    doctor_cmd.add_parser(sub)
    selftest_cmd.add_parser(sub)
    replay_cmd.add_parser(sub)
    tools_cmd.register_tool_parsers(sub)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry for the mcp-remote-control harness CLI.

    Exit codes: 0 success, 2 usage, 3 validation, 4 transport/connect failure.
    """
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    handler = getattr(args, "_handler", None)
    if handler is None:
        # Bare invocation with no subcommand: print help and succeed.
        if args.command is None:
            parser.print_help()
            return EXIT_OK
        parser.error(f"unknown command: {args.command}")
        return EXIT_USAGE  # pragma: no cover — parser.error exits

    return int(handler(args))


if __name__ == "__main__":
    sys.exit(main())

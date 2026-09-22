#!/usr/bin/env python3
"""In-process screen open -> send -> close for smoke_local.

Endpoint and screen registries are process-local. Multi-process CLI sequences
like ``mcp-remote-control-cli screen open`` then ``mcp-remote-control-cli screen send`` fail with SCREEN_NOT_FOUND
because each ``mcp-remote-control-cli`` invocation is a new process. This helper runs the full
screen loop in one Python process via Core APIs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from mcp_remote_control.core import screen_ops
from mcp_remote_control.endpoint import reset_registry
from mcp_remote_control.screen.registry import reset_screen_registry

_SIMPLE_SHELL = "/bin/bash" if Path("/bin/bash").is_file() else "/bin/sh"


def main() -> int:
    home_raw = os.environ.get("MRC_HOME") or os.environ.get(
        "MCP_REMOTE_CONTROL_HOME"
    )
    if not home_raw:
        print(
            "local_screen_smoke: set MRC_HOME to a config home",
            file=sys.stderr,
        )
        return 1
    home = Path(home_raw).expanduser().resolve()

    reset_registry()
    reset_screen_registry()

    opened = screen_ops.open_screen(
        ep="local",
        home=home,
        shell=_SIMPLE_SHELL,
        settle_s=0.5,
        cols=120,
        rows=40,
    )
    header = opened.render_text().splitlines()[0] if opened.render_text() else ""
    print(f"screen open: {header}")
    if opened.status != "ok":
        print(opened.render_text(), file=sys.stderr)
        return 1
    sid = opened.fields.get("id") or opened.fields.get("screen_id")
    if not sid:
        print("local_screen_smoke: open missing screen id", file=sys.stderr)
        return 1

    sent = screen_ops.send_screen(
        id=str(sid),
        actions=[{"type": "text", "text": "pwd", "submit": True}],
        wait={"until": "idle", "idle_ms": 150, "timeout_ms": 8000},
    )
    send_header = sent.render_text().splitlines()[0] if sent.render_text() else ""
    print(f"screen send: {send_header}")
    if sent.status not in ("ok", "unchanged"):
        print(sent.render_text(), file=sys.stderr)
        return 1

    closed = screen_ops.close_screen(id=str(sid))
    close_header = (
        closed.render_text().splitlines()[0] if closed.render_text() else ""
    )
    print(f"screen close: {close_header}")
    if closed.status != "ok":
        print(closed.render_text(), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

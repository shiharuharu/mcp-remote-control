#!/usr/bin/env bash
# smoke_mcp.sh - L3-lite MCP smoke: list tools + call implemented ops (local).
#
# Uses MCPServer in-process (same process for open->send->close), not multi-process
# CLI. Screen/console registries are process-local.
#
# Usage (from repo root):
#   export MRC_HOME="$(pwd)/tests/fixtures/config"
#   ./scripts/harness/smoke_mcp.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if [[ -z "${MRC_HOME:-}" ]]; then
  export MRC_HOME="$ROOT/tests/fixtures/config"
fi
export MRC_HOME
echo "smoke_mcp: MRC_HOME=$MRC_HOME"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi
echo "smoke_mcp: PYTHON=$PYTHON"

echo "smoke_mcp: checking tool_names() ..."
"$PYTHON" - <<'PY'
from mcp_remote_control.mcp_server import tool_names

expected = ["endpoint", "exec", "fs", "screen", "ps", "console", "config"]
names = tool_names()
assert names == expected, f"tool_names={names!r} want {expected!r}"
print("tools:", ",".join(names))
PY

echo "smoke_mcp: MCPServer list_tools + local tool calls ..."
"$PYTHON" - <<'PY'
import asyncio
import os

from mcp_remote_control.endpoint import reset_registry
from mcp_remote_control.mcp_server import create_server
from mcp_remote_control.screen.registry import reset_screen_registry
from mcp_remote_control.serial.registry import reset_serial_registry

EXPECTED = ["endpoint", "exec", "fs", "screen", "ps", "console", "config"]


def _text(out) -> str:
    # SDK v2: CallToolResult.content; v1: list|(blocks, structured)
    if hasattr(out, "content") and not isinstance(out, (list, tuple)):
        blocks = out.content or []
    elif isinstance(out, tuple):
        blocks = out[0]
    else:
        blocks = out
    return "\n".join(getattr(b, "text", "") or "" for b in blocks)


async def main() -> None:
    reset_registry()
    reset_screen_registry()
    reset_serial_registry()
    mcp = create_server()

    tools = await mcp.list_tools()
    listed = [t.name for t in tools]
    assert listed == EXPECTED, f"list_tools={listed!r} want {EXPECTED!r}"

    text = _text(await mcp.call_tool("endpoint", {"op": "list"}))
    assert "@endpoint ok" in text, text
    print("endpoint:", text.splitlines()[0])

    text = _text(
        await mcp.call_tool("exec", {"ep": "local", "command": "echo hello"})
    )
    assert "@exec ok" in text, text
    assert any(ln == "hello" for ln in text.splitlines()), text
    print("exec:", text.splitlines()[0])

    # Small known dir; never list TMPDIR or /tmp (huge trees under pipefail).
    list_path = os.environ.get("MRC_HOME") or os.path.join(
        os.getcwd(), "tests", "fixtures", "config"
    )
    text = _text(
        await mcp.call_tool(
            "fs", {"op": "list", "ep": "local", "path": list_path}
        )
    )
    assert "@fs list ok" in text, text
    print("fs:", text.splitlines()[0])

    text = _text(
        await mcp.call_tool(
            "screen", {"op": "open", "ep": "local", "shell": "/bin/bash"}
        )
    )
    header = text.splitlines()[0]
    assert header.startswith("@screen ok"), text
    print("screen open:", header)

    sid = None
    open_cwd = None
    for part in text.split():
        if part.startswith("id="):
            sid = part.split("=", 1)[1]
        elif part.startswith("cwd="):
            open_cwd = part.split("=", 1)[1]
    assert sid, f"no screen id in: {text}"
    assert open_cwd, f"no cwd= on open header: {text}"

    text = _text(
        await mcp.call_tool(
            "screen",
            {
                "op": "send",
                "id": sid,
                "actions": [{"type": "text", "text": "pwd", "submit": True}],
                "wait": {
                    "until": "idle",
                    "idle_ms": 150,
                    "timeout_ms": 8000,
                },
            },
        )
    )
    header = text.splitlines()[0]
    assert header.startswith("@screen ok") or header.startswith(
        "@screen unchanged"
    ), text
    send_cwd = None
    for part in header.split():
        if part.startswith("cwd="):
            send_cwd = part.split("=", 1)[1]
            break
    # Proof of pwd: new frame after @screen ok, or cwd moved vs the open snapshot.
    # @screen unchanged repeating the open cwd is not enough even if cwd= is present.
    lines = text.splitlines()
    body_lines = [ln for ln in lines[1:] if ln.strip() and not ln.startswith("|")]
    has_frame = header.startswith("@screen ok") and bool(body_lines)
    cwd_changed = send_cwd is not None and send_cwd != open_cwd
    assert has_frame or cwd_changed, text
    print("screen send:", header)

    text = _text(await mcp.call_tool("screen", {"op": "close", "id": sid}))
    assert "@screen ok" in text, text
    print("screen close:", text.splitlines()[0])

    text = _text(await mcp.call_tool("ps", {"op": "open", "ep": "local"}))
    header = text.splitlines()[0]
    assert header.startswith("@ps error"), text
    assert "code=CAP_DENIED" in header, text
    print("ps:", header)

    text = _text(await mcp.call_tool("console", {"op": "list"}))
    header = text.splitlines()[0]
    assert header.startswith("@console ok"), text
    assert "n=" in header, text
    print("console:", header)

    text = _text(await mcp.call_tool("config", {"op": "home"}))
    # op_home emits ready=/layout= and omits the home path field
    header = text.splitlines()[0]
    assert header.startswith("@config ok"), text
    assert "ready=1" in header, text
    assert "layout=profiles,secrets,notes,state" in header, text
    assert "home=" not in text, text
    assert "op=home" not in text, text
    print("config:", header)

    text = _text(await mcp.call_tool("config", {"op": "list_profiles"}))
    header = text.splitlines()[0]
    assert header.startswith("@config ok"), text
    assert "n=" in header, text
    assert "local" in text, text
    assert "lab-ssh" in text, text
    assert "lab-win" in text, text
    assert "home=" not in header, text
    print("config list_profiles:", header)

    # Host notes hang on config (not an 8th tool). Fixture has no notes file.
    text = _text(
        await mcp.call_tool(
            "config", {"op": "notes", "action": "stat", "name": "local"}
        )
    )
    header = text.splitlines()[0]
    assert header.startswith("@config error"), text
    assert "NOTES_NOT_FOUND" in header, text
    print("config notes:", header)


asyncio.run(main())
print("smoke_mcp: PASS")
PY

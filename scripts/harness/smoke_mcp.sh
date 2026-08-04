#!/usr/bin/env bash
# smoke_mcp.sh — L3-lite MCP smoke: list tools + call implemented ops (local).
#
# Uses FastMCP in-process (same process for open→send→close), not multi-process
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

echo "smoke_mcp: checking tool_names() …"
"$PYTHON" - <<'PY'
from mcp_remote_control.mcp_server import tool_names

expected = ["endpoint", "exec", "fs", "screen", "ps", "console", "config"]
names = tool_names()
assert names == expected, f"tool_names={names!r} want {expected!r}"
print("tools:", ",".join(names))
PY

echo "smoke_mcp: FastMCP list_tools + local tool calls …"
"$PYTHON" - <<'PY'
import asyncio
import os

from mcp_remote_control.endpoint import reset_registry
from mcp_remote_control.mcp_server import create_server, tool_names
from mcp_remote_control.screen.registry import reset_screen_registry
from mcp_remote_control.serial.registry import reset_serial_registry

EXPECTED = tool_names()


def _text(out) -> str:
    blocks = out[0] if isinstance(out, tuple) else out
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
    assert "hello" in text, text
    print("exec:", text.splitlines()[0])

    list_path = os.environ.get("TMPDIR") or "/tmp"
    text = _text(
        await mcp.call_tool(
            "fs", {"op": "list", "ep": "local", "path": list_path}
        )
    )
    assert "@fs list ok" in text, text
    print("fs:", text.splitlines()[0])

    text = _text(await mcp.call_tool("screen", {"op": "open", "ep": "local"}))
    header = text.splitlines()[0]
    assert header.startswith("@screen ok"), text
    print("screen open:", header)

    sid = None
    for part in text.split():
        if part.startswith("id=") or part.startswith("screen_id="):
            sid = part.split("=", 1)[1]
            break
    assert sid, f"no screen id in: {text}"

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
    print("screen send:", header)

    text = _text(await mcp.call_tool("screen", {"op": "close", "id": sid}))
    assert "@screen ok" in text, text
    print("screen close:", text.splitlines()[0])

    text = _text(await mcp.call_tool("ps", {"op": "open", "ep": "local"}))
    assert "@ps" in text, text
    print("ps:", text.splitlines()[0])

    text = _text(await mcp.call_tool("console", {"op": "list"}))
    assert "@console" in text, text
    assert text.lstrip().startswith("@console"), text
    print("console:", text.splitlines()[0])

    text = _text(await mcp.call_tool("config", {"op": "home"}))
    assert "@config" in text, text
    assert "home=" in text or "op=home" in text.replace(" ", ""), text
    print("config:", text.splitlines()[0])

    text = _text(await mcp.call_tool("config", {"op": "list_profiles"}))
    assert "@config" in text, text
    print("config list_profiles:", text.splitlines()[0])


asyncio.run(main())
print("smoke_mcp: PASS")
PY

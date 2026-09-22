#!/usr/bin/env bash
# ci.sh - default harness gate. This file is the single definition of the gate:
# .github/workflows/ci.yml runs it (after its own checkout / python / uv setup).
#
# Default CI (no LLM, no business Agent, no real multi-host):
#   1. ruff check src tests                   # lint (pinned via the dev extra)
#   2. pytest -q --ignore=tests/integration   # unit + service + mcp
#   3. mcp-remote-control-cli doctor && mcp-remote-control-cli selftest             # offline env / mock reachability
#   4. ./scripts/harness/smoke_local.sh       # CLI local path + in-process screen
#   5. ./scripts/harness/smoke_mcp.sh         # host 5 + console + config, MCPServer local
#   6. mcp-remote-control-cli replay --fixture bash_prompt --check  # PTY fixture (no live TUI)
#
# Integration / real hosts are NEVER part of this script. When you have a
# lab and secrets configured:
#   export MRC_INTEGRATION=1
#   pytest -q tests/integration   # optional future gate; not required for green
#
# Screen is process-local: open->send->close must share one Python process
# (smoke_local uses local_screen_smoke.py; smoke_mcp uses in-process MCPServer).
# Multi-process CLI sequences like `mcp-remote-control-cli screen open` then `mcp-remote-control-cli screen send`
# in separate invocations will fail with SCREEN_NOT_FOUND.
#
# Usage (from repo root):
#   export MRC_HOME="$(pwd)/tests/fixtures/config"   # optional; defaulted below
#   ./scripts/harness/ci.sh
#
# Expect: exit 0. Host primitives stay exactly:
#   endpoint, exec, fs, screen, ps
# Shipping also registers console|config (not extra host primitives); step 5 smokes all registered names.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

# Prefer the project venv so a system pytest/cli cannot collect against a
# different site-packages (the MCP SDK lives in .venv).
if [[ -d "$ROOT/.venv/bin" ]]; then
  PATH="$ROOT/.venv/bin:${PATH}"
  export PATH
fi

export MRC_HOME="${MRC_HOME:-$ROOT/tests/fixtures/config}"
echo "ci: MRC_HOME=$MRC_HOME"
echo "ci: ROOT=$ROOT"
echo "ci: gate = ruff+unit+service+mcp+doctor+selftest+smoke+replay (no integration)"

# --- 1. Lint (ruff, pinned via the dev extra) -------------------------------
echo "ci: [1/6] ruff check src tests ..."
ruff check src tests

# --- 2. L0/L1/L3-lite tests (skip tests/integration) ------------------------
echo "ci: [2/6] pytest (unit+service+mcp, --ignore=tests/integration) ..."
pytest -q --ignore=tests/integration

# --- 3. Offline gates (doctor + selftest) -----------------------------------
echo "ci: [3/6] mcp-remote-control-cli doctor ..."
mcp-remote-control-cli doctor

echo "ci: [3/6] mcp-remote-control-cli selftest ..."
mcp-remote-control-cli selftest

# --- 4. Local path smoke (CLI + in-process screen) --------------------------
echo "ci: [4/6] smoke_local ..."
./scripts/harness/smoke_local.sh

# --- 5. MCP surface smoke (host 5 + console + config) ------------------------
echo "ci: [5/6] smoke_mcp ..."
./scripts/harness/smoke_mcp.sh

# --- 6. Screen fixture replay (no live TUI) ---------------------------------
echo "ci: [6/6] mcp-remote-control-cli replay --fixture bash_prompt --check ..."
mcp-remote-control-cli replay --fixture bash_prompt --check

echo "ci: PASS (default gate green; integration skipped)"
echo "ci: tip: MRC_INTEGRATION=1 + pytest tests/integration  # optional real hosts"

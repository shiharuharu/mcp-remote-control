#!/usr/bin/env bash
# ci.sh — canonical green path for W0–W6 (full harness closure).
#
# Default CI (no LLM, no business Agent, no real multi-host):
#   1. pytest -q --ignore=tests/integration   # unit + service + mcp
#   2. mcp-remote-control-cli doctor && mcp-remote-control-cli selftest             # offline env / mock reachability
#   3. ./scripts/harness/smoke_local.sh       # CLI local path + in-process screen
#   4. ./scripts/harness/smoke_mcp.sh         # 7 tools + FastMCP local calls
#   5. mcp-remote-control-cli replay --fixture bash_prompt --check  # PTY fixture (no live TUI)
#
# Integration / real hosts are NEVER part of this script. When you have a
# lab and secrets configured:
#   export MRC_INTEGRATION=1
#   pytest -q tests/integration   # optional future gate; not required for green
#
# Screen is process-local: open→send→close must share one Python process
# (smoke_local uses local_screen_smoke.py; smoke_mcp uses in-process FastMCP).
# Multi-process CLI sequences like `mcp-remote-control-cli screen open` then `mcp-remote-control-cli screen send`
# in separate invocations will fail with SCREEN_NOT_FOUND.
#
# Usage (from repo root):
#   export MRC_HOME="$(pwd)/tests/fixtures/config"   # optional; defaulted below
#   ./scripts/harness/ci.sh
#
# Expect: exit 0. Tool surface stays exactly:
#   endpoint, exec, fs, screen, ps
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

export MRC_HOME="${MRC_HOME:-$ROOT/tests/fixtures/config}"
echo "ci: MRC_HOME=$MRC_HOME"
echo "ci: ROOT=$ROOT"
echo "ci: gate = unit+service+mcp+doctor+selftest+smoke+replay (no integration)"

# --- 1. L0/L1/L3-lite tests (skip tests/integration) ------------------------
echo "ci: [1/5] pytest (unit+service+mcp, --ignore=tests/integration) …"
pytest -q --ignore=tests/integration

# --- 2. W0 offline gates ----------------------------------------------------
echo "ci: [2/5] mcp-remote-control-cli doctor …"
mcp-remote-control-cli doctor

echo "ci: [2/5] mcp-remote-control-cli selftest …"
mcp-remote-control-cli selftest

# --- 3. Local path smoke (CLI + in-process screen) --------------------------
echo "ci: [3/5] smoke_local …"
./scripts/harness/smoke_local.sh

# --- 4. MCP surface smoke (host 5 + console + config) ------------------------
echo "ci: [4/5] smoke_mcp …"
./scripts/harness/smoke_mcp.sh

# --- 5. Screen fixture replay (W6; no live TUI) -----------------------------
echo "ci: [5/5] mcp-remote-control-cli replay --fixture bash_prompt --check …"
mcp-remote-control-cli replay --fixture bash_prompt --check

echo "ci: PASS (W0–W6 default gate green; integration skipped)"
echo "ci: tip: MRC_INTEGRATION=1 + pytest tests/integration  # optional real hosts"

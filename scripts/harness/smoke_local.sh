#!/usr/bin/env bash
# smoke_local.sh - local path gate (no LLM, no multi-host).
#
# Process-local sessions: endpoint and screen registries live in-process.
# Multi-process CLI sequences like:
#   mcp-remote-control-cli screen open
#   mcp-remote-control-cli screen send   # FAILS SCREEN_NOT_FOUND - different process
# will not work. Screen open->send->close must run in one Python process
# (scripts/harness/local_screen_smoke.py calls Core APIs in-process).
#
# Usage (from repo root):
#   export MRC_HOME="$(pwd)/tests/fixtures/config"
#   ./scripts/harness/smoke_local.sh
#
# Optional CI chain:
#   pytest -q --ignore=tests/integration && ./scripts/harness/smoke_local.sh \
#     && ./scripts/harness/smoke_mcp.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if [[ -z "${MRC_HOME:-}" ]]; then
  export MRC_HOME="$ROOT/tests/fixtures/config"
fi
export MRC_HOME
echo "smoke_local: MRC_HOME=$MRC_HOME"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi
echo "smoke_local: PYTHON=$PYTHON"

fail() {
  echo "smoke_local: FAIL: $*" >&2
  exit 1
}

# First line only. `echo | head` SIGPIPEs under pipefail on large stdout.
header_line() {
  printf '%s\n' "${1%%$'\n'*}"
}

# --- 1. endpoint list (CLI) -------------------------------------------------
echo "smoke_local: endpoint list ..."
out="$(mcp-remote-control-cli endpoint list)"
header_line "$out"
echo "$out" | grep -q '@endpoint ok' || fail "endpoint list header"
echo "$out" | grep -qE '^local transport=local( |$)' || fail "endpoint list missing local profile"

# --- 2. exec hello (CLI) ----------------------------------------------------
echo "smoke_local: exec hello ..."
out="$(mcp-remote-control-cli exec --ep local -- command 'echo hello')"
header_line "$out"
echo "$out" | grep -q '@exec ok' || fail "exec header"
echo "$out" | grep -qx 'hello' || fail "exec body missing hello"
echo "$out" | grep -qE 'cwd=/[^[:space:]]+' || fail "exec missing cwd"

# --- 3. fs list (CLI) -------------------------------------------------------
echo "smoke_local: fs list ..."
# Small known directory; avoid huge TMPDIR listings.
LIST_PATH="$ROOT/tests/fixtures/config"
out="$(mcp-remote-control-cli fs list --ep local --path "$LIST_PATH")"
header_line "$out"
echo "$out" | grep -q '@fs list ok' || fail "fs list header"
echo "$out" | grep -q 'ep=local' || fail "fs list missing ep=local"

# --- 4. screen open->send->close (single Python process) ----------------------
echo "smoke_local: screen open->send->close (in-process) ..."
out="$("$PYTHON" "$ROOT/scripts/harness/local_screen_smoke.py")" \
  || fail "screen in-process loop"
printf '%s\n' "$out"
# Helper prints "screen send: <header>"; require cwd=/ or an abs pwd line.
echo "$out" | grep -qE '^screen send:.*cwd=/[^[:space:]]+' \
  || echo "$out" | grep -qxE '/[^[:space:]]+' \
  || fail "screen send missing cwd=/pwd"

echo "smoke_local: PASS"

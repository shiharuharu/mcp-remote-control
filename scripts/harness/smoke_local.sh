#!/usr/bin/env bash
# smoke_local.sh — W1 local path gate (no LLM, no multi-host).
#
# Process-local sessions: endpoint and screen registries live in-process.
# Multi-process CLI sequences like:
#   mcp-remote-control-cli screen open
#   mcp-remote-control-cli screen send   # FAILS SCREEN_NOT_FOUND — different process
# will not work. Screen open→send→close must run in one Python process
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

fail() {
  echo "smoke_local: FAIL: $*" >&2
  exit 1
}

# --- 1. endpoint list (CLI) -------------------------------------------------
echo "smoke_local: endpoint list …"
out="$(mcp-remote-control-cli endpoint list)"
echo "$out" | head -n 1
echo "$out" | grep -q '@endpoint ok' || fail "endpoint list header"
echo "$out" | grep -q 'local' || fail "endpoint list missing local profile"

# --- 2. exec hello (CLI) ----------------------------------------------------
echo "smoke_local: exec hello …"
out="$(mcp-remote-control-cli exec --ep local -- command 'echo hello')"
echo "$out" | head -n 1
echo "$out" | grep -q '@exec ok' || fail "exec header"
echo "$out" | grep -q 'hello' || fail "exec body missing hello"
echo "$out" | grep -q 'cwd=' || fail "exec missing cwd"

# --- 3. fs list (CLI) -------------------------------------------------------
echo "smoke_local: fs list …"
# Use a known directory; avoid huge /tmp listings for the assertion path.
LIST_PATH="${TMPDIR:-/tmp}"
out="$(mcp-remote-control-cli fs list --ep local --path "$LIST_PATH")"
echo "$out" | head -n 1
echo "$out" | grep -q '@fs list ok' || fail "fs list header"
echo "$out" | grep -q 'ep=local' || fail "fs list missing ep=local"

# --- 4. screen open→send→close (single Python process) ----------------------
echo "smoke_local: screen open→send→close (in-process) …"
python "$ROOT/scripts/harness/local_screen_smoke.py" \
  || fail "screen in-process loop"

echo "smoke_local: PASS"

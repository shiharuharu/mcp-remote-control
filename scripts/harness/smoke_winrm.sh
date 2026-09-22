#!/usr/bin/env bash
# smoke_winrm.sh - WinRM path gate (open / exec / short timeout / fs / ps).
#
# Single-process Python smoke (scripts/harness/smoke_winrm.py): endpoint + ps
# registries are process-local. Do not chain multi-process CLI for ps.
#
# Requires a WinRM profile. Live default: buildbox-210-winrm under MRC_HOME.
# Offline: --mock (fixtures lab-win + hang connector; asserts wall-clock).
#
# Usage (from code-root / repo package root):
#   # dry-run (no network)
#   ./scripts/harness/smoke_winrm.sh --dry-run
#
#   # offline mock
#   ./scripts/harness/smoke_winrm.sh --mock
#
#   # true machine (WinRM profile + secrets under MRC_HOME)
#   MRC_HOME=~/.config/mcp-remote-control ./scripts/harness/smoke_winrm.sh
#   MRC_HOME=~/.config/mcp-remote-control ./scripts/harness/smoke_winrm.sh \
#     --profile buildbox-210-winrm
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi

exec "$PYTHON" "$ROOT/scripts/harness/smoke_winrm.py" "$@"

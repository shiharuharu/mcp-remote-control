#!/usr/bin/env bash
# W7 T26: Docker shell dialect matrix (busybox ash-friendly probes).
#
# Usage:
#   MRC_DOCKER=1 ./scripts/harness/docker_shell_matrix.sh
#   MRC_DOCKER=1 MATRIX_ONLY=busybox ./scripts/harness/docker_shell_matrix.sh
#
# Without MRC_DOCKER=1 -> exit 0 (skip). Requires Docker daemon.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if [[ "${MRC_DOCKER:-}" != "1" ]]; then
  echo "docker_shell_matrix: skip (set MRC_DOCKER=1 to run)"
  exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker_shell_matrix: docker not found" >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "docker_shell_matrix: docker daemon not available" >&2
  exit 2
fi

# Probe commands from registry (must stay in sync with shell/dialect.py).
# MARK must match PWD_MARKER.
MARK='__MRC_PWD__:'
PROBE_BASH=" set +o history 2>/dev/null||:;echo ${MARK}\$(pwd -P 2>/dev/null||pwd);set -o history 2>/dev/null||:;"
PROBE_ZSH=" fc -p 2>/dev/null||:;echo ${MARK}\$(pwd -P 2>/dev/null||pwd);fc -P 2>/dev/null||:;"
PROBE_SH=" echo ${MARK}\$(pwd -P 2>/dev/null||pwd)"
PROBE_BUSYBOX=" echo ${MARK}\$(pwd 2>/dev/null||pwd)"

ONLY="${MATRIX_ONLY:-all}"
FAIL=0
RAN=0

run_cell() {
  local name="$1" image="$2" shell_bin="$3" probe="$4" forbid_pat="$5"
  if [[ "$ONLY" != "all" && "$ONLY" != "$name" ]]; then
    return 0
  fi
  echo "=== matrix cell: $name ($image) ==="
  RAN=$((RAN + 1))
  if ! docker pull -q "$image" >/dev/null 2>&1; then
    if [[ "$name" == "busybox" ]]; then
      echo "FAIL: busybox image pull required" >&2
      FAIL=1
      return 1
    fi
    echo "SKIP: cannot pull $image"
    return 0
  fi
  # Reject forbidden substrings in probe for this cell
  if [[ -n "$forbid_pat" ]]; then
    if echo "$probe" | grep -E "$forbid_pat" >/dev/null; then
      echo "FAIL: $name probe contains forbidden pattern $forbid_pat" >&2
      FAIL=1
      return 1
    fi
  fi
  local out
  if ! out=$(docker run --rm "$image" "$shell_bin" -c "$probe" 2>/dev/null); then
    # Some images use sh without -c the same way - retry as sh -c
    if ! out=$(docker run --rm "$image" sh -c "$probe" 2>/dev/null); then
      echo "FAIL: $name probe command failed to execute" >&2
      FAIL=1
      return 1
    fi
  fi
  if ! echo "$out" | grep -F "$MARK" >/dev/null; then
    echo "FAIL: $name output missing MARK" >&2
    echo "output: $out" >&2
    FAIL=1
    return 1
  fi
  # Path after mark should look absolute (posix / or windows)
  local path
  path=$(echo "$out" | tr -d '\r' | grep -F "$MARK" | head -1 | sed "s/.*${MARK}//")
  if [[ -z "$path" || "$path" == "." ]]; then
    echo "FAIL: $name empty path after MARK ($path)" >&2
    FAIL=1
    return 1
  fi
  echo "OK $name path=$path"
}

# cells
run_cell bash "bash:5" bash "$PROBE_BASH" "fc -p|history -d" || true
run_cell busybox "busybox:1.36" sh "$PROBE_BUSYBOX" "fc -p|set \+o history|history -d" || true
run_cell sh "debian:bookworm-slim" sh "$PROBE_SH" "fc -p|set \+o history|history -d" || true
# zsh image may be large / missing - skip ok if pull fails
run_cell zsh "zshusers/zsh:5.9" zsh "$PROBE_ZSH" "set \+o history|history -d" || true

# Also assert host-side unit registry still green when venv present
if [[ -x "$ROOT/.venv/bin/pytest" ]]; then
  echo "=== host unit dialect lock ==="
  "$ROOT/.venv/bin/pytest" tests/unit/test_shell_dialect.py tests/unit/test_shell_wrap.py -q --tb=line
fi

if [[ "$RAN" -eq 0 ]]; then
  echo "FAIL: no cells ran (MATRIX_ONLY=$ONLY)" >&2
  exit 1
fi
if [[ "$FAIL" -ne 0 ]]; then
  echo "docker_shell_matrix: FAILED" >&2
  exit 1
fi
echo "docker_shell_matrix: all ran cells OK (ran=$RAN)"
exit 0

#!/usr/bin/env python3
"""WinRM smoke: open -> whoami -> short-timeout sleep -> whoami -> fs -> ps -> close.

Runs entirely in **one Python process** so endpoint + ps registries stay shared
(process-local; multi-process CLI ``ps open`` then ``ps invoke`` fails with
PS_NOT_FOUND).

Modes
-----
- **live** (default): real WinRM profile (default ``buildbox-210-winrm``).
  Requires ``MRC_HOME`` with that profile + secrets.
- **--mock**: offline path using fixture profile ``lab-win`` and an injectable
  connector that hangs on ``Start-Sleep`` so wall-clock timeout is asserted.
- **--dry-run**: print the planned steps / profile resolution; exit 0 without
  connecting.

Short-timeout step (W1/W4 acceptance):
  ``Start-Sleep 3`` with ``timeout=1`` must return with ``timed_out`` and wall
  clock under ~2s (client wait, not remote cancel).

Usage (from repo / code-root)::

    # dry-run (no network)
    .venv/bin/python scripts/harness/smoke_winrm.py --dry-run

    # offline mock (fixtures)
    .venv/bin/python scripts/harness/smoke_winrm.py --mock

    # true machine
    MRC_HOME=~/.config/mcp-remote-control \\
      .venv/bin/python scripts/harness/smoke_winrm.py --profile buildbox-210-winrm

    # or via shell wrapper
    ./scripts/harness/smoke_winrm.sh --mock
    MRC_HOME=~/.config/mcp-remote-control ./scripts/harness/smoke_winrm.sh
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent  # code-root/
_FIXTURES = _ROOT / "tests" / "fixtures" / "config"

DEFAULT_LIVE_PROFILE = "buildbox-210-winrm"
DEFAULT_MOCK_PROFILE = "lab-win"
DEFAULT_SLEEP_S = 3.0
DEFAULT_TIMEOUT_S = 1.0
# Upper bound for timeout=1 vs Start-Sleep 3 (leave slack for scheduling).
DEFAULT_WALL_MAX_S = 2.0
DEFAULT_FS_PATH = r"C:\Users"


def _fail(msg: str) -> int:
    print(f"smoke_winrm: FAIL: {msg}", file=sys.stderr)
    return 1


def _ok(msg: str) -> None:
    print(f"smoke_winrm: {msg}")


def _header(result: Any) -> str:
    text = result.render_text() if hasattr(result, "render_text") else str(result)
    line = text.splitlines()[0] if text else ""
    return line


# ---------------------------------------------------------------------------
# mock connector (offline path)
# ---------------------------------------------------------------------------


class _SmokeMockSession:
    """Injectable WinRM session: hang on Start-Sleep, whoami/fs/ps otherwise.

    ``run_command`` is used by WinRMTransport high-level path; hard-timeout
    wall-clock wraps it and disposes the session (W1). ``open_runspace`` feeds
    persistent PS. File methods satisfy ``is_file_client`` for fs list.
    """

    def __init__(self) -> None:
        self.cwd = r"C:\Users\Administrator"
        self.home = r"C:\Users\Administrator"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1.19041"
        self.closed = False
        self.block = threading.Event()
        self.commands: list[str] = []
        self._runspaces: list[Any] = []

    def close(self) -> None:
        self.closed = True
        self.block.set()
        for rs in self._runspaces:
            try:
                rs.close()
            except Exception:  # noqa: BLE001
                pass

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        del timeout_s, env
        from mcp_remote_control.transport.base import ExecResult

        self.commands.append(command)
        text = command if command is not None else ""
        # Hang only for short-timeout smoke step so wall-clock fires.
        if "Start-Sleep" in text:
            self.block.wait(timeout=60.0)
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr="mock still sleeping",
                cwd=cwd or self.cwd,
                timed_out=True,
            )
        if "whoami" in text.lower():
            return ExecResult(
                exit_code=0,
                stdout="mock\\administrator\n",
                stderr="",
                cwd=cwd or self.cwd,
            )
        return ExecResult(
            exit_code=0,
            stdout=f"winrm-out:{text}\n",
            stderr="",
            cwd=cwd or self.cwd,
        )

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        del timeout_s, env
        from mcp_remote_control.transport.base import ExecResult

        return ExecResult(
            exit_code=0,
            stdout=" ".join(str(a) for a in argv) + "\n",
            cwd=cwd or self.cwd,
        )

    def open_runspace(self) -> Any:
        from mcp_remote_control.ps.mock import MockRunspace

        rs = MockRunspace(location=self.cwd)
        self._runspaces.append(rs)
        return rs

    # --- file client surface (AdaptedWinRMSession.is_file_client) ---
    # Needs: stat, listdir, read_file, write_file

    def stat(self, path: str) -> dict[str, Any]:
        p = str(path).rstrip("\\/") or r"C:"
        lower = p.lower()
        if lower.endswith(".txt") or lower.endswith(".log"):
            return {
                "name": Path(p).name,
                "kind": "file",
                "size": 4,
                "mtime": 1.0,
                "mode": "Archive",
            }
        return {
            "name": Path(p).name or p,
            "kind": "dir",
            "size": 0,
            "mtime": 1.0,
            "mode": "Directory",
        }

    def listdir(self, path: str) -> list[str]:
        del path
        return ["Administrator", "Public"]

    def read_file(self, path: str, max_bytes: int | None = None) -> bytes:
        del path, max_bytes
        return b"mock"

    def write_file(self, path: str, data: bytes) -> None:
        del path, data


def _mock_connector(**_kwargs: object) -> _SmokeMockSession:
    return _SmokeMockSession()


# ---------------------------------------------------------------------------
# core smoke steps
# ---------------------------------------------------------------------------


def _require_home(home: Path) -> int | None:
    if not home.is_dir():
        return _fail(f"MRC_HOME not a directory: {home}")
    return None


def _profile_path(home: Path, name: str) -> Path:
    return home / "profiles" / f"{name}.toml"


def run_smoke(
    *,
    home: Path,
    profile: str,
    mock: bool,
    sleep_s: float,
    timeout_s: float,
    wall_max_s: float,
    fs_path: str,
) -> int:
    from mcp_remote_control.core import endpoint_ops, exec_ops, fs_ops, ps_ops
    from mcp_remote_control.endpoint import get_registry, reset_registry
    from mcp_remote_control.ps.registry import reset_ps_registry

    err = _require_home(home)
    if err is not None:
        return err
    if not _profile_path(home, profile).is_file():
        return _fail(
            f"profile {profile!r} missing under {home / 'profiles'} "
            f"(need a WinRM profile; live default is {DEFAULT_LIVE_PROFILE!r})"
        )

    reset_registry()
    reset_ps_registry()
    connector: Callable[..., Any] | None = _mock_connector if mock else None
    if connector is not None:
        # Ensure reconnect after hard-timeout dispose reuses the mock factory.
        get_registry().winrm_connector = connector

    home_s = str(home)
    ep = profile
    _ok(f"MRC_HOME={home}")
    _ok(f"profile={profile} mock={mock}")

    # --- 1. open ---
    _ok("endpoint open \u2026")
    opened = endpoint_ops.open_endpoint(
        profile=profile,
        home=home_s,
        connector=connector,
    )
    _ok(f"open: {_header(opened)}")
    if not opened.is_ok():
        return _fail(f"open failed: {opened.render_text()}")

    try:
        # --- 2. whoami ---
        _ok("exec whoami \u2026")
        who1 = exec_ops.run(
            ep=ep,
            command="whoami",
            home=home_s,
            connector=connector,
        )
        _ok(f"whoami: {_header(who1)}")
        if not who1.is_ok():
            return _fail(f"whoami failed: {who1.render_text()}")

        # --- 3. short timeout Start-Sleep ---
        sleep_cmd = f"Start-Sleep -Seconds {int(sleep_s) if sleep_s == int(sleep_s) else sleep_s}"
        _ok(f"exec {sleep_cmd!r} timeout={timeout_s} (wall \u2264 {wall_max_s}s) \u2026")
        t0 = time.monotonic()
        timed = exec_ops.run(
            ep=ep,
            command=sleep_cmd,
            timeout=timeout_s,
            home=home_s,
            connector=connector,
        )
        elapsed = time.monotonic() - t0
        _ok(f"timeout step: {_header(timed)} wall={elapsed:.3f}s")
        if timed.status != "timeout" and timed.fields.get("timed_out") is not True:
            return _fail(
                f"expected timeout/timed_out for {sleep_cmd!r} "
                f"timeout={timeout_s}, got status={timed.status!r} "
                f"fields={timed.fields!r}"
            )
        if timed.fields.get("timed_out") is not True:
            return _fail(f"fields.timed_out missing/false: {timed.fields!r}")
        if elapsed >= wall_max_s:
            return _fail(
                f"wall-clock {elapsed:.3f}s not under upper bound {wall_max_s}s "
                f"(timeout={timeout_s} vs Sleep {sleep_s})"
            )
        if elapsed < 0.05:
            # Defensive: zero-cost fake timeout is not a real wall-clock path.
            return _fail(f"wall-clock {elapsed:.3f}s suspiciously low")

        # --- 4. whoami again (ensure reconnect / recovery path) ---
        _ok("exec whoami (after timeout) \u2026")
        who2 = exec_ops.run(
            ep=ep,
            command="whoami",
            home=home_s,
            connector=connector,
        )
        _ok(f"whoami2: {_header(who2)}")
        if not who2.is_ok():
            # Honest failure: if reconnect is required and still broken, fail.
            return _fail(
                f"post-timeout whoami failed (expect ensure reconnect or "
                f"endpoint close+open): {who2.render_text()}"
            )
        # Success path should not carry dispose noise (W4).
        if who2.fields.get("timed_out") is True:
            return _fail("post-timeout whoami still marked timed_out")

        # --- 5. fs list ---
        _ok(f"fs list path={fs_path!r} \u2026")
        listed = fs_ops.run(
            op="list",
            ep=ep,
            path=fs_path,
            home=home_s,
            connector=connector,
        )
        _ok(f"fs: {_header(listed)}")
        if not listed.is_ok():
            return _fail(f"fs list failed: {listed.render_text()}")

        # --- 6. ps open / invoke / close (same process) ---
        _ok("ps open \u2192 invoke \u2192 close (in-process) \u2026")
        ps_open = ps_ops.open_session(
            ep=ep,
            home=home_s,
            connector=connector,
        )
        _ok(f"ps open: {_header(ps_open)}")
        if not ps_open.is_ok():
            return _fail(f"ps open failed: {ps_open.render_text()}")
        sid = ps_open.fields.get("id") or ps_open.fields.get("session_id")
        if not sid:
            return _fail(f"ps open missing id: {ps_open.render_text()}")

        # Dialect-safe for both live PS and MockRunspace.
        ps_script = "Write-Output 'ps-smoke-ok'"
        ps_inv = ps_ops.invoke(id=str(sid), script=ps_script)
        _ok(f"ps invoke: {_header(ps_inv)}")
        if not ps_inv.is_ok():
            return _fail(f"ps invoke failed: {ps_inv.render_text()}")
        if ps_inv.body is not None and "ps-smoke-ok" not in ps_inv.body:
            # Live hosts may wrap output; require body contains token when present.
            return _fail(f"ps invoke body missing token: {ps_inv.body!r}")

        ps_close = ps_ops.close_session(id=str(sid))
        _ok(f"ps close: {_header(ps_close)}")
        if not ps_close.is_ok():
            return _fail(f"ps close failed: {ps_close.render_text()}")
    finally:
        # --- 7. close endpoint ---
        _ok("endpoint close \u2026")
        closed = endpoint_ops.close_endpoint(ep=ep, home=home_s)
        _ok(f"close: {_header(closed)}")
        # close may fail if already torn down; only hard-fail when still open-ish.
        if not closed.is_ok() and closed.code not in (
            "ENDPOINT_NOT_FOUND",
        ):
            return _fail(f"close failed: {closed.render_text()}")

    _ok("PASS")
    return 0


def dry_run(*, home: Path | None, profile: str, mock: bool) -> int:
    _ok("mode=dry-run (no connect)")
    _ok(f"mock={mock}")
    _ok(f"profile={profile}")
    if home is None:
        _ok("MRC_HOME=<unset> \u2014 live mode needs MRC_HOME with WinRM profile")
        _ok(
            "steps: open \u2192 exec whoami \u2192 exec Start-Sleep "
            f"{DEFAULT_SLEEP_S}s timeout={DEFAULT_TIMEOUT_S} "
            f"(assert wall < {DEFAULT_WALL_MAX_S}s) \u2192 whoami \u2192 "
            f"fs list {DEFAULT_FS_PATH!r} \u2192 ps open/invoke/close \u2192 close"
        )
        return 0
    _ok(f"MRC_HOME={home}")
    if not home.is_dir():
        return _fail(f"MRC_HOME not a directory: {home}")
    p = _profile_path(home, profile)
    if p.is_file():
        _ok(f"profile file present: {p}")
    else:
        _ok(f"profile file missing (would fail live run): {p}")
    _ok(
        "steps: open \u2192 exec whoami \u2192 exec Start-Sleep "
        f"{DEFAULT_SLEEP_S}s timeout={DEFAULT_TIMEOUT_S} "
        f"(assert wall < {DEFAULT_WALL_MAX_S}s) \u2192 whoami \u2192 "
        f"fs list {DEFAULT_FS_PATH!r} \u2192 ps open/invoke/close \u2192 close"
    )
    _ok("PASS (dry-run)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="smoke_winrm",
        description=(
            "WinRM smoke (single process): open \u2192 whoami \u2192 short-timeout "
            "Start-Sleep \u2192 whoami \u2192 fs list \u2192 ps open/invoke/close \u2192 close. "
            "Requires a WinRM profile (live: buildbox-210-winrm; mock: lab-win)."
        ),
    )
    p.add_argument(
        "--profile",
        default=None,
        help=(
            f"endpoint profile name (default: {DEFAULT_LIVE_PROFILE} live, "
            f"{DEFAULT_MOCK_PROFILE} with --mock)"
        ),
    )
    p.add_argument(
        "--mock",
        action="store_true",
        help="offline mock path (fixture lab-win + hang-on-Start-Sleep connector)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print plan / profile resolution; do not connect",
    )
    p.add_argument(
        "--sleep-s",
        type=float,
        default=DEFAULT_SLEEP_S,
        help=f"remote Start-Sleep seconds (default {DEFAULT_SLEEP_S})",
    )
    p.add_argument(
        "--timeout-s",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"client wall-clock timeout for sleep step (default {DEFAULT_TIMEOUT_S})",
    )
    p.add_argument(
        "--wall-max-s",
        type=float,
        default=DEFAULT_WALL_MAX_S,
        help=f"assert timeout step finishes under this many seconds (default {DEFAULT_WALL_MAX_S})",
    )
    p.add_argument(
        "--fs-path",
        default=DEFAULT_FS_PATH,
        help=f"remote path for fs list (default {DEFAULT_FS_PATH!r})",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mock = bool(args.mock)
    profile = args.profile or (DEFAULT_MOCK_PROFILE if mock else DEFAULT_LIVE_PROFILE)

    home_raw = __import__("os").environ.get("MRC_HOME") or __import__("os").environ.get(
        "MCP_REMOTE_CONTROL_HOME"
    )
    if mock and not home_raw:
        home_raw = str(_FIXTURES)
    home: Path | None
    if home_raw:
        home = Path(home_raw).expanduser().resolve()
    else:
        home = None

    if args.dry_run:
        return dry_run(home=home, profile=profile, mock=mock)

    if home is None:
        return _fail(
            "set MRC_HOME to a config home with a WinRM profile "
            f"(e.g. buildbox-210-winrm), or pass --mock / --dry-run. "
            f"Example: MRC_HOME=~/.config/mcp-remote-control "
            f"{sys.argv[0]} --profile {DEFAULT_LIVE_PROFILE}"
        )

    return run_smoke(
        home=home,
        profile=profile,
        mock=mock,
        sleep_s=float(args.sleep_s),
        timeout_s=float(args.timeout_s),
        wall_max_s=float(args.wall_max_s),
        fs_path=str(args.fs_path),
    )


if __name__ == "__main__":
    sys.exit(main())

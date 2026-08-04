"""``mcp-remote-control-cli doctor`` — offline environment and config checks.

Validates config home, hard/soft Python dependencies, and profile syntax
without opening network connections.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_VALIDATION
from mcp_remote_control.config import (
    ConfigError,
    list_profiles,
    load_config,
    load_profile,
    resolve_home,
)

# Hard deps required for core transports / PTY.
_HARD_DEPS: tuple[str, ...] = ("asyncssh", "pyte", "pypsrp")
# Soft: MCP package optional when using the CLI harness alone.
_SOFT_DEPS: tuple[str, ...] = ("mcp",)


@dataclass
class CheckResult:
    """Single doctor check outcome."""

    name: str
    ok: bool
    detail: str = ""
    soft: bool = False  # soft failures warn only; they do not fail the run

    def line(self) -> str:
        if self.ok:
            status = "ok"
        elif self.soft:
            status = "warn"
        else:
            status = "fail"
        if self.detail:
            return f"{status}  {self.name}: {self.detail}"
        return f"{status}  {self.name}"


@dataclass
class DoctorReport:
    """Aggregated doctor results."""

    checks: list[CheckResult] = field(default_factory=list)
    home: Path | None = None

    @property
    def hard_failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok and not c.soft]

    @property
    def ok(self) -> bool:
        return not self.hard_failures

    def exit_code(self) -> int:
        return EXIT_OK if self.ok else EXIT_VALIDATION


def _try_import(modname: str) -> tuple[bool, str]:
    try:
        importlib.import_module(modname)
    except Exception as exc:  # noqa: BLE001 — report any import failure
        return False, f"{type(exc).__name__}: {exc}"
    return True, "importable"


def run_doctor(
    *,
    env: Mapping[str, str] | None = None,
    create: bool = False,
) -> DoctorReport:
    """Run doctor checks without printing (testable).

    Args:
        env: optional env mapping for ``resolve_home`` (defaults to ``os.environ``).
        create: if True and home is missing, create the directory (not default).
    """
    report = DoctorReport()

    # --- config home path resolved ---
    try:
        home = resolve_home(env=env)
    except Exception as exc:  # noqa: BLE001
        report.checks.append(
            CheckResult("config home", False, f"resolve failed: {exc}")
        )
        return report

    report.home = home
    report.checks.append(CheckResult("config home", True, str(home)))

    # --- home exists ---
    if home.is_dir():
        report.checks.append(CheckResult("home exists", True, str(home)))
    else:
        if create:
            try:
                home.mkdir(parents=True, exist_ok=True)
                report.checks.append(
                    CheckResult("home exists", True, f"created {home}")
                )
            except OSError as exc:
                report.checks.append(
                    CheckResult("home exists", False, f"create failed: {exc}")
                )
                return report
        else:
            report.checks.append(
                CheckResult(
                    "home exists",
                    False,
                    f"missing {home} (set MRC_HOME or pass --create)",
                )
            )

    # --- hard deps ---
    for mod in _HARD_DEPS:
        ok, detail = _try_import(mod)
        report.checks.append(CheckResult(f"import {mod}", ok, detail))

    # --- soft deps ---
    for mod in _SOFT_DEPS:
        ok, detail = _try_import(mod)
        report.checks.append(
            CheckResult(f"import {mod} (optional)", ok, detail, soft=True)
        )

    # If home missing and not created, skip config/profile load.
    if not home.is_dir():
        return report

    # --- load_config ---
    try:
        cfg = load_config(home)
        src = (
            "defaults (no config.toml)"
            if cfg.from_defaults
            else str(cfg.source_path or home / "config.toml")
        )
        report.checks.append(CheckResult("load_config", True, src))
    except ConfigError as exc:
        report.checks.append(CheckResult("load_config", False, str(exc)))
        return report
    except Exception as exc:  # noqa: BLE001
        report.checks.append(
            CheckResult("load_config", False, f"{type(exc).__name__}: {exc}")
        )
        return report

    # --- profiles syntax ---
    try:
        names = list_profiles(home)
    except Exception as exc:  # noqa: BLE001
        report.checks.append(
            CheckResult("list_profiles", False, f"{type(exc).__name__}: {exc}")
        )
        return report

    report.checks.append(
        CheckResult(
            "list_profiles",
            True,
            f"{len(names)} profile(s)" if names else "none",
        )
    )

    for name in names:
        try:
            profile = load_profile(home, name)
            report.checks.append(
                CheckResult(
                    f"profile {name}",
                    True,
                    f"transport={profile.transport}",
                )
            )
        except ConfigError as exc:
            report.checks.append(CheckResult(f"profile {name}", False, str(exc)))
        except Exception as exc:  # noqa: BLE001
            report.checks.append(
                CheckResult(
                    f"profile {name}",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            )

    return report


def format_report(report: DoctorReport) -> str:
    """Human-readable doctor output lines."""
    lines = [c.line() for c in report.checks]
    if report.ok:
        lines.append("doctor: PASS")
    else:
        n = len(report.hard_failures)
        lines.append(f"doctor: FAIL ({n} check(s) failed)")
    return "\n".join(lines) + "\n"


def cmd_doctor(
    *,
    env: Mapping[str, str] | None = None,
    create: bool = False,
    stdout: TextIO | None = None,
) -> int:
    """CLI entry for ``mcp-remote-control-cli doctor``. Returns exit code."""
    out = stdout if stdout is not None else sys.stdout
    report = run_doctor(env=env, create=create)
    out.write(format_report(report))
    return report.exit_code()


def add_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Register ``doctor`` on an argparse subparsers object."""
    p = subparsers.add_parser(
        "doctor",
        help="check config root, deps, and profile syntax (offline)",
    )
    p.add_argument(
        "--create",
        action="store_true",
        help="create config home directory if missing",
    )
    p.set_defaults(_handler=_handle_doctor)


def _handle_doctor(args: argparse.Namespace) -> int:
    return cmd_doctor(create=bool(args.create))

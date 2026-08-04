"""``mcp-remote-control-cli selftest`` — offline smoke checks (no network).

Exercises render roundtrip and config fixture load so a checkout can prove
basic health without transports or a live MCP host.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_VALIDATION
from mcp_remote_control.config import (
    list_profiles,
    load_config,
    load_profile,
    resolve_home,
)
from mcp_remote_control.render import render_agent_text, render_json


@dataclass
class SelftestStep:
    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        status = "ok" if self.ok else "fail"
        if self.detail:
            return f"{status}  {self.name}: {self.detail}"
        return f"{status}  {self.name}"


@dataclass
class SelftestReport:
    steps: list[SelftestStep] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    def exit_code(self) -> int:
        return EXIT_OK if self.ok else EXIT_VALIDATION


def locate_package_fixture_home() -> Path | None:
    """Locate ``tests/fixtures/config`` relative to a source/editable checkout.

    From this file under ``…/src/mcp_remote_control/cli_cmds/``, the package
    root is three parents up; also walks ancestors for non-src layouts.
    """
    here = Path(__file__).resolve()
    candidate = here.parents[3] / "tests" / "fixtures" / "config"
    if candidate.is_dir() and (candidate / "config.toml").is_file():
        return candidate
    for parent in here.parents:
        alt = parent / "tests" / "fixtures" / "config"
        if alt.is_dir() and (alt / "config.toml").is_file():
            return alt
    return None


def resolve_selftest_home(env: Mapping[str, str] | None = None) -> Path:
    """Pick config home for selftest: env override, else package fixture, else resolve_home."""
    mapping: Mapping[str, str] = os.environ if env is None else env
    for key in ("MRC_HOME", "MCP_REMOTE_CONTROL_HOME"):
        raw = mapping.get(key)
        if raw is not None and str(raw).strip() != "":
            return Path(raw).expanduser().resolve()

    fixture = locate_package_fixture_home()
    if fixture is not None:
        return fixture

    return resolve_home(env=mapping)


def _check_render() -> SelftestStep:
    """Basic render_agent_text + render_json roundtrip (no network)."""
    try:
        text = render_agent_text(
            "exec",
            "ok",
            cwd="/tmp",
            fields={"ep": "selftest", "exit": 0, "ms": 1},
            body="hello-selftest",
        )
        if not text.startswith("@exec ok"):
            return SelftestStep("render_agent_text", False, f"bad header: {text!r}")
        if "cwd=/tmp" not in text:
            return SelftestStep("render_agent_text", False, "missing cwd")
        if "hello-selftest" not in text:
            return SelftestStep("render_agent_text", False, "missing body")
        if text.lstrip().startswith("{"):
            return SelftestStep(
                "render_agent_text", False, "agent track looks like JSON"
            )

        raw = render_json(
            "exec",
            "ok",
            cwd="/tmp",
            fields={"ep": "selftest", "exit": 0, "ms": 1},
            body="hello-selftest",
        )
        data = json.loads(raw)
        if data.get("kind") != "exec" or data.get("status") != "ok":
            return SelftestStep("render_json", False, f"unexpected payload: {data!r}")
        if data.get("cwd") != "/tmp":
            return SelftestStep("render_json", False, "cwd mismatch")
        if data.get("body") != "hello-selftest":
            return SelftestStep("render_json", False, "body mismatch")

        # Compact (no indent)
        if "\n" in raw:
            return SelftestStep("render_json", False, "expected compact single-line JSON")

        return SelftestStep("render roundtrip", True, "agent+json ok")
    except Exception as exc:  # noqa: BLE001
        return SelftestStep("render roundtrip", False, f"{type(exc).__name__}: {exc}")


def _check_config(home: Path) -> list[SelftestStep]:
    steps: list[SelftestStep] = []
    steps.append(SelftestStep("config home", True, str(home)))

    if not home.is_dir():
        steps.append(SelftestStep("config home exists", False, f"missing {home}"))
        return steps
    steps.append(SelftestStep("config home exists", True, str(home)))

    try:
        cfg = load_config(home)
        detail = (
            "defaults"
            if cfg.from_defaults
            else f"from {cfg.source_path}"
        )
        steps.append(SelftestStep("load_config", True, detail))
    except Exception as exc:  # noqa: BLE001
        steps.append(
            SelftestStep("load_config", False, f"{type(exc).__name__}: {exc}")
        )
        return steps

    try:
        names = list_profiles(home)
        steps.append(
            SelftestStep(
                "list_profiles",
                True,
                f"{len(names)}: {', '.join(names) if names else '(none)'}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        steps.append(
            SelftestStep("list_profiles", False, f"{type(exc).__name__}: {exc}")
        )
        return steps

    for name in names:
        try:
            profile = load_profile(home, name)
            steps.append(
                SelftestStep(
                    f"load_profile {name}",
                    True,
                    f"transport={profile.transport}",
                )
            )
        except Exception as exc:  # noqa: BLE001
            steps.append(
                SelftestStep(
                    f"load_profile {name}",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            )

    return steps


def run_selftest(*, env: Mapping[str, str] | None = None) -> SelftestReport:
    """Run offline selftest steps (testable, no I/O to network)."""
    report = SelftestReport()
    report.steps.append(_check_render())
    home = resolve_selftest_home(env=env)
    report.steps.extend(_check_config(home))
    return report


def format_report(report: SelftestReport) -> str:
    lines = [s.line() for s in report.steps]
    if report.ok:
        lines.append("selftest: PASS")
    else:
        n = sum(1 for s in report.steps if not s.ok)
        lines.append(f"selftest: FAIL ({n} step(s) failed)")
    return "\n".join(lines) + "\n"


def cmd_selftest(
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
) -> int:
    """CLI entry for ``mcp-remote-control-cli selftest``. Returns exit code."""
    out = stdout if stdout is not None else sys.stdout
    report = run_selftest(env=env)
    out.write(format_report(report))
    return report.exit_code()


def add_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    p = subparsers.add_parser(
        "selftest",
        help="offline smoke: render roundtrip + config fixture load",
    )
    p.set_defaults(_handler=_handle_selftest)


def _handle_selftest(_args: argparse.Namespace) -> int:
    return cmd_selftest()

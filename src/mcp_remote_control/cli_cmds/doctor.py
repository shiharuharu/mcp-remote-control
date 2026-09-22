"""``mcp-remote-control-cli doctor`` - offline environment and config checks.

Validates config home, hard/soft Python dependencies, and profile syntax
without opening network connections. WinRM profiles additionally get a
self-check section: the effective scheme/auth/encryption, reconnect policy
and open-probe mode/budget the transport would use, plus risk notes for the
plain-HTTP message-encryption staleness window and for slow-link probe
budgets. Those notes are soft - they never change the exit code.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_VALIDATION
from mcp_remote_control.config import (
    ConfigError,
    Profile,
    list_profiles,
    load_config,
    load_profile,
    resolve_home,
)
from mcp_remote_control.endpoint.caps import coerce_toml_bool
from mcp_remote_control.transport import WINRM_AUTH_PROTOCOLS
from mcp_remote_control.transport.winrm_probe import (
    MRC_WINRM_PROBE_TIMEOUT_S,
    resolve_winrm_open_probe_mode,
    resolve_winrm_probe_timeout_s,
)
from mcp_remote_control.transport.winrm_timeouts import resolve_winrm_reconnect

# Hard deps required for core transports / PTY / serial console. Entries are
# import names, so they can differ from the distribution name (pyserial ships
# the ``serial`` package). Keep this list in step with ``pyproject.toml``:
# every required dependency must be probed here or in _SOFT_DEPS, or doctor
# reports PASS on an install where a tool is unusable.
_HARD_DEPS: tuple[str, ...] = ("asyncssh", "pyte", "pypsrp", "serial")
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
    except Exception as exc:  # noqa: BLE001 - report any import failure
        return False, f"{type(exc).__name__}: {exc}"
    return True, "importable"


def _winrm_effective_scheme(winrm: Mapping[str, Any]) -> str:
    """Effective ``http`` / ``https`` for a ``[winrm]`` table.

    Mirrors the transport's derivation: ``scheme`` decides, and a bare
    ``ssl`` flag switches to TLS with string-safe truthiness (``"false"`` /
    ``"0"`` / ``"no"`` must not enable it).
    """
    scheme = str(winrm.get("scheme") or "http").strip().lower()
    if scheme in ("https", "ssl", "true", "1"):
        return "https"
    return "https" if coerce_toml_bool(winrm.get("ssl", False)) else "http"


def _winrm_effective_auth(profile: Profile, winrm: Mapping[str, Any]) -> str:
    """pypsrp auth protocol this profile will use on the wire.

    ``[winrm].auth`` / ``auth_method`` wins; otherwise ``password`` maps to
    NTLM and any method the client does not implement falls back to NTLM
    (same mapping the transport applies when building the client).
    """
    explicit = winrm.get("auth") or winrm.get("auth_method")
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip().lower()
    method = profile.auth.method.lower() if profile.auth is not None else ""
    if method in ("", "password"):
        return "ntlm"
    return method if method in WINRM_AUTH_PROTOCOLS else "ntlm"


def _winrm_effective_encryption(winrm: Mapping[str, Any]) -> str:
    """Effective message-encryption mode (``auto`` when unset)."""
    return str(
        winrm.get("message_encryption") or winrm.get("encryption") or "auto"
    ).strip().lower()


def _winrm_reconnect_policy(winrm: Mapping[str, Any]) -> tuple[int, float] | None:
    """Effective ``(reconnection_retries, backoff_s)``, or ``None`` if opted out.

    Resolved by the transport's own resolver so the reported policy is the one
    pypsrp receives. A negative profile count is junk the transport drops
    before resolving; it must not be read here as the explicit opt-out that
    ``0`` means.
    """
    retries = winrm.get("reconnection_retries")
    if (
        isinstance(retries, (int, float))
        and not isinstance(retries, bool)
        and retries < 0
    ):
        retries = None
    return resolve_winrm_reconnect(retries, winrm.get("reconnection_backoff"))


def _winrm_self_checks(
    profile: Profile,
    *,
    global_winrm_probe: Any,
    env: Mapping[str, str] | None,
) -> list[CheckResult]:
    """Self-check lines for one WinRM profile (effective knobs + risk notes).

    Offline: reports the *configured* probe budget, never a measurement. The
    notes are soft so a lab-typical plain-HTTP or slow link stays a warning
    and the exit code is unchanged.
    """
    winrm = profile.winrm or {}
    scheme = _winrm_effective_scheme(winrm)
    encryption = _winrm_effective_encryption(winrm)
    reconnect = _winrm_reconnect_policy(winrm)
    reconnect_token = (
        "reconnection_retries=disabled(library default)"
        if reconnect is None
        else f"reconnection_retries={reconnect[0]} "
        f"reconnection_backoff={reconnect[1]:g}s"
    )
    probe_mode = resolve_winrm_open_probe_mode(
        winrm_cfg=winrm,
        profile_defaults=profile.defaults,
        global_winrm_probe=global_winrm_probe,
        env=env,
    )
    budget = resolve_winrm_probe_timeout_s(
        profile_value=winrm.get("probe_timeout_s"), env=env
    )
    checks = [
        CheckResult(
            f"winrm {profile.name}",
            True,
            f"scheme={scheme} auth={_winrm_effective_auth(profile, winrm)} "
            f"encryption={encryption} {reconnect_token} probe={probe_mode} "
            f"probe_timeout_s={budget:g}",
        )
    ]

    if scheme == "http" and encryption != "never":
        checks.append(
            CheckResult(
                f"winrm http+encryption ({profile.name})",
                False,
                f"scheme=http with encryption={encryption}: message encryption "
                "applies, so the session's encryption context goes stale after "
                "~4-6s idle; the link now self-heals (re-handshake + one "
                "replay) instead of failing the call. scheme=https has no such "
                "window.",
                soft=True,
            )
        )

    # Warn at or below the default, not only exactly at it: an explicitly
    # smaller budget is strictly worse for the open-time identity probe it
    # guards, so lowering the knob to "fix" a timeout must not stay silent.
    if budget <= MRC_WINRM_PROBE_TIMEOUT_S:
        checks.append(
            CheckResult(
                f"winrm probe budget ({profile.name})",
                False,
                f"probe_timeout_s={budget:g} is at or below the platform "
                f"default ({MRC_WINRM_PROBE_TIMEOUT_S:g}s); a high-latency "
                "link whose server round trips take several seconds needs a "
                "larger budget ([winrm].probe_timeout_s or "
                "MRC_WINRM_PROBE_TIMEOUT_S).",
                soft=True,
            )
        )

    return checks


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

    winrm_profiles: list[Profile] = []
    for name in names:
        try:
            profile = load_profile(home, name)
        except ConfigError as exc:
            report.checks.append(CheckResult(f"profile {name}", False, str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001
            report.checks.append(
                CheckResult(
                    f"profile {name}",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        report.checks.append(
            CheckResult(
                f"profile {name}",
                True,
                f"transport={profile.transport}",
            )
        )
        if profile.transport == "winrm":
            winrm_profiles.append(profile)

    # --- WinRM self-check (effective knobs + risk notes) ---
    # Only for profiles that actually exist: a home without WinRM adds no
    # lines at all, so the section cannot become noise.
    for profile in winrm_profiles:
        report.checks.extend(
            _winrm_self_checks(
                profile,
                global_winrm_probe=cfg.defaults.winrm_probe,
                env=env,
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


def format_report_json(report: DoctorReport) -> str:
    """Compact machine-track JSON for ``--json doctor`` (no indent)."""
    payload: dict[str, Any] = {
        "kind": "doctor",
        "status": "ok" if report.ok else "fail",
        "checks": [
            {
                "name": c.name,
                "ok": c.ok,
                "detail": c.detail,
                "soft": c.soft,
            }
            for c in report.checks
        ],
    }
    if report.home is not None:
        payload["home"] = str(report.home)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"


def cmd_doctor(
    *,
    env: Mapping[str, str] | None = None,
    create: bool = False,
    as_json: bool = False,
    stdout: TextIO | None = None,
) -> int:
    """CLI entry for ``mcp-remote-control-cli doctor``. Returns exit code."""
    out = stdout if stdout is not None else sys.stdout
    report = run_doctor(env=env, create=create)
    if as_json:
        out.write(format_report_json(report))
    else:
        out.write(format_report(report))
    return report.exit_code()


def add_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Register ``doctor`` on an argparse subparsers object."""
    p = subparsers.add_parser(
        "doctor",
        help="check config root, deps, and profile syntax (offline)",
    )
    # SUPPRESS so local --json does not overwrite a parent ``--json`` already True.
    p.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print compact machine-track JSON instead of human lines",
    )
    p.add_argument(
        "--create",
        action="store_true",
        help="create config home directory if missing",
    )
    p.set_defaults(_handler=_handle_doctor)


def _handle_doctor(args: argparse.Namespace) -> int:
    return cmd_doctor(
        create=bool(getattr(args, "create", False)),
        as_json=bool(getattr(args, "json", False)),
    )

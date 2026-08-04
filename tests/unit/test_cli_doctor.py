"""Unit tests for mrc doctor / selftest (T04)."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest

from mcp_remote_control.cli import main
from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_VALIDATION
from mcp_remote_control.cli_cmds.doctor import (
    cmd_doctor,
    format_report,
    run_doctor,
)
from mcp_remote_control.cli_cmds.selftest import (
    cmd_selftest,
    locate_package_fixture_home,
    run_selftest,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


# ---------------------------------------------------------------------------
# doctor — good fixture home
# ---------------------------------------------------------------------------


def test_doctor_fixture_home_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    report = run_doctor()
    assert report.ok, "\n".join(c.line() for c in report.checks)
    assert report.exit_code() == EXIT_OK
    assert report.home == FIXTURES.resolve()
    names = {c.name for c in report.checks}
    assert "config home" in names
    assert "home exists" in names
    assert "load_config" in names
    assert "import asyncssh" in names
    assert "import pyte" in names
    assert "import pypsrp" in names
    # fixture profiles
    assert any(c.name == "profile local" and c.ok for c in report.checks)
    assert any(c.name == "profile lab-ssh" and c.ok for c in report.checks)


def test_doctor_cmd_stdout_and_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    buf = StringIO()
    code = cmd_doctor(stdout=buf)
    out = buf.getvalue()
    assert code == EXIT_OK
    assert "doctor: PASS" in out
    assert "ok  config home:" in out or "ok  config home" in out


def test_main_doctor_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    assert main(["doctor"]) == EXIT_OK


# ---------------------------------------------------------------------------
# doctor — broken / missing home
# ---------------------------------------------------------------------------


def test_doctor_missing_home_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "no-such-mrc-home"
    monkeypatch.setenv("MRC_HOME", str(missing))
    report = run_doctor()
    assert not report.ok
    assert report.exit_code() == EXIT_VALIDATION
    assert any(c.name == "home exists" and not c.ok for c in report.checks)


def test_doctor_broken_config_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "bad-config"
    home.mkdir()
    (home / "config.toml").write_text("[[[not valid", encoding="utf-8")
    (home / "profiles").mkdir()
    monkeypatch.setenv("MRC_HOME", str(home))
    report = run_doctor()
    assert not report.ok
    assert report.exit_code() == EXIT_VALIDATION
    assert any(c.name == "load_config" and not c.ok for c in report.checks)


def test_doctor_broken_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "bad-profile"
    home.mkdir()
    (home / "config.toml").write_text(
        '[defaults]\nverbosity = "normal"\n',
        encoding="utf-8",
    )
    pdir = home / "profiles"
    pdir.mkdir()
    (pdir / "broken.toml").write_text(
        'name = "broken"\ntransport = "ssh"\n',  # missing host/username
        encoding="utf-8",
    )
    monkeypatch.setenv("MRC_HOME", str(home))
    report = run_doctor()
    assert not report.ok
    assert report.exit_code() == EXIT_VALIDATION
    assert any(c.name == "profile broken" and not c.ok for c in report.checks)


def test_doctor_create_missing_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "will-create"
    monkeypatch.setenv("MRC_HOME", str(missing))
    report = run_doctor(create=True)
    assert missing.is_dir()
    assert report.ok  # empty home: defaults config, no profiles
    assert report.exit_code() == EXIT_OK


# ---------------------------------------------------------------------------
# O12: doctor soft-warn — ``mcp`` import failure keeps report.ok True
# ---------------------------------------------------------------------------


def test_doctor_soft_warn_mcp_import_failure_still_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the optional ``mcp`` import fails, ``doctor`` must render a ``warn``
    line for ``import mcp (optional)`` and keep ``report.ok == True`` (soft
    failures do not fail the overall run). ``doctor.py:131`` marks soft-dep
    results with ``soft=True``; ``DoctorReport.hard_failures`` skips them.
    """
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    from mcp_remote_control.cli_cmds import doctor

    real_try_import = doctor._try_import

    def _force_mcp_fail(modname: str) -> tuple[bool, str]:
        if modname == "mcp":
            return False, "ImportError: forced soft-fail"
        return real_try_import(modname)

    monkeypatch.setattr(doctor, "_try_import", _force_mcp_fail)

    report = run_doctor()
    # Soft-failure keeps overall report.ok True.
    assert report.ok, "\n".join(c.line() for c in report.checks)
    assert report.exit_code() == EXIT_OK

    # The soft check is present, marked soft, not ok, and renders as `warn`.
    mcp_checks = [c for c in report.checks if c.name == "import mcp (optional)"]
    assert mcp_checks, "no `import mcp (optional)` check produced"
    mcp_check = mcp_checks[0]
    assert mcp_check.soft is True
    assert mcp_check.ok is False
    assert mcp_check.line().startswith("warn")  # soft failure → warn status

    # Hard deps still report ok (regression guard: monkeypatch only touched mcp).
    hard_names = {"import asyncssh", "import pyte", "import pypsrp"}
    hard_results = {c.name: c.ok for c in report.checks if c.name in hard_names}
    assert set(hard_results) == hard_names
    assert all(hard_results.values()), hard_results

    # The rendered text contains the warn line and still passes overall.
    text = format_report(report)
    assert "warn  import mcp (optional): ImportError: forced soft-fail" in text
    assert "doctor: PASS" in text
    # No `fail` line for mcp anywhere in the rendered output.
    assert "fail  import mcp" not in text


def test_doctor_cmd_stdout_renders_soft_warn_and_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cmd_doctor`` stdout must include the soft-warn line and PASS marker
    when ``mcp`` import fails. Also pins the CLI exit code (0).
    """
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    from mcp_remote_control.cli_cmds import doctor

    real_try_import = doctor._try_import

    def _force_mcp_fail(modname: str) -> tuple[bool, str]:
        if modname == "mcp":
            return False, "ImportError: forced"
        return real_try_import(modname)

    monkeypatch.setattr(doctor, "_try_import", _force_mcp_fail)

    buf = StringIO()
    code = cmd_doctor(stdout=buf)
    out = buf.getvalue()
    assert code == EXIT_OK
    assert "warn  import mcp (optional)" in out
    assert "doctor: PASS" in out
    assert "doctor: FAIL" not in out


def test_main_doctor_soft_warn_still_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: ``main(["doctor"])`` must return 0 when only the soft ``mcp``
    import fails. Guards the wiring ``_handle_doctor → cmd_doctor → exit_code``.
    """
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    from mcp_remote_control.cli_cmds import doctor

    real_try_import = doctor._try_import

    def _force_mcp_fail(modname: str) -> tuple[bool, str]:
        if modname == "mcp":
            return False, "ImportError: forced"
        return real_try_import(modname)

    monkeypatch.setattr(doctor, "_try_import", _force_mcp_fail)
    assert main(["doctor"]) == EXIT_OK


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------


def test_selftest_pass_with_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    report = run_selftest()
    assert report.ok, "\n".join(s.line() for s in report.steps)
    assert report.exit_code() == EXIT_OK


def test_selftest_cmd_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    buf = StringIO()
    code = cmd_selftest(stdout=buf)
    out = buf.getvalue()
    assert code == EXIT_OK
    assert "selftest: PASS" in out


def test_main_selftest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    assert main(["selftest"]) == EXIT_OK


def test_selftest_fixture_fallback_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MRC_HOME", raising=False)
    monkeypatch.delenv("MCP_REMOTE_CONTROL_HOME", raising=False)
    fixture = locate_package_fixture_home()
    # In repo checkout this must resolve.
    assert fixture is not None
    assert fixture.is_dir()
    report = run_selftest(env={})
    assert report.ok, "\n".join(s.line() for s in report.steps)


def test_selftest_bad_home_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "gone"
    monkeypatch.setenv("MRC_HOME", str(missing))
    report = run_selftest()
    assert not report.ok
    assert report.exit_code() == EXIT_VALIDATION
    buf = StringIO()
    code = cmd_selftest(stdout=buf)
    assert code == EXIT_VALIDATION
    assert "selftest: FAIL" in buf.getvalue()


# ---------------------------------------------------------------------------
# root CLI
# ---------------------------------------------------------------------------


def test_main_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as ei:
        main(["--help"])
    assert ei.value.code == 0


def test_main_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    code = main([])
    assert code == EXIT_OK
    captured = capsys.readouterr()
    assert "doctor" in captured.out or "usage" in captured.out.lower()


def test_main_unknown_command() -> None:
    with pytest.raises(SystemExit) as ei:
        main(["not-a-real-command"])
    assert ei.value.code == 2

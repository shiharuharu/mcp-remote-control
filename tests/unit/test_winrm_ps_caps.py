"""Unit tests: WinRM PowerShell capability probe parse / derive / collect_probe."""

from __future__ import annotations

import json
import threading
import time

import pytest

from mcp_remote_control.transport.winrm import (
    MRC_WINRM_PROBE_ENV,
    MRC_WINRM_PROBE_TIMEOUT_S,
    MRC_WINRM_PS_FS_MIN,
    WINRM_PS_CAPABILITY_PROBE,
    WinRMTransport,
    _incomplete_winrm_ps,
    derive_winrm_ps_caps,
    normalize_winrm_probe_mode,
    parse_winrm_ps_probe_output,
    resolve_winrm_open_probe_mode,
)

# ---------------------------------------------------------------------------
# parse_winrm_ps_probe_output
# ---------------------------------------------------------------------------


def test_parse_json_last_line() -> None:
    payload = {
        "ps_version": "5.1.19041.1",
        "ps_edition": "Desktop",
        "language_mode": "FullLanguage",
        "os_version": "10.0.19041.0",
        "has_convertto_json": True,
        "can_get_item": True,
        "can_file_io": True,
    }
    stdout = "\n" + json.dumps(payload, separators=(",", ":")) + "\n"
    raw = parse_winrm_ps_probe_output(stdout)
    assert raw["ps_version"] == "5.1.19041.1"
    assert raw["ps_edition"] == "Desktop"
    assert raw["language_mode"] == "FullLanguage"
    assert raw["has_convertto_json"] is True
    assert raw["can_get_item"] is True
    assert raw["can_file_io"] is True


def test_parse_json_ignores_leading_noise() -> None:
    body = json.dumps(
        {
            "ps_version": "7.4.0",
            "language_mode": "FullLanguage",
            "has_convertto_json": True,
            "can_get_item": True,
            "can_file_io": True,
        }
    )
    raw = parse_winrm_ps_probe_output(f"WARNING: something\n{body}")
    assert raw["ps_version"] == "7.4.0"
    assert raw["language_mode"] == "FullLanguage"


def test_parse_key_value_fallback() -> None:
    stdout = (
        "ps_version=5.1.14409\n"
        "ps_edition=Desktop\n"
        "language_mode=ConstrainedLanguage\n"
        "os_version=6.3.9600\n"
        "has_convertto_json=False\n"
        "can_get_item=True\n"
        "can_file_io=False"
    )
    raw = parse_winrm_ps_probe_output(stdout)
    assert raw["language_mode"] == "ConstrainedLanguage"
    assert raw["has_convertto_json"] is False
    assert raw["can_get_item"] is True
    assert raw["can_file_io"] is False


def test_parse_empty_and_garbage() -> None:
    assert parse_winrm_ps_probe_output("") == {}
    assert parse_winrm_ps_probe_output("   \n  ") == {}
    assert parse_winrm_ps_probe_output("ps-out\nnot-json") == {}


def test_parse_bool_coercion_from_strings() -> None:
    raw = parse_winrm_ps_probe_output(
        "language_mode=FullLanguage\n"
        "has_convertto_json=true\n"
        "can_get_item=1\n"
        "can_file_io=yes\n"
    )
    assert raw["has_convertto_json"] is True
    assert raw["can_get_item"] is True
    assert raw["can_file_io"] is True


# ---------------------------------------------------------------------------
# derive_winrm_ps_caps
# ---------------------------------------------------------------------------


def _full_raw(**overrides: object) -> dict:
    base = {
        "ps_version": "5.1.19041",
        "language_mode": "FullLanguage",
        "has_convertto_json": True,
        "can_get_item": True,
        "can_file_io": True,
    }
    base.update(overrides)
    return base


def test_derive_full_language_script_fs_true() -> None:
    out = derive_winrm_ps_caps(_full_raw())
    assert out["ps_script_fs"] is True
    assert out["ps_oneshot"] is True
    assert out["ps_runspace"] is True


def test_derive_language_mode_case_insensitive() -> None:
    out = derive_winrm_ps_caps(_full_raw(language_mode="fulllanguage"))
    assert out["ps_script_fs"] is True
    out2 = derive_winrm_ps_caps(_full_raw(language_mode="FULLLANGUAGE"))
    assert out2["ps_script_fs"] is True


def test_derive_constrained_disables_script_fs_and_runspace() -> None:
    out = derive_winrm_ps_caps(_full_raw(language_mode="ConstrainedLanguage"))
    assert out["ps_script_fs"] is False
    assert out["ps_oneshot"] is True
    assert out["ps_runspace"] is False


def test_derive_no_language_disables_oneshot() -> None:
    out = derive_winrm_ps_caps(_full_raw(language_mode="NoLanguage"))
    assert out["ps_script_fs"] is False
    assert out["ps_oneshot"] is False
    assert out["ps_runspace"] is False


def test_derive_missing_cmdlet_bits() -> None:
    out = derive_winrm_ps_caps(_full_raw(can_file_io=False))
    assert out["ps_script_fs"] is False
    out2 = derive_winrm_ps_caps(_full_raw(can_get_item=False))
    assert out2["ps_script_fs"] is False
    out3 = derive_winrm_ps_caps(_full_raw(has_convertto_json=False))
    assert out3["ps_script_fs"] is False


def test_derive_version_floor() -> None:
    assert MRC_WINRM_PS_FS_MIN == (5, 1)
    out = derive_winrm_ps_caps(_full_raw(ps_version="5.0.10240"))
    assert out["ps_script_fs"] is False
    # Exactly 5.1 is allowed.
    out_ok = derive_winrm_ps_caps(_full_raw(ps_version="5.1"))
    assert out_ok["ps_script_fs"] is True
    # Above the floor is allowed.
    out_above = derive_winrm_ps_caps(_full_raw(ps_version="7.2"))
    assert out_above["ps_script_fs"] is True
    # Tuple compare: (5, 10) >= (5, 1) even though "5.10" < "5.1"
    # lexicographically - guards against a string-compare regression.
    out_minor10 = derive_winrm_ps_caps(_full_raw(ps_version="5.10"))
    assert out_minor10["ps_script_fs"] is True
    # Unparseable version does not alone reject.
    out_na = derive_winrm_ps_caps(_full_raw(ps_version="unknown"))
    assert out_na["ps_script_fs"] is True
    # Missing version does not alone reject.
    raw = _full_raw()
    del raw["ps_version"]
    assert derive_winrm_ps_caps(raw)["ps_script_fs"] is True


def test_derive_incomplete_raw_all_false() -> None:
    # derive alone is conservative for empty raw; live incomplete probes use
    # _incomplete_winrm_ps (oneshot remains true).
    out = derive_winrm_ps_caps({})
    assert out["ps_script_fs"] is False
    assert out["ps_oneshot"] is False
    assert out["ps_runspace"] is False


def test_incomplete_winrm_ps_keeps_oneshot() -> None:
    """Incomplete probe must not hard-block oneshot exec."""
    out = _incomplete_winrm_ps(error="capability probe timed out")
    assert out["ps_script_fs"] is False
    assert out["ps_runspace"] is False
    assert out["ps_oneshot"] is True
    assert out["ps_probe"] == "failed"
    assert out["error"] == "capability probe timed out"
    # Without error message still marks probe failed and oneshot allowed.
    bare = _incomplete_winrm_ps()
    assert bare["ps_oneshot"] is True
    assert bare["ps_probe"] == "failed"
    assert "error" not in bare


def test_probe_timeout_constant() -> None:
    assert MRC_WINRM_PROBE_TIMEOUT_S == 5.0


# ---------------------------------------------------------------------------
# Open probe mode resolve (skip | light | full)
# ---------------------------------------------------------------------------


def test_normalize_winrm_probe_mode_tokens() -> None:
    assert normalize_winrm_probe_mode(None) is None
    assert normalize_winrm_probe_mode("") is None
    assert normalize_winrm_probe_mode("junk") is None
    assert normalize_winrm_probe_mode("full") == "full"
    assert normalize_winrm_probe_mode("LIGHT") == "light"
    assert normalize_winrm_probe_mode("skip") == "skip"
    assert normalize_winrm_probe_mode(True) == "full"
    assert normalize_winrm_probe_mode(False) == "skip"
    assert normalize_winrm_probe_mode("skipped") == "skip"
    assert normalize_winrm_probe_mode("hard") == "full"
    assert normalize_winrm_probe_mode("soft") == "light"


def test_resolve_winrm_open_probe_mode_default_full() -> None:
    assert resolve_winrm_open_probe_mode(env={}) == "full"
    assert resolve_winrm_open_probe_mode(explicit_probe=True, env={}) == "full"


def test_resolve_winrm_open_probe_mode_priority() -> None:
    # explicit probe=False always skip (API opt-out).
    assert (
        resolve_winrm_open_probe_mode(
            explicit_probe=False,
            winrm_cfg={"probe": "full"},
            env={MRC_WINRM_PROBE_ENV: "full"},
        )
        == "skip"
    )
    # env beats profile.
    assert (
        resolve_winrm_open_probe_mode(
            winrm_cfg={"probe": "light"},
            env={MRC_WINRM_PROBE_ENV: "skip"},
        )
        == "skip"
    )
    # profile [winrm].probe beats profile defaults and global.
    assert (
        resolve_winrm_open_probe_mode(
            winrm_cfg={"probe": "light"},
            profile_defaults={"winrm_probe": "skip"},
            global_winrm_probe="full",
            env={},
        )
        == "light"
    )
    # profile defaults beat global.
    assert (
        resolve_winrm_open_probe_mode(
            profile_defaults={"winrm_probe": "skip"},
            global_winrm_probe="full",
            env={},
        )
        == "skip"
    )
    # global when nothing else.
    assert (
        resolve_winrm_open_probe_mode(global_winrm_probe="light", env={}) == "light"
    )
    # unknown env ignored -> fall through to profile.
    assert (
        resolve_winrm_open_probe_mode(
            winrm_cfg={"probe": "light"},
            env={MRC_WINRM_PROBE_ENV: "not-a-mode"},
        )
        == "light"
    )


# ---------------------------------------------------------------------------
# collect_probe integration (no network; injectable session)
# ---------------------------------------------------------------------------


class _CapsPsSession:
    """Session with execute_ps only (no identity seeds) for live probe path."""

    def __init__(self, stdout: str, *, fail: bool = False) -> None:
        self.stdout = stdout
        self.fail = fail
        self.scripts: list[str] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, object, bool]:
        del environment
        self.scripts.append(script)
        if self.fail:
            raise RuntimeError("mock probe boom")
        return (self.stdout, None, False)


class _IdentitySeedSession:
    """Identity seeds only - no execute_ps; must not invent ps_script_fs."""

    def __init__(self) -> None:
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1.19041"
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> object:
        del command, cwd, timeout_s, env
        raise AssertionError("identity-seed path must not remote-exec for caps")


class _CapabilitySeedSession:
    """Pre-seeded FullLanguage capability bits (no remote)."""

    def __init__(self) -> None:
        self.os = "windows"
        self.shell = "powershell"
        self.ps_version = "5.1.19041"
        self.language_mode = "FullLanguage"
        self.has_convertto_json = True
        self.can_get_item = True
        self.can_file_io = True
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _connect(session: object) -> WinRMTransport:
    t = WinRMTransport(
        host="10.0.0.1",
        username="u",
        password="p",
        connector=lambda **_k: session,
    )
    t.connect()
    return t


def test_collect_probe_mode_skip_no_execute_ps() -> None:
    """mode=skip: marker only, no remote oneshot."""
    sess = _CapsPsSession(stdout="should-not-run")
    t = WinRMTransport(
        host="10.0.0.1",
        username="u",
        password="p",
        connector=lambda **_k: sess,
    )
    t.connect()
    probe = t.collect_probe(mode="skip")
    assert sess.scripts == []
    assert probe.get("ps_probe") == "skipped"
    assert probe.get("probe_mode") == "skip"
    assert t.is_connected() is True
    assert t.meta.get("winrm_ps") == {"ps_probe": "skipped"}


def test_collect_probe_mode_light_no_remote_oneshot() -> None:
    """mode=light: no execute_ps; unseeded -> partial + stay connected."""
    sess = _CapsPsSession(stdout="should-not-run")
    t = WinRMTransport(
        host="10.0.0.1",
        username="u",
        password="p",
        connector=lambda **_k: sess,
    )
    t.connect()
    probe = t.collect_probe(mode="light")
    assert sess.scripts == []
    assert probe.get("status") == "partial"
    assert probe.get("ps_probe") == "light"
    assert probe.get("probe_mode") == "light"
    assert t.is_connected() is True
    assert t.meta.get("probe_status") != "fail"
    assert t.meta.get("winrm_ps", {}).get("ps_probe") == "light"


def test_collect_probe_mode_light_with_identity_seeds_ok() -> None:
    """mode=light + identity seeds -> ok without remote RTT."""
    sess = _IdentitySeedSession()
    t = WinRMTransport(
        host="10.0.0.1",
        username="u",
        password="p",
        connector=lambda **_k: sess,
    )
    t.connect()
    probe = t.collect_probe(mode="light")
    assert probe.get("status") == "ok"
    assert probe.get("os") == "windows"
    assert probe.get("probe_mode") == "light"
    assert t.is_connected() is True


def test_collect_probe_mode_full_still_remote_oneshot() -> None:
    """mode=full (default) still issues capability oneshot when unseeded."""
    body = json.dumps(
        {
            "ps_version": "5.1.19041",
            "language_mode": "FullLanguage",
            "has_convertto_json": True,
            "can_get_item": True,
            "can_file_io": True,
        },
        separators=(",", ":"),
    )
    sess = _CapsPsSession(stdout=body)
    t = WinRMTransport(
        host="10.0.0.1",
        username="u",
        password="p",
        connector=lambda **_k: sess,
    )
    t.connect()
    probe = t.collect_probe(mode="full")
    assert sess.scripts, "full mode must call execute_ps"
    assert probe.get("status", "ok") == "ok"
    assert probe.get("probe_mode") == "full" or probe.get("language_mode") == "FullLanguage"


def test_collect_probe_remote_full_language() -> None:
    payload = json.dumps(
        {
            "ps_version": "5.1.19041",
            "ps_edition": "Desktop",
            "language_mode": "FullLanguage",
            "os_version": "10.0.19041.0",
            "has_convertto_json": True,
            "can_get_item": True,
            "can_file_io": True,
        },
        separators=(",", ":"),
    )
    sess = _CapsPsSession(payload + "\n")
    t = _connect(sess)
    probe = t.collect_probe()
    assert sess.scripts, "capability probe must call execute_ps"
    assert len(sess.scripts) == 1, "capability probe must be a single round-trip"
    assert WINRM_PS_CAPABILITY_PROBE.splitlines()[0] in sess.scripts[0]
    assert probe.get("status", "ok") == "ok"
    winrm_ps = probe["winrm_ps"]
    assert winrm_ps["ps_script_fs"] is True
    assert winrm_ps["ps_oneshot"] is True
    assert winrm_ps["ps_runspace"] is True
    assert t.meta["winrm_ps"]["ps_script_fs"] is True
    assert probe.get("language_mode") == "FullLanguage"
    assert probe.get("ps_version") == "5.1.19041"


def test_collect_probe_remote_constrained() -> None:
    payload = json.dumps(
        {
            "ps_version": "5.1.19041",
            "language_mode": "ConstrainedLanguage",
            "has_convertto_json": True,
            "can_get_item": True,
            "can_file_io": False,
        },
        separators=(",", ":"),
    )
    sess = _CapsPsSession(payload)
    t = _connect(sess)
    probe = t.collect_probe()
    assert probe["winrm_ps"]["ps_script_fs"] is False
    assert probe["winrm_ps"]["ps_runspace"] is False
    assert probe["winrm_ps"]["ps_oneshot"] is True
    assert t.meta["winrm_ps"]["ps_script_fs"] is False


def test_collect_probe_remote_failure_identity_hard_fail() -> None:
    """Unseeded remote RTT exception -> status=fail + mark_dead + dispose."""
    sess = _CapsPsSession("", fail=True)
    t = _connect(sess)
    probe = t.collect_probe()
    assert probe["status"] == "fail"
    winrm_ps = probe["winrm_ps"]
    assert winrm_ps["ps_script_fs"] is False
    assert winrm_ps["ps_runspace"] is False
    assert winrm_ps["ps_oneshot"] is True
    assert winrm_ps["ps_probe"] == "failed"
    assert t.meta["winrm_ps"]["ps_oneshot"] is True
    assert "error" in probe
    assert t.is_connected() is False
    assert t.meta.get("probe_status") == "fail"
    # Hard-fail must dispose session immediately (close + _session None).
    assert sess.closed is True
    assert t.session is None


def test_collect_probe_empty_stdout_identity_hard_fail() -> None:
    """Empty capability stdout (no seeds) -> fail + mark_dead + dispose.

    os/shell convenience defaults alone must not prove identity when the
    remote oneshot returns no usable content (JEA/restricted empty stdout).
    """
    sess = _CapsPsSession("")
    t = _connect(sess)
    probe = t.collect_probe()
    assert probe["status"] == "fail"
    assert "error" in probe
    err = str(probe.get("error") or "").lower()
    assert "empty" in err or "unusable" in err or "identity" in err
    winrm_ps = probe["winrm_ps"]
    assert winrm_ps["ps_script_fs"] is False
    assert winrm_ps["ps_runspace"] is False
    assert winrm_ps["ps_oneshot"] is True
    assert winrm_ps["ps_probe"] == "failed"
    assert t.is_connected() is False
    assert t.meta.get("probe_status") == "fail"
    # Invented defaults must not appear as proven identity after empty RTT.
    assert probe.get("os") in (None, "")
    assert probe.get("shell") in (None, "")
    assert sess.scripts, "empty-stdout path must have attempted execute_ps"
    # Dispose on empty-identity hard-fail.
    assert sess.closed is True
    assert t.session is None


def test_collect_probe_whitespace_stdout_identity_hard_fail() -> None:
    """Whitespace-only stdout is empty for identity purposes."""
    sess = _CapsPsSession("  \n\t\n  ")
    t = _connect(sess)
    probe = t.collect_probe()
    assert probe["status"] == "fail"
    assert t.is_connected() is False
    assert t.meta.get("probe_status") == "fail"
    assert sess.closed is True
    assert t.session is None


# ---------------------------------------------------------------------------
# _parse_probe_stdout must not invent identity from junk lines
# ---------------------------------------------------------------------------


class _IdentityRunCommandSession:
    """Unseeded session with run_command only (identity RTT path, no execute_ps)."""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.closed = False
        self.commands: list[str] = []
        # Paths only (not identity seeds for collect_probe early-return).
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"

    def close(self) -> None:
        self.closed = True

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> object:
        del cwd, timeout_s, env
        self.commands.append(command)
        from mcp_remote_control.transport.base import ExecResult

        return ExecResult(
            exit_code=0,
            stdout=self.stdout,
            stderr="",
            cwd=self.cwd,
        )


def test_parse_probe_stdout_access_denied_not_identity_proven() -> None:
    """Junk 'Access Denied' must not invent os/shell."""
    t = WinRMTransport(host="10.0.0.1", username="u", password="p")
    parsed = t._parse_probe_stdout("Access Denied", base={})
    assert parsed.get("os") in (None, "")
    assert parsed.get("shell") in (None, "")
    assert parsed.get("ps_version") in (None, "")
    assert t._identity_proven(parsed) is False
    assert parsed.get("status") == "partial"


def test_parse_probe_stdout_error_not_identity_proven() -> None:
    """Bare error text is non-heuristic junk - not proven."""
    t = WinRMTransport(host="10.0.0.1", username="u", password="p")
    parsed = t._parse_probe_stdout("error\nAccess is denied.", base={})
    assert t._identity_proven(parsed) is False
    assert parsed.get("os") in (None, "")
    assert parsed.get("shell") in (None, "")


def test_parse_probe_stdout_real_version_path_os_proven() -> None:
    """Real PS version / path / OSVersion lines still prove identity."""
    t = WinRMTransport(host="10.0.0.1", username="u", password="p")
    stdout = (
        "5.1.19041.1\n"
        r"C:\Users\Administrator" + "\n"
        r"C:\Users\Administrator" + "\n"
        "Microsoft Windows NT 10.0.19041.0\n"
    )
    parsed = t._parse_probe_stdout(stdout, base={})
    assert t._identity_proven(parsed) is True
    assert parsed.get("ps_version") == "5.1.19041.1"
    assert parsed.get("home") == r"C:\Users\Administrator"
    assert "Windows" in str(parsed.get("os") or "")
    assert parsed.get("shell") == "powershell"
    assert parsed.get("status") == "ok"


def test_parse_probe_stdout_empty_still_not_proven() -> None:
    """Empty/whitespace stdout remains unproven."""
    t = WinRMTransport(host="10.0.0.1", username="u", password="p")
    for raw in ("", "  \n\t\n  "):
        parsed = t._parse_probe_stdout(raw, base={})
        assert t._identity_proven(parsed) is False
        assert parsed.get("os") in (None, "")
        assert parsed.get("shell") in (None, "")


def test_parse_probe_stdout_http_status_not_identity_proven() -> None:
    """HTTP 401/500 lines are not version-shaped - must not prove identity."""
    t = WinRMTransport(host="10.0.0.1", username="u", password="p")
    for raw in ("401 Unauthorized", "500 Internal Server Error", "500 \u2026"):
        parsed = t._parse_probe_stdout(raw, base={})
        assert parsed.get("ps_version") in (None, "")
        assert parsed.get("os") in (None, "")
        assert parsed.get("shell") in (None, "")
        assert t._identity_proven(parsed) is False
        assert parsed.get("status") == "partial"


def test_parse_probe_stdout_windows_substring_not_identity_proven() -> None:
    """A line that merely contains 'Windows' is not an OS banner."""
    t = WinRMTransport(host="10.0.0.1", username="u", password="p")
    for raw in (
        "401 \u2026 Windows \u2026",
        "Access Denied from Windows host",
        "error talking to Windows endpoint",
    ):
        parsed = t._parse_probe_stdout(raw, base={})
        assert parsed.get("os") in (None, "")
        assert parsed.get("shell") in (None, "")
        assert parsed.get("ps_version") in (None, "")
        assert t._identity_proven(parsed) is False
        assert parsed.get("status") == "partial"


def test_parse_probe_stdout_bare_drive_prefix_not_identity_proven() -> None:
    """A lone C:\\ / path records home/pwd but must not invent os/shell."""
    t = WinRMTransport(host="10.0.0.1", username="u", password="p")
    parsed = t._parse_probe_stdout(r"C:\Users\x", base={})
    assert parsed.get("home") == r"C:\Users\x"
    assert parsed.get("os") in (None, "")
    assert parsed.get("shell") in (None, "")
    assert parsed.get("ps_version") in (None, "")
    assert t._identity_proven(parsed) is False
    assert parsed.get("status") == "partial"


def test_parse_probe_stdout_microsoft_windows_nt_still_proven() -> None:
    """Microsoft Windows NT banner (with or without a dotted version) still proves."""
    t = WinRMTransport(host="10.0.0.1", username="u", password="p")
    banner = t._parse_probe_stdout("Microsoft Windows NT 10.0.19041.0", base={})
    assert t._identity_proven(banner) is True
    assert "Windows" in str(banner.get("os") or "")
    assert banner.get("shell") == "powershell"
    both = t._parse_probe_stdout(
        "Microsoft Windows NT 10.0.19041.0\n5.1.19041.1\n",
        base={},
    )
    assert t._identity_proven(both) is True
    assert both.get("ps_version") == "5.1.19041.1"


def test_parse_probe_stdout_psversion_label_still_proven() -> None:
    """Dotted version and PSVersion+digit labels still prove identity."""
    t = WinRMTransport(host="10.0.0.1", username="u", password="p")
    dotted = t._parse_probe_stdout("5.1.19041.1", base={})
    assert dotted.get("ps_version") == "5.1.19041.1"
    assert t._identity_proven(dotted) is True
    assert dotted.get("status") == "ok"
    labeled = t._parse_probe_stdout("PSVersion 7.4", base={})
    assert labeled.get("ps_version") == "PSVersion 7.4"
    assert t._identity_proven(labeled) is True
    assert labeled.get("status") == "ok"


def test_collect_probe_run_command_access_denied_identity_hard_fail() -> None:
    """Access Denied identity -> fail + mark_dead + dispose."""
    sess = _IdentityRunCommandSession("Access Denied\n")
    t = _connect(sess)
    probe = t.collect_probe()
    assert probe["status"] == "fail"
    assert t.is_connected() is False
    assert t.meta.get("probe_status") == "fail"
    assert probe.get("os") in (None, "")
    assert probe.get("shell") in (None, "")
    assert sess.commands, "identity path must have attempted run_command"
    # Unproven/junk identity hard-fail disposes the session.
    assert sess.closed is True
    assert t.session is None


def test_collect_probe_run_command_real_probe_lines_ok() -> None:
    """run_command identity RTT with real PS/path/OS lines -> status ok."""
    stdout = (
        "Microsoft Windows NT 10.0.19041.0\n"
        "5.1.19041.1\n"
        r"C:\Users\Administrator" + "\n"
        r"C:\Users\Administrator" + "\n"
    )
    sess = _IdentityRunCommandSession(stdout)
    t = _connect(sess)
    probe = t.collect_probe()
    assert probe.get("status") == "ok"
    assert t.is_connected() is True
    assert t.meta.get("probe_status") != "fail"
    assert probe.get("ps_version") == "5.1.19041.1"
    assert probe.get("os")
    assert probe.get("shell") == "powershell"
    assert sess.commands
    # Successful identity probe must not dispose the live session.
    assert sess.closed is False
    assert t.session is not None


def test_collect_probe_unparseable_partial() -> None:
    """RTT non-empty but capability unparseable -> soft partial; still connected.

    Soft partial must not be upgraded to hard-fail when the oneshot returned
    content. os/shell invent-only defaults are not required.
    """
    sess = _CapsPsSession("ps-out\n")
    t = _connect(sess)
    probe = t.collect_probe()
    assert probe["status"] == "partial"
    winrm_ps = probe["winrm_ps"]
    assert winrm_ps["ps_script_fs"] is False
    assert winrm_ps["ps_runspace"] is False
    assert winrm_ps["ps_oneshot"] is True
    assert winrm_ps["ps_probe"] == "failed"
    # Successful non-empty oneshot: soft capability gap only (stay connected).
    assert t.is_connected() is True
    assert t.meta.get("probe_status") != "fail"


def test_collect_probe_unparseable_with_identity_seeds_stays_soft() -> None:
    """Soft partial with real identity seeds must not become hard-fail."""
    sess = _IdentitySeedSession()
    # Pre-seed soft capability partial the way adapters may surface it.
    t = _connect(sess)
    t.meta["probe_status"] = "partial"
    t.meta["probe_error"] = "capability incomplete"
    probe = t.collect_probe()
    assert probe["status"] == "partial"
    assert probe.get("os") == "windows"
    assert probe.get("shell") == "powershell"
    assert t.is_connected() is True
    assert t.meta.get("probe_status") == "partial"


def test_collect_probe_identity_seeds_do_not_set_ps_script_fs() -> None:
    sess = _IdentitySeedSession()
    t = _connect(sess)
    probe = t.collect_probe()
    assert probe.get("os") == "windows"
    assert probe.get("shell") == "powershell"
    # Seeds alone must not claim script FS capability.
    assert probe.get("winrm_ps") is None or probe.get("winrm_ps", {}).get(
        "ps_script_fs"
    ) is not True
    assert t.meta.get("winrm_ps") is None or t.meta["winrm_ps"].get(
        "ps_script_fs"
    ) is not True


def test_collect_probe_capability_seeds_derive_without_remote() -> None:
    sess = _CapabilitySeedSession()
    t = _connect(sess)
    probe = t.collect_probe()
    assert probe["winrm_ps"]["ps_script_fs"] is True
    assert probe["language_mode"] == "FullLanguage"
    assert t.meta["winrm_ps"]["ps_runspace"] is True


class _HangingPsSession:
    """execute_ps blocks longer than the probe wall-clock budget."""

    def __init__(self) -> None:
        self.closed = False
        self.scripts: list[str] = []

    def close(self) -> None:
        self.closed = True

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, object, bool]:
        del environment
        self.scripts.append(script)
        import time

        # Long enough to hit the shortened probe budget; short enough that
        # the orphaned executor thread exits before suite teardown.
        time.sleep(1.5)
        return ("never", None, False)


def test_collect_probe_timeout_identity_hard_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hung unseeded probe -> hard-fail + dispose; budget upper bound."""
    import mcp_remote_control.transport.winrm as winrm_mod

    # Short wall-clock budget so the unit test stays fast.
    monkeypatch.setattr(winrm_mod, "MRC_WINRM_PROBE_TIMEOUT_S", 0.15)
    sess = _HangingPsSession()
    closes = {"n": 0}
    orig_close = sess.close

    def _close() -> None:
        closes["n"] += 1
        orig_close()

    sess.close = _close  # type: ignore[method-assign]
    t = _connect(sess)
    t0 = time.monotonic()
    probe = t.collect_probe()
    elapsed = time.monotonic() - t0
    assert probe["status"] == "fail"
    winrm_ps = probe["winrm_ps"]
    assert winrm_ps["ps_script_fs"] is False
    assert winrm_ps["ps_runspace"] is False
    assert winrm_ps["ps_oneshot"] is True
    assert winrm_ps["ps_probe"] == "failed"
    assert "error" in probe or "error" in winrm_ps
    # Identity RTT timeout marks the transport dead (open must not stay live).
    assert t.is_connected() is False
    assert t.meta.get("probe_status") == "fail"
    assert elapsed < 2.0, f"probe timeout not bounded: {elapsed}s"
    assert sess.scripts, "probe must have attempted execute_ps"
    # Dispose immediately (parity with exec hard-timeout); do not retain
    # _session until next connect/close. WinRM cannot cancel the remote call.
    assert closes["n"] >= 1, "identity probe hard-fail must close the WinRM session"
    assert sess.closed is True
    assert t.session is None


class _HangingRunCommandSession:
    """run_command blocks longer than the probe wall-clock budget."""

    def __init__(self) -> None:
        self.closed = False
        self.commands: list[str] = []
        self.block = threading.Event()
        self.cwd = r"C:\Users\mock"
        self.home = r"C:\Users\mock"

    def close(self) -> None:
        self.closed = True
        self.block.set()

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> object:
        del cwd, timeout_s, env
        self.commands.append(command)
        self.block.wait(timeout=30.0)
        from mcp_remote_control.transport.base import ExecResult

        return ExecResult(
            exit_code=0,
            stdout="never",
            stderr="",
            cwd=self.cwd,
        )


def test_collect_probe_run_command_timeout_identity_hard_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hung identity run_command fails inside the probe wall-clock budget."""
    import mcp_remote_control.transport.winrm as winrm_mod

    monkeypatch.setattr(winrm_mod, "MRC_WINRM_PROBE_TIMEOUT_S", 0.15)
    sess = _HangingRunCommandSession()
    t = _connect(sess)
    t0 = time.monotonic()
    probe = t.collect_probe()
    elapsed = time.monotonic() - t0
    assert probe["status"] == "fail"
    assert t.is_connected() is False
    assert t.meta.get("probe_status") == "fail"
    assert elapsed < 2.0, f"identity run_command not bounded: {elapsed}s"
    assert sess.commands, "identity path must have attempted run_command"
    assert sess.closed is True
    assert t.session is None


class _ListCapsPsSession:
    """execute_ps returns a list/tuple of objects, not a joined string."""

    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.scripts: list[str] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> object:
        del environment
        self.scripts.append(script)
        return self.payload


def test_collect_probe_list_stdout_key_value_yields_language_mode() -> None:
    """pypsrp list of key=value lines must parse language_mode / ps_script_fs."""
    payload = [
        "ps_version=5.1.19041",
        "language_mode=FullLanguage",
        "has_convertto_json=True",
        "can_get_item=True",
        "can_file_io=True",
    ]
    sess = _ListCapsPsSession(payload)
    t = _connect(sess)
    probe = t.collect_probe()
    assert sess.scripts, "capability probe must call execute_ps"
    assert probe.get("language_mode") == "FullLanguage"
    assert probe.get("ps_version") == "5.1.19041"
    winrm_ps = probe["winrm_ps"]
    assert winrm_ps["ps_script_fs"] is True
    assert winrm_ps["ps_oneshot"] is True
    assert t.meta["winrm_ps"]["ps_script_fs"] is True
    assert t.is_connected() is True
    assert sess.closed is False


def test_collect_probe_list_stdout_json_objects_yields_language_mode() -> None:
    """List/tuple of JSON objects (not a str) must still derive caps."""
    obj = {
        "ps_version": "5.1.19041",
        "language_mode": "FullLanguage",
        "has_convertto_json": True,
        "can_get_item": True,
        "can_file_io": True,
    }
    # pypsrp shape: (output_list, streams, had_errors)
    sess = _ListCapsPsSession(([obj], None, False))
    t = _connect(sess)
    probe = t.collect_probe()
    assert probe.get("language_mode") == "FullLanguage"
    assert probe["winrm_ps"]["ps_script_fs"] is True
    assert t.is_connected() is True


class _TimedOutPsSession:
    """execute_ps returns ExecResult(timed_out=True) with leftover stdout."""

    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def execute_ps(
        self,
        script: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> object:
        del environment
        self.scripts.append(script)
        from mcp_remote_control.transport.base import ExecResult

        return ExecResult(
            exit_code=-1,
            stdout="leftover\nlanguage_mode=FullLanguage\n",
            stderr="timeout",
            timed_out=True,
        )


def test_collect_probe_execute_ps_timed_out_hard_fail() -> None:
    """timed_out ExecResult is identity fail + dispose, not ok/partial leftover."""
    sess = _TimedOutPsSession()
    t = _connect(sess)
    probe = t.collect_probe()
    assert probe["status"] == "fail"
    assert t.is_connected() is False
    assert t.meta.get("probe_status") == "fail"
    assert probe.get("language_mode") in (None, "")
    assert sess.scripts, "probe must have attempted execute_ps"
    assert sess.closed is True
    assert t.session is None

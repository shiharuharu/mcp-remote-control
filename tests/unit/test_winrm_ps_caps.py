"""Unit tests: WinRM PowerShell capability probe parse / derive / collect_probe."""

from __future__ import annotations

import json

import pytest

from mcp_remote_control.transport.winrm import (
    MRC_WINRM_PROBE_TIMEOUT_S,
    MRC_WINRM_PS_FS_MIN,
    WINRM_PS_CAPABILITY_PROBE,
    WinRMTransport,
    _incomplete_winrm_ps,
    derive_winrm_ps_caps,
    parse_winrm_ps_probe_output,
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
    # lexicographically — guards against a string-compare regression.
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
    """Identity seeds only — no execute_ps; must not invent ps_script_fs."""

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


def test_collect_probe_remote_failure_partial() -> None:
    sess = _CapsPsSession("", fail=True)
    t = _connect(sess)
    probe = t.collect_probe()
    assert probe["status"] == "partial"
    winrm_ps = probe["winrm_ps"]
    assert winrm_ps["ps_script_fs"] is False
    assert winrm_ps["ps_runspace"] is False
    assert winrm_ps["ps_oneshot"] is True
    assert winrm_ps["ps_probe"] == "failed"
    assert t.meta["winrm_ps"]["ps_oneshot"] is True
    assert "error" in probe


def test_collect_probe_unparseable_partial() -> None:
    sess = _CapsPsSession("ps-out\n")
    t = _connect(sess)
    probe = t.collect_probe()
    assert probe["status"] == "partial"
    winrm_ps = probe["winrm_ps"]
    assert winrm_ps["ps_script_fs"] is False
    assert winrm_ps["ps_runspace"] is False
    assert winrm_ps["ps_oneshot"] is True
    assert winrm_ps["ps_probe"] == "failed"


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


def test_collect_probe_timeout_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hung capability probe → incomplete (partial); connect remains ok."""
    import mcp_remote_control.transport.winrm as winrm_mod

    # Short wall-clock budget so the unit test stays fast.
    monkeypatch.setattr(winrm_mod, "MRC_WINRM_PROBE_TIMEOUT_S", 0.15)
    sess = _HangingPsSession()
    t = _connect(sess)
    probe = t.collect_probe()
    assert probe["status"] == "partial"
    winrm_ps = probe["winrm_ps"]
    assert winrm_ps["ps_script_fs"] is False
    assert winrm_ps["ps_runspace"] is False
    assert winrm_ps["ps_oneshot"] is True
    assert winrm_ps["ps_probe"] == "failed"
    assert "error" in probe or "error" in winrm_ps
    # Transport stays connected after a probe timeout (must not fail connect).
    assert t.is_connected()
    assert sess.scripts, "probe must have attempted execute_ps"

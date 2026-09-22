"""Service tests: endpoint lifecycle local + mock SSH."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_remote_control.cli import main
from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_TRANSPORT
from mcp_remote_control.config import Profile
from mcp_remote_control.config.store import (
    delete_profile,
    ensure_home_layout,
    put_profile,
    write_notes,
)
from mcp_remote_control.core import endpoint_ops
from mcp_remote_control.endpoint import ensure_endpoint, get_registry
from mcp_remote_control.transport import SSHTransport, TransportError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"

# Fake PEM material must never appear in Agent open output.
FAKE_PEM = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
    "QyNTUxOQAAACFakeKeyMaterialForRedactTestsOnlyXXXX=\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)
FAKE_PASSWORD = "super-secret-password-xyz"


@pytest.fixture
def mrc_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MRC_HOME", str(FIXTURES))
    return FIXTURES


# ---------------------------------------------------------------------------
# local open / list / close
# ---------------------------------------------------------------------------


def test_local_open_ok_caps(mrc_home: Path) -> None:
    r = endpoint_ops.run(op="open", profile="local", home=mrc_home)
    assert r.status == "ok"
    assert r.code is None
    assert r.fields.get("ep") == "local"
    assert r.fields.get("transport") == "local"
    caps = r.fields.get("caps") or ""
    assert "exec" in caps
    assert "fs" in caps
    assert "screen" in caps
    assert "ps" not in caps.split(",")
    text = r.render_text()
    assert text.startswith("@endpoint ok")
    assert "caps=exec,fs,screen" in text
    assert "transport=local" in text
    assert r.cwd is not None
    # Probe summary on meta (shell/uname/locale) when probe populated.
    # Local open seeds these from env/platform - at least one should appear.
    meta_blob = "\n".join(ln for ln in text.splitlines() if ln.startswith("| "))
    if r.fields.get("shell") or r.fields.get("uname") or r.fields.get("locale"):
        assert "shell=" in meta_blob or "uname=" in meta_blob or "locale=" in meta_blob
        # Free-text msg not required; probe must not dump whole dict.
        assert "shell_path=" not in text or "shell=" in text
        assert "probe=" not in text.splitlines()[0]


def test_local_list_shows_open(mrc_home: Path) -> None:
    endpoint_ops.run(op="open", profile="local", home=mrc_home)
    r = endpoint_ops.run(op="list", home=mrc_home)
    assert r.status == "ok"
    assert r.fields.get("open") == 1
    assert r.body is not None
    assert "local" in r.body
    assert "open=1" in r.body


def test_local_close_removes(mrc_home: Path) -> None:
    endpoint_ops.run(op="open", profile="local", home=mrc_home)
    r = endpoint_ops.run(op="close", ep="local")
    assert r.status == "ok"
    assert r.fields.get("disconnected") is True

    r2 = endpoint_ops.run(op="list", home=mrc_home)
    assert r2.fields.get("open") == 0
    assert r2.body is not None
    assert "local" in r2.body
    assert "open=0" in r2.body

    r3 = endpoint_ops.run(op="close", ep="local")
    assert r3.status == "error"
    assert r3.code == "ENDPOINT_NOT_FOUND"


def test_lazy_ensure_connected_local(mrc_home: Path) -> None:
    reg = get_registry()
    assert reg.get("local") is None
    ep = ensure_endpoint("local", home=mrc_home)
    assert ep.connected is True
    assert ep.transport_name == "local"
    assert ep.caps["exec"] is True
    assert ep.caps["ps"] is False
    # Second call is idempotent.
    ep2 = ensure_endpoint("local", home=mrc_home)
    assert ep2 is ep or ep2.name == "local"


def test_profile_not_found(mrc_home: Path) -> None:
    r = endpoint_ops.run(op="open", profile="no-such-profile", home=mrc_home)
    assert r.status == "error"
    assert r.code == "PROFILE_NOT_FOUND"


def test_open_missing_arg() -> None:
    r = endpoint_ops.run(op="open")
    assert r.status == "error"
    assert r.code == "MISSING_ARG"
    text = r.render_text()
    # Human message on meta line with spaces, not underscored on header.
    assert "| msg=" in text
    assert "profile name required" in text


def test_probe_summary_fields_mapping() -> None:
    from mcp_remote_control.core.endpoint_ops import _probe_summary_fields

    out = _probe_summary_fields(
        {
            "shell_base": "zsh",
            "uname": "Darwin-arm64",
            "charmap": "UTF-8",
            "home": "/Users/x",
            "pwd": "/tmp",
            "status": "ok",
        }
    )
    assert out == {"shell": "zsh", "uname": "Darwin-arm64", "locale": "UTF-8"}

    out2 = _probe_summary_fields({"shell_path": "/bin/bash", "text_encoding": "utf-8"})
    assert out2["shell"] == "bash"
    assert out2["locale"] == "utf-8"

    assert _probe_summary_fields({}) == {}
    assert _probe_summary_fields({"status": "ok", "home": "/x"}) == {}


def _isolated_local_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    ensure_home_layout(home)
    put_profile(home, name="alpha", transport="local")
    put_profile(home, name="beta", transport="local")
    return home


def _line_for(body: str | None, name: str) -> str:
    assert body is not None
    for ln in body.splitlines():
        if ln.startswith(f"{name} ") or ln == name:
            return ln
    raise AssertionError(f"no list row for {name!r} in {body!r}")


def test_list_open_omits_notes_flag_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _isolated_local_home(tmp_path, monkeypatch)
    listed = endpoint_ops.run(op="list", home=home)
    assert listed.status == "ok"
    assert "notes=1" not in (listed.body or "")
    assert "notes=1" not in listed.render_text()

    opened = endpoint_ops.run(op="open", profile="alpha", home=home)
    assert opened.status == "ok"
    assert opened.fields.get("notes") != 1
    text = opened.render_text()
    assert "notes=1" not in text


def test_list_open_notes_flag_without_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _isolated_local_home(tmp_path, monkeypatch)
    marker = "UNIQUE-ENDPOINT-NOTES-BODY-DO-NOT-INLINE"
    write_notes(home, "alpha", marker)

    listed = endpoint_ops.run(op="list", home=home)
    assert listed.status == "ok"
    alpha_line = _line_for(listed.body, "alpha")
    beta_line = _line_for(listed.body, "beta")
    assert "notes=1" in alpha_line
    assert "notes=1" not in beta_line
    assert marker not in (listed.body or "")
    assert marker not in listed.render_text()

    opened = endpoint_ops.run(op="open", profile="alpha", home=home)
    assert opened.status == "ok"
    assert opened.fields.get("notes") == 1
    text = opened.render_text()
    assert "notes=1" in text
    assert marker not in text
    assert marker not in repr(opened.fields)
    assert opened.body is None or marker not in opened.body

    listed_open = endpoint_ops.run(op="list", home=home)
    alpha_open = _line_for(listed_open.body, "alpha")
    assert "open=1" in alpha_open
    assert "notes=1" in alpha_open
    assert marker not in (listed_open.body or "")

    opened_beta = endpoint_ops.run(op="open", profile="beta", home=home)
    assert opened_beta.status == "ok"
    assert opened_beta.fields.get("notes") != 1
    assert "notes=1" not in opened_beta.render_text()


def test_list_open_empty_notes_omits_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _isolated_local_home(tmp_path, monkeypatch)
    write_notes(home, "alpha", "keep")
    write_notes(home, "alpha", "")
    notes = home / "notes" / "alpha.md"
    assert notes.is_file()
    assert notes.stat().st_size == 0

    listed = endpoint_ops.run(op="list", home=home)
    assert listed.status == "ok"
    assert "notes=1" not in (listed.body or "")
    assert "notes=1" not in _line_for(listed.body, "alpha")

    opened = endpoint_ops.run(op="open", profile="alpha", home=home)
    assert opened.status == "ok"
    assert opened.fields.get("notes") != 1
    assert "notes=1" not in opened.render_text()


def test_list_count_matches_body_after_profile_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """list n counts the rows it returns, not the on-disk profile stems.

    An endpoint stays open after its profile file is removed and list still
    renders its row, so the count must include that appended row.
    """
    home = tmp_path / "mrc"
    monkeypatch.setenv("MRC_HOME", str(home))
    ensure_home_layout(home)
    put_profile(home, name="solo", transport="local")

    opened = endpoint_ops.run(op="open", profile="solo", home=home, probe=False)
    assert opened.status == "ok"
    delete_profile(home, "solo")

    listed = endpoint_ops.run(op="list", home=home)
    assert listed.status == "ok"
    lines = (listed.body or "").splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("solo ")
    assert "open=1" in lines[0]
    assert listed.fields.get("n") == 1
    assert listed.fields.get("open") == 1


def test_list_count_matches_body_with_disk_and_open_mix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """on-disk profiles plus an open endpoint are each counted exactly once.

    The body holds every name once - disk rows first, then the open endpoint
    whose profile file is gone - and n equals that row count.
    """
    home = _isolated_local_home(tmp_path, monkeypatch)
    put_profile(home, name="gamma", transport="local")
    opened = endpoint_ops.run(op="open", profile="beta", home=home, probe=False)
    assert opened.status == "ok"
    delete_profile(home, "beta")

    listed = endpoint_ops.run(op="list", home=home)
    assert listed.status == "ok"
    lines = (listed.body or "").splitlines()
    assert listed.fields.get("n") == len(lines)
    assert len(lines) == 3
    assert listed.fields.get("open") == 1
    for name in ("alpha", "beta", "gamma"):
        assert sum(1 for ln in lines if ln.startswith(f"{name} ")) == 1
    assert "open=1" in _line_for(listed.body, "beta")


def test_probe_summary_fields_winrm_ps_tokens() -> None:
    """WinRM probe -> ps_version / lang_mode / ps_fs / ps_edition Agent tokens."""
    from mcp_remote_control.core.endpoint_ops import _probe_summary_fields

    # Flat probe keys (as collect_probe merges them).
    out = _probe_summary_fields(
        {
            "ps_version": "5.1.19041",
            "language_mode": "FullLanguage",
            "ps_script_fs": True,
            "ps_edition": "Desktop",
        }
    )
    assert out["ps_version"] == "5.1.19041"
    assert out["lang_mode"] == "FullLanguage"
    assert out["ps_fs"] == 1
    assert out["ps_edition"] == "Desktop"

    # Nested winrm_ps only.
    out2 = _probe_summary_fields(
        {
            "winrm_ps": {
                "ps_version": "7.4.0",
                "language_mode": "ConstrainedLanguage",
                "ps_script_fs": False,
                "ps_edition": "Core",
            }
        }
    )
    assert out2["ps_version"] == "7.4.0"
    assert out2["lang_mode"] == "ConstrainedLanguage"
    assert out2["ps_fs"] == 0
    assert out2["ps_edition"] == "Core"

    # SSH-like probe must not invent ps_* tokens.
    out3 = _probe_summary_fields(
        {"shell_base": "bash", "uname": "Linux", "charmap": "UTF-8"}
    )
    assert "ps_version" not in out3
    assert "lang_mode" not in out3
    assert "ps_fs" not in out3


# ---------------------------------------------------------------------------
# SSH mock connector
# ---------------------------------------------------------------------------


class _MockConn:
    def __init__(self) -> None:
        self.cwd = "/var/www"
        self.home = "/home/deploy"
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_ssh_open_mock_success(mrc_home: Path) -> None:
    def connector(**_kwargs: object) -> _MockConn:
        return _MockConn()

    r = endpoint_ops.run(
        op="open",
        profile="lab-ssh",
        home=mrc_home,
        connector=connector,
    )
    assert r.status == "ok"
    assert r.fields.get("transport") == "ssh"
    assert r.fields.get("ep") == "lab-ssh"
    caps = r.fields.get("caps") or ""
    assert "screen" in caps
    assert "exec" in caps
    assert "ps" not in caps.split(",")
    text = r.render_text()
    assert "BEGIN" not in text
    assert "PRIVATE KEY" not in text
    assert FAKE_PASSWORD not in text
    # Prefer not echoing key paths in success path.
    assert "lab_ssh_ed25519" not in text
    assert "key_path=" not in text


def test_ssh_open_mock_connect_failed(mrc_home: Path) -> None:
    def connector(**_kwargs: object) -> None:
        raise TransportError("CONNECT_FAILED", "mock refused")

    r = endpoint_ops.run(
        op="open",
        profile="lab-ssh",
        home=mrc_home,
        connector=connector,
    )
    assert r.status == "error"
    assert r.code == "CONNECT_FAILED"
    assert "mock" in (r.fields.get("msg") or "").lower() or "refused" in (
        r.fields.get("msg") or ""
    ).lower()
    # reg.open raises before insert - failed connect must not leave a name.
    assert get_registry().get("lab-ssh") is None
    text = r.render_text()
    assert FAKE_PEM not in text
    assert "PRIVATE KEY" not in text


def test_ssh_open_mock_raises_generic(mrc_home: Path) -> None:
    def connector(**_kwargs: object) -> None:
        raise OSError("network unreachable")

    r = endpoint_ops.run(
        op="open",
        profile="lab-ssh",
        home=mrc_home,
        connector=connector,
    )
    assert r.status == "error"
    assert r.code == "CONNECT_FAILED"


def test_cli_local_open_list_close(
    mrc_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["endpoint", "open", "--profile", "local"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "@endpoint ok" in out
    assert "caps=exec,fs,screen" in out

    assert main(["endpoint", "list", "--json"]) == EXIT_OK
    raw = capsys.readouterr().out.strip()
    data = json.loads(raw)
    assert data["status"] == "ok"
    assert data.get("open") == 1

    assert main(["endpoint", "close", "--ep", "local"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "@endpoint ok" in out
    assert "disconnected" in out


def test_cli_ssh_connect_failed_exit(
    mrc_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Inject failing connector via registry so CLI path fails without network.
    reg = get_registry()

    def boom(**_kwargs: object) -> None:
        raise TransportError("CONNECT_FAILED", "injected failure")

    reg.ssh_connector = boom  # type: ignore[assignment]
    code = main(["endpoint", "open", "--profile", "lab-ssh"])
    assert code == EXIT_TRANSPORT
    out = capsys.readouterr().out
    assert "CONNECT_FAILED" in out
    assert "@endpoint error" in out


def test_agent_text_never_contains_key_material(
    mrc_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Open success/failure must not leak PEM or passwords into Agent text."""
    # Point a disposable profile-like key that holds PEM body on disk;
    # resolution returns path only - open still uses mock connector.
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    key_file = secrets / "evil_key"
    key_file.write_text(FAKE_PEM + FAKE_PASSWORD, encoding="utf-8")

    def connector(**kwargs: object) -> _MockConn:
        # Even if caller tried to read keys, connector must not put body in errors.
        _ = kwargs
        return _MockConn()

    r = endpoint_ops.run(
        op="open",
        profile="lab-ssh",
        home=mrc_home,
        connector=connector,
    )
    text = r.render_text()
    assert FAKE_PEM not in text
    assert "BEGIN OPENSSH" not in text
    assert FAKE_PASSWORD not in text
    raw_json = r.render_json()
    assert FAKE_PEM not in raw_json
    assert FAKE_PASSWORD not in raw_json


# ---------------------------------------------------------------------------
# SSH force_utf8_remote: explicit false/0 must not fall through to defaults
# ---------------------------------------------------------------------------


class _RecordingRunConn:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def is_closing(self) -> bool:
        return False

    def close(self) -> None:
        return None

    def run(self, command: str, **_kwargs: object) -> object:
        self.commands.append(command)

        class Raw:
            exit_status = 0
            stdout = b""
            stderr = b""

        return Raw()


def _ssh_profile(
    *,
    ssh: dict[str, object] | None = None,
    defaults: dict[str, object] | None = None,
) -> Profile:
    return Profile(
        name="utf8-lab",
        transport="ssh",
        host="h",
        username="u",
        ssh=dict(ssh or {}),
        defaults=dict(defaults or {}),
    )


def _run_via_built_ssh(
    *,
    ssh: dict[str, object] | None = None,
    defaults: dict[str, object] | None = None,
) -> tuple[SSHTransport, list[str]]:
    conn = _RecordingRunConn()
    transport = get_registry()._build_transport(
        _ssh_profile(ssh=ssh, defaults=defaults),
        connector=lambda **_k: conn,
    )
    assert isinstance(transport, SSHTransport)
    transport.connect()
    transport.run_command("echo hi")
    return transport, conn.commands


def test_ssh_explicit_force_utf8_remote_false_not_wrapped_by_defaults() -> None:
    t, commands = _run_via_built_ssh(
        ssh={"force_utf8_remote": False},
        defaults={"force_utf8_remote": True},
    )
    assert t.force_utf8_remote is False
    assert commands
    assert "LC_ALL" not in commands[0]
    assert "echo hi" in commands[0]


@pytest.mark.parametrize("raw", [False, 0, "false", "0"])
def test_ssh_explicit_force_utf8_alias_false_not_wrapped(raw: object) -> None:
    t, commands = _run_via_built_ssh(
        ssh={"force_utf8": raw},
        defaults={"force_utf8_remote": True},
    )
    assert t.force_utf8_remote is False
    assert commands
    assert "LC_ALL" not in commands[0]


def test_ssh_omitted_force_utf8_falls_back_to_defaults_and_wraps() -> None:
    t, commands = _run_via_built_ssh(defaults={"force_utf8_remote": True})
    assert t.force_utf8_remote is True
    assert commands
    assert "LC_ALL" in commands[0]
    assert "echo hi" in commands[0]


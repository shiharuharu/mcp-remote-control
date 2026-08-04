"""Service tests: endpoint lifecycle local + mock SSH (T06)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_remote_control.cli import main
from mcp_remote_control.cli_cmds import EXIT_OK, EXIT_TRANSPORT
from mcp_remote_control.core import endpoint_ops
from mcp_remote_control.endpoint import ensure_endpoint, get_registry, reset_registry
from mcp_remote_control.transport import TransportError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"

# Fake PEM material must never appear in Agent open output.
FAKE_PEM = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
    "QyNTUxOQAAACFakeKeyMaterialForRedactTestsOnlyXXXX=\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)
FAKE_PASSWORD = "super-secret-password-xyz"


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    reset_registry()
    yield
    reset_registry()


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
    # Local open seeds these from env/platform — at least one should appear.
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


def test_probe_summary_fields_winrm_ps_tokens() -> None:
    """WinRM probe → ps_version / lang_mode / ps_fs / ps_edition Agent tokens."""
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
    # resolution returns path only — open still uses mock connector.
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

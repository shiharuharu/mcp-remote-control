"""Unit tests: WinRM enterprise auth param assembly (no domain/network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_remote_control.config import ProfileInvalid, load_profile
from mcp_remote_control.endpoint.registry import EndpointRegistry
from mcp_remote_control.transport import (
    TransportError,
    WinRMTransport,
    assemble_pypsrp_kwargs,
    parse_spn,
)
from mcp_remote_control.transport.base import ExecResult
from mcp_remote_control.transport.winrm import (
    _coerce_exec_result,
    resolve_pypsrp_op_read_timeouts,
)
from mcp_remote_control.transport.winrm_timeouts import PYPSRP_HTTP_TIMEOUT_SLACK_S

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"


# ---------------------------------------------------------------------------
# SPN parsing
# ---------------------------------------------------------------------------


def test_parse_spn_service_host() -> None:
    service, host = parse_spn("WSMAN/win.corp.example")
    assert service == "WSMAN"
    assert host == "win.corp.example"


def test_parse_spn_bare_host() -> None:
    service, host = parse_spn("win.corp.example")
    assert service is None
    assert host == "win.corp.example"


def test_parse_spn_empty() -> None:
    assert parse_spn(None) == (None, None)
    assert parse_spn("  ") == (None, None)


# ---------------------------------------------------------------------------
# assemble_pypsrp_kwargs - enterprise methods
# ---------------------------------------------------------------------------


def test_assemble_ntlm_basic_password() -> None:
    kw = assemble_pypsrp_kwargs(
        host="10.0.0.20",
        port=5985,
        username="Administrator",
        password="s3cret-ntlm",
        auth="ntlm",
        ssl=False,
        encryption="auto",
    )
    assert kw["auth"] == "ntlm"
    assert kw["username"] == "Administrator"
    assert kw["password"] == "s3cret-ntlm"
    assert kw["ssl"] is False
    assert "certificate_pem" not in kw


def test_assemble_basic_auth() -> None:
    kw = assemble_pypsrp_kwargs(
        host="h",
        username="u",
        password="p",
        auth="basic",
        ssl=True,
        encryption="never",
    )
    assert kw["auth"] == "basic"
    assert kw["password"] == "p"


def test_assemble_kerberos_with_spn() -> None:
    kw = assemble_pypsrp_kwargs(
        host="win.corp.example",
        port=5985,
        username="CORP\\alice",
        password=None,  # ticket cache OK
        auth="kerberos",
        ssl=False,
        encryption="auto",
        spn="WSMAN/win.corp.example",
        negotiate_delegate=True,
    )
    assert kw["auth"] == "kerberos"
    assert kw["negotiate_service"] == "WSMAN"
    assert kw["negotiate_hostname_override"] == "win.corp.example"
    assert kw["negotiate_delegate"] is True
    assert kw.get("password") is None
    # No cert fields
    assert "certificate_pem" not in kw
    assert "credssp_auth_mechanism" not in kw


def test_assemble_kerberos_hostname_override_wins_over_spn() -> None:
    kw = assemble_pypsrp_kwargs(
        host="ip-literal",
        username="u",
        auth="kerberos",
        spn="WSMAN/from-spn.example",
        negotiate_hostname_override="explicit.example",
        negotiate_service="HTTP",
    )
    assert kw["negotiate_hostname_override"] == "explicit.example"
    assert kw["negotiate_service"] == "HTTP"  # explicit beats SPN service


def test_assemble_credssp() -> None:
    kw = assemble_pypsrp_kwargs(
        host="jump.corp.example",
        port=5986,
        username="CORP\\bob",
        password="s3cret-credssp",
        auth="credssp",
        ssl=True,
        encryption="auto",
        credssp_auth_mechanism="kerberos",
        credssp_minimum_version=2,
        credssp_disable_tlsv1_2=False,
    )
    assert kw["auth"] == "credssp"
    assert kw["password"] == "s3cret-credssp"
    assert kw["ssl"] is True
    assert kw["credssp_auth_mechanism"] == "kerberos"
    assert kw["credssp_minimum_version"] == 2
    assert kw["credssp_disable_tlsv1_2"] is False


def test_assemble_certificate() -> None:
    kw = assemble_pypsrp_kwargs(
        host="win.corp.example",
        port=5986,
        username="cert-user",
        auth="certificate",
        ssl=True,
        encryption="auto",
        certificate_pem="/cfg/secrets/client.pem",
        certificate_key_pem="/cfg/secrets/client-key.pem",
        certificate_key_password="key-pass-body",
    )
    assert kw["auth"] == "certificate"
    assert kw["ssl"] is True
    assert kw["certificate_pem"] == "/cfg/secrets/client.pem"
    assert kw["certificate_key_pem"] == "/cfg/secrets/client-key.pem"
    assert kw["certificate_key_password"] == "key-pass-body"
    # Password material not used for cert protocol.
    assert kw["password"] is None


def test_assemble_certificate_requires_ssl() -> None:
    with pytest.raises(TransportError) as ei:
        assemble_pypsrp_kwargs(
            host="h",
            username="u",
            auth="certificate",
            ssl=False,
            certificate_pem="/c.pem",
            certificate_key_pem="/k.pem",
        )
    assert ei.value.code == "INVALID_ARG"
    assert "ssl" in str(ei.value).lower() or "https" in str(ei.value).lower()


def test_assemble_certificate_requires_paths() -> None:
    with pytest.raises(TransportError) as ei:
        assemble_pypsrp_kwargs(
            host="h",
            username="u",
            auth="certificate",
            ssl=True,
        )
    assert ei.value.code == "INVALID_ARG"
    assert "certificate" in str(ei.value).lower()


def test_assemble_credssp_requires_password() -> None:
    with pytest.raises(TransportError) as ei:
        assemble_pypsrp_kwargs(
            host="h",
            username="u",
            auth="credssp",
            password=None,
        )
    assert ei.value.code == "AUTH_FAILED"


def test_assemble_basic_requires_password() -> None:
    with pytest.raises(TransportError) as ei:
        assemble_pypsrp_kwargs(
            host="h",
            username="u",
            auth="basic",
        )
    assert ei.value.code == "AUTH_FAILED"


def test_assemble_encryption_always_incompatible_with_basic() -> None:
    with pytest.raises(TransportError) as ei:
        assemble_pypsrp_kwargs(
            host="h",
            username="u",
            password="p",
            auth="basic",
            encryption="always",
        )
    assert ei.value.code == "INVALID_ARG"


def test_assemble_unknown_protocol() -> None:
    with pytest.raises(TransportError) as ei:
        assemble_pypsrp_kwargs(host="h", username="u", auth="digest")
    assert ei.value.code == "INVALID_ARG"


# ---------------------------------------------------------------------------
# resolve_pypsrp_op_read_timeouts + connect kwargs
# ---------------------------------------------------------------------------


def test_resolve_op_read_from_timeout_s_ceil() -> None:
    """Positive timeout_s -> ceil for op; read keeps the HTTP slack above op."""
    slack = PYPSRP_HTTP_TIMEOUT_SLACK_S
    assert resolve_pypsrp_op_read_timeouts(timeout_s=5) == (5, 5 + slack)
    assert resolve_pypsrp_op_read_timeouts(timeout_s=5.0) == (5, 5 + slack)
    assert resolve_pypsrp_op_read_timeouts(timeout_s=5.01) == (6, 6 + slack)
    assert resolve_pypsrp_op_read_timeouts(timeout_s=0.5) == (1, 1 + slack)


def test_resolve_op_read_profile_explicit_wins() -> None:
    """Profile operation_timeout_s / read_timeout_s override derivation.

    The profile read also caps the profile op (the ordering invariant), so the
    pair pinned here is an ordered one.
    """
    op, rd = resolve_pypsrp_op_read_timeouts(
        timeout_s=5,
        operation_timeout_s=60,
        read_timeout_s=88,
    )
    assert op == 60
    assert rd == 88
    # Per-field: only op explicit -> read derived above that effective op.
    op2, rd2 = resolve_pypsrp_op_read_timeouts(
        timeout_s=5,
        operation_timeout_s=60,
        read_timeout_s=None,
    )
    assert op2 == 60
    assert rd2 == 60 + PYPSRP_HTTP_TIMEOUT_SLACK_S


def test_resolve_op_read_no_timeout_not_forced_short() -> None:
    """timeout omitted/None -> no derived short op/read."""
    assert resolve_pypsrp_op_read_timeouts(timeout_s=None) == (None, None)
    assert resolve_pypsrp_op_read_timeouts() == (None, None)
    assert resolve_pypsrp_op_read_timeouts(timeout_s=0) == (None, None)
    assert resolve_pypsrp_op_read_timeouts(timeout_s=-1) == (None, None)
    # Explicit profile still applies without a call timeout.
    assert resolve_pypsrp_op_read_timeouts(
        timeout_s=None,
        operation_timeout_s=120,
        read_timeout_s=150,
    ) == (120, 150)


def test_assemble_includes_operation_and_read_timeout() -> None:
    kw = assemble_pypsrp_kwargs(
        host="h",
        username="u",
        password="p",
        auth="ntlm",
        operation_timeout=45,
        read_timeout=50,
    )
    assert kw["operation_timeout"] == 45
    assert kw["read_timeout"] == 50


def test_transport_connect_kwargs_profile_op_read_not_from_connect_ms() -> None:
    """Connect uses connect_timeout_ms only; profile op/read when set."""
    t = WinRMTransport(
        host="h",
        username="u",
        password="p",
        connect_timeout_ms=15000,
        operation_timeout_s=77,
        read_timeout_s=88,
        connector=lambda **_k: object(),
    )
    kw = t.connect_kwargs()
    assert kw["connect_timeout"] == 15.0
    assert kw["operation_timeout"] == 77
    assert kw["read_timeout"] == 88

    t2 = WinRMTransport(
        host="h",
        username="u",
        password="p",
        connect_timeout_ms=8000,
        connector=lambda **_k: object(),
    )
    kw2 = t2.connect_kwargs()
    assert kw2["connect_timeout"] == 8.0
    # No profile op/read -> not forced short in connect kwargs.
    assert "operation_timeout" not in kw2
    assert "read_timeout" not in kw2


# ---------------------------------------------------------------------------
# WinRMTransport.connect_kwargs + connector capture
# ---------------------------------------------------------------------------


def test_transport_connect_kwargs_kerberos() -> None:
    t = WinRMTransport(
        host="win.corp.example",
        username="CORP\\alice",
        auth="kerberos",
        spn="WSMAN/win.corp.example",
        negotiate_delegate=False,
        connector=lambda **_k: object(),
    )
    kw = t.connect_kwargs()
    assert kw["auth"] == "kerberos"
    assert kw["negotiate_service"] == "WSMAN"
    assert "password" not in repr(t)
    assert t.meta == {}  # not connected yet


def test_transport_connector_receives_credssp_kwargs() -> None:
    seen: dict[str, object] = {}

    def connector(**kwargs: object) -> object:
        seen.update(kwargs)
        return object()

    t = WinRMTransport(
        host="h",
        username="u",
        password="cred-pass",
        auth="credssp",
        ssl=True,
        credssp_auth_mechanism="ntlm",
        connector=connector,
    )
    t.connect()
    assert seen["auth"] == "credssp"
    assert seen["password"] == "cred-pass"
    assert seen["credssp_auth_mechanism"] == "ntlm"
    assert "cred-pass" not in repr(t)
    assert t.meta.get("auth") == "credssp"
    assert "password" not in t.meta


def test_transport_connector_receives_certificate_kwargs() -> None:
    seen: dict[str, object] = {}

    def connector(**kwargs: object) -> object:
        seen.update(kwargs)
        return object()

    t = WinRMTransport(
        host="h",
        username="u",
        auth="certificate",
        ssl=True,
        certificate_pem="/secrets/c.pem",
        certificate_key_pem="/secrets/k.pem",
        certificate_key_password="key-body-secret",
        connector=connector,
    )
    t.connect()
    assert seen["auth"] == "certificate"
    assert seen["certificate_pem"] == "/secrets/c.pem"
    assert seen["certificate_key_pem"] == "/secrets/k.pem"
    assert seen["certificate_key_password"] == "key-body-secret"
    assert seen.get("password") is None
    assert t.meta.get("cert_path") == "/secrets/c.pem"
    assert "key-body-secret" not in repr(t)
    assert "key-body-secret" not in str(t.meta)


def test_transport_invalid_cert_combo_raises_before_connector() -> None:
    called = {"n": 0}

    def connector(**_k: object) -> object:
        called["n"] += 1
        return object()

    t = WinRMTransport(
        host="h",
        username="u",
        auth="certificate",
        ssl=False,  # invalid
        certificate_pem="/c.pem",
        certificate_key_pem="/k.pem",
        connector=connector,
    )
    with pytest.raises(TransportError) as ei:
        t.connect()
    assert ei.value.code == "INVALID_ARG"
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# Profile load: enterprise methods + invalid combos
# ---------------------------------------------------------------------------


def _write_profile(home: Path, name: str, body: str) -> None:
    pdir = home / "profiles"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{name}.toml").write_text(body, encoding="utf-8")


def test_load_kerberos_profile(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "win-kerb",
        "\n".join(
            [
                'name = "win-kerb"',
                'transport = "winrm"',
                'host = "win.corp.example"',
                'username = "CORP\\\\alice"',
                "[auth]",
                'method = "kerberos"',
                'spn = "WSMAN/win.corp.example"',
                "negotiate_delegate = true",
                "[winrm]",
                'scheme = "http"',
                'message_encryption = "auto"',
            ]
        )
        + "\n",
    )
    profile = load_profile(tmp_path, "win-kerb")
    assert profile.auth is not None
    assert profile.auth.method == "kerberos"
    assert profile.auth.spn == "WSMAN/win.corp.example"
    assert profile.auth.negotiate_delegate is True
    # No secret bodies in repr
    blob = repr(profile)
    assert "BEGIN" not in blob


def test_load_credssp_profile(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "pw").write_text("domain-password\n", encoding="utf-8")
    _write_profile(
        tmp_path,
        "win-credssp",
        "\n".join(
            [
                'name = "win-credssp"',
                'transport = "winrm"',
                'host = "jump.corp.example"',
                'port = 5986',
                'username = "CORP\\\\bob"',
                "[auth]",
                'method = "credssp"',
                'password_path = "secrets/pw"',
                'credssp_auth_mechanism = "auto"',
                "[winrm]",
                'scheme = "https"',
                'server_cert_validation = "ignore"',
            ]
        )
        + "\n",
    )
    profile = load_profile(tmp_path, "win-credssp")
    assert profile.auth is not None
    assert profile.auth.method == "credssp"
    assert profile.auth.password_path is not None
    assert profile.auth.password_path.name == "pw"
    assert profile.auth.credssp_auth_mechanism == "auto"
    assert "domain-password" not in repr(profile)


def test_load_certificate_profile(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "client.pem").write_text(
        "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n",
        encoding="utf-8",
    )
    (secrets / "client-key.pem").write_text(
        "-----BEGIN PRIVATE KEY-----\nFAKEKEYBODY\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    _write_profile(
        tmp_path,
        "win-cert",
        "\n".join(
            [
                'name = "win-cert"',
                'transport = "winrm"',
                'host = "win.corp.example"',
                'username = "cert-mapped"',
                "[auth]",
                'method = "certificate"',
                'cert_path = "secrets/client.pem"',
                'cert_key_path = "secrets/client-key.pem"',
                "[winrm]",
                'scheme = "https"',
                'server_cert_validation = "validate"',
            ]
        )
        + "\n",
    )
    profile = load_profile(tmp_path, "win-cert")
    assert profile.auth is not None
    assert profile.auth.method == "certificate"
    assert profile.auth.cert_path is not None
    assert profile.auth.cert_path.name == "client.pem"
    assert profile.auth.cert_key_path is not None
    # PEM bodies must never appear in profile repr
    blob = repr(profile) + str(profile.auth)
    assert "FAKEKEYBODY" not in blob
    assert "BEGIN PRIVATE KEY" not in blob
    assert "BEGIN CERTIFICATE" not in blob
    assert "client.pem" in blob or "cert_path" in blob


def test_load_certificate_rejects_pem_body_as_cert_path(tmp_path: Path) -> None:
    """PEM armor in cert_path / certificate_pem / cert_key_path is not a path."""
    pem = "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"
    key = "-----BEGIN PRIVATE KEY-----\nFAKEKEYBODY\n-----END PRIVATE KEY-----\n"
    cases = (
        ("cert_path", pem),
        ("certificate_pem", pem),
        ("cert_key_path", key),
        ("certificate_key_pem", key),
    )
    for field, body in cases:
        name = f"pem-{field.replace('_', '-')}"
        _write_profile(
            tmp_path,
            name,
            "\n".join(
                [
                    f'name = "{name}"',
                    'transport = "winrm"',
                    'host = "h"',
                    'username = "u"',
                    "[auth]",
                    'method = "certificate"',
                    f'{field} = """{body}"""',
                    "[winrm]",
                    'scheme = "https"',
                ]
            )
            + "\n",
        )
        with pytest.raises(ProfileInvalid) as ei:
            load_profile(tmp_path, name)
        msg = str(ei.value)
        assert field in msg
        assert "PEM" in msg or "path" in msg.lower()


def test_invalid_certificate_missing_paths(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "bad-cert",
        "\n".join(
            [
                'name = "bad-cert"',
                'transport = "winrm"',
                'host = "h"',
                'username = "u"',
                "[auth]",
                'method = "certificate"',
                "[winrm]",
                'scheme = "https"',
            ]
        )
        + "\n",
    )
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(tmp_path, "bad-cert")
    assert "cert_path" in str(ei.value) or "certificate" in str(ei.value).lower()


def test_invalid_certificate_requires_https(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "c.pem").write_text("x", encoding="utf-8")
    (secrets / "k.pem").write_text("y", encoding="utf-8")
    _write_profile(
        tmp_path,
        "bad-cert-http",
        "\n".join(
            [
                'name = "bad-cert-http"',
                'transport = "winrm"',
                'host = "h"',
                'username = "u"',
                "[auth]",
                'method = "certificate"',
                'cert_path = "secrets/c.pem"',
                'cert_key_path = "secrets/k.pem"',
                "[winrm]",
                'scheme = "http"',
            ]
        )
        + "\n",
    )
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(tmp_path, "bad-cert-http")
    assert "https" in str(ei.value).lower()


def test_invalid_credssp_without_password(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "bad-credssp",
        "\n".join(
            [
                'name = "bad-credssp"',
                'transport = "winrm"',
                'host = "h"',
                'username = "u"',
                "[auth]",
                'method = "credssp"',
            ]
        )
        + "\n",
    )
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(tmp_path, "bad-credssp")
    assert "password" in str(ei.value).lower()


def test_invalid_ssh_method_on_winrm(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "bad-ssh-on-win",
        "\n".join(
            [
                'name = "bad-ssh-on-win"',
                'transport = "winrm"',
                'host = "h"',
                'username = "u"',
                "[auth]",
                'method = "private_key_path"',
                'key_path = "secrets/k"',
            ]
        )
        + "\n",
    )
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(tmp_path, "bad-ssh-on-win")
    assert "winrm" in str(ei.value).lower() or "private_key" in str(ei.value)


def test_invalid_kerberos_on_ssh(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "bad-kerb-ssh",
        "\n".join(
            [
                'name = "bad-kerb-ssh"',
                'transport = "ssh"',
                'host = "h"',
                'username = "u"',
                "[auth]",
                'method = "kerberos"',
            ]
        )
        + "\n",
    )
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(tmp_path, "bad-kerb-ssh")
    assert "ssh" in str(ei.value).lower() or "kerberos" in str(ei.value)


def test_invalid_cert_paths_without_certificate_method(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "c.pem").write_text("x", encoding="utf-8")
    (secrets / "k.pem").write_text("y", encoding="utf-8")
    _write_profile(
        tmp_path,
        "bad-cert-ntlm",
        "\n".join(
            [
                'name = "bad-cert-ntlm"',
                'transport = "winrm"',
                'host = "h"',
                'username = "u"',
                "[auth]",
                'method = "ntlm"',
                'password_path = "secrets/c.pem"',
                'cert_path = "secrets/c.pem"',
                'cert_key_path = "secrets/k.pem"',
            ]
        )
        + "\n",
    )
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(tmp_path, "bad-cert-ntlm")
    assert "certificate" in str(ei.value).lower()


def test_invalid_encryption_always_with_certificate(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "c.pem").write_text("x", encoding="utf-8")
    (secrets / "k.pem").write_text("y", encoding="utf-8")
    _write_profile(
        tmp_path,
        "bad-enc",
        "\n".join(
            [
                'name = "bad-enc"',
                'transport = "winrm"',
                'host = "h"',
                'username = "u"',
                "[auth]",
                'method = "certificate"',
                'cert_path = "secrets/c.pem"',
                'cert_key_path = "secrets/k.pem"',
                "[winrm]",
                'scheme = "https"',
                'message_encryption = "always"',
            ]
        )
        + "\n",
    )
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(tmp_path, "bad-enc")
    assert "encryption" in str(ei.value).lower() or "always" in str(ei.value)


def test_existing_lab_win_password_path_still_loads() -> None:
    """Regression: basic password_path path for lab-win fixture."""
    profile = load_profile(FIXTURES, "lab-win")
    assert profile.transport == "winrm"
    assert profile.auth is not None
    assert profile.auth.method == "password"
    assert profile.auth.password_path is not None
    assert profile.auth.password_path.name == "lab_win_password"


# ---------------------------------------------------------------------------
# Registry wiring: profile -> transport connect kwargs (mock, no network)
# ---------------------------------------------------------------------------


def test_registry_kerberos_assembly(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "win-kerb",
        "\n".join(
            [
                'name = "win-kerb"',
                'transport = "winrm"',
                'host = "win.corp.example"',
                'username = "CORP\\\\alice"',
                "[auth]",
                'method = "kerberos"',
                'spn = "WSMAN/win.corp.example"',
                "[winrm]",
                'scheme = "http"',
            ]
        )
        + "\n",
    )
    seen: dict[str, object] = {}

    def connector(**kwargs: object) -> object:
        seen.update(kwargs)

        class Sess:
            cwd = r"C:\Users\alice"
            os = "windows"
            shell = "powershell"

            def run_command(self, *a: object, **k: object) -> ExecResult:
                return ExecResult(0, "ok", "", cwd=self.cwd)

        return Sess()

    reg = EndpointRegistry()
    ep = reg.open("win-kerb", home=tmp_path, connector=connector, probe=False)
    assert ep.connected is True
    assert seen["auth"] == "kerberos"
    assert seen["negotiate_service"] == "WSMAN"
    assert seen["negotiate_hostname_override"] == "win.corp.example"
    assert isinstance(ep.transport, WinRMTransport)
    assert ep.transport.auth == "kerberos"
    assert "password" not in (ep.meta or {})


def test_registry_credssp_assembly(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "pw").write_text("domain-password-value\n", encoding="utf-8")
    _write_profile(
        tmp_path,
        "win-credssp",
        "\n".join(
            [
                'name = "win-credssp"',
                'transport = "winrm"',
                'host = "jump.example"',
                'username = "bob"',
                "[auth]",
                'method = "credssp"',
                'password_path = "secrets/pw"',
                'credssp_auth_mechanism = "ntlm"',
                "[winrm]",
                'scheme = "https"',
                'server_cert_validation = "ignore"',
            ]
        )
        + "\n",
    )
    seen: dict[str, object] = {}

    def connector(**kwargs: object) -> object:
        seen.update(kwargs)
        return object()

    reg = EndpointRegistry()
    ep = reg.open("win-credssp", home=tmp_path, connector=connector, probe=False)
    assert seen["auth"] == "credssp"
    assert seen["password"] == "domain-password-value"
    assert seen["credssp_auth_mechanism"] == "ntlm"
    assert seen["ssl"] is True
    # Secret not in endpoint meta / transport repr
    assert "domain-password-value" not in repr(ep.transport)
    assert "domain-password-value" not in str(ep.meta)


def test_registry_certificate_assembly(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    pem_body = "-----BEGIN CERTIFICATE-----\nCERTBODY\n-----END CERTIFICATE-----\n"
    key_body = "-----BEGIN PRIVATE KEY-----\nKEYBODYSECRET\n-----END PRIVATE KEY-----\n"
    (secrets / "c.pem").write_text(pem_body, encoding="utf-8")
    (secrets / "k.pem").write_text(key_body, encoding="utf-8")
    (secrets / "keypass").write_text("key-pass-secret\n", encoding="utf-8")
    _write_profile(
        tmp_path,
        "win-cert",
        "\n".join(
            [
                'name = "win-cert"',
                'transport = "winrm"',
                'host = "secure.example"',
                'username = "mapped"',
                "[auth]",
                'method = "certificate"',
                'cert_path = "secrets/c.pem"',
                'cert_key_path = "secrets/k.pem"',
                'cert_key_password_path = "secrets/keypass"',
                "[winrm]",
                'scheme = "https"',
            ]
        )
        + "\n",
    )
    seen: dict[str, object] = {}

    def connector(**kwargs: object) -> object:
        seen.update(kwargs)
        return object()

    reg = EndpointRegistry()
    ep = reg.open("win-cert", home=tmp_path, connector=connector, probe=False)
    assert seen["auth"] == "certificate"
    assert seen["ssl"] is True
    assert str(seen["certificate_pem"]).endswith("c.pem")
    assert str(seen["certificate_key_pem"]).endswith("k.pem")
    assert seen["certificate_key_password"] == "key-pass-secret"
    # Paths only in meta - never PEM bodies or key password
    assert "KEYBODYSECRET" not in str(ep.meta)
    assert "key-pass-secret" not in str(ep.meta)
    assert "KEYBODYSECRET" not in repr(ep.transport)
    assert "CERTBODY" not in repr(ep.transport)


# ---------------------------------------------------------------------------
# WinRM profile bool string-safe (no bool("false") enable)
# ---------------------------------------------------------------------------


def test_build_winrm_cert_validation_string_false_disables() -> None:
    """cert_validation=\"false\"/\"0\"/\"no\"/\"off\" -> False (not bool(str))."""
    from mcp_remote_control.config.models import Profile
    from mcp_remote_control.endpoint.registry import _build_winrm_transport

    for raw in ("false", "False", "0", "no", "off", "  false  "):
        t = _build_winrm_transport(
            Profile(
                name="w",
                transport="winrm",
                host="h",
                username="u",
                winrm={"cert_validation": raw},
            ),
            connector=lambda **_k: object(),
        )
        assert t.cert_validation is False, f"raw={raw!r}"
        assert t.connect_kwargs()["cert_validation"] is False


def test_build_winrm_cert_validation_true_forms() -> None:
    """Native True and string true/1 still enable cert validation."""
    from mcp_remote_control.config.models import Profile
    from mcp_remote_control.endpoint.registry import _build_winrm_transport

    for raw in (True, "true", "TRUE", "1", "yes", "on"):
        t = _build_winrm_transport(
            Profile(
                name="w",
                transport="winrm",
                host="h",
                username="u",
                winrm={"cert_validation": raw},
            ),
            connector=lambda **_k: object(),
        )
        assert t.cert_validation is True, f"raw={raw!r}"


def test_build_winrm_cert_validation_bool_false() -> None:
    """Native TOML bool false still disables."""
    from mcp_remote_control.config.models import Profile
    from mcp_remote_control.endpoint.registry import _build_winrm_transport

    t = _build_winrm_transport(
        Profile(
            name="w",
            transport="winrm",
            host="h",
            username="u",
            winrm={"cert_validation": False},
        ),
        connector=lambda **_k: object(),
    )
    assert t.cert_validation is False


def test_build_winrm_server_cert_validation_ignore_path_unchanged() -> None:
    """server_cert_validation ignore/false/0/no still disable validation."""
    from mcp_remote_control.config.models import Profile
    from mcp_remote_control.endpoint.registry import _build_winrm_transport

    for scv in ("ignore", "false", "0", "no", "IGNORE"):
        t = _build_winrm_transport(
            Profile(
                name="w",
                transport="winrm",
                host="h",
                username="u",
                winrm={"server_cert_validation": scv},
            ),
            connector=lambda **_k: object(),
        )
        assert t.cert_validation is False, f"scv={scv!r}"

    # validate / other tokens keep default True
    t_ok = _build_winrm_transport(
        Profile(
            name="w",
            transport="winrm",
            host="h",
            username="u",
            winrm={"server_cert_validation": "validate"},
        ),
        connector=lambda **_k: object(),
    )
    assert t_ok.cert_validation is True


def test_build_winrm_ssl_string_false_does_not_force_ssl() -> None:
    """ssl=\"false\" with non-https scheme must not enable SSL via bool(str)."""
    from mcp_remote_control.config.models import Profile
    from mcp_remote_control.endpoint.registry import _build_winrm_transport

    for raw in ("false", "0", "no", "off", False):
        t = _build_winrm_transport(
            Profile(
                name="w",
                transport="winrm",
                host="h",
                username="u",
                port=5985,
                winrm={"scheme": "http", "ssl": raw},
            ),
            connector=lambda **_k: object(),
        )
        assert t.ssl is False, f"raw={raw!r}"
        assert t.connect_kwargs()["ssl"] is False


def test_build_winrm_ssl_string_true_enables() -> None:
    """ssl=\"true\"/1 with http scheme enables SSL; https scheme still forces ssl."""
    from mcp_remote_control.config.models import Profile
    from mcp_remote_control.endpoint.registry import _build_winrm_transport

    for raw in (True, "true", "1", "yes", "on"):
        t = _build_winrm_transport(
            Profile(
                name="w",
                transport="winrm",
                host="h",
                username="u",
                port=5985,
                winrm={"scheme": "http", "ssl": raw},
            ),
            connector=lambda **_k: object(),
        )
        assert t.ssl is True, f"raw={raw!r}"

    t_https = _build_winrm_transport(
        Profile(
            name="w",
            transport="winrm",
            host="h",
            username="u",
            port=5986,
            winrm={"scheme": "https", "ssl": "false"},
        ),
        connector=lambda **_k: object(),
    )
    # scheme=https still wins over ssl flag
    assert t_https.ssl is True


def test_build_winrm_credssp_disable_tlsv1_2_string_false() -> None:
    """[winrm.credssp] disable_tlsv1_2=\"false\" -> False (not True)."""
    from mcp_remote_control.config.models import Profile
    from mcp_remote_control.endpoint.registry import _build_winrm_transport

    for raw in ("false", "0", "no", "off", False):
        t = _build_winrm_transport(
            Profile(
                name="w",
                transport="winrm",
                host="h",
                username="u",
                winrm={"credssp": {"disable_tlsv1_2": raw}},
            ),
            connector=lambda **_k: object(),
        )
        assert t.credssp_disable_tlsv1_2 is False, f"raw={raw!r}"


def test_build_winrm_credssp_disable_tlsv1_2_true_forms() -> None:
    """disable_tlsv1_2 true/\"true\"/1 enable the flag."""
    from mcp_remote_control.config.models import Profile
    from mcp_remote_control.endpoint.registry import _build_winrm_transport

    for raw in (True, "true", "1", "yes", "on"):
        t = _build_winrm_transport(
            Profile(
                name="w",
                transport="winrm",
                host="h",
                username="u",
                winrm={"credssp": {"disable_tlsv1_2": raw}},
            ),
            connector=lambda **_k: object(),
        )
        assert t.credssp_disable_tlsv1_2 is True, f"raw={raw!r}"


def test_registry_profile_bool_strings_map_to_transport(tmp_path: Path) -> None:
    """End-to-end: TOML string false flags reach transport (and credssp kwargs)."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "pw").write_text("pw-value\n", encoding="utf-8")
    _write_profile(
        tmp_path,
        "win-bools",
        "\n".join(
            [
                'name = "win-bools"',
                'transport = "winrm"',
                'host = "h.example"',
                "port = 5985",
                'username = "admin"',
                "[auth]",
                'method = "credssp"',
                'password_path = "secrets/pw"',
                "[winrm]",
                'scheme = "http"',
                'ssl = "false"',
                'cert_validation = "false"',
                "[winrm.credssp]",
                'disable_tlsv1_2 = "false"',
            ]
        )
        + "\n",
    )
    seen: dict[str, object] = {}

    def connector(**kwargs: object) -> object:
        seen.update(kwargs)
        return object()

    reg = EndpointRegistry()
    ep = reg.open("win-bools", home=tmp_path, connector=connector, probe=False)
    assert isinstance(ep.transport, WinRMTransport)
    assert ep.transport.ssl is False
    assert ep.transport.cert_validation is False
    assert ep.transport.credssp_disable_tlsv1_2 is False
    assert seen.get("ssl") is False
    assert seen.get("cert_validation") is False
    # CredSSP-only kwargs appear when auth=credssp (False must not be dropped).
    assert seen.get("credssp_disable_tlsv1_2") is False


# ---------------------------------------------------------------------------
# _coerce_exec_result - missing status -> -1 (align SSH)
# ---------------------------------------------------------------------------


class _ExecRaw:
    def __init__(self, **attrs: object) -> None:
        for k, v in attrs.items():
            setattr(self, k, v)


def test_coerce_exec_result_no_status_object_exit_minus_one() -> None:
    """No status object -> exit_code=-1 (not fake success 0)."""
    raw = _ExecRaw(exit_code=None, exit_status=None, returncode=None, stdout=b"", stderr=b"")
    r = _coerce_exec_result(raw, default_cwd=None)
    assert r.exit_code == -1


def test_coerce_exec_result_normal_returncode_zero_unchanged() -> None:
    """returncode=0 stays exit_code=0 with stdout intact."""
    raw = _ExecRaw(returncode=0, stdout=b"hi", stderr=b"")
    r = _coerce_exec_result(raw, default_cwd=r"C:\Users\u")
    assert r.exit_code == 0
    assert r.stdout == "hi"
    assert r.cwd == r"C:\Users\u"

"""Unit tests for list_profiles, load_profile, password, and probe."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_remote_control.config import (
    ConfigError,
    Profile,
    ProfileInvalid,
    ProfileNotFound,
    list_profiles,
    load_profile,
)
from mcp_remote_control.config.errors import reject_dual_password_sources

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"



def _write_ssh_profile(tmp_path: Path, name: str, auth_lines: list[str]) -> None:
    pdir = tmp_path / "profiles"
    pdir.mkdir(parents=True, exist_ok=True)
    body = "\n".join(
        [
            f'name = "{name}"',
            'transport = "ssh"',
            'host = "10.0.0.1"',
            'username = "root"',
            "[auth]",
            *auth_lines,
        ]
    )
    (pdir / f"{name}.toml").write_text(body + "\n", encoding="utf-8")


def _write_winrm_profile(
    tmp_path: Path, name: str, auth_lines: list[str], *, winrm_lines: list[str] | None = None
) -> None:
    pdir = tmp_path / "profiles"
    pdir.mkdir(parents=True, exist_ok=True)
    body_lines = [
        f'name = "{name}"',
        'transport = "winrm"',
        'host = "10.0.0.2"',
        'username = "Administrator"',
        "[auth]",
        *auth_lines,
    ]
    if winrm_lines:
        body_lines.append("[winrm]")
        body_lines.extend(winrm_lines)
    (pdir / f"{name}.toml").write_text("\n".join(body_lines) + "\n", encoding="utf-8")


def _assert_no_home_abs_in_msg(msg: str, home: Path) -> None:
    """Error messages must not embed the resolved config-home absolute prefix."""
    home_s = str(home.resolve())
    assert home_s not in msg, f"home abs leaked into msg: {msg!r}"
    # Common agent-lure prefixes (when home is under them).
    for prefix in ("/Users/", "/home/"):
        if home_s.startswith(prefix):
            assert prefix not in msg, f"{prefix!r} leaked into msg: {msg!r}"


# ---------------------------------------------------------------------------
# list_profiles / load_profile
# ---------------------------------------------------------------------------

def test_list_profiles_fixture() -> None:
    names = list_profiles(FIXTURES)
    assert "lab-ssh" in names
    assert "local" in names
    assert names == sorted(names)


def test_list_profiles_empty(tmp_path: Path) -> None:
    assert list_profiles(tmp_path) == []
    (tmp_path / "profiles").mkdir()
    assert list_profiles(tmp_path) == []


def test_load_good_ssh_profile() -> None:
    profile = load_profile(FIXTURES, "lab-ssh")
    assert isinstance(profile, Profile)
    assert profile.name == "lab-ssh"
    assert profile.transport == "ssh"
    assert profile.host == "10.0.0.5"
    assert profile.port == 22
    assert profile.username == "deploy"
    assert profile.label == "Lab SSH fixture"
    assert profile.auth is not None
    assert profile.auth.method == "private_key_path"
    assert profile.auth.key_path is not None
    assert profile.auth.key_path.name == "lab_ssh_ed25519"
    assert "secrets" in profile.auth.key_path.parts
    assert profile.defaults.get("cwd") == "/var/www"


def test_load_profile_winrm_probe_modes(tmp_path: Path) -> None:
    """Profile [winrm].probe and [defaults].winrm_probe normalize."""
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "pw").write_text("x\n", encoding="utf-8")
    (pdir / "win-skip.toml").write_text(
        'name = "win-skip"\n'
        'transport = "winrm"\n'
        'host = "10.0.0.9"\n'
        'username = "Administrator"\n'
        "[auth]\n"
        'method = "password"\n'
        'password_path = "secrets/pw"\n'
        "[winrm]\n"
        'probe = "SKIP"\n'
        'scheme = "http"\n'
        'auth = "ntlm"\n',
        encoding="utf-8",
    )
    (pdir / "win-light.toml").write_text(
        'name = "win-light"\n'
        'transport = "winrm"\n'
        'host = "10.0.0.9"\n'
        'username = "Administrator"\n'
        "[auth]\n"
        'method = "password"\n'
        'password_path = "secrets/pw"\n'
        "[defaults]\n"
        'winrm_probe = "light"\n',
        encoding="utf-8",
    )
    skip_p = load_profile(tmp_path, "win-skip")
    assert skip_p.winrm.get("probe") == "skip"
    light_p = load_profile(tmp_path, "win-light")
    assert light_p.defaults.get("winrm_probe") == "light"


def test_load_profile_winrm_probe_invalid_raises(tmp_path: Path) -> None:
    """Invalid [winrm].probe -> ProfileInvalid."""
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "pw").write_text("x\n", encoding="utf-8")
    (pdir / "win-bad.toml").write_text(
        'name = "win-bad"\n'
        'transport = "winrm"\n'
        'host = "10.0.0.9"\n'
        'username = "Administrator"\n'
        "[auth]\n"
        'method = "password"\n'
        'password_path = "secrets/pw"\n'
        "[winrm]\n"
        'probe = "maybe"\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(tmp_path, "win-bad")
    assert "probe" in str(ei.value).lower()


def test_load_local_profile() -> None:
    profile = load_profile(FIXTURES, "local")
    assert profile.transport == "local"
    assert profile.host is None
    assert profile.auth is None


def test_load_profile_not_found(tmp_path: Path) -> None:
    (tmp_path / "profiles").mkdir()
    with pytest.raises(ProfileNotFound) as ei:
        load_profile(tmp_path, "missing")
    assert isinstance(ei.value, ConfigError)


def test_load_profile_bad_toml(tmp_path: Path) -> None:
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "broken.toml").write_text("name = [unterminated\n", encoding="utf-8")
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(tmp_path, "broken")
    assert "TOML" in str(ei.value) or "invalid" in str(ei.value).lower()
    assert isinstance(ei.value, ConfigError)


def test_load_profile_utf8_bom(tmp_path: Path) -> None:
    """UTF-8 BOM on profile TOML loads successfully."""
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    body = (
        b"\xef\xbb\xbf"
        b'name = "bom-local"\n'
        b'transport = "local"\n'
    )
    (pdir / "bom-local.toml").write_bytes(body)
    profile = load_profile(tmp_path, "bom-local")
    assert profile.name == "bom-local"
    assert profile.transport == "local"


def test_load_profile_missing_required_field(tmp_path: Path) -> None:
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    # transport missing
    (pdir / "no-transport.toml").write_text(
        'name = "no-transport"\nhost = "1.2.3.4"\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(tmp_path, "no-transport")
    assert "transport" in str(ei.value)


def test_load_profile_missing_host_for_ssh(tmp_path: Path) -> None:
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "nohost.toml").write_text(
        'name = "nohost"\ntransport = "ssh"\nusername = "u"\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(tmp_path, "nohost")
    assert "host" in str(ei.value)


def test_load_profile_name_mismatch(tmp_path: Path) -> None:
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "alpha.toml").write_text(
        'name = "beta"\ntransport = "local"\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(tmp_path, "alpha")
    assert "does not match" in str(ei.value)


def test_load_profile_rejects_traversal_name(tmp_path: Path) -> None:
    """load_profile must reject path-traversal names with ProfileInvalid
    and never read files outside profiles/."""
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    # Plant a toml file OUTSIDE profiles/ that must never be read.
    planted = tmp_path / "config.toml"
    planted_content = 'name = "config"\ntransport = "local"\n'
    planted.write_text(planted_content, encoding="utf-8")
    for bad in ("../config", "..%2f", "a/../b", "..", "../", "/etc/passwd"):
        with pytest.raises(ProfileInvalid):
            load_profile(tmp_path, bad)
    # The planted outside file must be byte-for-byte untouched (proves the
    # ``../config`` case never opened it).
    assert planted.read_text() == planted_content


def test_load_profile_rejects_trailing_newline_name(tmp_path: Path) -> None:
    """fullmatch (not ``$``) rejects a trailing newline in the name."""
    (tmp_path / "profiles").mkdir()
    for bad in ("box\n", "box "):
        with pytest.raises(ProfileInvalid):
            load_profile(tmp_path, bad)


def test_secret_path_stored_but_contents_not_in_repr() -> None:
    profile = load_profile(FIXTURES, "lab-ssh")
    assert profile.auth is not None
    assert profile.auth.key_path is not None
    # Path is recorded...
    assert profile.auth.key_path.exists()
    secret_body = profile.auth.key_path.read_text(encoding="utf-8")
    assert "DUMMY_FIXTURE_KEY" in secret_body

    # ...but file contents must not appear in str/repr of profile or auth.
    text = repr(profile) + str(profile) + repr(profile.auth) + str(profile.auth)
    assert "DUMMY_FIXTURE_KEY" not in text
    assert "BEGIN OPENSSH" not in text
    # Path (or at least the filename) may appear - that is fine.
    assert "lab_ssh_ed25519" in text or "key_path" in text


def test_inline_password_stored_but_hidden_in_repr(tmp_path: Path) -> None:
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "with-pass.toml").write_text(
        "\n".join(
            [
                'name = "with-pass"',
                'transport = "ssh"',
                'host = "10.0.0.1"',
                'username = "root"',
                "[auth]",
                'method = "password"',
                'password = "s3cr3t-plain-ok"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    profile = load_profile(tmp_path, "with-pass")
    assert profile.auth is not None
    assert profile.auth.password == "s3cr3t-plain-ok"
    assert profile.auth.has_inline_password is True
    # repr still masks the body (debug dumps).
    blob = repr(profile) + str(profile)
    assert "s3cr3t-plain-ok" not in blob
    assert "password=<set>" in blob


# ---------------------------------------------------------------------------
# Empty / whitespace password must not claim material; dual password
# sources hard-rejected; each single source (plain / path / env) loads.
# ---------------------------------------------------------------------------

def test_reject_dual_password_sources_helper() -> None:
    """Shared helper owns detection + choose-one message (load/store/ops)."""
    # Single sources / blanks: no raise.
    reject_dual_password_sources(password="only")
    reject_dual_password_sources(password_path="secrets/p")
    reject_dual_password_sources(password_env="MRC_PW")
    reject_dual_password_sources(password=True)
    reject_dual_password_sources(password="")  # empty does not count
    reject_dual_password_sources(password="   ")  # whitespace-only
    reject_dual_password_sources(password="", password_path="secrets/p")
    reject_dual_password_sources(password="  ", password_env="MRC_PW")
    reject_dual_password_sources(password=False, password_path="secrets/p")

    # Dual sources: hard-fail with choose-one listing all three options.
    with pytest.raises(ProfileInvalid) as ei:
        reject_dual_password_sources(
            password="inline", password_path="secrets/p"
        )
    msg = str(ei.value)
    assert "multiple password sources" in msg
    assert "choose exactly one of: password | password_path | password_env" in msg
    assert "password" in msg and "password_path" in msg

    with pytest.raises(ProfileInvalid) as ei:
        reject_dual_password_sources(
            password=True, password_env="MRC_X10"
        )
    assert "password" in str(ei.value) and "password_env" in str(ei.value)

    with pytest.raises(ProfileInvalid) as ei:
        reject_dual_password_sources(
            password_path="secrets/p", password_env="MRC_X10"
        )
    assert "password_path" in str(ei.value) and "password_env" in str(ei.value)

    # Load-style message includes profile name + loc.
    with pytest.raises(ProfileInvalid) as ei:
        reject_dual_password_sources(
            password="x",
            password_path="secrets/p",
            profile_name="dual",
            loc="profiles/dual.toml",
        )
    msg = str(ei.value)
    assert "profile 'dual'" in msg
    assert "profiles/dual.toml" in msg
    assert "choose exactly one of: password | password_path | password_env" in msg


def test_load_empty_password_not_material(tmp_path: Path) -> None:
    """password=\"\" / whitespace-only -> unset; has_inline_password=False."""
    for name, pw in (
        ("empty-pw", 'password = ""'),
        ("ws-pw", 'password = "   \\t  "'.replace("\\t", "\t")),
        ("spaces-pw", 'password = "     "'),
    ):
        _write_ssh_profile(
            tmp_path,
            name,
            ['method = "password"', pw],
        )
        profile = load_profile(tmp_path, name)
        assert profile.auth is not None
        assert profile.auth.password is None
        assert profile.auth.has_inline_password is False
        assert profile.auth.password_path is None
        assert profile.auth.password_env is None
        # Public view must not claim material either.
        from mcp_remote_control.config.store import profile_public_dict

        pub = profile_public_dict(profile, home=tmp_path)
        auth = pub.get("auth") or {}
        assert "password" not in auth
        assert auth.get("has_inline_password") is not True


def test_load_dual_password_sources_rejected(tmp_path: Path) -> None:
    """password + password_path / password_env -> ProfileInvalid choose-one."""
    (tmp_path / "secrets").mkdir(parents=True, exist_ok=True)
    (tmp_path / "secrets" / "pw").write_text("file-pw\n", encoding="utf-8")

    cases = [
        (
            "dual-pp",
            [
                'method = "password"',
                'password = "inline"',
                'password_path = "secrets/pw"',
            ],
            ("password", "password_path"),
        ),
        (
            "dual-pe",
            [
                'method = "password"',
                'password = "inline"',
                'password_env = "MRC_TEST_PW"',
            ],
            ("password", "password_env"),
        ),
        (
            "dual-path-env",
            [
                'method = "password"',
                'password_path = "secrets/pw"',
                'password_env = "MRC_TEST_PW"',
            ],
            ("password_path", "password_env"),
        ),
    ]
    for name, auth_lines, expected_bits in cases:
        _write_ssh_profile(tmp_path, name, auth_lines)
        with pytest.raises(ProfileInvalid) as ei:
            load_profile(tmp_path, name)
        msg = str(ei.value)
        assert "multiple password sources" in msg
        assert "choose exactly one" in msg
        for bit in expected_bits:
            assert bit in msg


def test_load_single_source_plain_password(tmp_path: Path) -> None:
    """Plain password alone loads with material."""
    _write_ssh_profile(
        tmp_path,
        "plain-only",
        ['method = "password"', 'password = "only-plain"'],
    )
    p = load_profile(tmp_path, "plain-only")
    assert p.auth is not None
    assert p.auth.password == "only-plain"
    assert p.auth.has_inline_password is True
    assert p.auth.password_path is None
    assert p.auth.password_env is None


def test_load_single_source_password_path(tmp_path: Path) -> None:
    """password_path alone loads; no inline material flags."""
    (tmp_path / "secrets").mkdir(parents=True, exist_ok=True)
    (tmp_path / "secrets" / "only_path").write_text("from-file\n", encoding="utf-8")
    _write_ssh_profile(
        tmp_path,
        "path-only",
        ['method = "password"', 'password_path = "secrets/only_path"'],
    )
    p = load_profile(tmp_path, "path-only")
    assert p.auth is not None
    assert p.auth.password is None
    assert p.auth.has_inline_password is False
    assert p.auth.password_path is not None
    assert p.auth.password_path.name == "only_path"
    assert p.auth.password_env is None


def test_load_single_source_password_env(tmp_path: Path) -> None:
    """password_env alone loads; no inline/path material."""
    _write_ssh_profile(
        tmp_path,
        "env-only",
        ['method = "password"', 'password_env = "MRC_J2_ENV_PW"'],
    )
    p = load_profile(tmp_path, "env-only")
    assert p.auth is not None
    assert p.auth.password is None
    assert p.auth.has_inline_password is False
    assert p.auth.password_path is None
    assert p.auth.password_env == "MRC_J2_ENV_PW"


def test_load_empty_password_with_path_is_single_path(tmp_path: Path) -> None:
    """Blank password + password_path -> blank ignored; path-only OK."""
    (tmp_path / "secrets").mkdir(parents=True, exist_ok=True)
    (tmp_path / "secrets" / "real").write_text("ok\n", encoding="utf-8")
    _write_ssh_profile(
        tmp_path,
        "blank-plus-path",
        [
            'method = "password"',
            'password = ""',
            'password_path = "secrets/real"',
        ],
    )
    p = load_profile(tmp_path, "blank-plus-path")
    assert p.auth is not None
    assert p.auth.password is None
    assert p.auth.has_inline_password is False
    assert p.auth.password_path is not None
    assert p.auth.password_path.name == "real"


@pytest.mark.parametrize("method", ["basic", "credssp"])
def test_basic_credssp_missing_password_lists_plain(
    tmp_path: Path, method: str
) -> None:
    """No password material -> ProfileInvalid names password | path | env."""
    _write_winrm_profile(
        tmp_path,
        f"no-pw-{method}",
        [f'method = "{method}"'],
    )
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(tmp_path, f"no-pw-{method}")
    msg = str(ei.value)
    assert "password" in msg
    assert "password_path" in msg
    assert "password_env" in msg
    # Preferred pipe listing (not only path/env).
    assert "password | password_path | password_env" in msg
    assert method in msg


@pytest.mark.parametrize(
    "method,auth_extra,check",
    [
        ("basic", ['password = "plain-basic"'], "plain"),
        ("credssp", ['password = "plain-credssp"'], "plain"),
        ("basic", ['password_path = "secrets/win_pw"'], "path"),
        ("credssp", ['password_path = "secrets/win_pw"'], "path"),
        ("basic", ['password_env = "MRC_X9_BASIC_PW"'], "env"),
        ("credssp", ['password_env = "MRC_X9_CREDSSP_PW"'], "env"),
    ],
)
def test_basic_credssp_single_source_password_ok(
    tmp_path: Path,
    method: str,
    auth_extra: list[str],
    check: str,
) -> None:
    """Single-source plain / path / env still validates for basic+credssp."""
    if check == "path":
        (tmp_path / "secrets").mkdir(parents=True, exist_ok=True)
        (tmp_path / "secrets" / "win_pw").write_text("from-file\n", encoding="utf-8")
    name = f"ok-{method}-{check}"
    _write_winrm_profile(
        tmp_path,
        name,
        [f'method = "{method}"', *auth_extra],
    )
    p = load_profile(tmp_path, name)
    assert p.auth is not None
    assert p.auth.method == method
    if check == "plain":
        assert p.auth.password is not None
        assert p.auth.has_inline_password is True
        assert p.auth.password_path is None
        assert p.auth.password_env is None
    elif check == "path":
        assert p.auth.password is None
        assert p.auth.password_path is not None
        assert p.auth.password_path.name == "win_pw"
        assert p.auth.password_env is None
    else:
        assert p.auth.password is None
        assert p.auth.password_path is None
        assert p.auth.password_env is not None


def test_load_profile_relative_secrets_still_resolve() -> None:
    """Load still resolves relative secrets/ refs to real absolute Paths."""
    profile = load_profile(FIXTURES, "lab-ssh")
    assert profile.auth is not None
    assert profile.auth.key_path is not None
    # Internal model keeps absolute resolved path for connect.
    assert profile.auth.key_path.is_absolute()
    assert profile.auth.key_path.name == "lab_ssh_ed25519"
    assert "secrets" in profile.auth.key_path.parts
    assert profile.auth.key_path.exists()
    profile_w = load_profile(FIXTURES, "lab-win")
    assert profile_w.auth is not None
    assert profile_w.auth.password_path is not None
    assert profile_w.auth.password_path.is_absolute()
    assert profile_w.auth.password_path.exists()


def test_load_profile_not_found_msg_relative(tmp_path: Path) -> None:
    """ProfileNotFound embeds profiles/<name>.toml, not absolute home."""
    home = tmp_path / "mrc"
    (home / "profiles").mkdir(parents=True)
    with pytest.raises(ProfileNotFound) as ei:
        load_profile(home, "missing")
    msg = str(ei.value)
    assert "profile not found" in msg
    assert "missing" in msg
    assert "profiles/missing.toml" in msg
    _assert_no_home_abs_in_msg(msg, home)


def test_load_profile_invalid_toml_msg_relative(tmp_path: Path) -> None:
    """Bad TOML ProfileInvalid uses profiles/... location fragment."""
    home = tmp_path / "mrc"
    pdir = home / "profiles"
    pdir.mkdir(parents=True)
    (pdir / "broken.toml").write_text("name = [unterminated\n", encoding="utf-8")
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(home, "broken")
    msg = str(ei.value)
    assert "profiles/broken.toml" in msg
    _assert_no_home_abs_in_msg(msg, home)


def test_load_profile_validation_msg_relative(tmp_path: Path) -> None:
    """Missing transport / name mismatch msgs use relative loc."""
    home = tmp_path / "mrc"
    pdir = home / "profiles"
    pdir.mkdir(parents=True)
    (pdir / "no-transport.toml").write_text(
        'name = "no-transport"\nhost = "1.2.3.4"\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(home, "no-transport")
    msg = str(ei.value)
    assert "transport" in msg
    assert "profiles/no-transport.toml" in msg
    _assert_no_home_abs_in_msg(msg, home)

    (pdir / "alpha.toml").write_text(
        'name = "beta"\ntransport = "local"\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileInvalid) as ei2:
        load_profile(home, "alpha")
    msg2 = str(ei2.value)
    assert "does not match" in msg2
    assert "profiles/alpha.toml" in msg2
    _assert_no_home_abs_in_msg(msg2, home)


def test_load_profile_dual_password_msg_relative(tmp_path: Path) -> None:
    """Dual password sources error includes relative profile loc."""
    home = tmp_path / "mrc"
    pdir = home / "profiles"
    pdir.mkdir(parents=True)
    (home / "secrets").mkdir(parents=True)
    (home / "secrets" / "pw").write_text("secret\n", encoding="utf-8")
    (pdir / "dual.toml").write_text(
        'name = "dual"\n'
        'transport = "ssh"\n'
        'host = "h"\n'
        'username = "u"\n'
        "[auth]\n"
        'method = "password"\n'
        'password = "inline"\n'
        'password_path = "secrets/pw"\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(home, "dual")
    msg = str(ei.value)
    assert "multiple password sources" in msg
    assert "profiles/dual.toml" in msg
    _assert_no_home_abs_in_msg(msg, home)


# ---------------------------------------------------------------------------
# cert_path-shaped fields are filesystem paths; PEM armor is not a path.
# ---------------------------------------------------------------------------

_PEM_CERT = (
    "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"
)
_PEM_KEY = "-----BEGIN PRIVATE KEY-----\nFAKEKEYBODY\n-----END PRIVATE KEY-----\n"
_PEM_RSA = (
    "-----BEGIN RSA PRIVATE KEY-----\nFAKERSABODY\n-----END RSA PRIVATE KEY-----\n"
)
_PEM_EC = "-----BEGIN EC PRIVATE KEY-----\nFAKEECBODY\n-----END EC PRIVATE KEY-----\n"
_PEM_OPENSSH = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n"
)


@pytest.mark.parametrize(
    "field,body",
    [
        ("cert_path", _PEM_CERT),
        ("certificate_pem", _PEM_CERT),
        ("cert_key_path", _PEM_KEY),
        ("certificate_key_pem", _PEM_KEY),
        ("cert_key_path", _PEM_RSA),
        ("cert_key_path", _PEM_EC),
        ("cert_key_path", _PEM_OPENSSH),
    ],
)
def test_load_profile_rejects_pem_body_as_cert_path(
    tmp_path: Path, field: str, body: str
) -> None:
    """PEM armor in a cert/key path field is not resolved under MRC_HOME."""
    _write_winrm_profile(
        tmp_path,
        "win-pem",
        [
            'method = "certificate"',
            f'{field} = """{body}"""',
        ],
        winrm_lines=['scheme = "https"'],
    )
    with pytest.raises(ProfileInvalid) as ei:
        load_profile(tmp_path, "win-pem")
    msg = str(ei.value)
    assert field in msg
    assert "PEM" in msg or "path" in msg.lower()
    # Body must not be Path-joined (error stays a typed reject, not a path).
    assert "BEGIN" not in msg


def test_load_profile_accepts_real_cert_file_path(tmp_path: Path) -> None:
    """A real existing cert/key file path still loads."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "client.pem").write_text(_PEM_CERT, encoding="utf-8")
    (secrets / "client-key.pem").write_text(_PEM_KEY, encoding="utf-8")
    _write_winrm_profile(
        tmp_path,
        "win-cert",
        [
            'method = "certificate"',
            'cert_path = "secrets/client.pem"',
            'cert_key_path = "secrets/client-key.pem"',
        ],
        winrm_lines=['scheme = "https"'],
    )
    profile = load_profile(tmp_path, "win-cert")
    assert profile.auth is not None
    assert profile.auth.cert_path is not None
    assert profile.auth.cert_path.is_file()
    assert profile.auth.cert_path.name == "client.pem"
    assert profile.auth.cert_key_path is not None
    assert profile.auth.cert_key_path.is_file()
    assert profile.auth.cert_key_path.name == "client-key.pem"

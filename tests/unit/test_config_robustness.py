"""Profile values that are legal TOML but unusable as a path or a duration.

Three defects of one class live here: a value reaches the config layer
verbatim and then hits an unguarded conversion.

* An auth path whose ``~user`` has no home directory on this host.
  ``Path.expanduser`` raises ``RuntimeError`` for it, so the home-escape
  guard must not expand it blindly and every auth path field must reject it
  as a profile defect instead of letting the exception escape as a bogus
  write/network failure.
* A WinRM tuning knob written as ``inf`` (``1e400`` is the same value after
  parsing) or as a bool. ``int(float("inf"))`` raises ``OverflowError`` and
  ``int(True)`` silently means one unit, so both must degrade to "unset" and
  leave the transport its default.
* A ``[defaults]`` key advertised as consumed must have a consumer.
* The same unresolvable ``~user`` reaching two more path expanders that
  answer to a different caller: ``[defaults] cwd`` on the local transport
  (endpoint open) and a caller-named path in the local fs backend. Each must
  name the profile or the path in a structured error instead of leaking
  ``RuntimeError: Could not determine home directory.``
* The secret-file reader used while building a transport must answer "no
  secret" for a path it cannot expand, per its own never-raise contract.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_remote_control.config import AuthConfig, Profile, ProfileInvalid, load_profile
from mcp_remote_control.config.load import auth_path_escapes_home
from mcp_remote_control.config.models import DefaultsConfig
from mcp_remote_control.config.paths import profiles_dir
from mcp_remote_control.config.store import ensure_home_layout
from mcp_remote_control.core import config_ops, endpoint_ops
from mcp_remote_control.endpoint import connect
from mcp_remote_control.endpoint.registry import EndpointRegistry
from mcp_remote_control.fs.backends.local import LocalFs
from mcp_remote_control.fs.types import FsError
from mcp_remote_control.transport import TransportError, WinRMTransport

_SRC = Path(__file__).resolve().parents[2] / "src" / "mcp_remote_control"

# The user in these values must not exist on the test host.
_UNRESOLVABLE_TILDE = "~deploy/.ssh/id_rsa"


def _tilde_user_unresolvable() -> bool:
    """True when this host cannot expand ``~deploy`` (the defect's precondition)."""
    try:
        Path(_UNRESOLVABLE_TILDE).expanduser()
    except RuntimeError:
        return True
    return False


requires_missing_user = pytest.mark.skipif(
    not _tilde_user_unresolvable(),
    reason="host has a 'deploy' user, so ~deploy is not an unresolvable tilde",
)


def _write_profile(home: Path, name: str, body: str) -> None:
    (profiles_dir(home) / f"{name}.toml").write_text(body, encoding="utf-8")


def _auth_profile(
    home: Path, name: str, transport: str, method: str, field: str, value: str
) -> None:
    _write_profile(
        home,
        name,
        f'name = "{name}"\ntransport = "{transport}"\nhost = "h"\n'
        f'username = "u"\n[auth]\nmethod = "{method}"\n{field} = "{value}"\n',
    )


def _winrm_profile(home: Path, name: str, winrm_body: str) -> None:
    _write_profile(
        home,
        name,
        f'name = "{name}"\ntransport = "winrm"\nhost = "h"\nusername = "u"\n'
        f"{winrm_body}",
    )


# ---------------------------------------------------------------------------
# an unresolvable ~user is not a home escape
# ---------------------------------------------------------------------------


@requires_missing_user
def test_unresolvable_tilde_user_is_not_a_home_escape() -> None:
    """A ``~user`` this host cannot expand names no location, so it is no traversal."""
    assert auth_path_escapes_home(_UNRESOLVABLE_TILDE) is False
    assert auth_path_escapes_home("~root/id_rsa") is False
    assert auth_path_escapes_home("secrets/id_rsa") is False
    assert auth_path_escapes_home("/abs/~deploy/id_rsa") is False


@requires_missing_user
def test_traversal_behind_an_unresolvable_tilde_is_still_a_traversal() -> None:
    """Falling back to the literal form must not lose the ``..`` check."""
    assert auth_path_escapes_home("~deploy/../escape") is True
    assert auth_path_escapes_home("~deploy/..") is True
    assert auth_path_escapes_home("../escape") is True


@requires_missing_user
@pytest.mark.parametrize(
    ("transport", "method", "field"),
    [
        ("ssh", "private_key_path", "key_path"),
        ("ssh", "private_key_path", "passphrase_path"),
        ("winrm", "certificate", "cert_path"),
        ("winrm", "certificate", "cert_key_path"),
        ("winrm", "certificate", "cert_key_password_path"),
    ],
)
def test_every_auth_path_field_rejects_an_unresolvable_tilde_user(
    tmp_path: Path, transport: str, method: str, field: str
) -> None:
    """Every ``_optional_secret_path`` caller surfaces PROFILE_INVALID, not RuntimeError."""
    ensure_home_layout(tmp_path)
    _auth_profile(tmp_path, "p", transport, method, field, _UNRESOLVABLE_TILDE)

    with pytest.raises(ProfileInvalid) as excinfo:
        load_profile(tmp_path, "p")

    msg = str(excinfo.value)
    assert field in msg
    assert "cannot be resolved" in msg
    # The value is not a traversal, so it must not be reported as one.
    assert "must not contain '..'" not in msg

    res = config_ops.run("get_profile", name="p", home=str(tmp_path))
    assert res.status == "error"
    assert res.code == "PROFILE_INVALID"


@requires_missing_user
def test_put_profile_stores_an_unresolvable_tilde_user_relative_to_home(
    tmp_path: Path,
) -> None:
    """``put_profile`` must not report a disk problem for a value it can store.

    An unexpandable ``~user`` is not caller-rooted, so the documented
    non-rooted rewrite applies and the value lands under ``secrets/``.
    """
    ensure_home_layout(tmp_path)
    res = config_ops.run(
        "put_profile",
        name="p",
        transport="ssh",
        host="h",
        username="u",
        auth={"method": "private_key_path", "key_path": _UNRESOLVABLE_TILDE},
        home=str(tmp_path),
    )

    assert res.code != "CONFIG_WRITE_FAILED", res.fields
    assert "RuntimeError" not in str(res.fields.get("msg", ""))
    assert res.status == "ok", res.fields

    stored = load_profile(tmp_path, "p")
    assert stored.auth is not None
    assert stored.auth.key_path is not None
    assert stored.auth.key_path.parts[-4:] == ("secrets", "~deploy", ".ssh", "id_rsa")


# ---------------------------------------------------------------------------
# non-finite / bool WinRM tuning knobs
# ---------------------------------------------------------------------------

# ``inf`` and ``1e400`` both parse from TOML to float infinity; ``nan`` and
# ``true`` reach the coercers as a float NaN and as a bool.
_JUNK_LITERALS = ("inf", "1e400", "nan", "-inf", "true")

_INT_KEYS = ("operation_timeout_s", "read_timeout_s")
_OPTIONAL_KEYS = (
    "reconnection_retries",
    "reconnection_backoff",
    "probe_timeout_s",
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, None),
        (False, None),
        (7, 7),
        ("7", 7),
        (7.9, 7),
        (float("inf"), None),
        (float("-inf"), None),
        (float("nan"), None),
        (1e400, None),
        ("inf", None),
        ("not-a-number", None),
        ([], None),
    ],
)
def test_optional_int_degrades_values_it_cannot_use(value: object, expected: int | None) -> None:
    assert connect._optional_int(value) == expected


@pytest.mark.parametrize("literal", _JUNK_LITERALS)
@pytest.mark.parametrize("key", _INT_KEYS + _OPTIONAL_KEYS + ("connect_timeout_ms",))
def test_junk_winrm_knobs_leave_the_transport_default(
    tmp_path: Path, key: str, literal: str
) -> None:
    """A junk tuning knob must not make the endpoint unopenable, nor mean 1."""
    ensure_home_layout(tmp_path)
    _winrm_profile(tmp_path, "p", f"[winrm]\n{key} = {literal}\n")

    transport = connect._build_winrm_transport(load_profile(tmp_path, "p"), connector=None)

    if key == "connect_timeout_ms":
        # Unset/junk keeps the 15s default (0 would be a real, unusable budget).
        assert transport.connect_timeout_ms == 15000
    else:
        assert getattr(transport, key) is None


def test_usable_winrm_knobs_are_still_forwarded(tmp_path: Path) -> None:
    """The junk guard must not swallow valid values: only junk degrades to unset."""
    ensure_home_layout(tmp_path)
    _winrm_profile(
        tmp_path,
        "p",
        "[winrm]\nconnect_timeout_ms = 5000\noperation_timeout_s = 30\n"
        "read_timeout_s = 45\nreconnection_retries = 2\n"
        "reconnection_backoff = 1.5\nprobe_timeout_s = 7\n",
    )

    transport = connect._build_winrm_transport(
        load_profile(tmp_path, "p"), connector=None
    )

    assert transport.connect_timeout_ms == 5000
    assert transport.operation_timeout_s == 30
    assert transport.read_timeout_s == 45
    assert transport.reconnection_retries == 2
    assert transport.reconnection_backoff == 1.5
    assert transport.probe_timeout_s == 7


def test_junk_credssp_minimum_version_leaves_it_unset(tmp_path: Path) -> None:
    """``[winrm.credssp] minimum_version`` shares ``_optional_int``."""
    ensure_home_layout(tmp_path)
    _winrm_profile(
        tmp_path,
        "p",
        "[winrm]\nconnect_timeout_ms = 1000\n[winrm.credssp]\nminimum_version = inf\n",
    )

    transport = connect._build_winrm_transport(load_profile(tmp_path, "p"), connector=None)

    assert transport.connect_timeout_ms == 1000
    assert transport.credssp_minimum_version is None


def test_endpoint_open_tolerates_junk_winrm_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public open path must not report a network failure for a profile typo."""
    ensure_home_layout(tmp_path)
    _winrm_profile(
        tmp_path,
        "p",
        "[winrm]\noperation_timeout_s = inf\nconnect_timeout_ms = 1e400\n"
        "reconnection_retries = true\n",
    )
    # A fresh registry keeps the global one free of this profile.
    monkeypatch.setattr(endpoint_ops, "get_registry", EndpointRegistry)

    res = endpoint_ops.open_endpoint(profile="p", probe=False, home=str(tmp_path))

    assert res.status == "ok", (res.status, res.code, res.fields)
    assert res.code != "CONNECT_FAILED"


# ---------------------------------------------------------------------------
# the same ~user reaching the endpoint-cwd and local-fs path expanders
# ---------------------------------------------------------------------------


@requires_missing_user
def test_local_endpoint_cwd_with_unresolvable_tilde_names_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local profile whose ``defaults.cwd`` names a missing user is a profile defect."""
    ensure_home_layout(tmp_path)
    _write_profile(
        tmp_path,
        "p",
        'name = "p"\ntransport = "local"\nhost = "localhost"\n'
        '[defaults]\ncwd = "~deploy/workdir"\n',
    )
    monkeypatch.setattr(endpoint_ops, "get_registry", EndpointRegistry)

    res = endpoint_ops.open_endpoint(profile="p", probe=False, home=str(tmp_path))

    assert res.status == "error", res.fields
    assert res.code == "INVALID_CWD", (res.status, res.code, res.fields)
    assert "RuntimeError" not in str(res.fields.get("msg", ""))


@requires_missing_user
def test_seed_cwd_remote_tilde_user_still_passes_through() -> None:
    """Only the local branch expands; a remote shell resolves its own ``~user``."""
    stub = SimpleNamespace(cwd=None)
    profiles = [
        Profile(name="s", transport="ssh", host="h", username="u",
                defaults={"cwd": "~deploy/workdir"}),
        Profile(name="w", transport="winrm", host="h", username="u",
                defaults={"cwd": "~deploy/workdir"}),
    ]
    for profile in profiles:
        assert connect._seed_cwd(profile, stub) == "~deploy/workdir"

    local = Profile(name="l", transport="local", defaults={"cwd": "~deploy/workdir"})
    with pytest.raises(TransportError) as excinfo:
        connect._seed_cwd(local, stub)
    assert excinfo.value.code == "INVALID_CWD"
    assert "RuntimeError" not in excinfo.value.msg


@requires_missing_user
def test_local_fs_path_with_unresolvable_tilde_is_a_path_diagnosis(tmp_path: Path) -> None:
    """A caller-named fs path is rejected as INVALID_ARG, not leaked interpreter text."""
    fs = LocalFs(cwd=str(tmp_path))

    for call in (lambda: fs.list(_UNRESOLVABLE_TILDE), lambda: fs.stat(_UNRESOLVABLE_TILDE)):
        with pytest.raises(FsError) as excinfo:
            call()
        assert excinfo.value.code == "INVALID_ARG", excinfo.value
        assert "RuntimeError" not in excinfo.value.msg
        assert _UNRESOLVABLE_TILDE in excinfo.value.msg


def test_local_fs_ordinary_paths_still_resolve(tmp_path: Path) -> None:
    """The expansion guard must not disturb ~, relative or absolute paths."""
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    fs = LocalFs(cwd=str(tmp_path))

    assert fs.read("f.txt").data == b"x"
    assert fs.read(str(tmp_path / "f.txt")).data == b"x"
    assert fs.stat("~/").path == str(Path("~").expanduser())


# ---------------------------------------------------------------------------
# connecting with a secret path that cannot be expanded
# ---------------------------------------------------------------------------


@requires_missing_user
def test_secret_reader_defers_an_unexpandable_path_to_connect() -> None:
    """A path this host cannot expand is "no secret", not an escaping error.

    Profile loads reject such an auth path, but the reader's contract is that
    it never raises, so a directly constructed profile must reach the
    connect-time auth failure instead of a ``RuntimeError``.
    """
    assert connect._read_secret_file_first_line(_UNRESOLVABLE_TILDE) is None


@requires_missing_user
def test_winrm_build_defers_an_unexpandable_secret_path() -> None:
    """``_build_winrm_transport`` must not raise on an unexpandable secret path."""
    profile = Profile(
        name="p",
        transport="winrm",
        host="h",
        username="u",
        auth=AuthConfig(method="password", password_path=Path(_UNRESOLVABLE_TILDE)),
    )

    transport = connect._build_winrm_transport(profile, connector=None)

    assert isinstance(transport, WinRMTransport)
    assert connect._resolve_password(profile) is None


def test_secret_reader_still_reads_an_existing_file(tmp_path: Path) -> None:
    """The tolerance must not swallow a secret that is really there."""
    secret = tmp_path / "pw"
    secret.write_text("s3cret\nsecond line\n", encoding="utf-8")

    assert connect._read_secret_file_first_line(secret) == "s3cret"
    assert connect._read_secret_file_first_line(str(secret)) == "s3cret"
    assert connect._read_secret_file_first_line(tmp_path / "absent") is None


# ---------------------------------------------------------------------------
# [defaults] verbosity: advertised as consumed, acted on by nothing
# ---------------------------------------------------------------------------
# A read of the global ``[defaults]`` table's ``verbosity`` key. The
# declaration and the parser name the key without reading it, so those files
# are excluded; template text (the generated config.toml) has no ``.defaults``
# receiver and does not match.
_DEFAULTS_VERBOSITY_READ = re.compile(
    r"""\.defaults\b[^\n]*(?:\.verbosity\b|["']verbosity["'])"""
)
_DECLARATION_ONLY = frozenset({"config/models.py", "config/load.py"})


def _defaults_verbosity_readers() -> list[str]:
    """Source lines that read ``cfg.defaults.verbosity`` outside its declaration."""
    hits: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel in _DECLARATION_ONLY:
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if _DEFAULTS_VERBOSITY_READ.search(line):
                hits.append(f"{rel}:{lineno}: {line.strip()}")
    return hits


def test_defaults_docstring_does_not_advertise_verbosity_as_consumed() -> None:
    doc = " ".join((DefaultsConfig.__doc__ or "").split())
    consumed = doc.split("Consumed today:", 1)[1].split(".", 1)[0]

    assert "max_body_chars" in consumed
    assert "winrm_probe" in consumed
    assert "verbosity" not in consumed
    # Still disclosed, and disclosed as inert: setting it must not be
    # mistaken for a way to change output.
    assert "verbosity" in doc
    assert "no effect" in doc


def test_defaults_verbosity_is_only_echoed_by_config_get() -> None:
    """``config op=get`` reports the parsed value; nothing acts on it."""
    hits = _defaults_verbosity_readers()

    assert len(hits) == 1, hits
    assert hits[0].startswith("core/config_ops.py:")
    assert "verbosity={" in hits[0]

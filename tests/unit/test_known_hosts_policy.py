"""SSH host-key policy: profile value -> connect kwargs, and what asyncssh does.

asyncssh overloads ``known_hosts``: an *explicit* ``None`` disables host key
validation, while a specified-but-empty value (``()``, ``""``) means "no
configured source" and makes asyncssh fall back to ``~/.ssh/known_hosts`` and
verify against that. Mapping ``known_hosts = "none"`` onto ``()`` therefore did
the opposite of what it says: the documented lab escape hatch silently enforced
verification against the operator's real known_hosts file, and an unknown or
changed host key blocked the connection.

The last two sections drive real paths rather than a hand-composed pair of
stages: the registry wiring is exercised end to end (its ``_build_transport``
is the only caller of ``_ssh_known_hosts``), and the ``None`` case is checked
against a real asyncssh server so the contract is pinned against the library
rather than against our reading of it.

``known_hosts`` is not the only ``[ssh]`` key ``_build_transport`` reads: the
connect budget and the keepalive interval sit in the same block and are pinned
by the same tests, because a refactor that drops the table is a single edit
away from any of the three.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

from mcp_remote_control.config import Profile, load_profile
from mcp_remote_control.config.store import put_profile
from mcp_remote_control.endpoint.connect import _ssh_known_hosts
from mcp_remote_control.endpoint.registry import EndpointRegistry
from mcp_remote_control.transport.base import TransportError
from mcp_remote_control.transport.ssh import KNOWN_HOSTS_UNSET, SSHTransport


# ---------------------------------------------------------------------------
# profile value -> transport value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table",
    [{}, {"known_hosts": None}, {"known_hosts": ""}, {"known_hosts": "   "}, {"known_hosts": True}],
)
def test_unset_values_ask_for_asyncssh_default(table: dict) -> None:
    assert _ssh_known_hosts(table) is KNOWN_HOSTS_UNSET


@pytest.mark.parametrize(
    "value",
    ["none", "NONE", "off", "false", "0", "disable", "disabled", False],
)
def test_skip_tokens_disable_validation(value: object) -> None:
    # None (not () / "") is the only value asyncssh reads as "disabled".
    assert _ssh_known_hosts({"known_hosts": value}) is None


def test_explicit_path_is_kept() -> None:
    assert _ssh_known_hosts({"known_hosts": "/etc/ssh/known_hosts"}) == "/etc/ssh/known_hosts"


# ---------------------------------------------------------------------------
# transport -> connector kwargs
# ---------------------------------------------------------------------------


def _connect_kwargs(known_hosts: object) -> dict:
    seen: dict = {}

    def connector(**kwargs: object) -> object:
        seen.update(kwargs)

        class Conn:
            pass

        return Conn()

    t = SSHTransport(
        host="h", username="u", known_hosts=known_hosts, connector=connector
    )
    t.connect()
    t.close()
    return seen


def test_unset_omits_the_kwarg_so_asyncssh_uses_its_default() -> None:
    # Omitting the argument is what asks for ~/.ssh/known_hosts; forwarding the
    # sentinel (or None) would instead disable validation.
    assert "known_hosts" not in _connect_kwargs(KNOWN_HOSTS_UNSET)


def test_disabled_is_forwarded_as_an_explicit_none() -> None:
    kwargs = _connect_kwargs(None)
    assert "known_hosts" in kwargs and kwargs["known_hosts"] is None


def test_path_is_forwarded_verbatim() -> None:
    assert _connect_kwargs("/tmp/kh")["known_hosts"] == "/tmp/kh"


# ---------------------------------------------------------------------------
# profile table -> registry -> connector kwargs (the real wiring)
# ---------------------------------------------------------------------------


def _connector_kwargs_from_profile(
    ssh: dict | None, defaults: dict | None = None
) -> tuple[SSHTransport, dict]:
    """Build a transport the way the registry does, and record what the
    connector was handed.

    The sections above assert ``_ssh_known_hosts`` and ``SSHTransport``
    separately, so neither can see a regression in the wiring between them.
    ``EndpointRegistry._build_transport`` is the only caller of
    ``_ssh_known_hosts``, and a refactor that forwarded the raw profile value
    would hand asyncssh the string ``"none"`` - which it reads as a *path* -
    silently enforcing verification against the operator's known_hosts file
    with every other test still green. The connector kwargs are the last
    observable hop before asyncssh sees them.
    """
    seen: dict = {}

    def connector(**kwargs: object) -> object:
        seen.update(kwargs)

        class Conn:
            pass

        return Conn()

    profile = Profile(
        name="kh-lab",
        transport="ssh",
        host="h",
        username="u",
        ssh=dict(ssh or {}),
        defaults=dict(defaults or {}),
    )
    transport = EndpointRegistry()._build_transport(profile, connector=connector)
    assert isinstance(transport, SSHTransport)
    try:
        transport.connect()
    finally:
        transport.close()
    return transport, seen


@pytest.mark.parametrize("value", ["none", "NONE", "off", "false", "0", False])
def test_registry_maps_a_skip_token_onto_disabled_validation(value: object) -> None:
    transport, seen = _connector_kwargs_from_profile({"known_hosts": value})
    assert transport.known_hosts is None
    # An explicit None (not the raw string, and not ()) is the only value
    # asyncssh reads as "do not verify".
    assert seen.get("known_hosts", "absent") is None


def test_registry_keeps_an_explicit_path() -> None:
    transport, seen = _connector_kwargs_from_profile(
        {"known_hosts": "/etc/ssh/known_hosts"}
    )
    assert transport.known_hosts == "/etc/ssh/known_hosts"
    assert seen["known_hosts"] == "/etc/ssh/known_hosts"


@pytest.mark.parametrize("ssh", [None, {}, {"encoding": "utf-8"}])
def test_registry_omits_the_kwarg_for_a_silent_profile(ssh: dict | None) -> None:
    transport, seen = _connector_kwargs_from_profile(ssh)
    assert transport.known_hosts is KNOWN_HOSTS_UNSET
    assert "known_hosts" not in seen


def test_registry_ignores_known_hosts_in_the_defaults_table() -> None:
    """``known_hosts`` is an ``[ssh]`` key; ``[defaults]`` must not disable
    validation behind the operator's back.

    Other transport settings (``encoding``, ``force_utf8_remote``) do fall
    back to ``[defaults]`` by name. Host-key policy is deliberately not one
    of them: silently inheriting "disabled" would remove a check the profile
    never asked to remove.
    """
    transport, seen = _connector_kwargs_from_profile(
        {}, defaults={"known_hosts": "none"}
    )
    assert transport.known_hosts is KNOWN_HOSTS_UNSET
    assert "known_hosts" not in seen


# ---------------------------------------------------------------------------
# the same wiring for the tuning keys that share the [ssh] block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ssh", "expected_ms"),
    [
        ({"connect_timeout_ms": 2000}, 2000),
        # The config layer accepts a string; the registry coerces it rather
        # than forwarding it to asyncssh, which wants seconds as a float.
        ({"connect_timeout_ms": "2500"}, 2500),
        # Absent, explicitly unset and unparsable all land on the transport's
        # own default -- never on 0, which ssh.py reads as "clamp to 1 ms".
        ({}, 15000),
        ({"connect_timeout_ms": None}, 15000),
        ({"connect_timeout_ms": "soon"}, 15000),
    ],
)
def test_registry_maps_connect_timeout_ms(ssh: dict, expected_ms: int) -> None:
    transport, seen = _connector_kwargs_from_profile(ssh)
    assert transport.connect_timeout_ms == expected_ms
    # The connector kwargs are the last hop before asyncssh sees the budget.
    assert seen["connect_timeout"] == max(expected_ms, 1) / 1000.0


@pytest.mark.parametrize(
    ("ssh", "expected_s"),
    [
        ({"keepalive_interval_s": 30}, 30.0),
        ({"keepalive_interval_s": "45"}, 45.0),
        # 0 is a defined interval, not "unset": it must survive as 0.0.
        ({"keepalive_interval_s": 0}, 0.0),
        ({}, None),
        ({"keepalive_interval_s": None}, None),
        ({"keepalive_interval_s": "often"}, None),
    ],
)
def test_registry_maps_keepalive_interval_s(
    ssh: dict, expected_s: float | None
) -> None:
    transport, seen = _connector_kwargs_from_profile(ssh)
    assert transport.keepalive_interval_s == expected_s
    # None means "asyncssh's own default", so it must reach the connector as
    # None rather than being dropped from the mapping.
    assert seen["keepalive_interval"] == expected_s


def test_a_profile_on_disk_reaches_the_transport_intact(tmp_path: Path) -> None:
    """Drive TOML -> store -> load_profile -> registry -> transport.

    The parametrized tests above hand ``_build_transport`` a dict; this one
    writes a real profile file, so a value lost in config parsing (rather than
    in the registry) is caught too. The connect budget is deliberately not the
    transport default, which a dropped wiring would silently reinstate.
    """
    put_profile(
        tmp_path,
        name="tuned-lab",
        transport="ssh",
        host="h",
        port=2222,
        username="u",
        auth={"method": "password", "password": "x"},
        ssh={
            "connect_timeout_ms": 2000,
            "keepalive_interval_s": 30,
            "encoding": "gb18030",
        },
    )
    profile = load_profile(tmp_path, "tuned-lab")

    transport = EndpointRegistry()._build_transport(
        profile, connector=lambda **_k: object()
    )
    assert isinstance(transport, SSHTransport)
    assert transport.connect_timeout_ms == 2000
    assert transport.keepalive_interval_s == 30.0
    assert transport.text_encoding == "gb18030"
    assert transport.port == 2222


@pytest.mark.parametrize(
    ("ssh", "defaults", "expected"),
    [
        ({"encoding": "gb18030"}, None, "gb18030"),
        # Surrounding whitespace is stripped before the name reaches the codec.
        ({"encoding": "  latin-1  "}, None, "latin-1"),
        # [ssh] wins over [defaults]; an empty [ssh] value is "unset".
        ({"encoding": "utf-8"}, {"encoding": "utf-16"}, "utf-8"),
        ({"encoding": ""}, {"encoding": "utf-16"}, "utf-16"),
        (None, {"encoding": "utf-16"}, "utf-16"),
        (None, None, None),
    ],
)
def test_registry_maps_text_encoding(
    ssh: dict | None, defaults: dict | None, expected: str | None
) -> None:
    """The third ``[ssh]`` key read in the same block as ``known_hosts``.

    Only the transport stage is pinned elsewhere (``SSHTransport(
    text_encoding=...)`` in tests/unit/test_ssh_auth_kwargs.py); without this,
    a refactor that dropped the ``encoding`` lookup would leave every
    non-UTF-8 remote decoding with the wrong codec and no test would notice.
    """
    transport, _seen = _connector_kwargs_from_profile(ssh, defaults)
    assert transport.text_encoding == expected


# ---------------------------------------------------------------------------
# the library contract, driven for real
# ---------------------------------------------------------------------------


def _start_server(ready: threading.Event, port_box: list[int]) -> threading.Thread:
    asyncssh = pytest.importorskip("asyncssh")

    class _Server(asyncssh.SSHServer):  # type: ignore[misc, valid-type]
        def begin_auth(self, username: str) -> bool:
            return False

    def _run() -> None:
        async def _serve() -> None:
            host_key = asyncssh.generate_private_key("ssh-ed25519")
            server = await asyncssh.create_server(
                _Server, "127.0.0.1", 0, server_host_keys=[host_key]
            )
            port_box.append(server.get_addresses()[0][1])
            ready.set()
            await asyncio.Event().wait()
            server.close()

        asyncio.run(_serve())

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


def test_none_connects_where_the_default_would_be_blocked() -> None:
    """The escape hatch must actually escape, against a real server.

    The server runs on its own thread/loop: transport.connect() is synchronous
    and blocks its calling thread while it drives the bridge loop, so a server
    sharing that loop could never answer the handshake.
    """
    if sys.platform == "win32":  # pragma: no cover - lab/CI hosts are POSIX
        pytest.skip("in-process asyncssh server test is POSIX-only here")

    ready: threading.Event = threading.Event()
    port_box: list[int] = []
    _start_server(ready, port_box)
    if not ready.wait(timeout=15):  # pragma: no cover - environment failure
        pytest.skip("could not start a local asyncssh server")
    port = port_box[0]

    def connect(known_hosts: object) -> str:
        t = SSHTransport(
            host="127.0.0.1",
            port=port,
            username="probe",
            known_hosts=known_hosts,
            connect_timeout_ms=5000,
        )
        try:
            t.connect()
        except TransportError as exc:
            # Name the failure precisely: "it raised something" would also be
            # satisfied by an unreachable server.
            return f"blocked:{exc.code}"
        except Exception as exc:  # noqa: BLE001
            return f"error:{type(exc).__name__}"
        finally:
            try:
                t.close()
            except Exception:  # noqa: BLE001
                pass
        return "connected"

    assert connect(None) == "connected", "known_hosts=None must disable validation"

    # An unknown host must still be rejected under asyncssh's own default -
    # this is what makes the "none" case meaningful rather than universal.
    blocked = connect(KNOWN_HOSTS_UNSET)
    assert blocked == "blocked:HOSTKEY_MISMATCH", (
        "an unknown host under the default policy should fail host key "
        f"verification; got {blocked!r}"
    )

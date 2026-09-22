"""SSH identity fallback chain."""

from __future__ import annotations

import asyncio
import errno
import shutil
import socket
import subprocess
from pathlib import Path

import asyncssh
import pytest

from mcp_remote_control.config.models import AuthConfig, Profile
from mcp_remote_control.identity.ssh_keys import (
    DEFAULT_SSH_IDENTITY_BASENAMES,
    _key_import_probe,
    _key_import_verdict,
    _key_is_chainable,
    _key_is_importable,
    _key_load_failure_sentence,
    key_load_failure_message,
    resolve_ssh_key_paths,
)
from mcp_remote_control.transport.async_bridge import AsyncLoopBridge
from mcp_remote_control.transport.base import TransportError
from mcp_remote_control.transport.ssh import (
    SSHTransport,
    _is_key_load_error,
    _map_ssh_connect_error,
)

_SSH_KEYGEN = shutil.which("ssh-keygen")
_OPENSSL = shutil.which("openssl")

# Distinctive so a leak check cannot pass by accident.
_KEY_PASSPHRASE = "key-passphrase-do-not-log"
_WRONG_PASSPHRASE = "wrong-passphrase-do-not-log"


def _bcrypt_available() -> bool:
    """True when asyncssh can decrypt OpenSSH-format keys (optional extra)."""
    try:
        import bcrypt
    except ImportError:
        return False
    return hasattr(bcrypt, "kdf")


_BCRYPT_AVAILABLE = _bcrypt_available()


def _write_key(path: Path) -> None:
    """Write a real, importable private key at *path*."""
    asyncssh.generate_private_key("ssh-ed25519").write_private_key(str(path))


def _write_encrypted_key(path: Path, passphrase: str) -> None:
    """Write a PKCS#8-encrypted private key at *path*.

    PKCS#8 decryption is PBES-based and needs no optional dependency, so this
    key stays encrypted regardless of whether bcrypt is installed.
    """
    asyncssh.generate_private_key("ssh-ed25519").write_private_key(
        str(path), "pkcs8-pem", passphrase=passphrase
    )


def _write_scrypt_pkcs8_key(path: Path, passphrase: str) -> None:
    """Write an openssl scrypt-KDF PKCS#8 key: a scheme asyncssh cannot read.

    scrypt is a PBES2 key-derivation function OpenSSL offers and asyncssh does
    not implement, so the file is encrypted under a passphrase that provably
    decrypts it (openssl does) while the SSH stack refuses it outright - and
    reports that refusal with the same generic message a wrong passphrase gets.
    """
    plain = path.with_name(path.name + ".plain")
    subprocess.run(
        [_OPENSSL, "genpkey", "-algorithm", "ed25519", "-out", str(plain)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            _OPENSSL,
            "pkcs8",
            "-topk8",
            "-in",
            str(plain),
            "-scrypt",
            "-out",
            str(path),
            "-passout",
            f"pass:{passphrase}",
        ],
        check=True,
        capture_output=True,
    )


def _connect_error(
    keys: list[Path], *, passphrase: str | None = None, connector: object = None
) -> TransportError:
    """Connect *keys* to a closed local port; return the error it raises.

    A closed port tells the two failure stages apart: a connect that gets past
    key loading fails on the network, one that cannot load a key never dials.
    """
    bridge = AsyncLoopBridge()
    bridge.start()
    transport = SSHTransport(
        host="127.0.0.1",
        port=_closed_port(),
        username="x",
        client_keys=keys,
        password="pw",
        passphrase=passphrase,
        known_hosts=None,
        connect_timeout_ms=2000,
        bridge=bridge,
        connector=connector,  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(TransportError) as excinfo:
            transport.connect()
        return excinfo.value
    finally:
        transport.close()
        bridge.stop()


def _closed_port() -> int:
    """A local port nothing listens on (bind then release)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _profile(*, passphrase_path: Path | None = None) -> Profile:
    auth = AuthConfig(method="password", password="pw", passphrase_path=passphrase_path)
    return Profile(name="x", transport="ssh", host="127.0.0.1", username="u", auth=auth)


def test_default_order_id_rsa_before_ed25519() -> None:
    names = list(DEFAULT_SSH_IDENTITY_BASENAMES)
    assert names.index("id_rsa") < names.index("id_ed25519")
    assert names.index("id_ecdsa") < names.index("id_ed25519")


def test_explicit_key_path_only(tmp_path: Path) -> None:
    key = tmp_path / "only_this"
    _write_key(key)
    # Also plant default ids that must be ignored.
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    _write_key(ssh_dir / "id_rsa")
    _write_key(ssh_dir / "id_ed25519")

    profile = Profile(
        name="x",
        transport="ssh",
        host="h",
        username="u",
        auth=AuthConfig(method="private_key_path", key_path=key),
    )
    paths = resolve_ssh_key_paths(profile, ssh_dir=ssh_dir)
    assert paths == [key]


def test_fallback_only_existing_in_order(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    # Create ed25519 and ecdsa only - rsa missing -> not listed.
    ed = ssh_dir / "id_ed25519"
    ec = ssh_dir / "id_ecdsa"
    _write_key(ed)
    _write_key(ec)

    paths = resolve_ssh_key_paths(_profile(), ssh_dir=ssh_dir)
    assert paths == [ec, ed]


def test_fallback_empty_when_none_exist(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    assert resolve_ssh_key_paths(_profile(), ssh_dir=ssh_dir) == []


def test_only_existing_false_returns_full_chain(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    paths = resolve_ssh_key_paths(_profile(), ssh_dir=ssh_dir, only_existing=False)
    assert [p.name for p in paths] == list(DEFAULT_SSH_IDENTITY_BASENAMES)


def test_key_is_importable_rejects_unusable_but_keeps_unreadable(tmp_path: Path) -> None:
    good = tmp_path / "good"
    _write_key(good)
    # A public key saved under a private-key name: exists, cannot be imported.
    public = tmp_path / "public"
    public.write_text(
        asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode(),
        encoding="utf-8",
    )
    missing = tmp_path / "missing"

    assert _key_is_importable(good) is True
    assert _key_is_importable(public) is False
    # Unreadable / absent stays "usable" so the connect reports the real error.
    assert _key_is_importable(missing) is True


def test_unimportable_default_keys_are_skipped_in_order(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    good_rsa = ssh_dir / "id_rsa"
    _write_key(good_rsa)
    # Malformed file under a later name must not abort the chain.
    (ssh_dir / "id_ed25519").write_text("not a private key\n", encoding="utf-8")
    good_ecdsa = ssh_dir / "id_ecdsa"
    _write_key(good_ecdsa)

    paths = resolve_ssh_key_paths(_profile(), ssh_dir=ssh_dir)
    assert paths == [good_rsa, good_ecdsa]


def test_encrypted_default_key_is_skipped_without_passphrase_source(
    tmp_path: Path,
) -> None:
    if _SSH_KEYGEN is None:
        pytest.skip("ssh-keygen not available to build an encrypted key")
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    encrypted = ssh_dir / "id_ed25519"
    subprocess.run(
        [_SSH_KEYGEN, "-t", "ed25519", "-N", "hunter2", "-f", str(encrypted), "-q"],
        check=True,
    )
    good = ssh_dir / "id_ecdsa"
    _write_key(good)

    # asyncssh cannot decrypt it here, and an explicit client_keys entry would
    # abort the whole connect; the chain must drop it instead.
    assert resolve_ssh_key_paths(_profile(), ssh_dir=ssh_dir) == [good]


def test_passphrase_source_keeps_encrypted_keys_but_drops_unparseable(
    tmp_path: Path,
) -> None:
    if _SSH_KEYGEN is None:
        pytest.skip("ssh-keygen not available to build an encrypted key")
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    encrypted = ssh_dir / "id_rsa"
    subprocess.run(
        [_SSH_KEYGEN, "-t", "ed25519", "-N", "hunter2", "-f", str(encrypted), "-q"],
        check=True,
    )
    good = ssh_dir / "id_ed25519"
    _write_key(good)
    # A parse failure is not a passphrase problem: asyncssh raises on it rather
    # than skipping, so a passphrase source must not readmit it to the chain.
    (ssh_dir / "id_ecdsa").write_text("not a private key\n", encoding="utf-8")

    paths = resolve_ssh_key_paths(
        _profile(passphrase_path=tmp_path / "passphrase"), ssh_dir=ssh_dir
    )
    assert paths == [encrypted, good]


def test_passphrase_profile_with_a_bad_default_key_reaches_the_dial(
    tmp_path: Path,
) -> None:
    """One unparseable default key must not abort a passphrase connect.

    ``client_keys`` is an explicit list, so a key asyncssh cannot parse aborts
    the connect with ``KeyImportError`` before any dial - losing the supplied
    password and the operator's own passphrase. The chain has to carry only
    keys that can still be judged, so the failure is the network's.
    """
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    _write_key(ssh_dir / "id_rsa")
    (ssh_dir / "id_ed25519").write_text("not a private key\n", encoding="utf-8")

    paths = resolve_ssh_key_paths(
        _profile(passphrase_path=tmp_path / "passphrase"), ssh_dir=ssh_dir
    )
    assert [p.name for p in paths] == ["id_rsa"]

    keys = [str(p) for p in paths]
    with pytest.raises(OSError):

        async def _connect() -> None:
            await asyncssh.connect(
                "127.0.0.1",
                port=_closed_port(),
                username="x",
                client_keys=keys or None,
                passphrase="hunter2",
                password="pw",
                known_hosts=None,
                connect_timeout=2,
            )

        asyncio.run(_connect())


def test_explicit_unimportable_key_is_returned_verbatim(tmp_path: Path) -> None:
    broken = tmp_path / "named_by_operator"
    broken.write_text("not a private key\n", encoding="utf-8")
    profile = Profile(
        name="x",
        transport="ssh",
        host="h",
        username="u",
        auth=AuthConfig(method="private_key_path", key_path=broken),
    )
    # An explicit choice is surfaced to the connect, not silently dropped.
    assert resolve_ssh_key_paths(profile, ssh_dir=tmp_path) == [broken]


def test_default_chain_never_aborts_a_password_connect(tmp_path: Path) -> None:
    """Unusable default ids must not win over a password or ssh-agent.

    An explicit ``client_keys`` entry asyncssh cannot import fails the whole
    connect before any auth exchange, so the chain has to leave such files
    out rather than hand them over and lose the supplied password.
    """
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa").write_text("not a private key\n", encoding="utf-8")
    (ssh_dir / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n", encoding="utf-8")

    keys = [str(p) for p in resolve_ssh_key_paths(_profile(), ssh_dir=ssh_dir)]
    assert keys == []

    # asyncssh loads client_keys before dialing.
    assert list(asyncssh.load_keypairs(keys)) == []

    # End to end: the connect fails on the network, never on the key loader.
    with pytest.raises(OSError):

        async def _connect() -> None:
            await asyncssh.connect(
                "127.0.0.1",
                port=_closed_port(),
                username="x",
                client_keys=keys or None,
                password="pw",
                known_hosts=None,
                connect_timeout=2,
            )

        asyncio.run(_connect())


def test_key_load_failure_sentence_names_file_reason_and_remedy() -> None:
    """Each verdict gets its own reason and remedy wording."""
    path = Path("/home/u/.ssh/id_ed25519")

    undecryptable = _key_load_failure_sentence(
        path,
        verdict="undecryptable",
        detail="OpenSSH private key encryption requires bcrypt with KDF support",
        passphrase=_KEY_PASSPHRASE,
    )
    assert str(path) in undecryptable
    assert "cannot decrypt it" in undecryptable
    assert "asyncssh[bcrypt]" in undecryptable

    no_passphrase = _key_load_failure_sentence(
        path,
        verdict="passphrase",
        detail="Passphrase must be specified to import encrypted private keys",
        passphrase=None,
    )
    assert "no passphrase was supplied" in no_passphrase
    assert "[auth].passphrase_path" in no_passphrase

    wrong = _key_load_failure_sentence(
        path,
        verdict="passphrase",
        detail="Incorrect passphrase",
        passphrase=_WRONG_PASSPHRASE,
    )
    assert "did not decrypt it" in wrong
    assert _WRONG_PASSPHRASE not in wrong

    unparseable = _key_load_failure_sentence(
        path, verdict="unparseable", detail="Invalid private key", passphrase=None
    )
    assert "cannot read it as a private key" in unparseable
    assert "Invalid private key" in unparseable


def test_key_load_failure_message_reports_only_proven_failures(
    tmp_path: Path,
) -> None:
    """Loadable chains and unjudgeable files are never relabelled."""
    good = tmp_path / "good"
    _write_key(good)
    broken = tmp_path / "broken"
    broken.write_text("not a private key\n", encoding="utf-8")
    missing = tmp_path / "missing"

    assert key_load_failure_message([good]) is None
    assert key_load_failure_message([good], passphrase=_KEY_PASSPHRASE) is None
    # A vanished file is surfaced verbatim by the connect, not guessed at here.
    assert key_load_failure_message([missing]) is None
    assert key_load_failure_message([]) is None

    sentence = key_load_failure_message([good, broken])
    assert sentence is not None
    assert str(broken) in sentence


def test_key_classification_pins_the_neighbouring_verdicts(tmp_path: Path) -> None:
    """Verdict vocabulary and chain policy for the cases around the abort.

    These outcomes are load-bearing: an unparseable entry is never chainable,
    an encrypted one is chainable only where a passphrase source can be
    applied to it, and a healthy one is always chainable.
    """
    good = tmp_path / "good"
    _write_key(good)
    encrypted = tmp_path / "encrypted"
    _write_encrypted_key(encrypted, _KEY_PASSPHRASE)
    broken = tmp_path / "broken"
    broken.write_text("not a private key\n", encoding="utf-8")
    missing = tmp_path / "missing"

    assert _key_import_verdict(good) == "usable"
    assert _key_import_verdict(encrypted) == "passphrase"
    assert _key_import_verdict(broken) == "unparseable"
    assert _key_import_verdict(missing) == "unknown"

    assert _key_is_chainable(good, passphrase_source=False) is True
    assert _key_is_chainable(good, passphrase_source=True) is True
    assert _key_is_chainable(broken, passphrase_source=False) is False
    assert _key_is_chainable(broken, passphrase_source=True) is False
    assert _key_is_chainable(encrypted, passphrase_source=False) is False
    assert _key_is_chainable(encrypted, passphrase_source=True) is True


@pytest.mark.skipif(
    _BCRYPT_AVAILABLE,
    reason="bcrypt extra installed: an OpenSSH-format key decrypts here",
)
def test_encrypted_default_key_abort_names_the_key_and_the_bcrypt_route(
    tmp_path: Path,
) -> None:
    """The key-layer abort must name the file, the reason and the remedy.

    With a passphrase source configured the chain keeps an encrypted key, and
    asyncssh refuses the connect while loading it - before the dial, so the
    supplied password is never tried. What it reports is its own wording
    alone, which names neither the file nor a way out; the connect error has
    to carry both.
    """
    if _SSH_KEYGEN is None:
        pytest.skip("ssh-keygen not available to build an OpenSSH-format key")
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    encrypted = ssh_dir / "id_ed25519"
    subprocess.run(
        [
            _SSH_KEYGEN,
            "-t",
            "ed25519",
            "-N",
            _KEY_PASSPHRASE,
            "-f",
            str(encrypted),
            "-q",
        ],
        check=True,
    )
    passphrase_file = tmp_path / "passphrase"
    passphrase_file.write_text(_KEY_PASSPHRASE + "\n", encoding="utf-8")

    profile = _profile(passphrase_path=passphrase_file)
    paths = resolve_ssh_key_paths(profile, ssh_dir=ssh_dir)
    assert paths == [encrypted]

    err = _connect_error(paths, passphrase=_KEY_PASSPHRASE)
    assert err.code == "CONNECT_FAILED"
    assert str(encrypted) in err.msg
    assert "cannot decrypt it" in err.msg
    assert "asyncssh[bcrypt]" in err.msg
    assert _KEY_PASSPHRASE not in err.msg


def test_wrong_passphrase_abort_names_the_key_and_not_the_passphrase(
    tmp_path: Path,
) -> None:
    """An encrypted key the passphrase cannot open also reports as such."""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    encrypted = ssh_dir / "id_ed25519"
    _write_encrypted_key(encrypted, _KEY_PASSPHRASE)
    passphrase_file = tmp_path / "passphrase"
    passphrase_file.write_text(_WRONG_PASSPHRASE + "\n", encoding="utf-8")

    profile = _profile(passphrase_path=passphrase_file)
    paths = resolve_ssh_key_paths(profile, ssh_dir=ssh_dir)
    assert paths == [encrypted]

    err = _connect_error(paths, passphrase=_WRONG_PASSPHRASE)
    assert err.code == "CONNECT_FAILED"
    assert str(encrypted) in err.msg
    assert "did not decrypt it" in err.msg
    assert "[auth].passphrase_path" in err.msg
    assert _KEY_PASSPHRASE not in err.msg
    assert _WRONG_PASSPHRASE not in err.msg


def test_explicit_encrypted_key_without_a_passphrase_abort_names_the_key(
    tmp_path: Path,
) -> None:
    """A named key that needs a passphrase has no fallback: still fails, named."""
    key = tmp_path / "explicit"
    _write_encrypted_key(key, _KEY_PASSPHRASE)
    profile = Profile(
        name="x",
        transport="ssh",
        host="h",
        username="u",
        auth=AuthConfig(method="private_key_path", key_path=key),
    )
    paths = resolve_ssh_key_paths(profile, ssh_dir=tmp_path)
    assert paths == [key]

    err = _connect_error(paths, passphrase=None)
    assert err.code == "CONNECT_FAILED"
    assert str(key) in err.msg
    assert "no passphrase was supplied" in err.msg
    assert _KEY_PASSPHRASE not in err.msg


def test_unparseable_explicit_key_still_aborts_and_names_the_key(
    tmp_path: Path,
) -> None:
    """An explicit key that is not a private key keeps its outcome, now named."""
    broken = tmp_path / "named_by_operator"
    broken.write_text("not a private key\n", encoding="utf-8")
    profile = Profile(
        name="x",
        transport="ssh",
        host="h",
        username="u",
        auth=AuthConfig(method="private_key_path", key_path=broken),
    )
    paths = resolve_ssh_key_paths(profile, ssh_dir=tmp_path)
    assert paths == [broken]

    err = _connect_error(paths, passphrase=None)
    assert err.code == "CONNECT_FAILED"
    assert str(broken) in err.msg
    assert "[auth].key_path" in err.msg


def test_healthy_default_key_still_reaches_the_dial(tmp_path: Path) -> None:
    """A loadable chain is never reported as a key failure."""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    good = ssh_dir / "id_ed25519"
    _write_key(good)
    passphrase_file = tmp_path / "passphrase"
    passphrase_file.write_text(_KEY_PASSPHRASE + "\n", encoding="utf-8")

    profile = _profile(passphrase_path=passphrase_file)
    paths = resolve_ssh_key_paths(profile, ssh_dir=ssh_dir)
    assert paths == [good]

    err = _connect_error(paths, passphrase=_KEY_PASSPHRASE)
    assert "cannot load ssh client key" not in err.msg
    assert str(good) not in err.msg


def test_encrypted_default_key_without_a_passphrase_source_reaches_the_dial(
    tmp_path: Path,
) -> None:
    """The chain still drops an encrypted key no passphrase source can serve."""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    _write_encrypted_key(ssh_dir / "id_ed25519", _KEY_PASSPHRASE)
    good = ssh_dir / "id_ecdsa"
    _write_key(good)

    paths = resolve_ssh_key_paths(_profile(), ssh_dir=ssh_dir)
    assert paths == [good]

    err = _connect_error(paths, passphrase=None)
    assert "cannot load ssh client key" not in err.msg


def test_encrypted_key_with_the_passphrase_source_reaches_the_dial(
    tmp_path: Path,
) -> None:
    """A key the passphrase does open is not a failure at all."""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    encrypted = ssh_dir / "id_ed25519"
    _write_encrypted_key(encrypted, _KEY_PASSPHRASE)
    passphrase_file = tmp_path / "passphrase"
    passphrase_file.write_text(_KEY_PASSPHRASE + "\n", encoding="utf-8")

    profile = _profile(passphrase_path=passphrase_file)
    paths = resolve_ssh_key_paths(profile, ssh_dir=ssh_dir)
    assert paths == [encrypted]

    err = _connect_error(paths, passphrase=_KEY_PASSPHRASE)
    assert "cannot load ssh client key" not in err.msg
    assert _KEY_PASSPHRASE not in err.msg


@pytest.mark.skipif(
    _OPENSSL is None, reason="openssl needed to build a scrypt PKCS#8 key"
)
def test_pkcs8_scheme_the_stack_cannot_decrypt_names_the_real_remedy(
    tmp_path: Path,
) -> None:
    """A key this interpreter cannot decrypt is not a passphrase story.

    asyncssh rewrites every PKCS#8 decryption failure into one generic message
    ("Unable to decrypt PKCS#8 private key"), so a wrong passphrase and a PBES2
    scheme it does not implement read identically. The passphrase here is
    provably right - openssl decrypts the key with it - which is what separates
    the two: the sentence must name the re-encryption remedy instead of sending
    the caller back to a passphrase file that is already correct.
    """
    key = tmp_path / "scrypt.pem"
    _write_scrypt_pkcs8_key(key, _KEY_PASSPHRASE)

    verdict, detail = _key_import_probe(key, passphrase=_KEY_PASSPHRASE)
    assert verdict == "undecryptable", detail

    sentence = key_load_failure_message([key], passphrase=_KEY_PASSPHRASE)
    assert sentence is not None
    assert str(key) in sentence
    assert "cannot decrypt it" in sentence
    assert "aes-256-cbc" in sentence
    assert "[auth].passphrase_path" not in sentence
    assert _KEY_PASSPHRASE not in sentence

    # A passphrase that does not open the key stays a passphrase story: no
    # reader can corroborate it, so no claim about the interpreter is made.
    assert _key_import_probe(key, passphrase=_WRONG_PASSPHRASE)[0] == "passphrase"
    wrong_sentence = key_load_failure_message([key], passphrase=_WRONG_PASSPHRASE)
    assert wrong_sentence is not None
    assert "did not decrypt it" in wrong_sentence
    assert _WRONG_PASSPHRASE not in wrong_sentence


def test_connector_error_is_not_relabelled_as_a_key_failure(tmp_path: Path) -> None:
    """A connector's own error class is not asyncssh's, whatever it is called.

    Key-load errors are recognised by name (the SSH stack is imported lazily),
    so the name alone must not be enough: a connector raising its own
    ``KeyImportError`` - with a genuinely unloadable key in the chain, which is
    what makes the key sentence available at all - keeps its own words.
    """

    class KeyImportError(Exception):  # noqa: N818 - sharing the name is the point
        """Same name as asyncssh's, different class."""

    assert _is_key_load_error(KeyImportError("boom")) is False
    assert _is_key_load_error(asyncssh.KeyImportError("boom")) is True

    broken = tmp_path / "broken"
    broken.write_text("not a private key\n", encoding="utf-8")

    def _connector(**kwargs: object) -> object:
        raise KeyImportError("connector said no")

    err = _connect_error([broken], passphrase=None, connector=_connector)
    assert err.code == "CONNECT_FAILED"
    assert "connector said no" in err.msg
    assert "cannot load ssh client key" not in err.msg


def test_unreadable_client_key_is_a_local_file_problem(tmp_path: Path) -> None:
    """A key this user cannot read is not the peer rejecting a credential.

    The chain keeps an unreadable entry (it cannot be judged here), asyncssh
    then fails while preparing the connect, and its "Permission denied" text
    reads like a rejected credential while the fix is a chmod. The chain is
    ours, so the error has to say so - and say that nothing was dialled.
    """
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    unreadable = ssh_dir / "id_rsa"
    _write_key(unreadable)
    unreadable.chmod(0)
    try:
        paths = resolve_ssh_key_paths(_profile(), ssh_dir=ssh_dir)
        assert paths == [unreadable]

        err = _connect_error(paths, passphrase=None)
        assert err.code == "CONNECT_FAILED"
        assert str(unreadable) in err.msg
        assert "cannot read ssh client key" in err.msg
        assert "no credential was tried" in err.msg
        assert err.details.get("client_key") == str(unreadable)
    finally:
        unreadable.chmod(0o600)


def test_directory_client_key_is_a_local_file_problem(tmp_path: Path) -> None:
    """A directory handed over as a client key is caught the same way."""
    adir = tmp_path / "id_ed25519"
    adir.mkdir()

    err = _connect_error([adir], passphrase=None)
    assert err.code == "CONNECT_FAILED"
    assert str(adir) in err.msg
    assert "cannot read ssh client key" in err.msg


def test_only_our_own_client_key_paths_are_claimed_as_local(tmp_path: Path) -> None:
    """The local-file mapping needs our path, not just a local-access errno.

    A refused connection is an OSError too, and a peer-side "Permission denied"
    has no filename; both must keep the AUTH_FAILED reading because both are
    about the peer. Only an errno *and* a name from our own chain qualifies.
    """
    ours = tmp_path / "ours"
    theirs = tmp_path / "theirs"

    peer = _map_ssh_connect_error(
        PermissionError("Permission denied (publickey)"), host="h", port=22
    )
    assert peer.code == "AUTH_FAILED"

    foreign = _map_ssh_connect_error(
        PermissionError(errno.EACCES, "Permission denied", str(theirs)),
        host="h",
        port=22,
        client_keys=[ours],
    )
    assert foreign.code == "AUTH_FAILED"

    refused = _map_ssh_connect_error(
        ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
        host="h",
        port=22,
        client_keys=[ours],
    )
    assert refused.code == "CONNECT_FAILED"

    local = _map_ssh_connect_error(
        PermissionError(errno.EACCES, "Permission denied", str(ours)),
        host="h",
        port=22,
        client_keys=[ours],
    )
    assert local.code == "CONNECT_FAILED"
    assert str(ours) in local.msg

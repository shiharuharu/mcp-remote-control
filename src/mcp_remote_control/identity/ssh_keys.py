"""SSH identity file resolution and default fallback chain.

Rules:

- If ``profile.auth.key_path`` is set -> only that path (explicit key).
- Else default chain under ``~/.ssh``: ``id_rsa``, ``id_ecdsa``, ``id_ed25519``
  (and optional ``*_sk`` variants). Order is intentional: **id_rsa before
  id_ed25519**. Only files that exist on disk are returned.

Default-chain entries must also be *importable*: a client-key list is handed
to asyncssh as an explicit ``client_keys`` sequence, and asyncssh then aborts
the whole connect with ``KeyImportError`` on the first unloadable file - so a
malformed ``id_*`` would defeat a password or ssh-agent profile. Chain entries
that cannot be **parsed** are therefore skipped in every profile, passphrase
source or not: asyncssh raises on them rather than skipping, so leaving one in
after a passphrase became available would abort the connect the same way. An
entry that parses but merely *needs* its passphrase is a different case - it
cannot be judged importable here, and it is exactly what the profile's
passphrase source is for, so it stays when (and only when) one is configured.

When a client key cannot be loaded, asyncssh refuses the whole connect while
*preparing* it - before any dial - so the supplied password and ssh-agent
never get their turn and the caller is left with asyncssh's own words, which
name neither the file nor a remedy. :func:`key_load_failure_message` turns that
into a sentence naming the offending file, the reason and the remedy; the
connect still fails, because which credentials to attempt is a policy this
module does not decide.

Never loads key **contents** into returned structures or logs; the import
probe parses a key in place and reports a verdict plus asyncssh's own error
message, nothing that came out of the file. The one extra read - a decryption
attempt by an independent reader, used only to tell an undecryptable key from
a wrong passphrase - keeps the bytes in a local buffer for that call and
returns a boolean.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp_remote_control.config.models import Profile

# Default identity basenames under ~/.ssh. Order: id_rsa before id_ed25519.
DEFAULT_SSH_IDENTITY_BASENAMES: tuple[str, ...] = (
    "id_rsa",
    "id_ecdsa",
    "id_ecdsa_sk",
    "id_ed25519",
    "id_ed25519_sk",
)


def default_ssh_dir() -> Path:
    """Return ``~/.ssh`` (expanded)."""
    return (Path.home() / ".ssh").expanduser()


# asyncssh reports a missing passphrase and an undecryptable key in the same
# ``KeyImportError`` class as a structural parse failure, and its
# ``KeyEncryptionError`` (raised when the PBE layer is unavailable) is a
# ``ValueError`` that is *not* a ``KeyImportError`` subclass. Both stages mean
# "the key file is encrypted", so they are told apart from a parse failure by
# what asyncssh says it choked on.
_ENCRYPTION_MARKERS: tuple[str, ...] = ("passphrase", "decrypt", "encrypt")


def _has_encryption_wording(detail: str) -> bool:
    """Does an import error talk about the key's encryption layer?"""
    lowered = detail.lower()
    return any(marker in lowered for marker in _ENCRYPTION_MARKERS)


# Wording asyncssh uses when the PBE layer refuses because *this interpreter*
# lacks a primitive it needs - bcrypt/KDF support, an unknown cipher or KDF.
# No passphrase gets past that, which separates it from a wrong passphrase
# ("Incorrect passphrase", "Unable to decrypt key"); other encryption errors
# stay on the passphrase side, so a reworded message loses the sharper remedy
# but never the failure itself. The PKCS#8 path erases these words: its decoder
# rewrites every precise PBES2 failure into one generic KeyImportError, so
# :func:`_passphrase_opens_key` settles the unimplemented-scheme case instead.
_MISSING_SUPPORT_MARKERS: tuple[str, ...] = (
    "requires bcrypt",
    "unknown cipher",
    "unknown kdf",
)


def _passphrase_opens_key(path: Path, passphrase: str) -> bool:
    """Does an independent reader in this interpreter decrypt *path* with it?

    ``cryptography`` (the same interpreter, a different decoder) is asked to
    open the key with the passphrase a connect would use. Success proves the
    passphrase right, so a connect that still fails on the key is failing on
    this interpreter's decryption support - a PBES2 scheme or OpenSSH KDF the
    SSH stack does not implement, which no passphrase can get past.

    False whenever the reader is unavailable or also refuses the file: an
    unproven claim is never made. The key bytes are read into a local buffer
    for this check only and are never returned, logged or stored.
    """
    try:
        from cryptography.hazmat.primitives.serialization import (
            load_pem_private_key,
        )
    except ImportError:  # pragma: no cover - ships with the SSH stack's own deps
        return False
    try:
        data = path.read_bytes()
        load_pem_private_key(data, password=passphrase.encode("utf-8"))
    except Exception:  # noqa: BLE001 - any refusal, IO or format problem: unproven
        return False
    return True


def _decryption_verdict(path: Path, *, detail: str, passphrase: str | None) -> str:
    """Which obstacle an encrypted-key failure is: the passphrase, or the stack?

    ``"undecryptable"`` is claimed only from evidence: asyncssh named a missing
    primitive, or the supplied passphrase was proven right by an independent
    reader while asyncssh still refused the key. Everything else - no
    passphrase supplied, a passphrase that nothing can corroborate, an
    unrecognised message - stays ``"passphrase"``, which is a claim about the
    one thing a caller can act on without being told something false.
    """
    if passphrase is None:
        return "passphrase"
    if any(marker in detail.lower() for marker in _MISSING_SUPPORT_MARKERS):
        return "undecryptable"
    if _passphrase_opens_key(path, passphrase):
        return "undecryptable"
    return "passphrase"


def _key_import_probe(
    path: Path, *, passphrase: str | None = None
) -> tuple[str, str]:
    """Classify one key file, optionally with a passphrase a connect would use.

    Returns ``(verdict, detail)``; *detail* is asyncssh's own message for a
    failed import (``""`` when the file loads) and never holds key material.
    Without a passphrase: ``"usable"`` (imports without one), ``"passphrase"``
    (encrypted; a passphrase can take it further), ``"unparseable"`` (not a
    private key asyncssh reads at all), ``"unknown"`` (anything else - vanished
    file, permissions, IO - kept so the connect reports the real error instead
    of hiding it behind an empty chain).

    With a passphrase the question becomes whether *this* interpreter can load
    the key with it, which adds ``"undecryptable"``: the key is encrypted and
    the obstacle is the interpreter, not the passphrase (OpenSSH-format
    decryption needs the optional ``bcrypt`` dependency; an unsupported cipher
    or KDF fails the same way; for PKCS#8, where asyncssh erases the precise
    wording, an independent reader proves the passphrase right first). No
    passphrase can get past any of them.

    Only a proven *parse* failure is separable without a passphrase, so an
    unrecognised message is treated as usable rather than guessed at.
    asyncssh is imported lazily to keep this module importable without the
    SSH stack (the same reason the transport defers it to connect).
    """
    try:
        import asyncssh
    except ImportError:  # pragma: no cover - asyncssh is a hard dependency
        return "usable", ""
    encryption_error = getattr(asyncssh, "KeyEncryptionError", None)

    try:
        asyncssh.read_private_key(str(path), passphrase)
    except asyncssh.KeyImportError as exc:
        # ``KeyEncryptionError`` cannot land here (it is not a
        # ``KeyImportError`` subclass), so the message alone decides - plus
        # the independent reader when a passphrase was supplied, because
        # asyncssh collapses PKCS#8 decryption failures into one message.
        detail = str(exc)
        if _has_encryption_wording(detail):
            return (
                _decryption_verdict(path, detail=detail, passphrase=passphrase),
                detail,
            )
        return "unparseable", detail
    except Exception as exc:  # noqa: BLE001 - only a proven import failure may drop a key
        detail = str(exc)
        if encryption_error is not None and isinstance(exc, encryption_error):
            return (
                _decryption_verdict(path, detail=detail, passphrase=passphrase),
                detail,
            )
        # A vanished or unreadable file stays in the chain so the connect
        # reports it verbatim; guessing here could silently discard a key.
        return "unknown", detail
    return "usable", ""


def _key_import_verdict(path: Path) -> str:
    """Classify why asyncssh can or cannot load *path* as a private key.

    Returns the verdict of :func:`_key_import_probe` for an import without a
    passphrase; see there for the vocabulary and its limits.
    """
    return _key_import_probe(path)[0]


def _key_is_chainable(path: Path, *, passphrase_source: bool) -> bool:
    """Should a default-chain entry be handed to asyncssh as a client key?

    A *parse* failure never can: asyncssh raises instead of skipping, so the
    entry would abort the connect before any auth exchange. A key that only
    needs its passphrase is unusable without a passphrase source and is meant
    to be used with one - which is the single case that keeping it can help.
    """
    verdict = _key_import_verdict(path)
    if verdict == "passphrase":
        return passphrase_source
    return verdict != "unparseable"


def _key_load_failure_sentence(
    path: Path, *, verdict: str, detail: str, passphrase: str | None
) -> str:
    """One-line, actionable report for a chain key asyncssh cannot load.

    *detail* is asyncssh's own message, quoted so the caller sees the version's
    exact wording; nothing here is derived from key contents or the passphrase.
    """
    if verdict == "undecryptable":
        reason = "the key is encrypted and this interpreter cannot decrypt it"
        # Two obstacles reach this verdict, with different remedies: the
        # OpenSSH-format path names its missing dependency (bcrypt), while the
        # PKCS#8 path names nothing, so the key has to be re-encrypted into a
        # scheme the SSH stack implements.
        if "bcrypt" in detail.lower():
            remedy = (
                "install the optional dependency (pip install 'asyncssh[bcrypt]') "
                "or use a key that is not encrypted"
            )
        else:
            remedy = (
                "re-encrypt it with a scheme the SSH stack decrypts (openssl "
                "pkcs8 -topk8 -v2 aes-256-cbc -v2prf hmacWithSHA256), or use a "
                "key that is not encrypted"
            )
    elif verdict == "passphrase":
        if passphrase is None:
            reason = "the key is encrypted and no passphrase was supplied"
            remedy = (
                "check that [auth].passphrase_path is set and readable, or use "
                "a key that is not encrypted"
            )
        else:
            reason = (
                "the key is encrypted and the supplied passphrase did not "
                "decrypt it"
            )
            remedy = "check [auth].passphrase_path, or use a key that is not encrypted"
    else:
        reason = "asyncssh cannot read it as a private key"
        remedy = "replace the file, or point [auth].key_path at a readable private key"
    suffix = f" (asyncssh: {detail})" if detail else ""
    return f"cannot load ssh client key {path}: {reason}{suffix}; {remedy}"


def key_load_failure_message(
    paths: Sequence[Path | str],
    *,
    passphrase: str | None = None,
) -> str | None:
    """Sentence naming the first chain entry asyncssh cannot load, or ``None``.

    An explicit ``client_keys`` list is imported entry by entry while asyncssh
    *prepares* the connect, and the first entry it cannot load aborts the whole
    connect before any dial - so a failure here is about a local file, not the
    peer, however much the raw error reads like a connection problem. Replays
    that import per entry with the passphrase the connect supplies and reports
    the first proven failure.

    Entries that load, and entries that cannot be judged here (missing file, no
    read permission, an unexpected error), yield ``None``: a diagnostic must
    never relabel a connect error it did not verify.
    """
    for path in paths:
        try:
            candidate = Path(path)
            verdict, detail = _key_import_probe(candidate, passphrase=passphrase)
        except Exception:  # noqa: BLE001 - a diagnostic must not replace the error
            return None
        if verdict in ("usable", "unknown"):
            continue
        return _key_load_failure_sentence(
            candidate, verdict=verdict, detail=detail, passphrase=passphrase
        )
    return None


def resolve_ssh_key_paths(
    profile: Profile,
    *,
    ssh_dir: Path | None = None,
) -> list[Path]:
    """Resolve ordered client key paths for *profile*.

    Args:
        profile: Loaded connection profile.
        ssh_dir: Override for ``~/.ssh`` (injection / alternate homes).

    Returns:
        Ordered list of key **paths** only (never file contents). Default-chain
        entries that exist but cannot be imported are skipped; an empty result
        means "no usable default key", leaving password / ssh-agent auth to
        proceed instead of failing the connect on a stray ``id_*`` file.
    """
    auth = profile.auth
    if auth is not None and auth.key_path is not None:
        # Explicit single key: do not scan the default chain. Returned even
        # when broken so the connect surfaces why the named key failed.
        return [Path(auth.key_path).expanduser()]

    root = Path(ssh_dir) if ssh_dir is not None else default_ssh_dir()
    root = root.expanduser()
    paths: list[Path] = []
    for name in DEFAULT_SSH_IDENTITY_BASENAMES:
        candidate = root / name
        if candidate.is_file():
            paths.append(candidate)
    # A configured passphrase source means the chain may legitimately hold
    # encrypted keys, so entries that merely need the passphrase are kept.
    # Entries that cannot be parsed are dropped either way: asyncssh raises on
    # them rather than skipping, so one stray file would abort the whole
    # connect - losing the passphrase and the supplied password with it.
    has_passphrase_source = auth is not None and auth.passphrase_path is not None
    paths = [
        p
        for p in paths
        if _key_is_chainable(p, passphrase_source=has_passphrase_source)
    ]
    return paths

"""Config surfaces that must not claim more than the code does.

Two guards live here:

* ``[defaults]`` / ``[logging]`` accept keys that no code path reads yet.
  They may not be advertised as effective knobs, so the docstrings must say
  the keys have no effect (and name where the real behaviour comes from),
  and the "no reader" invariant is pinned by scanning the source tree.
* A notes file that is not valid UTF-8 must report an encoding error, not a
  write failure - no write happened and the bytes on disk are untouched.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

import pytest

from mcp_remote_control.config import load_config
from mcp_remote_control.config.models import DefaultsConfig, LoggingConfig
from mcp_remote_control.config.store import ensure_home_layout, put_profile
from mcp_remote_control.core import config_ops
from mcp_remote_control.screen.geometry import GeometryAdapter, GeometryMemory

SRC = Path(__file__).resolve().parents[2] / "src" / "mcp_remote_control"

# ``[defaults]`` keys that are parsed and type-checked but consumed by nothing.
INERT_DEFAULTS_KEYS = (
    "screen_cols",
    "screen_rows",
    "screen_term",
    "default_shell",
    "exec_timeout_ms",
)

# ``[logging]`` keys; the whole table is unwired.
INERT_LOGGING_KEYS = ("level", "dir", "max_bytes", "backup_count", "audit")

# Files allowed to name an inert key without reading it: the dataclass field
# and the TOML parser that validates it.
_DECLARATION_ONLY_FILES = frozenset({"config/models.py", "config/load.py"})


def _key_read_patterns(key: str) -> tuple[re.Pattern[str], ...]:
    """Regexes matching the idiomatic ways a key is read in this code base.

    Attribute access (``cfg.defaults.screen_cols``) is only one spelling: the
    same read can be written as ``getattr(cfg.defaults, "screen_cols")`` or as
    a mapping lookup (``cfg.defaults_raw["exec_timeout_ms"]``), and the table
    itself can be pulled wholesale with ``getattr(cfg, "logging")``. Missing
    any of those would leave the docstring's "read by no code path" claim
    false while this guard stayed green.

    ``<table>.get("<key>")`` and the other receiver-dependent spellings are
    handled by :func:`_line_reads_key` rather than here: unlike the three above
    they cannot be matched on their own text, because the receiver decides
    which table is being read (see :func:`_line_reads_key`).
    """
    return (
        re.compile(rf"\.{key}\b"),
        re.compile(rf"""getattr\(.*,\s*["']{key}["']"""),
        re.compile(rf"""\[\s*["']{key}["']\s*\]"""),
    )


# One dotted access path: ``cfg``, ``self._config()``, ``profile.defaults``.
_RECEIVER = r"[A-Za-z_]\w*(?:\(\))?(?:\.[A-Za-z_]\w*(?:\(\))?)*"

# The global tables the guards above protect, allowlisted by shape rather than
# by the name of the variable that happens to hold the config object: a table
# reached through *any* prefix (``cfg.defaults_raw``, ``loaded.defaults_raw``)
# is still the global table.
_GLOBAL_TABLE_RECEIVER = re.compile(
    rf"^(?:{_RECEIVER}\.)?(?:defaults|defaults_raw|logging)$"
    rf"|^self\._defaults\(\)$"
)

# The config object that owns those tables, for a whole-table read spelled
# ``cfg.get("logging")``. Listed by name: an identifier that merely ends in
# ``cfg`` is usually some other table (``winrm_cfg``, ``ssh_cfg``), and
# counting those would flag unrelated WinRM/SSH keys as consumed defaults.
_GLOBAL_CONFIG_RECEIVER = re.compile(
    r"^(?:cfg|config|self\._config\(\)|self\._config|self\._cfg)$"
)

# ``profile`` as its own segment, or the profile seed table by name. Matched on
# segments, never as a substring: ``profiles_cfg.defaults_raw`` is the global
# table reached through a local named after the profiles it holds, and a
# substring test would hide it.
_PROFILE_SCOPED_RECEIVER = re.compile(r"(?:^|\.)profile(?:_defaults)?(?:\.|$)")

# ``<receiver>.get(<arg>)`` / ``.pop(<arg>)``; group 2 is the first argument.
_TABLE_CALL = re.compile(rf"({_RECEIVER})\.(?:get|pop)\(\s*([^,)]*)")

# A leading string literal in an argument or an ``in`` operand.
_LITERAL_ARG = re.compile(r"""^["']([^"']*)["']""")

# ``<left> in <receiver>`` - a membership test against a table. Group 1 is the
# left side (a whole token: a quoted key or a single name) and group 2 the
# receiver; matching into the middle of an expression would let an unrelated
# ``elif "other_key" in cfg:`` pass as a read of this one.
_MEMBERSHIP = re.compile(
    rf"""(["'][^"']*["']|[A-Za-z_]\w*)\s+(?:not\s+)?in\s+({_RECEIVER})"""
)

# ``<receiver>.items()`` / ``.keys()`` / ``.values()`` - the whole table.
_WHOLE_TABLE = re.compile(rf"({_RECEIVER})\.(?:items|keys|values)\(\s*\)")


def _is_global_receiver(receiver: str) -> bool:
    """True when *receiver* names the global config object or one of its tables.

    An allowlist, not a denylist. A receiver that merely looks profile-ish
    (``profiles_cfg.defaults_raw``) still reaches the global table, while an
    unknown one is not a read of the knobs these guards protect - including
    the per-profile seed table, which is the documented live knob.
    """
    if _PROFILE_SCOPED_RECEIVER.search(receiver):
        return False
    return bool(
        _GLOBAL_TABLE_RECEIVER.match(receiver)
        or _GLOBAL_CONFIG_RECEIVER.match(receiver)
    )


def _line_reads_key(line: str, key: str) -> bool:
    """True when *line* reads *key* from a global table, in any known spelling.

    Attribute access, ``getattr`` and bracket lookup are matched on the text
    alone. The remaining spellings need the receiver, because the same call
    against the *profile seed* (``profile_defaults.get("screen_cols")`` in
    ``screen/geometry.py``) reads a different, live table: ``.get`` / ``.pop``,
    ``<key> in <table>``, and ``.items()`` / ``.keys()`` / ``.values()`` are
    therefore counted only when the receiver is the global config object or
    one of its tables (see :func:`_is_global_receiver`).

    A table call whose argument is not a literal - ``cfg.defaults_raw.get(k)``,
    ``for k in cfg.defaults_raw`` - could name any key, so it counts as a read
    of every key. The residual risk is a false *positive* on an unrelated
    mapping whose key happens to collide - a loud failure that costs a look,
    unlike the silent false negative this closes.
    """
    if any(p.search(line) for p in _key_read_patterns(key)):
        return True

    for match in _TABLE_CALL.finditer(line):
        if not _is_global_receiver(match.group(1)):
            continue
        literal = _LITERAL_ARG.match(match.group(2).strip())
        if literal is None or literal.group(1) == key:
            return True

    for match in _MEMBERSHIP.finditer(line):
        if not _is_global_receiver(match.group(2)):
            continue
        literal = _LITERAL_ARG.match(match.group(1))
        if literal is None or literal.group(1) == key:
            return True

    for match in _WHOLE_TABLE.finditer(line):
        if _is_global_receiver(match.group(1)):
            return True
    return False


def _attribute_reads(key: str) -> list[str]:
    """Return source lines that read *key* outside the declaration files."""
    hits: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in _DECLARATION_ONLY_FILES:
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if _line_reads_key(line, key):
                hits.append(f"{rel}:{lineno}: {line.strip()}")
    return hits


def test_defaults_keys_advertised_as_inert_have_no_reader() -> None:
    for key in INERT_DEFAULTS_KEYS:
        assert _attribute_reads(key) == [], (
            f"config.defaults.{key} now has a consumer; wire it through "
            "and drop it from the inert list in DefaultsConfig's docstring"
        )


def test_logging_table_has_no_reader() -> None:
    # [logging] is reached as a whole table (cfg.logging), so the whole
    # LoggingConfig surface must stay unread outside its declaration.
    assert _attribute_reads("logging") == []


def _flat_doc(cls: type) -> str:
    """Class docstring with wrapping collapsed, so reflow does not break checks."""
    return " ".join((cls.__doc__ or "").split())


def test_defaults_docstring_discloses_inert_keys() -> None:
    doc = _flat_doc(DefaultsConfig)
    assert "no effect" in doc
    for key in INERT_DEFAULTS_KEYS:
        assert key in doc, f"{key} missing from DefaultsConfig docstring"
    # The live source of the geometry/shell behaviour is named so an operator
    # can find the knob that does work.
    assert "profile" in doc


def test_logging_docstring_discloses_unwired_table() -> None:
    doc = _flat_doc(LoggingConfig)
    assert "no effect" in doc
    for key in INERT_LOGGING_KEYS:
        assert key in doc, f"{key} missing from LoggingConfig docstring"


def test_logging_table_does_not_configure_logging(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[logging]\nlevel = "debug"\ndir = "logs"\n', encoding="utf-8"
    )
    root = logging.getLogger()
    level_before, handlers_before = root.level, list(root.handlers)
    load_config(tmp_path)
    assert root.level == level_before
    assert root.handlers == handlers_before


def test_global_defaults_do_not_seed_screen_geometry(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        "[defaults]\nscreen_cols = 80\nscreen_rows = 24\n", encoding="utf-8"
    )
    load_config(tmp_path)
    adapter = GeometryAdapter(memory=GeometryMemory.for_home(tmp_path))
    plan = adapter.plan_open(
        command="/bin/bash",
        argv=None,
        cols=None,
        rows=None,
        endpoint_id="",
        profile_defaults=None,
    )
    assert (plan.cols, plan.rows) != (80, 24)


# ---------------------------------------------------------------------------
# notes: a non-UTF-8 file is an encoding error, not a write error
# ---------------------------------------------------------------------------

_LATIN1_NOTE = b"caf\xe9 latin-1 note\n"


@pytest.mark.parametrize("action", ["read", "append", "prepend"])
def test_notes_decode_failure_reports_encoding_not_write(
    tmp_path: Path, action: str
) -> None:
    ensure_home_layout(tmp_path)
    put_profile(tmp_path, name="lab", transport="local")
    notes_file = tmp_path / "notes" / "lab.md"
    notes_file.write_bytes(_LATIN1_NOTE)

    kwargs: dict[str, object] = {
        "action": action,
        "name": "lab",
        "home": str(tmp_path),
    }
    if action in ("append", "prepend"):
        kwargs["content"] = "more\n"
    res = config_ops.run("notes", **kwargs)

    assert res.status == "error"
    assert res.code == "NOTES_ENCODING_INVALID"
    # The bytes on disk are untouched: nothing was written.
    assert notes_file.read_bytes() == _LATIN1_NOTE


def test_notes_decode_failure_message_names_the_file(tmp_path: Path) -> None:
    ensure_home_layout(tmp_path)
    put_profile(tmp_path, name="lab", transport="local")
    (tmp_path / "notes" / "lab.md").write_bytes(_LATIN1_NOTE)

    res = config_ops.run("notes", action="read", name="lab", home=str(tmp_path))
    msg = str(res.fields.get("msg", ""))
    assert "notes/lab.md" in msg
    assert "UTF-8" in msg


# ---------------------------------------------------------------------------
# the no-reader scan: the spelling of a read must not decide whether it counts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "snippet",
    [
        "cfg.defaults.screen_cols",
        'getattr(cfg.defaults, "screen_cols")',
        "getattr(cfg.defaults, 'screen_cols', 120)",
        "getattr(self._config().defaults, 'screen_cols')",
        'cfg.defaults_raw["screen_cols"]',
        "cfg.defaults_raw['screen_cols']",
    ],
)
def test_key_read_patterns_cover_getattr_and_mapping_lookups(snippet: str) -> None:
    """A read written as getattr(...) or a mapping key still counts as a read."""
    patterns = _key_read_patterns("screen_cols")
    assert any(p.search(snippet) for p in patterns), snippet


def test_guard_is_not_vacuous() -> None:
    """Consumed siblings must be found, or the scan above proves nothing."""
    assert _attribute_reads("max_body_chars")
    assert _attribute_reads("winrm_probe")


@pytest.mark.parametrize(
    ("key", "line"),
    [
        ("screen_cols", 'cfg.defaults_raw.get("screen_cols")'),
        ("screen_cols", "cfg.defaults_raw.get('screen_cols')"),
        ("exec_timeout_ms", 'defaults.get("exec_timeout_ms", 60)'),
        ("default_shell", 'self._defaults().get("default_shell")'),
        ("level", 'cfg.logging.get("level")'),
        ("logging", 'cfg.get("logging")'),
    ],
)
def test_table_get_spelled_reads_count_as_reads(key: str, line: str) -> None:
    """A read written as ``table.get("key")`` is still a read.

    This is the spelling a defaults table is actually read with in this tree,
    so a scanner blind to it would let a real consumer appear while the
    "read by no code path" claim stayed green.
    """
    assert _line_reads_key(line, key), line


@pytest.mark.parametrize(
    ("key", "line"),
    [
        ("screen_cols", 'profile_defaults.get("screen_cols")'),
        ("screen_rows", "profile_defaults.get('screen_rows')"),
        ("screen_cols", 'endpoint.profile.defaults.get("screen_cols")'),
        ("exec_timeout_ms", 'cfg.profile_defaults.get("exec_timeout_ms")'),
    ],
)
def test_profile_scoped_get_is_not_a_read_of_the_global_table(
    key: str, line: str
) -> None:
    """The profile seed is a different table from the global ``[defaults]``.

    ``screen/geometry.py`` reads its resize seed with exactly this spelling;
    counting it would make the inert list it is meant to protect wrong, so the
    scan must stay blind to a profile-scoped receiver - and therefore the real
    tree must still come back clean.
    """
    assert not _line_reads_key(line, key), line
    assert _attribute_reads(key) == []


def test_scan_finds_a_table_get_reader(tmp_path: Path, monkeypatch) -> None:
    """Positive control: a ``.get("key")`` reader in the tree must be seen.

    Without this, the ``.get`` branch could be dropped from
    :func:`_line_reads_key` and every guard above would still pass on a tree
    that reads no inert key that way - the blind spot this branch closes.
    """
    scratch = tmp_path / "mcp_remote_control"
    (scratch / "screen").mkdir(parents=True)
    (scratch / "screen" / "geometry.py").write_text(
        'cols = cfg.defaults_raw.get("screen_cols", 120)\n', encoding="utf-8"
    )
    monkeypatch.setattr(sys.modules[__name__], "SRC", scratch)
    assert _attribute_reads("screen_cols"), "a .get(...) reader went unseen"


@pytest.mark.parametrize(
    ("key", "line"),
    [
        # ``pop`` is a read as well as a removal: the value is handed back.
        ("screen_cols", 'cfg.defaults_raw.pop("screen_cols", None)'),
        ("level", 'cfg.logging.pop("level")'),
        # Membership of the key in the table.
        ("screen_cols", 'if "screen_cols" in cfg.defaults_raw:'),
        ("screen_cols", 'if "screen_cols" not in cfg.defaults_raw:'),
        ("screen_cols", 'assert "screen_cols" in cfg.defaults_raw'),
        # A lookup or iteration whose key is not a literal could name any key.
        ("screen_cols", "cfg.defaults_raw.get(k)"),
        ("screen_cols", "for k in cfg.defaults_raw:"),
        ("exec_timeout_ms", "for k, v in cfg.defaults_raw.items():"),
        ("level", "for k in cfg.logging.keys():"),
        ("screen_cols", "for v in cfg.defaults_raw.values():"),
    ],
)
def test_reads_spelled_beyond_a_literal_lookup_count_as_reads(
    key: str, line: str
) -> None:
    """The spellings that reach a table without naming the key in a literal.

    ``in cfg.defaults_raw``, ``for k in ...`` and ``get(k)`` all read from the
    table without the key appearing as a quoted string; a scanner that only
    looks for the literal would let a consumer appear unnoticed.
    """
    assert _line_reads_key(line, key), line


@pytest.mark.parametrize(
    ("key", "line"),
    [
        ("screen_cols", "profile_defaults.get(dynamic_key)"),
        ("screen_cols", 'if "screen_cols" in profile_defaults:'),
        ("screen_cols", "for k in profile_defaults.items():"),
        ("screen_cols", 'profile_defaults.pop("screen_cols", None)'),
        ("screen_cols", "for k in endpoint.profile.defaults:"),
        # Not a config table at all, whatever the key collides with.
        ("screen_cols", 'opts.get("screen_cols")'),
        ("screen_cols", 'if "screen_cols" in os.environ:'),
        ("level", "for k in logging.getLogger().handlers:"),
    ],
)
def test_non_global_receivers_stay_invisible(key: str, line: str) -> None:
    """The receiver scoping applies to every spelling, not just ``.get``.

    Counting these would flag the profile seed the tree actually reads
    (``screen/geometry.py``), turning the guard red on correct code.
    """
    assert not _line_reads_key(line, key), line


@pytest.mark.parametrize(
    ("key", "line"),
    [
        ("screen_cols", 'n = profiles_cfg.defaults_raw.get("screen_cols")'),
        ("screen_cols", 'n = loaded.defaults.get("screen_cols")'),
        ("screen_cols", 'if "screen_cols" in profiles_cfg.defaults_raw:'),
        ("screen_cols", "for k in profiles_cfg.defaults_raw.items():"),
        ("level", 'profiles_cfg.logging.pop("level")'),
    ],
)
def test_global_table_is_recognised_through_any_local_name(
    key: str, line: str
) -> None:
    """The receiver allowlist keys on the *table*, not on the variable name.

    ``profiles_cfg`` holds the loaded global config, so
    ``profiles_cfg.defaults_raw`` is the very table ``cfg.defaults_raw`` names.
    Deciding by substring (``"profile" in receiver``) would swallow it and let
    a genuine consumer appear while the docstrings stayed green.
    """
    assert _line_reads_key(line, key), line


@pytest.mark.parametrize(
    "source",
    [
        'for k in cfg.defaults_raw.items():\n    pass\n',
        'if "screen_cols" in cfg.defaults_raw:\n    pass\n',
        'cols = cfg.defaults_raw.pop("screen_cols", None)\n',
        'cols = cfg.defaults_raw.get(dynamic_key)\n',
    ],
)
def test_scan_finds_every_reader_spelling(
    tmp_path: Path, monkeypatch, source: str
) -> None:
    """Positive control for the tree scan, not just the line matcher."""
    scratch = tmp_path / "mcp_remote_control"
    (scratch / "core").mkdir(parents=True)
    (scratch / "core" / "ops.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "SRC", scratch)
    assert _attribute_reads("screen_cols"), source


def test_scan_ignores_a_profile_scoped_table(tmp_path: Path, monkeypatch) -> None:
    """Negative control for the receiver scoping: the profile seed must not
    be reported, or the guard fails on the tree it is written against."""
    scratch = tmp_path / "mcp_remote_control"
    (scratch / "screen").mkdir(parents=True)
    (scratch / "screen" / "geometry.py").write_text(
        'cols = profile_defaults.get("screen_cols", 120)\n', encoding="utf-8"
    )
    monkeypatch.setattr(sys.modules[__name__], "SRC", scratch)
    assert _attribute_reads("screen_cols") == []


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission bits gate reads only for a non-root POSIX user",
)
def test_unreadable_notes_reports_unreadable_not_write(tmp_path: Path) -> None:
    """A read that could not open the file must not report a write failure."""
    ensure_home_layout(tmp_path)
    put_profile(tmp_path, name="lab", transport="local")
    notes_file = tmp_path / "notes" / "lab.md"
    notes_file.write_text("hello\n", encoding="utf-8")
    os.chmod(notes_file, 0)
    try:
        res = config_ops.run("notes", action="read", name="lab", home=str(tmp_path))
        # stat needs no read permission: the file is there, only unreadable.
        stat_res = config_ops.run(
            "notes", action="stat", name="lab", home=str(tmp_path)
        )
    finally:
        os.chmod(notes_file, 0o600)

    assert res.status == "error"
    assert res.code == "NOTES_UNREADABLE"
    assert stat_res.status == "ok"

    msg = str(res.fields.get("msg", ""))
    assert "notes/lab.md" in msg
    # The absolute config-home path must not be echoed back to the agent.
    assert str(tmp_path) not in msg

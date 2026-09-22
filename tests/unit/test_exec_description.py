"""The WinRM mojibake pointer in the ``exec`` / ``ps`` tool descriptions.

A child process that writes UTF-8 (``pwsh 7`` is the common shape) has its
stdout bytes decoded by the Windows PowerShell 5.1 PSRP runspace's console
code page before any of this project's code sees them, and what pypsrp hands
back is an already-decoded ``str`` that reads like ordinary text. No result
field can therefore report the loss, so the tool description is the only
surface an agent reads *before* acting on such output, and it must name a
recovery route that actually exists.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from mcp_remote_control.core.fs_ops import VALID_OPS as FS_OPS
from mcp_remote_control.mcp_server import (
    _WINRM_UTF8_MOJIBAKE_CLAUSE,
    create_server,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"

# The two WinRM PowerShell surfaces share the runspace boundary the clause
# describes: ``exec`` runs a oneshot pipeline in it, ``ps invoke`` a persistent
# one. Both were measured returning the mojibake with no other signal.
_MOJIBAKE_SURFACES = ("exec", "ps")


def _tools() -> dict[str, object]:
    async def _list() -> dict[str, object]:
        mcp = create_server()
        return {t.name: t for t in await mcp.list_tools()}

    return asyncio.run(_list())


def _input_properties(tool: object) -> set[str]:
    # SDK v2 moved ``inputSchema`` to ``input_schema``; accept either so the
    # pin survives an SDK bump.
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
    if not isinstance(schema, dict):
        return set()
    props = schema.get("properties")
    return set(props) if isinstance(props, dict) else set()


def test_exec_and_ps_descriptions_carry_the_mojibake_clause() -> None:
    tools = _tools()
    for name in _MOJIBAKE_SURFACES:
        desc = str(getattr(tools[name], "description", "") or "")
        assert _WINRM_UTF8_MOJIBAKE_CLAUSE in desc, (name, desc)


def test_clause_names_the_route_that_exists() -> None:
    """The clause must not invent its escape hatch.

    It tells the agent to capture with a ``cmd /c`` redirect and read the file
    back with ``fs read`` / ``fs get``; those ops and the arguments they need
    (``path`` for both, ``local`` for ``get``) have to be on the fs surface.
    """
    clause = _WINRM_UTF8_MOJIBAKE_CLAUSE
    assert "cmd /c" in clause or "cmd/c" in clause.lower()
    fs_tool = _tools()["fs"]
    props = _input_properties(fs_tool)
    assert {"path", "local"} <= props, props
    fs_desc = str(getattr(fs_tool, "description", "") or "")
    for op in ("read", "get"):
        assert op in FS_OPS, op
        assert op in fs_desc, (op, fs_desc)


def test_recipe_arguments_exist_on_the_tools_it_names() -> None:
    """The README recipe the description points at must be paste-able.

    Its lines are ``exec ep=... command='cmd /c "..."'`` and ``fs op=read|get
    ep=... path=... [local=...]``, so those argument names have to be on the tool
    schemas an operator would call.
    """
    tools = _tools()
    exec_props = _input_properties(tools["exec"])
    assert {"ep", "command"} <= exec_props, exec_props
    fs_props = _input_properties(tools["fs"])
    assert {"op", "path", "local"} <= fs_props, fs_props


def test_clause_promises_no_detection() -> None:
    """The corruption is not observable from the received string.

    The same text can come from a UTF-8 child mis-decoded by the runspace code
    page and from a peer that emits those characters on purpose, so the clause
    must offer a route out and must not claim the tool can flag the condition.
    """
    lower = _WINRM_UTF8_MOJIBAKE_CLAUSE.lower()
    for claim in ("detect", "warn", "hint", "diagnos"):
        assert claim not in lower, (claim, _WINRM_UTF8_MOJIBAKE_CLAUSE)


def test_readme_documents_the_route_and_the_measured_dead_ends() -> None:
    """The description points at the README; the pointer must resolve.

    Every claim the note makes was measured live against a Windows host: the
    mojibake signature, the cmd-redirect capture, the two read-back routes, and
    the non-remedies an operator would otherwise try first.
    """
    text = README.read_text(encoding="utf-8")
    assert "### `[winrm]` \u914d\u7f6e" in text
    for token in (
        "\u74ba\u5ba0\u7e43",  # the incident signature: UTF-8 bytes decoded as cp936
        "cmd /c",  # the redirect that keeps the bytes intact
        "fs get",  # byte-preserving fetch
        "\u53e5\u67c4\u65e0\u6548",  # SSH-style prologue, which a PSRP runspace rejects
        "$OutputEncoding",  # ineffective
        "chcp 65001",  # ineffective
    ):
        assert token in text, token

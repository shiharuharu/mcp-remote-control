"""MCP server surface - thin shells over Core ops.

Handler path only: parse arguments -> call Core -> map :class:`OpResult`
to MCP ``CallToolResult``. No business logic lives here.

MCP tools/call result shape used here:

- ``content``: Agent-track text; a successful image read appends an
  ``image`` block (Base64 lives there, never in the text)
- ``isError`` / ``is_error``: ``not OpResult.is_ok()`` (``ok`` and
  ``unchanged`` are success)
- ``structuredContent`` / ``structured_content``: omitted for Agent-track tools

``MCPServer`` defaults for ``-> str`` invent ``output_schema {result: string}``
and wrap as structured content. We force unstructured output
(``structured_output=False``) so hosts do not wrap Agent text in a JSON
``{"result":...}`` shell. Handlers return ``CallToolResult`` so Core
failures set the protocol error flag.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import CallToolResult, TextContent

from mcp_remote_control.core import (
    config_ops,
    console_ops,
    endpoint_ops,
    exec_ops,
    fs_ops,
    ps_ops,
    screen_ops,
)
from mcp_remote_control.core.result import OpResult, render_result

# Host tools + serial console + self-config (agent manages MRC_HOME).
TOOL_NAMES: tuple[str, ...] = (
    "endpoint",
    "exec",
    "fs",
    "screen",
    "ps",
    "console",
    "config",
)

# Agent-track text only - no ``{"result": ...}`` structured wrap.
# Pass as an explicit kwarg (not ``**dict``) so type checkers bind
# ``structured_output: bool`` correctly on MCPServer.tool.
_STRUCTURED_OUTPUT_AGENT = False

# One clause for both WinRM PowerShell surfaces (``exec`` and ``ps invoke``,
# which share the PSRP runspace boundary): the runspace decodes a child's
# stdout bytes with its console code page before any of our code sees them, and
# what we receive is already a plausible-looking ``str``, so no result field
# can report the loss. The description is the only surface an agent reads
# before acting, so each one names the documented route out. Shared wording
# keeps the two surfaces from drifting apart.
_WINRM_UTF8_MOJIBAKE_CLAUSE = (
    "WinRM silently mojibakes a child's UTF-8 stdout (the 5.1 runspace decodes "
    "it with its console code page): capture it with a cmd /c redirect, then "
    "fs read/fs get (README has the measured remedies)."
)


def _image_format(mime: object) -> str:
    """SDK Image.format is the subtype; JPEG is ``jpeg``, not ``jpg``."""
    if not isinstance(mime, str) or "/" not in mime:
        return "png"
    subtype = mime.split("/", 1)[1].lower()
    if subtype in {"jpg", "jpeg"}:
        return "jpeg"
    return subtype or "png"


def _to_call_tool_result(result: OpResult) -> CallToolResult:
    """Map Core success onto MCP ``is_error``; Agent text is the content.

    Hosts that only inspect the protocol error flag otherwise treat Core
    failures as successful tool calls. ``ok`` and ``unchanged`` are success.
    Image bytes are attached only on success; errors and truncated reads
    stay text-only. The text block never carries the Base64 body.
    """
    content: list[Any] = [TextContent(type="text", text=render_result(result))]
    data = result.image_data
    if result.is_ok() and data:
        fmt = _image_format(result.fields.get("mime_type"))
        content.append(Image(data=data, format=fmt).to_image_content())
    return CallToolResult(
        content=content,
        is_error=not result.is_ok(),
    )


def tool_names() -> list[str]:
    """Return the exact registered MCP tool names (pure; no stdio server)."""
    return list(TOOL_NAMES)


def create_server(*, name: str = "mcp-remote-control") -> MCPServer[Any]:
    """Build MCPServer with host tools, console, and config."""
    mcp = MCPServer(
        name,
        instructions=(
            "mcp-remote-control tools: "
            "endpoint|exec|fs|screen|ps (hosts); "
            "console (serial, not PTY); "
            "config (ONLY way to manage profiles/secrets/notes \u2014 do NOT shell cat/ls/edit "
            "~/.config or MRC_HOME files). "
            "Bootstrap: config ensure_home \u2192 put_profile with "
            'auth={"method":"password","password":"<plain>"} '
            'ssh={"known_hosts":"none"} \u2192 endpoint open. '
            "put_secret is optional (SSH keys / password_path / cert PEMs). "
            "If unsure about auth JSON (ssh_agent, password_env, certificate, "
            "CredSSP/credssp, keys), config op=help. "
            "Hosts: endpoint open \u2192 exec/fs/screen/ps with ep=. "
            "Console: list \u2192 open path=<device> \u2192 send/views \u2192 close. "
            "Secret bodies only via put_secret; never returned by reads. "
            "Host notes: config op=notes action=read|write|append|prepend|stat|rm "
            "(body only on read; write/append/prepend need an existing profile)."
        ),
    )
    register_tools(mcp)
    return mcp


def register_tools(mcp: MCPServer[Any]) -> None:
    """Register MCP tools (idempotent per instance)."""

    @mcp.tool(
        name="endpoint",
        description=(
            "Host endpoint lifecycle only: op=list|open|close. "
            "open uses profile=; close uses ep=. "
            "Not for serial ports \u2014 use the console tool."
        ),
        structured_output=_STRUCTURED_OUTPUT_AGENT,
    )
    def endpoint(
        op: str = "list",
        profile: str | None = None,
        ep: str | None = None,
    ) -> CallToolResult:
        result = endpoint_ops.run(op=op, profile=profile, ep=ep)
        return _to_call_tool_result(result)

    @mcp.tool(
        name="exec",
        description=(
            "Remote non-interactive exec on ep=. "
            "Provide command= or argv= or script=/script_path=; "
            "optional runtime=, script_args=, cwd=, timeout=. "
            "script_args bind as the script body's positional parameters \u2014 "
            "the same contract on every runtime (bash $1.., pwsh $args); a "
            "bare program name in script= therefore runs without them, while a "
            ".cmd/.bat path binds them as %1 on cmd. Passing script_args (or "
            "runtime=) alongside command=/argv= is INVALID_ARG: to run a "
            "program with arguments use argv= or put it in command=. "
            "timeout= is local wait wall-clock (not a remote kill guarantee); "
            "omit/None = unlimited; timeout=0 is INVALID_ARG; "
            "WinRM cannot guarantee the remote pipeline stops immediately; "
            "repeated timeouts \u2192 endpoint close then open. "
            f"{_WINRM_UTF8_MOJIBAKE_CLAUSE}"
        ),
        structured_output=_STRUCTURED_OUTPUT_AGENT,
    )
    def exec_tool(
        ep: str | None = None,
        command: str | None = None,
        argv: list[str] | None = None,
        script: str | None = None,
        script_path: str | None = None,
        runtime: str | None = None,
        script_args: list[str] | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> CallToolResult:
        result = exec_ops.run(
            ep=ep,
            command=command,
            argv=argv,
            script=script,
            script_path=script_path,
            runtime=runtime,
            script_args=script_args,
            cwd=cwd,
            timeout=timeout,
        )
        return _to_call_tool_result(result)

    @mcp.tool(
        name="fs",
        description=(
            "Filesystem on ep=: op=list|stat|read|write|put|get|mkdir|rm; "
            "path= absolute preferred; optional max_bytes= for read budget "
            "(default 1 MiB). "
            "read of a complete PNG/JPEG/GIF/WebP returns an image content "
            "block (truncated images error). "
            "WinRM fs uses oneshot shells: any timeout is local wait wall-clock "
            "and cannot guarantee remote cancel; "
            "repeated timeouts \u2192 endpoint close then open."
        ),
        structured_output=_STRUCTURED_OUTPUT_AGENT,
    )
    def fs(
        op: str,
        ep: str | None = None,
        path: str | None = None,
        content: str | None = None,
        local: str | None = None,
        recursive: bool | None = None,
        max_bytes: int | None = None,
    ) -> CallToolResult:
        result = fs_ops.run(
            op=op,
            ep=ep,
            path=path,
            content=content,
            local=local,
            recursive=recursive,
            max_bytes=max_bytes,
        )
        return _to_call_tool_result(result)

    @mcp.tool(
        name="screen",
        description=(
            "Interactive PTY screen on host ep=: op=open|send|close|list. "
            "Loop: open \u2192 frame \u2192 send(actions) \u2192 frame \u2192 close. "
            "open: ep= required; optional cwd=, cols=, rows=, shell=. "
            "send actions (type=): text|key|keys|go|click|move|to_text|submit|"
            "clear_line|interrupt|eof|escape|resize|wait|paste|raw|nop. "
            "Not for serial hardware \u2014 use the console tool."
        ),
        structured_output=_STRUCTURED_OUTPUT_AGENT,
    )
    def screen(
        op: str,
        ep: str | None = None,
        id: str | None = None,
        actions: list[dict[str, Any]] | None = None,
        wait: dict[str, Any] | None = None,
        shot: bool | None = None,
        cwd: str | None = None,
        cols: int | None = None,
        rows: int | None = None,
        shell: str | None = None,
    ) -> CallToolResult:
        result = screen_ops.run(
            op=op,
            ep=ep,
            id=id,
            actions=actions,
            wait=wait,
            shot=shot,
            cwd=cwd,
            cols=cols,
            rows=rows,
            shell=shell,
        )
        return _to_call_tool_result(result)

    @mcp.tool(
        name="ps",
        description=(
            "Persistent PowerShell runspace (WinRM): op=open|invoke|close. "
            "open needs ep=; invoke/close need id=; invoke uses script=. "
            "invoke optional timeout= seconds (local wait wall-clock; "
            "stops the local wait / best-effort pipeline stop; "
            "omit/None = unlimited; timeout=0 is INVALID_ARG). "
            "WinRM cannot guarantee remote cancel; "
            "repeated timeouts \u2192 endpoint close then open (close+reopen). "
            f"{_WINRM_UTF8_MOJIBAKE_CLAUSE}"
        ),
        structured_output=_STRUCTURED_OUTPUT_AGENT,
    )
    def ps(
        op: str,
        ep: str | None = None,
        id: str | None = None,
        script: str | None = None,
        timeout: float | None = None,
    ) -> CallToolResult:
        result = ps_ops.run(
            op=op, ep=ep, id=id, script=script, timeout=timeout
        )
        return _to_call_tool_result(result)

    @mcp.tool(
        name="console",
        description=(
            "Serial Console (embedded device link on this host; not PTY/SSH). "
            "op=list|open|send|views|close|sessions. "
            "list: system device= names (do not scan /dev or drivers). "
            "open: path= or device= from list, optional baud=, max_lines= "
            "(default huge buffer; background capture starts immediately), "
            "encoding= (peer text codec when the console is not UTF-8, e.g. "
            "gb18030; unknown names warn and keep utf-8). "
            "send: id= + data= or data_b64=, optional newline=. "
            "views: id= + mode=tail|since|contains; n=; since=; contains=; "
            "context=; settle_ms=; with_seq=; queries capture buffer (not driver recv). "
            "close/sessions: id= / list open sessions."
        ),
        structured_output=_STRUCTURED_OUTPUT_AGENT,
    )
    def console(
        op: str = "list",
        path: str | None = None,
        device: str | None = None,
        baud: int | None = None,
        id: str | None = None,
        data: str | None = None,
        data_b64: str | None = None,
        newline: bool = False,
        mode: str | None = None,
        n: int | None = None,
        since: int | None = None,
        contains: str | None = None,
        context: int | None = None,
        settle_ms: int | None = None,
        max_lines: int | None = None,
        with_seq: bool = False,
        encoding: str | None = None,
    ) -> CallToolResult:
        result = console_ops.run(
            op=op,
            path=path or device,
            baud=baud,
            id=id,
            data=data,
            data_b64=data_b64,
            newline=newline,
            mode=mode,
            n=n,
            since=since,
            contains=contains,
            context=context,
            settle_ms=settle_ms,
            max_lines=max_lines,
            with_seq=with_seq,
            encoding=encoding,
        )
        return _to_call_tool_result(result)

    @mcp.tool(
        name="config",
        description=(
            "Complete self-config for profiles. Do NOT shell-edit config files. "
            "ops: help|home|ensure_home|get|list_profiles|get_profile|put_profile|"
            "delete_profile|put_secret(optional)|list_secrets|notes. "
            "Password is NOT private \u2014 preferred bootstrap is inline: "
            'put_profile name=lab transport=ssh host=\u2026 username=\u2026 '
            'auth={"method":"password","password":"<plain>"} '
            'ssh={"known_hosts":"none"} then endpoint open profile=lab. '
            "put_secret is optional (SSH keys / password_path / cert PEMs). "
            "WinRM: auth password=plain + winrm={scheme,auth}; optional defaults/caps. "
            "op=help recipes: password, password_env, private_key, ssh_agent, "
            "WinRM CredSSP/credssp, certificate (cert paths). "
            "op=notes action=read|write|append|prepend|stat|rm name= [content=]; "
            "body only on read; write \"\" truncates; do not use fs on config home."
        ),
        structured_output=_STRUCTURED_OUTPUT_AGENT,
    )
    def config(
        op: str = "home",
        action: str | None = None,
        name: str | None = None,
        transport: str | None = None,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        label: str | None = None,
        auth: dict[str, Any] | str | None = None,
        ssh: dict[str, Any] | str | None = None,
        winrm: dict[str, Any] | str | None = None,
        defaults: dict[str, Any] | str | None = None,
        caps: dict[str, Any] | str | None = None,
        body: str | None = None,
        content: str | None = None,
    ) -> CallToolResult:
        result = config_ops.run(
            op=op,
            action=action,
            name=name,
            transport=transport,
            host=host,
            port=port,
            username=username,
            label=label,
            auth=auth,
            ssh=ssh,
            winrm=winrm,
            defaults=defaults,
            caps=caps,
            body=body,
            content=content,
        )
        return _to_call_tool_result(result)


def main() -> None:
    """stdio MCP entry (optional; hosts may import create_server)."""
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()

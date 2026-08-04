"""Shared structured result type for Core ops → MCP/CLI render.

Core never imports MCP. Surfaces only parse args, call Core, and render
:class:`OpResult` to Agent semantic text or compact JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mcp_remote_control.render import render_agent_text, render_json


@dataclass
class OpResult:
    """Structured Core result for dual-track render (Agent text or JSON).

    MCP and CLI must only parse arguments, call Core, and render this object.
    """

    kind: str
    status: str = "error"
    code: str | None = None
    cwd: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    body: str | None = None
    hint: str | None = None

    def is_ok(self) -> bool:
        # ``unchanged`` is a successful screen act+observe with a stable frame.
        return self.status in ("ok", "unchanged")

    def render_text(self) -> str:
        return render_agent_text(
            self.kind,
            self.status,
            cwd=self.cwd,
            code=self.code,
            fields=dict(self.fields) if self.fields else None,
            body=self.body,
            hint=self.hint,
        )

    def render_json(self) -> str:
        return render_json(
            self.kind,
            self.status,
            cwd=self.cwd,
            code=self.code,
            fields=dict(self.fields) if self.fields else None,
            body=self.body,
        )


def render_result(result: OpResult, *, as_json: bool = False) -> str:
    """Render *result* as Agent text (default) or machine-track JSON."""
    if as_json:
        return result.render_json()
    return result.render_text()

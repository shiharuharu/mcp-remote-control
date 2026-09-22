"""Shared structured result type for Core ops -> MCP/CLI render.

Core never imports MCP. Surfaces only parse args, call Core, and render
:class:`OpResult` to Agent semantic text or compact JSON. Optional image
file bytes live on ``OpResult.image_data`` and are never serialized by
``render_text`` / ``render_json``.

Also hosts small helpers shared by multiple core ops modules (``_home`` /
``_short``) so error truncation and home resolve stay behavior-identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_remote_control.config import resolve_home
from mcp_remote_control.render import render_agent_text, render_json


def _home(home: Path | str | None) -> Path:
    """Resolve config root for core ops (``None`` -> :func:`resolve_home`)."""
    if home is None:
        return resolve_home()
    return Path(home).expanduser().resolve()


def _short(msg: str, limit: int = 200) -> str:
    """Collapse whitespace and truncate for Agent error ``msg`` fields."""
    text = " ".join(str(msg).split())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


@dataclass
class OpResult:
    """Structured Core result for dual-track render (Agent text or JSON).

    MCP and CLI must only parse arguments, call Core, and render this object.

    ``image_data`` holds raw encoded image file bytes (not pixels). When set,
    the result is successful, ``fields["type"]`` is ``"image"``, and
    ``fields["mime_type"]`` is the recognized MIME. Error and truncated
    results must leave it ``None``. It is omitted from ``repr`` and from
    ``render_text`` / ``render_json``.
    """

    kind: str
    status: str = "error"
    code: str | None = None
    cwd: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    body: str | None = None
    hint: str | None = None
    image_data: bytes | None = field(default=None, repr=False)

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
        # ``hint`` rides the ``**extra`` channel: same redaction as the text
        # track (redact_mapping runs redact_string over the value), emitted
        # under the same key. A remedy that only the Agent track carries is
        # lost to every ``--json`` caller, so both tracks must render it.
        return render_json(
            self.kind,
            self.status,
            cwd=self.cwd,
            code=self.code,
            fields=dict(self.fields) if self.fields else None,
            body=self.body,
            hint=self.hint,
        )


def render_result(result: OpResult, *, as_json: bool = False) -> str:
    """Render *result* as Agent text (default) or machine-track JSON."""
    if as_json:
        return result.render_json()
    return result.render_text()

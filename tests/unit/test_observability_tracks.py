"""Observability tracks: JSON remedy parity, remedy truncation, attribution.

Pins the dual-track contract and the one report field that must not claim
work its call did not do:

- every populated ``OpResult`` field, ``hint`` included, reaches both the
  Agent text track and the ``--json`` machine track, with the same redaction.
- the ``msg`` field of a failed screen send carries the whole curated remedy
  from ``screen/send.py``, including the condition on its escape hatch.
- ``endpoint open`` reports ``session_resynced`` only for a recovery the call
  itself performed, never for a concurrent same-name open's.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from mcp_remote_control.core import endpoint_ops, screen_ops
from mcp_remote_control.core.result import OpResult
from mcp_remote_control.endpoint.registry import Endpoint
from mcp_remote_control.screen.registry import get_screen_registry
from mcp_remote_control.screen.send import NO_TRACKING_HINT, SendOutcome
from mcp_remote_control.screen.session import ScreenSession

# ---------------------------------------------------------------------------
# hint must ride both tracks
# ---------------------------------------------------------------------------


def test_hint_reaches_the_json_track() -> None:
    """A remedy on the Agent track is a remedy for ``--json`` callers too."""
    result = OpResult(
        kind="exec",
        status="error",
        code="MISSING_ARG",
        fields={"form": "none"},
        hint="provide command= or argv= or script=/script_path=",
    )
    payload = json.loads(result.render_json())

    assert payload["hint"] == "provide command= or argv= or script=/script_path="
    assert "provide command= or argv= or script=/script_path=" in result.render_text()


def test_hint_is_redacted_on_both_tracks() -> None:
    """The JSON track redacts a hint the same way the text track does."""
    result = OpResult(
        kind="exec",
        status="error",
        code="EXEC_FAILED",
        hint="retry with token=abc123def",
    )
    payload = json.loads(result.render_json())

    assert "abc123def" not in payload["hint"]
    assert "abc123def" not in result.render_text()


def test_the_two_tracks_carry_no_field_the_other_drops() -> None:
    """Field-by-field: every populated ``OpResult`` field is on both tracks.

    ``kind`` / ``status`` build the header, ``code`` / ``cwd`` / ``fields``
    become tokens, ``body`` is the body and ``hint`` the trailing remedy, so
    every populated field appears on both tracks. ``image_data`` is raw encoded
    image bytes for the image transport and is deliberately on neither.
    """
    result = OpResult(
        kind="screen",
        status="ok",
        code="NAV_OK",
        cwd="/tmp",
        fields={"op": "send", "id": "scr_01", "nav": "keys"},
        body="frame",
        hint="read cur= before next send",
        image_data=b"\x89PNG\r\n\x1a\n",
    )
    payload = json.loads(result.render_json())
    text = result.render_text()

    for key, value in (
        ("kind", "screen"),
        ("status", "ok"),
        ("code", "NAV_OK"),
        ("cwd", "/tmp"),
        ("body", "frame"),
        ("hint", "read cur= before next send"),
        ("op", "send"),
        ("id", "scr_01"),
        ("nav", "keys"),
    ):
        assert payload[key] == value, key
        assert str(value) in text, key
    assert "image_data" not in payload
    assert "image_data" not in text


def test_empty_hint_adds_no_key_and_no_trailing_line() -> None:
    """Absent hint stays absent on both tracks (omit-nulls contract)."""
    result = OpResult(kind="endpoint", status="ok", fields={"op": "list"})
    payload = json.loads(result.render_json())

    assert "hint" not in payload
    assert "@hint" not in result.render_text()


# ---------------------------------------------------------------------------
# the actionable no-tracking remedy must arrive whole
# ---------------------------------------------------------------------------


class _FakePty:
    """PTY double: records writes, never produces output."""

    def __init__(self, cols: int = 80, rows: int = 24, cwd: str = "/tmp") -> None:
        self.cols = cols
        self.rows = rows
        self.cwd = cwd
        self.written = bytearray()
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive

    def exit_code(self) -> int | None:
        return None if self._alive else 0

    def read(self, max_bytes: int = 8192) -> bytes:
        return b""

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows

    def drain_for(self, seconds: float, *, on_data: Any = None) -> int:
        return 0

    def close(self) -> None:
        self._alive = False


def test_no_tracking_remedy_reaches_the_caller_whole() -> None:
    """A click on a non-tracking peer delivers the remedy, not a cut version.

    ``NO_TRACKING_HINT`` names both escapes (``go how=keys`` and
    ``force_click``); the second one's condition is the tail of the text, so
    the ``msg`` cap must clear the hint behind its ``action_<i>_failed: ``
    prefix in full rather than chopping it mid-word.
    """
    pty = _FakePty()
    sess = ScreenSession(
        id="scr_no_tracking",
        ep="local",
        pty=pty,  # type: ignore[arg-type]
        cols=80,
        rows=24,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
    )
    registry = get_screen_registry()
    registry.add(sess)
    try:
        out = screen_ops.send_screen(
            id="scr_no_tracking",
            actions=[{"type": "click", "row": 1, "col": 2}],
            wait={"until": "deadline", "timeout_ms": 0},
        )
    finally:
        registry.remove("scr_no_tracking")

    assert out.status == "error"
    assert out.code == "NAV_NO_TRACKING"
    assert out.fields["nav"] == "no_tracking"
    msg = out.fields["msg"]
    assert msg == f"action_0_failed: {NO_TRACKING_HINT}"
    assert msg.endswith("force_click if this peer does decode SGR reports")
    assert "go how=keys" in msg
    rendered = out.render_text()
    assert "force_click if this peer does decode SGR reports" in rendered


def test_runaway_send_message_is_still_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Widening the send ``msg`` budget must not leave the field unbounded.

    The budget exists to stop a runaway interpolated exception repr from being
    pasted whole; it only has to clear the longest curated remedy, so an
    over-long repr is still cut.
    """
    pty = _FakePty()
    sess = ScreenSession(
        id="scr_runaway",
        ep="local",
        pty=pty,  # type: ignore[arg-type]
        cols=80,
        rows=24,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
    )
    registry = get_screen_registry()
    registry.add(sess)

    def _fake_send(*_args: Any, **_kwargs: Any) -> SendOutcome:
        return SendOutcome(
            status="error",
            frame=None,
            hash="h",
            cur="1,1",
            gen=1,
            cols=80,
            rows=24,
            alive=True,
            exit_code=None,
            error_code="EXEC_FAILED",
            error_msg="x" * 900,
        )

    monkeypatch.setattr(screen_ops, "execute_send", _fake_send)
    try:
        out = screen_ops.send_screen(
            id="scr_runaway",
            actions=[],
            wait={"until": "deadline", "timeout_ms": 0},
        )
    finally:
        registry.remove("scr_runaway")

    msg = out.fields["msg"]
    assert len(msg) == screen_ops._SEND_MSG_CHARS
    assert msg.endswith("...")


@pytest.mark.parametrize("value_len", [138, 139, 200])
def test_long_caller_value_keeps_the_remedy_whole(value_len: int) -> None:
    """A caller value must not spend the budget the remedy needs.

    ``click.force_click`` is read strictly, and an unrecognised value is
    interpolated into the refusal ahead of its curated escape-hatch advice.
    The repr of that value is unbounded, so a plain head cut spends the whole
    ``msg`` cap on it and the caller learns the field was wrong but never how
    to fix it. The lengths straddle the cap: 138 is the longest value whose
    refusal still fits ``screen_ops._SEND_MSG_CHARS`` un-truncated, 139 is the
    first that must be cut.
    """
    remedy = (
        "pass force_click=true to write the SGR report on a peer that never "
        "announced DECSET 1006, or omit it"
    )
    pty = _FakePty()
    sess = ScreenSession(
        id="scr_long_value",
        ep="local",
        pty=pty,  # type: ignore[arg-type]
        cols=80,
        rows=24,
        cwd="/tmp",
        surface="shell",
        open_mode="shell",
    )
    registry = get_screen_registry()
    registry.add(sess)
    try:
        out = screen_ops.send_screen(
            id="scr_long_value",
            actions=[
                {
                    "type": "click",
                    "row": 1,
                    "col": 2,
                    "force_click": "z" * value_len,
                }
            ],
            wait={"until": "deadline", "timeout_ms": 0},
        )
    finally:
        registry.remove("scr_long_value")

    assert out.status == "error"
    assert out.code == "INVALID_ARG"
    msg = out.fields["msg"]
    assert len(msg) <= screen_ops._SEND_MSG_CHARS
    assert msg.endswith(remedy)
    assert "click.force_click must be a boolean" in msg
    if value_len > 138:
        # Over the cap: the value, not the remedy, is the part that is elided.
        assert "z" * value_len not in msg
    else:
        # At or under the cap the refusal is delivered whole.
        assert msg == (
            "action_0_failed: click.force_click must be a boolean, "
            f"got {'z' * value_len!r}; {remedy}"
        )


def test_send_msg_bound_holds_when_the_tail_cannot_be_kept() -> None:
    """Keeping the remedy whole must never make the field unbounded.

    The trailing clause is preserved only while the diagnosis keeps at least
    half the budget; past that the plain head cut applies, so every input
    still renders within the cap.
    """
    limit = screen_ops._SEND_MSG_CHARS

    # Tail alone is over half the budget: fall back to the plain head cut.
    capped = screen_ops._send_msg(f"{'d' * 400}; {'r' * (limit // 2 + 10)}")
    assert len(capped) == limit
    assert capped.startswith("d" * 40)
    assert capped.endswith("...")

    # A remedy that fits is kept whole; the diagnosis is what gets cut.
    kept_tail = "pass force_click=true, or omit it"
    capped = screen_ops._send_msg(f"{'d' * 400}; {kept_tail}")
    assert len(capped) == limit
    assert capped.startswith("d" * 40)
    assert capped.endswith(kept_tail)

    assert screen_ops._send_msg("short; remedy") == "short; remedy"


# ---------------------------------------------------------------------------
# session_resynced attribution under a same-name open race
# ---------------------------------------------------------------------------


class _Fence:
    """Per-name lock with an owner trace (stands in for the registry's RLock)."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.holder: int | None = None

    def try_enter(self) -> bool:
        """Take the fence only if it is free *right now*; never blocks.

        A blocking acquire cannot tell "the fence is held by the call under
        test" from "this thread has not been scheduled yet". The negative
        direction of the race test turns on exactly that difference: an
        unfree fence is proof the window is guarded, while a slow thread would
        merely look like one.
        """
        if not self.lock.acquire(blocking=False):
            return False
        self.holder = threading.get_ident()
        return True

    def leave(self) -> None:
        self.holder = None
        self.lock.release()

    def __enter__(self) -> "_Fence":
        self.lock.acquire()
        self.holder = threading.get_ident()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.leave()
        return False


class _StubTransport:
    """Transport double reporting liveness and a sticky recovery marker."""

    def __init__(self, *, live: bool, meta: dict[str, Any] | None = None) -> None:
        self._live = live
        self.meta = dict(meta or {})

    def is_connected(self) -> bool:
        return self._live


def _endpoint(transport: _StubTransport) -> Endpoint:
    return Endpoint(
        name="lab-win",
        transport_name="winrm",
        caps={},
        connected=True,
        transport=transport,  # type: ignore[arg-type]
    )


class _RacingRegistry:
    """Registry double shaped like the real one: same-name open is fenced.

    ``get`` runs exactly what a concurrent same-name open does - pop the dead
    handle and register a live, already-resynced replacement - on another
    thread, and returns only once that thread has settled. The rival claims
    the name fence with a **non-blocking** acquire, so where it lands is
    decided by whether this call holds the fence, never by thread scheduling:
    an unguarded window lets it swap before ``get`` returns, a guarded one
    cannot let it in at all.
    """

    def __init__(
        self, dead: Endpoint, replacement: Endpoint, own: Endpoint
    ) -> None:
        self._lock = threading.RLock()
        self._fences: dict[str, _Fence] = {}
        self._ep = dead
        self._replacement = replacement
        self._own = own
        self.verbatim = 0  # Phase-1 pass-throughs: no reconnect by this call
        self.created = 0  # handles this call's open established itself
        self.settled = threading.Event()
        self._race_armed = True

    def _get_or_create_name_lock(self, name: str) -> _Fence:
        """Same contract as ``EndpointRegistry``: caller holds ``_lock``."""
        return self._fence_unguarded(name)

    def _fence_unguarded(self, name: str) -> _Fence:
        fence = self._fences.get(name)
        if fence is None:
            fence = self._fences[name] = _Fence()
        return fence

    def _fence(self, name: str) -> _Fence:
        with self._lock:
            return self._fence_unguarded(name)

    def get(self, name: str) -> Endpoint | None:
        ep = self._ep
        if self._race_armed:
            self._race_armed = False
            threading.Thread(
                target=self._concurrent_open, daemon=True
            ).start()
            # Settled means the rival has resolved its claim on the fence:
            # either it swapped (guard absent) or it could not enter. Waiting
            # on that, rather than on a timer, is what makes the assertion
            # below a statement about the guard instead of about scheduling.
            if not self.settled.wait(timeout=30):
                raise AssertionError("rival open never reached the name fence")
        return ep

    def open(self, name: str, **_kwargs: Any) -> Endpoint:
        with self._fence(name):
            current = self._ep
            transport = current.transport
            if transport is not None and transport.is_connected():
                self.verbatim += 1
                return current
            self.created += 1
            self._ep = self._own
            return self._own

    def close_if_same(self, name: str, handle: Endpoint | None) -> None:
        return None

    def _concurrent_open(self) -> None:
        try:
            fence = self._fence("lab-win")
            if fence.try_enter():
                try:
                    self._ep = self._replacement
                finally:
                    fence.leave()
        finally:
            self.settled.set()


def test_concurrent_replacement_is_not_credited_to_this_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``session_resynced`` is reported only for a recovery this call performed.

    The rival open replaces the dead handle with a live, already-resynced one.
    Unguarded, it lands between this call's snapshot and its open, which then
    hands back the rival's handle verbatim - no reconnect here - while the
    sticky marker still credits the recovery to this call.
    """
    dead = _endpoint(_StubTransport(live=False))
    replacement = _endpoint(
        _StubTransport(live=True, meta={"session_resynced": True})
    )
    own = _endpoint(_StubTransport(live=True, meta={"session_resynced": True}))
    registry = _RacingRegistry(dead, replacement, own)
    monkeypatch.setattr(endpoint_ops, "get_registry", lambda: registry)

    result = endpoint_ops.open_endpoint(profile="lab-win", home=tmp_path)

    assert result.status == "ok"
    assert result.fields["session_resynced"] == 1
    # True only because the window is fenced: the recovery is this call's.
    assert registry.created == 1
    assert registry.verbatim == 0

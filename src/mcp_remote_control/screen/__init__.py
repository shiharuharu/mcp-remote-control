"""Remote / local interactive PTY screen sessions.

Public surface: registry, session, send actions, geometry adapter, key
encoder, buffer helpers, and fixture replay.
"""

from __future__ import annotations

from mcp_remote_control.screen.actions import action_truthy, actions_include_submit
from mcp_remote_control.screen.buffer import (
    SEED_SHELL_COLS,
    SEED_SHELL_ROWS,
    dump_frame,
    format_cur,
    frame_hash,
)
from mcp_remote_control.screen.geometry import (
    GeometryAdapter,
    GeometryMemory,
    classify_command,
    seed_for_class,
)
from mcp_remote_control.screen.keys import (
    encode_key,
    encode_keys,
    encode_paste,
    encode_text,
)
from mcp_remote_control.screen.registry import (
    ScreenRegistry,
    get_screen_registry,
    reset_screen_registry,
)
from mcp_remote_control.screen.replay import replay_ansi, replay_fixture
from mcp_remote_control.screen.send import ACTION_TYPES, execute_send
from mcp_remote_control.screen.session import ScreenSession, default_shell_geometry

__all__ = [
    "ACTION_TYPES",
    "SEED_SHELL_COLS",
    "SEED_SHELL_ROWS",
    "GeometryAdapter",
    "GeometryMemory",
    "ScreenRegistry",
    "ScreenSession",
    "action_truthy",
    "actions_include_submit",
    "classify_command",
    "default_shell_geometry",
    "dump_frame",
    "encode_key",
    "encode_keys",
    "encode_paste",
    "encode_text",
    "execute_send",
    "format_cur",
    "frame_hash",
    "get_screen_registry",
    "replay_ansi",
    "replay_fixture",
    "reset_screen_registry",
    "seed_for_class",
]

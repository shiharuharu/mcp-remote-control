"""Unit tests for SFTP remote path helpers (Windows OpenSSH)."""

from __future__ import annotations

from mcp_remote_control.fs.remote_path import (
    is_abs_remote,
    is_root_remote,
    remote_join,
    remote_parent,
)


def test_windows_parent() -> None:
    assert remote_parent(r"C:\Users\a\file.txt") == r"C:\Users\a"
    # Parent of C:\Users is the drive root form C:\
    parent_users = remote_parent(r"C:\Users")
    assert parent_users in ("C:\\", "C:")
    # drive root has no parent
    assert remote_parent("C:\\") is None
    assert remote_parent("C:") is None


def test_posix_parent() -> None:
    assert remote_parent("/tmp/a/b") == "/tmp/a"
    assert remote_parent("/tmp") == "/"
    assert remote_parent("/") is None


def test_is_abs() -> None:
    assert is_abs_remote("/tmp")
    assert is_abs_remote(r"C:\Windows")
    assert not is_abs_remote("rel/path")


def test_join_windows_style() -> None:
    j = remote_join(r"C:\Users", "a")
    assert "a" in j


def test_unc_is_root() -> None:
    # UNC share root is a root (no operable parent).
    assert is_root_remote(r"\\srv\share") is True
    # Forward-slash UNC form too.
    assert is_root_remote("//srv/share") is True
    # Deeper UNC paths are not roots.
    assert is_root_remote(r"\\srv\share\path") is False
    # Server-only (no share) is treated as a root so callers never climb
    # above the share into invalid territory.
    assert is_root_remote(r"\\srv") is True
    # Existing roots still hold.
    assert is_root_remote("/") is True
    assert is_root_remote("") is True
    assert is_root_remote("C:") is True
    assert is_root_remote("C:\\") is True


def test_unc_parent() -> None:
    # Share root has no parent.
    assert remote_parent(r"\\srv\share") is None
    assert remote_parent("//srv/share") is None
    # Climb toward the share root, never above it.
    assert remote_parent(r"\\srv\share\path") == r"\\srv\share"
    assert remote_parent(r"\\srv\share\a\b") == r"\\srv\share\a"
    assert remote_parent(r"\\srv\share\dir") == r"\\srv\share"
    # Forward-slash form climbs back to a backslash share root.
    assert remote_parent("//srv/share/path") == r"\\srv\share"

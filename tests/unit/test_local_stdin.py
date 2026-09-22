"""Local exec must not inherit the MRC process stdin; local read must not
block on special files.

The MCP server runs over stdio, so this process's fd 0 can be the JSON-RPC
request stream. Exec children therefore get ``DEVNULL`` on stdin, and
``LocalFs.read`` refuses non-regular files (FIFO/socket/device) instead of
blocking in the kernel, since no fs operation carries a wall-clock budget.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from mcp_remote_control.fs.backends.local import LocalFs
from mcp_remote_control.fs.types import FsError, ReadResult

SRC_DIR = Path(__file__).resolve().parents[2] / "src"

# Stand-in for a pending MCP request line on fd 0.
MCP_BYTES = b'{"jsonrpc":"2.0","id":1,"method":"tools/call"}\n'

# Runs inside a child process whose stdin is the fake MCP stream. Reports the
# command's stdout and what is left of the stream for the "server" to read.
_CHILD_SRC = """
import sys
sys.path.insert(0, {src!r})
from mcp_remote_control.transport import LocalTransport

mode = sys.argv[1]
t = LocalTransport()
t.connect()
if mode == "shell":
    r = t.run_command("head -c 20", timeout_s=5)
else:
    r = t.run_argv(["/bin/cat"], timeout_s=5)
print("exit:", r.exit_code, flush=True)
print("cmd-stdout:", repr(r.stdout), flush=True)
print("mcp-leftover:", repr(sys.stdin.buffer.read()), flush=True)
"""


def _run_in_mcp_child(mode: str) -> dict[str, str]:
    """Run the transport inside a child whose stdin carries the MCP stream."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC_DIR), env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_SRC.format(src=str(SRC_DIR)), mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    out, err = proc.communicate(input=MCP_BYTES, timeout=30)
    assert proc.returncode == 0, err.decode(errors="replace")
    fields: dict[str, str] = {}
    for line in out.decode(errors="replace").splitlines():
        key, _, value = line.partition(": ")
        fields[key.strip()] = value
    return fields


@pytest.mark.skipif(os.name != "posix", reason="uses a POSIX shell command")
def test_shell_command_does_not_consume_mcp_stdin() -> None:
    """A stdin-reading shell command must see EOF, not the JSON-RPC stream."""
    fields = _run_in_mcp_child("shell")
    assert fields["exit"] == "0"
    assert fields["cmd-stdout"] == "''"
    assert fields["mcp-leftover"] == repr(MCP_BYTES)


@pytest.mark.skipif(os.name != "posix", reason="uses /bin/cat")
def test_argv_command_does_not_consume_mcp_stdin() -> None:
    """The argv form detaches stdin the same way the shell form does."""
    fields = _run_in_mcp_child("argv")
    assert fields["exit"] == "0"
    assert fields["cmd-stdout"] == "''"
    assert fields["mcp-leftover"] == repr(MCP_BYTES)


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO")
def test_read_fifo_returns_error_without_blocking(tmp_path: Path) -> None:
    """A writer-less FIFO must fail fast, not park the calling thread."""
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    outcome = _read_in_thread(fifo)
    assert isinstance(outcome, FsError), outcome
    assert outcome.code == "NOT_A_FILE"


def _read_in_thread(path: Path) -> object:
    """Call ``read`` on a worker thread so a regression fails, not hangs."""
    box: list[object] = []

    def worker() -> None:
        try:
            box.append(LocalFs().read(str(path)))
        except FsError as exc:
            box.append(exc)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    th.join(10.0)
    assert not th.is_alive(), "LocalFs.read blocked indefinitely"
    assert len(box) == 1, box
    return box[0]


def test_read_regular_file_still_works(tmp_path: Path) -> None:
    f = tmp_path / "plain.txt"
    f.write_text("hello-local", encoding="utf-8")
    result = LocalFs().read(str(f))
    assert isinstance(result, ReadResult)
    assert result.data == b"hello-local"


def test_read_directory_and_missing_still_rejected(tmp_path: Path) -> None:
    """Neighbouring path checks keep their codes after the regular-file gate."""
    with pytest.raises(FsError) as dir_exc:
        LocalFs().read(str(tmp_path))
    assert dir_exc.value.code == "IS_A_DIR"

    with pytest.raises(FsError) as missing_exc:
        LocalFs().read(str(tmp_path / "nope"))
    assert missing_exc.value.code == "NOT_FOUND"

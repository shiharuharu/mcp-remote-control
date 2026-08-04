"""SSH connector kwargs: password / passphrase / known_hosts / keepalive."""

from __future__ import annotations

from mcp_remote_control.transport.ssh import SSHTransport


def test_connector_receives_password_and_passphrase() -> None:
    seen: dict[str, object] = {}

    def connector(**kwargs: object) -> object:
        seen.update(kwargs)

        class Conn:
            def run_command(self, command: str, **_k: object) -> object:
                from mcp_remote_control.transport.base import ExecResult

                return ExecResult(exit_code=0, stdout="ok", stderr="")

        return Conn()

    t = SSHTransport(
        host="h",
        username="u",
        password="secret-pass",
        passphrase="key-phrase",
        keepalive_interval_s=30,
        known_hosts=(),
        connector=connector,
    )
    t.connect()
    assert seen.get("password") == "secret-pass"
    assert seen.get("passphrase") == "key-phrase"
    assert seen.get("keepalive_interval") == 30.0
    assert seen.get("known_hosts") == ()
    t.close()


def test_text_encoding_decode_path() -> None:
    gbk_hello = "你好".encode("gbk")

    class Conn:
        def run_command(self, command: str, **_k: object) -> object:
            from mcp_remote_control.transport.base import ExecResult

            return ExecResult(exit_code=0, stdout=gbk_hello, stderr=b"")  # type: ignore[arg-type]

    def connector(**_k: object) -> object:
        return Conn()

    t = SSHTransport(
        host="h",
        username="u",
        text_encoding="gb18030",
        connector=connector,
    )
    t.connect()
    # Coercion path via mock run_command returns ExecResult with bytes — 
    # _coerce may not re-decode ExecResult.stdout if already ExecResult.
    # Exercise _decode_stream via collect when returning raw process.
    from mcp_remote_control.transport.ssh import _decode_stream

    assert _decode_stream(gbk_hello, "gb18030") == "你好"
    t.close()

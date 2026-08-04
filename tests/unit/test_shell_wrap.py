"""Unit tests for remote shell wrap + probe parse (Windows OpenSSH)."""

from __future__ import annotations

from mcp_remote_control.transport.shell_wrap import (
    coerce_cwd_path,
    normalize_shell_family,
    parse_probe_output,
    wrap_with_cwd,
)


def test_wrap_posix() -> None:
    out = wrap_with_cwd("echo hi", "/tmp/work", shell_family="posix")
    assert out.startswith("cd ")
    assert "&&" in out
    assert "echo hi" in out


def test_wrap_cmd() -> None:
    out = wrap_with_cwd("dir", r"C:\Users\a", shell_family="cmd")
    assert out.startswith('cd /d "C:\\Users\\a"')
    assert " & " in out
    assert out.endswith("dir")


def test_wrap_powershell() -> None:
    out = wrap_with_cwd("Get-ChildItem", r"C:\Users\a", shell_family="powershell")
    assert "Set-Location -LiteralPath 'C:\\Users\\a'" in out
    assert "Get-ChildItem" in out


def test_wrap_rejects_bool_cwd_true() -> None:
    """Regression: probe cap_pwd must never produce `cd True`."""
    assert wrap_with_cwd("whoami", True, shell_family="posix") == "whoami"  # type: ignore[arg-type]
    assert wrap_with_cwd("whoami", "True", shell_family="posix") == "whoami"
    assert wrap_with_cwd("whoami", False, shell_family="cmd") == "whoami"  # type: ignore[arg-type]
    assert coerce_cwd_path(True) is None
    assert coerce_cwd_path("True") is None
    assert coerce_cwd_path("/home/u") == "/home/u"


def test_normalize_shell_family() -> None:
    assert normalize_shell_family("cmd.exe") == "cmd"
    assert normalize_shell_family("pwsh") == "powershell"
    assert normalize_shell_family("bash") == "posix"


def test_parse_probe_posix() -> None:
    text = "uname=Linux-x86_64\nshell_path=/bin/bash\ncharmap=UTF-8\nhome=/home/u\npwd=/tmp\nos=posix\n"
    d = parse_probe_output(text)
    assert d.get("os") == "posix"
    assert d.get("shell_base") == "bash"
    assert d.get("charmap") == "UTF-8"
    assert d.get("dialect") == "posix-bash"
    assert d.get("shell_family") == "posix"
    assert d.get("pwd") == "/tmp"


def test_parse_probe_pwd_not_overwritten_by_cap_pwd() -> None:
    """cap_pwd=1 must not clobber path field pwd=/real/path → cwd=True bug."""
    text = (
        "uname=Linux-x86_64\n"
        "shell_path=/bin/zsh\n"
        "home=/home/lab\n"
        "pwd=/home/lab\n"
        "cap_pwd=1\n"
        "cap_pwd_p=1\n"
        "cap_printf=1\n"
        "os=posix\n"
    )
    d = parse_probe_output(text)
    assert d.get("pwd") == "/home/lab"
    assert d.get("pwd") is not True
    assert d.get("cap_pwd") is True
    assert d.get("cap_pwd_p") is True
    caps = d.get("caps")
    assert isinstance(caps, dict)
    assert caps.get("pwd") is True
    assert caps.get("pwd_p") is True
    # Downstream seed path used by SSHTransport.collect_probe
    assert coerce_cwd_path(d.get("pwd")) == "/home/lab"


def test_parse_probe_busybox() -> None:
    text = (
        "uname=Linux-x86_64\n"
        "shell_path=/bin/sh\n"
        "pwd=/root\n"
        "busybox=/bin/busybox\n"
        "busybox_banner=BusyBox v1.36.1 (2024) multi-call binary.\n"
        "sh_link=busybox\n"
        "cap_pwd=1\n"
        "cap_pwd_p=0\n"
        "os=posix\n"
    )
    d = parse_probe_output(text)
    assert d.get("shell_base") == "sh"
    assert d.get("dialect") == "posix-busybox"
    assert d.get("pwd") == "/root"
    caps = d.get("caps")
    assert isinstance(caps, dict)
    assert caps.get("busybox") is True
    assert caps.get("pwd") is True
    assert caps.get("pwd_p") is False


def test_parse_probe_plain_sh() -> None:
    text = "uname=Linux-x86_64\nshell_path=/usr/bin/dash\nos=posix\nbusybox=\n"
    d = parse_probe_output(text)
    assert d.get("shell_base") == "dash"
    assert d.get("dialect") == "posix-sh"


def test_parse_probe_windows_chcp() -> None:
    text = "os=windows\ncomspec=C:\\Windows\\system32\\cmd.exe\nhome=C:\\Users\\a\npwd=C:\\Users\\a\nActive code page: 936\n"
    d = parse_probe_output(text)
    assert d.get("os") == "windows"
    assert d.get("chcp") == 936
    assert d.get("shell_base") == "cmd"
    assert d.get("dialect") == "cmd"
    assert d.get("pwd") == r"C:\Users\a"


def test_wrap_powershell_composes_with_call_operator() -> None:
    """O5: ssh.run_argv prefixes PowerShell argv with the call operator `&`.
    The cwd wrap must keep that invocation valid: Set-Location then `& ...`."""
    out = wrap_with_cwd(
        "& 'echo' 'hello'", r"C:\Users\a", shell_family="powershell"
    )
    assert out.startswith("Set-Location -LiteralPath 'C:\\Users\\a'; ")
    assert out.endswith("& 'echo' 'hello'")


def test_wrap_powershell_no_cwd_preserves_call_operator() -> None:
    """Without a cwd, the call-operator command passes through unchanged."""
    out = wrap_with_cwd(
        "& 'pwsh' '-NoProfile' '-Command' 'Get-Date'",
        None,
        shell_family="powershell",
    )
    assert out == "& 'pwsh' '-NoProfile' '-Command' 'Get-Date'"

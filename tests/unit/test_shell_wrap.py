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
    assert " && " in out
    assert " & " not in out  # bare & does not short-circuit on cd failure
    assert out.endswith("dir")
    assert out == 'cd /d "C:\\Users\\a" && dir'


def test_wrap_cmd_short_circuits_like_posix() -> None:
    """cmd and posix both use && so a failed cd does not run the command."""
    cmd_out = wrap_with_cwd("echo ok", r"C:\missing\path", shell_family="cmd")
    posix_out = wrap_with_cwd("echo ok", "/missing/path", shell_family="posix")
    assert " && " in cmd_out
    assert " && " in posix_out
    # cmd: cd fails -> remainder not executed; exit non-zero (cmd.exe semantics)
    assert cmd_out.startswith("cd /d ")
    assert cmd_out.endswith("echo ok")
    # bare & would still run the command in the default directory
    assert " & " not in cmd_out


def test_wrap_powershell() -> None:
    out = wrap_with_cwd("Get-ChildItem", r"C:\Users\a", shell_family="powershell")
    assert "Set-Location -LiteralPath 'C:\\Users\\a' -ErrorAction Stop" in out
    assert "Get-ChildItem" in out
    assert out == (
        "Set-Location -LiteralPath 'C:\\Users\\a' -ErrorAction Stop; Get-ChildItem"
    )


def test_wrap_powershell_set_location_short_circuits() -> None:
    """Failed Set-Location must be terminating so body is not free-standing after ';'."""
    body = "Write-Output 'BODY_RAN'"
    out = wrap_with_cwd(body, r"C:\missing\path", shell_family="powershell")
    # Portable short-circuit: -ErrorAction Stop (PS 5.1 + 7+), not bare ';'.
    assert "-ErrorAction Stop" in out
    assert out.startswith(
        "Set-Location -LiteralPath 'C:\\missing\\path' -ErrorAction Stop; "
    )
    assert out.endswith(body)
    # Old form without Stop left body reachable after non-terminating failure.
    assert "Set-Location -LiteralPath 'C:\\missing\\path'; " not in out
    # Body is only after the Stop-qualified Set-Location, not a bare semicolon join.
    loc_stmt, _, rest = out.partition("; ")
    assert "-ErrorAction Stop" in loc_stmt
    assert rest == body


def test_wrap_rejects_bool_cwd_true() -> None:
    """Regression: probe cap_pwd must never produce `cd True`."""
    assert wrap_with_cwd("whoami", True, shell_family="posix") == "whoami"  # type: ignore[arg-type]
    assert wrap_with_cwd("whoami", "True", shell_family="posix") == "whoami"
    assert wrap_with_cwd("whoami", False, shell_family="cmd") == "whoami"  # type: ignore[arg-type]
    assert wrap_with_cwd("Get-ChildItem", True, shell_family="powershell") == "Get-ChildItem"  # type: ignore[arg-type]
    assert wrap_with_cwd("Get-ChildItem", "False", shell_family="powershell") == "Get-ChildItem"
    assert wrap_with_cwd("dir", "None", shell_family="cmd") == "dir"
    assert coerce_cwd_path(True) is None
    assert coerce_cwd_path("True") is None
    assert coerce_cwd_path(False) is None
    assert coerce_cwd_path("False") is None
    assert coerce_cwd_path(None) is None
    assert coerce_cwd_path("None") is None
    assert coerce_cwd_path(1) is None
    assert coerce_cwd_path(b"/tmp") is None
    assert coerce_cwd_path("/home/u") == "/home/u"
    assert coerce_cwd_path(r"C:\Users\a") == r"C:\Users\a"


def test_wrap_cmd_quote_escaping_matches_double_quote_rule() -> None:
    """cmd path quoting: always double-quoted; embedded \" -> \"\" (not \\\")."""
    path = r'C:\dir\with"quote'
    out = wrap_with_cwd("echo ok", path, shell_family="cmd")
    assert out == 'cd /d "C:\\dir\\with""quote" && echo ok'
    # Spaces still quoted
    spaced = wrap_with_cwd("dir", r"C:\Users\My Dir", shell_family="cmd")
    assert spaced == 'cd /d "C:\\Users\\My Dir" && dir'


def test_wrap_powershell_apostrophe_escaping() -> None:
    """PS single-quoted path doubles embedded apostrophes."""
    path = r"C:\Users\O'Brien"
    out = wrap_with_cwd("Get-Location", path, shell_family="powershell")
    assert out == (
        "Set-Location -LiteralPath 'C:\\Users\\O''Brien' -ErrorAction Stop; Get-Location"
    )


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
    """cap_pwd=1 must not clobber path field pwd=/real/path -> cwd=True bug."""
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


def test_parse_probe_shell_base_powershell_alone_not_windows() -> None:
    """shell_base=powershell is a wrap hint; it must not set os=windows."""
    d = parse_probe_output("shell_base=powershell\n")
    assert d.get("os") != "windows"
    assert d.get("shell_base") == "powershell"
    d_pwsh = parse_probe_output("shell_base=pwsh\n")
    assert d_pwsh.get("os") != "windows"
    assert d_pwsh.get("shell_base") == "pwsh"
    d_cmd = parse_probe_output("shell_base=cmd\n")
    assert d_cmd.get("os") != "windows"
    assert d_cmd.get("shell_base") == "cmd"


def test_parse_probe_linux_pwsh_path_not_windows() -> None:
    """Linux pwsh: real POSIX uname + /usr/bin/pwsh is not a Windows host."""
    text = "uname=Linux-x86_64\nshell_path=/usr/bin/pwsh\nhome=/home/u\npwd=/home/u\n"
    d = parse_probe_output(text)
    assert d.get("os") == "posix"
    assert d.get("uname") == "Linux-x86_64"
    assert d.get("shell_base") == "pwsh"
    assert d.get("shell_path") == "/usr/bin/pwsh"


def test_parse_probe_powershell_with_real_comspec_is_windows() -> None:
    """A real COMSPEC path still proves Windows; shell_base stays a wrap hint."""
    text = (
        "os=windows\n"
        "shell_base=powershell\n"
        "comspec=C:\\Windows\\system32\\cmd.exe\n"
        "home=C:\\Users\\a\n"
        "pwd=C:\\Users\\a\n"
    )
    d = parse_probe_output(text)
    assert d.get("os") == "windows"
    assert d.get("shell_base") == "powershell"
    assert d.get("comspec") == r"C:\Windows\system32\cmd.exe"
    assert d.get("dialect") == "powershell"


def test_wrap_powershell_composes_with_call_operator() -> None:
    """ssh.run_argv prefixes PowerShell argv with the call operator `&`.
    The cwd wrap must keep that invocation valid: Set-Location then `& ...`."""
    out = wrap_with_cwd(
        "& 'echo' 'hello'", r"C:\Users\a", shell_family="powershell"
    )
    assert out.startswith(
        "Set-Location -LiteralPath 'C:\\Users\\a' -ErrorAction Stop; "
    )
    assert out.endswith("& 'echo' 'hello'")


def test_wrap_powershell_no_cwd_preserves_call_operator() -> None:
    """Without a cwd, the call-operator command passes through unchanged."""
    out = wrap_with_cwd(
        "& 'pwsh' '-NoProfile' '-Command' 'Get-Date'",
        None,
        shell_family="powershell",
    )
    assert out == "& 'pwsh' '-NoProfile' '-Command' 'Get-Date'"

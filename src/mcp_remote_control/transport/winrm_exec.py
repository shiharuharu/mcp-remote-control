"""WinRM oneshot exec quoting, exit-probe, and result coercion."""

from __future__ import annotations

import json
from typing import Any

from mcp_remote_control.transport.base import ExecResult, TransportError

# Exit probe: captures native $LASTEXITCODE so execute_ps / pool invoke do not
# report false success when had_errors=False but a native exe returned non-zero
# (e.g. cmd.exe /c exit 7).
_EXIT_MARKER = "__MRC_PS_EXIT_MARKER__"

# Statement prepended by :func:`_append_ps_exit_probe` to clear the exit code
# the probe is about to read.
#
# $LASTEXITCODE is an automatic variable of the *runspace*, not of the pipeline
# that set it: on a persistent runspace it still holds the previous invoke's
# native exit code while the current script runs, so a pure-PS script would
# report that stale code as its own. Clearing the runspace-global variable and
# then reading the unqualified name in the probe keeps the value the script
# itself produces (a native command - also from inside a function or a
# sub-block - and a plain or ``$global:`` assignment all read back), and a
# script that sets nothing now reads unset instead of stale. A value set in a
# scope the probe cannot see (a plain assignment inside a function) is not
# reported, exactly as any later statement of that script would read it. On the
# oneshot path pypsrp opens a fresh runspace per call, where the variable starts
# unset and the reset is a no-op.
_PS_EXIT_PROBE_RESET = "$global:LASTEXITCODE = $null"


def _decode_stream(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _win_quote(arg: str) -> str:
    """Minimal Windows command-line quoting for joined argv strings.

    Note on ``%``: ``cmd.exe`` expands ``%VAR%`` even inside double quotes, and
    there is no reliable command-line escape for ``%`` (``^%`` leaves a literal
    ``^`` and breaks the value; ``%%`` only works inside batch files). This
    quoter is therefore reserved for shell-string ``run_command`` (where ``%``
    expansion is the caller's intent). The no-shell ``run_argv`` contract is
    satisfied via the PowerShell call-operator path (``& exe @(args)``) which
    never goes through ``cmd.exe`` - see :meth:`WinRMTransport.run_argv`.
    """
    if not arg:
        return '""'
    if any(c in arg for c in ' \t"&|<>^'):
        return '"' + arg.replace('"', r'\"') + '"'
    return arg


def _append_ps_exit_probe(script: str) -> str:
    """Append a Write-Output probe that emits native ``$LASTEXITCODE``.

    pypsrp ``execute_ps`` / pool invoke only expose ``had_errors`` natively.
    External programs (``& cmd.exe @('/c','exit','7')``) leave ``had_errors``
    false while setting ``$LASTEXITCODE``. The probe runs after the user script
    and reports the code that script's own native commands left behind -
    ``$null`` (no native exe ran) reports 0.

    The probe is prefixed with :data:`_PS_EXIT_PROBE_RESET` because
    ``$LASTEXITCODE`` outlives a single pipeline on a persistent runspace; see
    that constant for the mechanism and why it cannot mask a value the user
    script sets. Callers strip the marker from stdout and map it into
    ``exit_code``.

    A script that ends its own block before the probe runs leaves no marker: a
    top-level ``exit`` always ends it, and a top-level ``return`` does too when
    the caller composed *script* straight into the block instead of isolating it
    with :func:`_isolate_user_script`. Callers must treat a missing marker as
    "no evidence", not as exit 0 - see ``RunspaceResult.exit_probe_ran``.
    """
    return (
        f"{_PS_EXIT_PROBE_RESET}\n"
        f"{script}\n"
        f"Write-Output ('{_EXIT_MARKER}' + "
        f"[string]($(if ($null -ne $LASTEXITCODE) {{ $LASTEXITCODE }} else {{ 0 }})))"
    )


def _isolate_user_script(script: str) -> str:
    """Wrap caller-supplied script text as its own dot-sourced block.

    :func:`_append_ps_exit_probe` puts the reset statement before *script*, so
    for a caller that passes user text straight through - a pooled ``ps invoke``
    hands the text to a runspace as a script block - the reset becomes that
    block's first statement. PowerShell accepts a ``param(...)`` block or a
    ``using`` directive only in the first position and fails the whole block
    otherwise ("The term 'param' is not recognized ..."), so such a script would
    never run. Isolating it keeps the reset outside the user's text and the
    user's first statement first.

    Dot-sourcing (``.`` rather than ``&``) is required, not stylistic: the block
    then runs in the caller's scope, so assignments and function definitions
    still land in the runspace's global state, which is what makes one pooled
    invoke's variables visible to the next. Callers whose text already starts
    with a generated statement (the cwd wrapper of the oneshot exec path) do not
    need this.
    """
    return f". {{\n{script}\n}}"


def _parse_exit_marker_value(text: str, marker: str) -> int | None:
    """Parse ``marker + integer``; return None when the payload is non-numeric."""
    if not text.startswith(marker):
        return None
    rest = text[len(marker) :].strip()
    if rest == "":
        return 0
    try:
        return int(rest)
    except ValueError:
        return None


def _split_exit_marker(output: Any, marker: str) -> tuple[int | None, Any]:
    """Extract the MRC exit probe from PS output; return ``(rc|None, remaining)``.

    List/tuple outputs keep list shape (marker-prefixed elements removed).
    String/other outputs return a cleaned string. All marker-prefixed elements
    are stripped; the exit code is taken from the last one (the probe is the
    final statement relative to other probes when both exit + location fire).
    """
    if output is None:
        return None, None

    if isinstance(output, (list, tuple)):
        remaining = list(output)
        indices = [i for i, v in enumerate(remaining) if str(v).startswith(marker)]
        if not indices:
            return None, remaining
        exit_code = _parse_exit_marker_value(str(remaining[indices[-1]]), marker)
        for i in sorted(indices, reverse=True):
            del remaining[i]
        return exit_code, remaining

    text = _decode_stream(output)
    if marker not in text:
        return None, text

    # Line-oriented strip (pypsrp typically joins Write-Output with newlines).
    lines = text.splitlines(keepends=True)
    exit_code: int | None = None
    kept: list[str] = []
    found_line = False
    for line in lines:
        content = line.rstrip("\r\n")
        if content.startswith(marker):
            found_line = True
            parsed = _parse_exit_marker_value(content, marker)
            if parsed is not None:
                exit_code = parsed
        else:
            kept.append(line)
    if found_line:
        return exit_code, "".join(kept)

    # Marker embedded without a clean line boundary - take last occurrence.
    idx = text.rfind(marker)
    if idx < 0:
        return None, text
    tail = text[idx:]
    # Consume through end of integer payload (or end of string).
    end = len(tail)
    for i, ch in enumerate(tail[len(marker) :], start=len(marker)):
        if ch in "\r\n":
            end = i
            break
        if i > len(marker) and ch not in "-0123456789":
            # Allow leading '-' for negative codes; stop on other junk.
            if not (i == len(marker) and ch == "-"):
                end = i
                break
    payload = tail[:end]
    exit_code = _parse_exit_marker_value(payload.rstrip("\r\n"), marker)
    cleaned = text[:idx] + text[idx + end :]
    return exit_code, cleaned


def _exit_code_from_ps(*, captured: int | None, had_errors: bool) -> int:
    """Combine LASTEXITCODE probe with PS ``had_errors`` into an exit code.

    Prefer the native code when the probe ran. PS terminating errors still fail
    the result when the native code is 0/absent (``had_errors`` alone used to
    be the only signal).
    """
    if captured is not None:
        exit_code = int(captured)
    else:
        exit_code = 1 if had_errors else 0
    if had_errors and exit_code == 0:
        exit_code = 1
    return exit_code


def _coerce_exec_result(raw: Any, *, default_cwd: str | None) -> ExecResult:
    """Normalize session API / pypsrp return shapes into ``ExecResult``."""
    if isinstance(raw, ExecResult):
        if raw.cwd is None and default_cwd is not None:
            return ExecResult(
                exit_code=raw.exit_code,
                stdout=raw.stdout,
                stderr=raw.stderr,
                cwd=default_cwd,
                timed_out=raw.timed_out,
            )
        return raw

    if raw is None:
        raise TransportError("EXEC_FAILED", "remote run returned no result")

    # Duck-type objects exposing exit_code / exit_status / returncode.
    # Missing/None exit attrs -> -1 (not 0), matching SSH _coerce_exec_result,
    # so unknown results are never reported as success.
    if hasattr(raw, "exit_code") or hasattr(raw, "exit_status") or hasattr(raw, "returncode"):
        exit_code = getattr(raw, "exit_code", None)
        if exit_code is None:
            exit_code = getattr(raw, "exit_status", None)
        if exit_code is None:
            # Fall back to subprocess-style returncode; missing/None -> -1
            # (not 0) to avoid false success when all exit attrs are absent.
            exit_code = getattr(raw, "returncode", None)
        if exit_code is None:
            exit_code = -1
        timed_out = bool(getattr(raw, "timed_out", False))
        cwd = getattr(raw, "cwd", None) or default_cwd
        return ExecResult(
            exit_code=int(exit_code),
            stdout=_decode_stream(getattr(raw, "stdout", "")),
            stderr=_decode_stream(getattr(raw, "stderr", "")),
            cwd=cwd,
            timed_out=timed_out,
        )

    # pypsrp execute_cmd -> (stdout, stderr, rc); also (exit, stdout[, stderr]).
    if isinstance(raw, tuple) and len(raw) >= 2:
        if len(raw) >= 3 and isinstance(raw[2], (int, float)):
            return ExecResult(
                exit_code=int(raw[2]),
                stdout=_decode_stream(raw[0]),
                stderr=_decode_stream(raw[1]),
                cwd=default_cwd,
            )
        exit_code = int(raw[0])
        stdout = _decode_stream(raw[1])
        stderr = _decode_stream(raw[2]) if len(raw) > 2 else ""
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            cwd=default_cwd,
        )

    raise TransportError(
        "EXEC_FAILED",
        f"unrecognized remote run result type: {type(raw).__name__}",
    )


def _ps_single_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _is_timeout_exc(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return "timeout" in name or "timeout" in text or "timed out" in text


# Failure taxonomy for classify_winrm_failure, expressed as (module, qualname)
# pairs on the exception's MRO. Matching names instead of importing the
# libraries keeps classification identical whether or not pypsrp/requests are
# installed, and lets a test double describe a pypsrp failure without the
# package present.
#
# requests timeouts: the HTTP exchange may have completed on the remote side
# before the response was lost, so these must never be replayed.
_REQUESTS_TIMEOUT_TYPES = frozenset(
    {
        ("requests.exceptions", "Timeout"),
        ("requests.exceptions", "ReadTimeout"),
        ("requests.exceptions", "ConnectTimeout"),
    }
)

# Only a WSMan/HTTP *rejection* proves the request did not execute remotely.
# pypsrp raises ``WinRMTransportError`` when the response body is not a
# parseable WSMan fault - an empty-body HTTP 400 from the framing layer, the
# signature of a stale message-encryption context. A real pipeline failure
# arrives as a WSMan fault instead (see ``_LINK_FATAL_TYPES``/``other``), so
# this bucket cannot carry a command that ran. Which rejections in this bucket
# are *provably* pre-execution is decided by :func:`is_winrm_refusal`.
#
# Connection-level failures are deliberately absent. requests raises the same
# ``ConnectionError`` whether the socket died before the request was sent or
# after the server had already run it and the response was lost, and urllib3
# classifies a mid-exchange ``ProtocolError`` as a read error for the same
# reason. Replaying one of those could repeat a side effect, so they are fatal.
_LINK_RETRYABLE_TYPES = frozenset(
    {
        ("pypsrp.exceptions", "WinRMTransportError"),
    }
)

# Link suspect and the request may already have run: read/connect timeouts, any
# connection-level failure, retry exhaustion. Report the failure, mark the
# session dead, never replay.
_LINK_FATAL_TYPES = frozenset(
    {
        ("requests.exceptions", "Timeout"),
        ("requests.exceptions", "ConnectionError"),
        ("requests.exceptions", "ProtocolError"),
        ("urllib3.exceptions", "ProtocolError"),
        ("urllib3.exceptions", "NewConnectionError"),
        ("urllib3.exceptions", "ReadTimeoutError"),
        ("urllib3.exceptions", "TimeoutError"),
        ("urllib3.exceptions", "MaxRetryError"),
        ("http.client", "RemoteDisconnected"),
        ("builtins", "ConnectionError"),
        ("builtins", "ConnectionResetError"),
        ("builtins", "BrokenPipeError"),
        ("builtins", "OSError"),
    }
)


def _matches_type_table(exc: BaseException, table: frozenset[tuple[str, str]]) -> bool:
    """True when *exc*'s type or one of its bases is an entry of *table*.

    MRO order means subclasses of a listed failure (a pypsrp subclass, a
    requests adapter's own ConnectionError, ``RemoteDisconnected`` under
    ``ConnectionResetError``) classify like their base.
    """
    for cls in type(exc).__mro__:
        if (getattr(cls, "__module__", None), getattr(cls, "__qualname__", "")) in table:
            return True
    return False


def classify_winrm_failure(exc: BaseException) -> str:
    """Bucket a WinRM failure by whether a retry is provably safe.

    ``"budget_timeout"``: the async bridge's user-budget expiry (builtin
        ``TimeoutError``). The remote side may still be running the request.
    ``"link_retryable"``: pypsrp raised ``WinRMTransportError`` with no
        parseable WSMan fault, so no pipeline answered.
        :func:`is_winrm_refusal` separates a provable pre-execution rejection
        from a gateway error page that may have forwarded the request.
        Replaying a *structural* first exchange (a shell ``Create``, the
        read-only identity probe) cannot duplicate a side effect either way.
    ``"link_fatal"``: link suspect and the request may already have run -
        read/connect timeouts, connection-level failures, retry exhaustion,
        bare ``OSError``. Report, mark the session dead, never replay.
    ``"other"``: unrecognized, plus every answer a live link produced. A
        ``WSManFaultError`` is a SOAP fault from a reachable server and must
        not mark the session dead; the operation-timeout fault is in this
        bucket, and the pipeline may have run through it, so faults are not
        replayable either.

    ``requests.exceptions.Timeout`` subclasses are classified ``link_fatal``
    before the builtin ``TimeoutError`` check; ``socket.timeout`` is aliased to
    that builtin (an ``OSError`` subclass), so a bare socket timeout lands in
    ``budget_timeout`` by design rather than being called fatal.
    """
    if _matches_type_table(exc, _REQUESTS_TIMEOUT_TYPES):
        return "link_fatal"
    if isinstance(exc, TimeoutError):
        return "budget_timeout"
    if _matches_type_table(exc, _LINK_RETRYABLE_TYPES):
        return "link_retryable"
    if _matches_type_table(exc, _LINK_FATAL_TYPES):
        return "link_fatal"
    return "other"


def is_winrm_refusal(exc: BaseException) -> bool:
    """True when *exc* is a pypsrp rejection that provably dispatched nothing.

    Of the two rejection shapes measured on live hosts, only the first is
    safe to replay:

    - ``WinRMTransportError("http", 400, "")`` - the stale message-encryption
      signature. The framing layer refused the request before any WSMan
      pipeline could be built, so the payload never ran.
    - ``WinRMTransportError("http", 502, "...")`` - a front gateway's own error
      page. That body proves an intermediary answered in place of the server:
      whether it forwarded the request first is unknown, so a replay could
      repeat a side effect.

    A refusal is therefore an empty body **plus a 4xx status**: 4xx means the
    receiver rejected *this request*, while 5xx means something failed *while
    handling* it, and a request being handled may already have run. On the
    pooled-invoke path the rejected request carries the user's script, so
    guessing wrong runs it twice. 400 is the status measured for the staleness
    signature; the rest of the 4xx class stays eligible because a stale auth
    context can plausibly be answered 401/403 as well.

    Conservative by construction: anything that is not a
    ``WinRMTransportError``, or whose ``args`` are not exactly the
    ``("http"|"https", <int status>, <body>)`` triple with a 4xx status and an
    empty/whitespace body, is False. A longer tuple is a shape no observed
    pypsrp raises, so its extra slots cannot be assumed to carry the body.
    """
    if not _matches_type_table(exc, _LINK_RETRYABLE_TYPES):
        return False
    try:
        args = exc.args
    except Exception:  # noqa: BLE001 - a hostile double is not a proof
        return False
    if not isinstance(args, tuple) or len(args) != 3:
        return False
    protocol, status, body = args[0], args[1], args[2]
    if protocol not in ("http", "https"):
        return False
    if isinstance(status, bool) or not isinstance(status, int):
        return False
    if not 400 <= status < 500:
        # A server-side or intermediary failure, not a rejection: the request
        # may have been handled before the response was lost.
        return False
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    elif not isinstance(body, str):
        # None or an unexpected type: no observed body, so no proof.
        return False
    return not body.strip()



def _is_execute_ps_result_tuple(raw: tuple[Any, ...]) -> bool:
    """True when *raw* looks like pypsrp ``(output, streams, had_errors)``.

    A tuple of pipeline objects (key=value strings or JSON dicts) is *not*
    this shape - those are formatted as stdout by :func:`_ps_result_to_exec`.
    """
    if not raw:
        return False
    # Classic (output, streams, had_errors). had_errors is bool; mocks may use 0/1.
    if len(raw) >= 3 and isinstance(raw[2], bool):
        return True
    if len(raw) >= 3 and isinstance(raw[2], int) and raw[2] in (0, 1):
        return True
    # (output, streams) - streams is None or a PSDataStreams-like object.
    if len(raw) == 2 and (raw[1] is None or hasattr(raw[1], "error")):
        return True
    # Single payload (output,) - format raw[0] via the 3-tuple path.
    if len(raw) == 1:
        return True
    return False


def _ps_result_to_exec(raw: Any, *, default_cwd: str | None) -> ExecResult:
    """Map pypsrp ``execute_ps`` ``(output, streams, had_errors)`` -> ``ExecResult``.

    Also accepts a bare list/tuple of pipeline objects (key=value strings or
    JSON dicts). Those are formatted via :func:`_format_ps_output` so probe
    parsers see real lines rather than ``str(list)``.

    Exit code preference:
    1. ``$LASTEXITCODE`` captured by :func:`_append_ps_exit_probe` (stripped
       from stdout) - native programs like ``cmd.exe /c exit 7``.
    2. Else ``1 if had_errors else 0`` (legacy / unwrapped mocks).
    3. PS terminating errors still force non-zero when native rc is 0.
    """
    if isinstance(raw, ExecResult):
        return _coerce_exec_result(raw, default_cwd=default_cwd)

    # Bare list of pipeline objects (not wrapped in the pypsrp 3-tuple).
    if isinstance(raw, list):
        captured_rc, remaining = _split_exit_marker(raw, _EXIT_MARKER)
        stdout = _format_ps_output(remaining)
        exit_code = _exit_code_from_ps(captured=captured_rc, had_errors=False)
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr="",
            cwd=default_cwd,
        )

    if isinstance(raw, tuple) and len(raw) >= 1:
        if not _is_execute_ps_result_tuple(raw):
            # Tuple of output objects (strings / dicts), not (output, streams, ...).
            return _ps_result_to_exec(list(raw), default_cwd=default_cwd)
        captured_rc, remaining = _split_exit_marker(raw[0], _EXIT_MARKER)
        if isinstance(remaining, (list, tuple)):
            stdout = _format_ps_output(remaining)
        else:
            stdout = _decode_stream(remaining) if remaining is not None else ""
        stderr = ""
        had_errors = False
        if len(raw) >= 3:
            had_errors = bool(raw[2])
        if len(raw) >= 2 and raw[1] is not None:
            streams = raw[1]
            # PSDataStreams.error is a list of error records.
            err_list = getattr(streams, "error", None)
            if err_list:
                parts = []
                for item in err_list:
                    parts.append(str(item))
                stderr = "\n".join(parts)
            elif not isinstance(streams, (str, bytes)):
                pass
            else:
                stderr = _decode_stream(streams)
        exit_code = _exit_code_from_ps(captured=captured_rc, had_errors=had_errors)
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            cwd=default_cwd,
        )

    return _coerce_exec_result(raw, default_cwd=default_cwd)


def _format_ps_output_item(item: Any) -> str:
    """One pipeline object -> a single stdout line.

    Mappings are emitted as compact JSON so capability-probe parsers can
    read a ConvertTo-Json object that arrived as a dict rather than a string.
    """
    if isinstance(item, dict):
        return json.dumps(item, separators=(",", ":"), default=str)
    return str(item)


def _format_ps_output(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output if output.endswith("\n") or not output else output + "\n"
    if isinstance(output, dict):
        text = _format_ps_output_item(output)
        return text if text.endswith("\n") else text + "\n"
    if isinstance(output, (list, tuple)):
        lines = [_format_ps_output_item(item) for item in output]
        body = "\n".join(lines)
        if body and not body.endswith("\n"):
            body += "\n"
        return body
    text = str(output)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def _format_ps_errors(ps: Any) -> str:
    streams = getattr(ps, "streams", None)
    if streams is None:
        return ""
    err_list = getattr(streams, "error", None) or []
    if not err_list:
        return ""
    return "\n".join(str(item) for item in err_list)


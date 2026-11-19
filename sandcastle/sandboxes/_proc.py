"""Process helpers shared by every local provider.

``run_process`` spawns a command, optionally pipes ``stdin``, streams stdout
line by line to ``on_line`` (keeping a bounded tail, as the TS providers do
with ``BoundedTail``), and returns an :class:`ExecResult`. Cancelling the
awaiting task kills the process (and its process group when
``new_session=True``), so idle timeouts and aborts really stop the agent.
"""

from __future__ import annotations

import asyncio
import codecs
import os
import signal
import sys
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Union

from ..display import MAX_TAIL_CHARS, BoundedTail
from ..sandbox_provider import ExecResult

_READ_CHUNK = 64 * 1024


def kill_process(proc: "asyncio.subprocess.Process", group: bool) -> None:
    if proc.returncode is not None:
        return
    try:
        if group and sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def _pump_lines(reader: asyncio.StreamReader, on_line: Callable[[str], None], tail: BoundedTail) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    while True:
        chunk = await reader.read(_READ_CHUNK)
        if not chunk:
            break
        pending += decoder.decode(chunk)
        while True:
            idx = pending.find("\n")
            if idx == -1:
                break
            line = pending[:idx]
            pending = pending[idx + 1 :]
            if line.endswith("\r"):
                line = line[:-1]
            tail.push(line)
            on_line(line)
    pending += decoder.decode(b"", final=True)
    if pending:
        if pending.endswith("\r"):
            pending = pending[:-1]
        tail.push(pending)
        on_line(pending)


async def _pump_raw(reader: asyncio.StreamReader, tail: BoundedTail) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    while True:
        chunk = await reader.read(_READ_CHUNK)
        if not chunk:
            break
        tail.push(decoder.decode(chunk))
    rest = decoder.decode(b"", final=True)
    if rest:
        tail.push(rest)


async def run_process(
    argv: Union[Sequence[str], str],
    *,
    cwd: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    stdin: Optional[str] = None,
    on_line: Optional[Callable[[str], None]] = None,
    max_tail_chars: int = MAX_TAIL_CHARS,
    new_session: bool = False,
    on_spawn: Optional[Callable[["asyncio.subprocess.Process"], None]] = None,
    preexec_fn: Optional[Callable[[], None]] = None,
    pass_fds: Sequence[int] = (),
) -> ExecResult:
    """Run ``argv`` (a list, or a verbatim command-line string on Windows)."""
    kwargs: Dict = dict(
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if new_session and sys.platform != "win32":
        kwargs["start_new_session"] = True
    if preexec_fn is not None:
        kwargs["preexec_fn"] = preexec_fn
    if pass_fds:
        kwargs["pass_fds"] = tuple(pass_fds)
    if isinstance(argv, str):
        # A ready-made shell line (sh on POSIX, cmd.exe on Windows).
        proc = await asyncio.create_subprocess_shell(argv, **kwargs)
    else:
        proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
    if on_spawn is not None:
        on_spawn(proc)
    streamed = on_line is not None
    max_chars = max_tail_chars if streamed else 1 << 62
    out_tail = BoundedTail(max_chars, "\n" if streamed else "")
    err_tail = BoundedTail(max_chars, "")

    async def feed() -> None:
        if stdin is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(stdin.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass

    assert proc.stdout is not None and proc.stderr is not None
    out_task = _pump_lines(proc.stdout, on_line, out_tail) if streamed else _pump_raw(proc.stdout, out_tail)  # type: ignore[arg-type]
    try:
        await asyncio.gather(feed(), out_task, _pump_raw(proc.stderr, err_tail))
        code = await proc.wait()
    except asyncio.CancelledError:
        kill_process(proc, new_session)
        raise
    except BaseException:
        kill_process(proc, new_session)
        raise
    # A signal-terminated process reports a negative code: map to non-zero.
    exit_code = code if code is not None and code >= 0 else 1
    return ExecResult(stdout=str(out_tail), stderr=str(err_tail), exit_code=exit_code)


async def run_interactive(
    argv: Sequence[str], *, cwd: Optional[str] = None, env: Optional[Mapping[str, str]] = None, stdin=None, stdout=None, stderr=None
) -> int:
    """Run ``argv`` attached to the given (default: inherited) streams."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    )
    try:
        code = await proc.wait()
    except asyncio.CancelledError:
        kill_process(proc, False)
        raise
    return code if code is not None and code >= 0 else 1


async def check_output(argv: Sequence[str], **kwargs) -> ExecResult:
    return await run_process(argv, **kwargs)

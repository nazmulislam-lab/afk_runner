"""Run a shell command on the host. Port of ``hostShell.ts``.

Like ``asyncio.create_subprocess_shell`` + ``communicate``, but the shell
leads its own process group and an abort (or cancellation, or process
shutdown) kills the whole group, so grandchildren don't outlive the caller.
On Windows there are no process groups, so only the shell is killed.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from dataclasses import dataclass
from typing import Optional, Sequence

from .abort import AbortSignal
from .shutdown import register_shutdown


class HostShellError(Exception):
    def __init__(self, message: str, code: Optional[int], stdout: str, stderr: str) -> None:
        super().__init__(message)
        self.code = code
        self.stdout = stdout
        self.stderr = stderr


class HostShellAbortError(Exception):
    name = "AbortError"


@dataclass(frozen=True)
class HostShellResult:
    stdout: str
    stderr: str


def _kill_tree(proc: asyncio.subprocess.Process, uses_group: bool) -> None:
    if proc.returncode is not None:
        return
    try:
        if uses_group:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def exec_host_shell(
    command: str,
    *,
    cwd: str,
    signals: Sequence[Optional[AbortSignal]] = (),
    env: Optional[dict] = None,
) -> HostShellResult:
    live = [s for s in signals if s is not None]
    for s in live:
        if s.aborted:
            raise HostShellAbortError(f"Command aborted: {command} ({s.reason})")
    uses_group = sys.platform != "win32"
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=uses_group,
    )
    unregister = register_shutdown(lambda: _kill_tree(proc, uses_group))
    loop = asyncio.get_running_loop()
    aborted_by: list = []

    def on_abort_factory(sig: AbortSignal):
        def on_abort() -> None:
            aborted_by.append(sig)
            loop.call_soon_threadsafe(_kill_tree, proc, uses_group)

        return on_abort

    removers = [s.add_listener(on_abort_factory(s)) for s in live]
    try:
        out_b, err_b = await proc.communicate()
    except asyncio.CancelledError:
        _kill_tree(proc, uses_group)
        raise
    finally:
        for r in removers:
            r()
        unregister()
    stdout = out_b.decode("utf-8", "replace")
    stderr = err_b.decode("utf-8", "replace")
    if aborted_by:
        raise HostShellAbortError(f"Command aborted: {command} ({aborted_by[0].reason})")
    if proc.returncode == 0:
        return HostShellResult(stdout, stderr)
    raise HostShellError(f"Command failed: {command}\n{stderr}", proc.returncode, stdout, stderr)

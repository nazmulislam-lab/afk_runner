"""Shared machinery for container-engine providers (docker, podman).

Port of ``engineExitStatus.ts`` plus the parts ``docker.ts`` and
``podman.ts`` share: ``<engine> exec`` with a trustworthy exit status,
interactive exec, ``<engine> cp``, and synchronous shutdown removal.

The engine CLI's exit code can't be trusted on its own (docker and podman
trap SIGTERM and exit 0), so the command runs in a subshell of a wrapper
that prints its status as the last stderr line. A missing line means the
command never finished.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..display import MAX_TAIL_CHARS
from ..sandbox_provider import ExecResult, InteractiveExecOptions
from ..shutdown import register_shutdown
from ._proc import run_interactive, run_process

SENTINEL = "__SANDCASTLE_EXIT_STATUS="
WRAPPER = "\n".join(
    [
        '__sandcastle_cmd=$1; shift; (eval "unset __sandcastle_cmd',
        "$__sandcastle_cmd",
        f"\"); __sandcastle_status=$?; printf '\\n{SENTINEL}%s\\n' \"$__sandcastle_status\" >&2; exit \"$__sandcastle_status\"",
    ]
)
_SENTINEL_LINE = re.compile("\n" + re.escape(SENTINEL) + r"(\d+)\n$")


def shell_args_reporting_exit(command: str) -> List[str]:
    """The ``sh -c`` argv that runs ``command`` and reports its status."""
    return ["sh", "-c", WRAPPER, "sh", command]


def resolve_engine_exit(stderr: str, cli_code: Optional[int]) -> Tuple[str, int]:
    m = _SENTINEL_LINE.search(stderr)
    if m is not None:
        return stderr[: m.start()], int(m.group(1))
    return stderr, 1 if cli_code is None or cli_code == 0 else cli_code


async def engine_exec_raw(engine: str, args: Sequence[str]) -> str:
    """``<engine> <args>`` (buffered); raises RuntimeError with stderr on failure."""
    r = await run_process([engine, *args])
    if r.exit_code != 0:
        raise RuntimeError(f"{engine} {args[0]} failed: {r.stderr or f'exit {r.exit_code}'}")
    return r.stdout


def _is_tty(stream: Any) -> bool:
    try:
        return bool(stream is not None and stream.isatty())
    except Exception:  # noqa: BLE001
        return False


class EngineHandle:
    """A running container addressed by name through the engine CLI."""

    def __init__(self, engine: str, container_name: str, worktree_path: str, max_tail: int = MAX_TAIL_CHARS, rm_args: Sequence[str] = ("rm", "-f")) -> None:
        self.engine = engine
        self.sandbox_id = container_name
        self.worktree_path = worktree_path
        self._max_tail = max_tail
        self._rm_args = list(rm_args)

        def remove_sync() -> None:
            try:
                subprocess.run([engine, *self._rm_args, container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5 if engine == "podman" else None)
            except Exception:  # noqa: BLE001
                pass

        self._unregister = register_shutdown(remove_sync)

    async def exec(self, command: str, *, on_line=None, cwd: Optional[str] = None, sudo: bool = False, stdin: Optional[str] = None) -> ExecResult:
        args = ["exec"]
        if sudo:
            args += ["--user", "0:0"]
        if stdin is not None:
            args.append("-i")
        if cwd:
            args += ["-w", cwd]
        args += [self.sandbox_id, *shell_args_reporting_exit(command)]
        try:
            r = await run_process([self.engine, *args], stdin=stdin, on_line=on_line, max_tail_chars=self._max_tail)
        except FileNotFoundError as e:
            raise RuntimeError(f"{self.engine} exec failed: {e}") from e
        stderr, code = resolve_engine_exit(r.stderr, r.exit_code)
        return ExecResult(r.stdout, stderr, code)

    async def interactive_exec(self, args: List[str], options: InteractiveExecOptions) -> int:
        eargs = ["exec", "-it" if _is_tty(options.stdin) else "-i"]
        if options.cwd:
            eargs += ["-w", options.cwd]
        eargs += [self.sandbox_id, *args]
        return await run_interactive([self.engine, *eargs], stdin=options.stdin, stdout=options.stdout, stderr=options.stderr)

    async def copy_file_in(self, host_path: str, sandbox_path: str) -> None:
        r = await run_process([self.engine, "cp", host_path, f"{self.sandbox_id}:{sandbox_path}"])
        if r.exit_code != 0:
            raise RuntimeError(f"{self.engine} cp (in) failed: {r.stderr}")

    async def copy_file_out(self, sandbox_path: str, host_path: str) -> None:
        r = await run_process([self.engine, "cp", f"{self.sandbox_id}:{sandbox_path}", host_path])
        if r.exit_code != 0:
            raise RuntimeError(f"{self.engine} cp (out) failed: {r.stderr}")

    async def close(self) -> None:
        self._unregister()
        await self._remove()

    async def _remove(self) -> None:  # overridden per engine
        r = await run_process([self.engine, *self._rm_args, self.sandbox_id])
        if r.exit_code != 0:
            raise RuntimeError(f"{self.engine} rm failed: {r.stderr}")


async def create_file_mount_parents(engine: str, container: str, dirs: Sequence[str], uid: int, gid: int) -> None:
    for d in dirs:
        r = await run_process([engine, "exec", "--user", "0:0", container, "sh", "-c", 'mkdir -p "$1" && chown "$2" "$1"', "sh", d, f"{uid}:{gid}"])
        if r.exit_code != 0:
            raise RuntimeError(f"Failed to create parent directory '{d}' in container: {r.stderr}")


def repeat_flag(flag: str, values: Optional[Sequence[Any]]) -> List[str]:
    out: List[str] = []
    for v in values or ():
        out += [flag, str(v)]
    return out


def networks_of(network: Any) -> List[str]:
    if network is None or network == "":
        return []
    return [network] if isinstance(network, str) else list(network)

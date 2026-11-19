"""No-sandbox provider: runs the agent directly on the host. Port of
``sandboxes/no-sandbox.ts``.

Accepted by ``run()``, ``create_sandbox()`` and ``interactive()``. Skips
isolation entirely. All three branch strategies work (default ``head``).
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional

from ..display import MAX_TAIL_CHARS
from ..sandbox_provider import ExecResult, InteractiveExecOptions, NoSandboxCreateOptions, NoSandboxProvider
from ._proc import run_interactive, run_process
from ._windows import plan_windows_exec, plan_windows_spawn


class NoSandboxHandle:
    def __init__(self, worktree_path: str, env: Dict[str, str], max_tail: int) -> None:
        self.worktree_path = worktree_path
        self._env = env
        self._max_tail = max_tail

    async def exec(self, command: str, *, on_line=None, cwd: Optional[str] = None, sudo: bool = False, stdin: Optional[str] = None) -> ExecResult:
        # sudo is a no-op: the user is already on the host.
        cwd = cwd or self.worktree_path
        if sys.platform == "win32":
            plan = plan_windows_exec(command, self._env, cwd)
            return await run_process(plan.command_line(), cwd=cwd, env=plan.env, stdin=stdin, on_line=on_line, max_tail_chars=self._max_tail)
        return await run_process(["sh", "-c", command], cwd=cwd, env=self._env, stdin=stdin, on_line=on_line, max_tail_chars=self._max_tail, new_session=True)

    async def interactive_exec(self, args: List[str], options: Optional[InteractiveExecOptions] = None) -> int:
        if not args:
            raise ValueError("interactiveExec needs a command to run")
        cwd = (options.cwd if options else None) or self.worktree_path
        argv = list(args)
        if sys.platform == "win32":
            plan = plan_windows_spawn(args, self._env, cwd)
            if plan is not None and not plan.verbatim:
                argv = [plan.file, *plan.args]
        return await run_interactive(
            argv,
            cwd=cwd,
            env=self._env,
            stdin=options.stdin if options else None,
            stdout=options.stdout if options else None,
            stderr=options.stderr if options else None,
        )

    async def close(self) -> None:
        return None


def no_sandbox(*, env: Optional[Dict[str, str]] = None, max_output_tail_chars: int = MAX_TAIL_CHARS) -> NoSandboxProvider:
    """Run the agent directly on the host (no isolation)."""

    async def create(opts: NoSandboxCreateOptions) -> NoSandboxHandle:
        return NoSandboxHandle(opts.worktree_path, {**os.environ, **opts.env}, max_output_tail_chars)

    return NoSandboxProvider(name="no-sandbox", create=create, env=dict(env or {}))


noSandbox = no_sandbox

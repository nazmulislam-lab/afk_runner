"""E2B sandbox provider (Firecracker microVM). Port of ``sandboxes/e2b.ts``.

Needs ``pip install e2b`` and ``E2B_API_KEY`` (or ``api_key=``). Don't put the
key in ``.sandcastle/.env``: keys declared there are forwarded into the
sandbox. E2B's default template has no agent CLI; its prebuilt ``claude`` and
``codex`` templates ship one: ``e2b(template="claude")``. The repo lands in
``$HOME/workspace``. Each sandbox is killed on close, or when ``timeout_ms``
(default 30 min) runs out.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, Optional

from ..display import MAX_TAIL_CHARS
from ..sandbox_provider import ExecResult, IsolatedCreateOptions, IsolatedSandboxProvider
from ._cloud import LineSink, make_tarball, require, result, shell_escape, stdin_temp_path, with_stdin_file

DEFAULT_TIMEOUT_MS = 30 * 60 * 1000


class E2BHandle:
    def __init__(self, sandbox: Any, worktree_path: str, max_tail: int) -> None:
        self._sb = sandbox
        self.worktree_path = worktree_path
        self.sandbox_id = getattr(sandbox, "sandbox_id", None)
        self._max_tail = max_tail

    async def _run_to_end(self, command: str) -> str:
        r = await self._sb.commands.run(command, timeout=0)
        return r.stdout

    async def exec(self, command: str, *, on_line=None, cwd: Optional[str] = None, sudo: bool = False, stdin: Optional[str] = None) -> ExecResult:
        e2b = require("e2b", "e2b")
        if stdin is not None:
            path = stdin_temp_path()
            await self._sb.files.write(path, stdin.encode("utf-8"))
            command = with_stdin_file(command, path)
        out = LineSink(on_line, self._max_tail if on_line else None)
        err = LineSink(None, self._max_tail if on_line else None, "")
        kwargs: Dict[str, Any] = {"cwd": cwd or self.worktree_path, "timeout": 0}
        if sudo:
            kwargs["user"] = "root"
        if on_line is not None:
            kwargs["on_stdout"] = lambda data: out.push_text(getattr(data, "line", data) if not isinstance(data, str) else data)
            kwargs["on_stderr"] = lambda data: err.push_text(getattr(data, "line", data) if not isinstance(data, str) else data)
        exit_exc = getattr(e2b, "CommandExitException", None) or getattr(e2b, "CommandExitError", Exception)
        try:
            r = await self._sb.commands.run(command, **kwargs)
            code, so, se = r.exit_code, r.stdout, r.stderr
        except exit_exc as e:  # type: ignore[misc]
            code, so, se = getattr(e, "exit_code", 1), getattr(e, "stdout", ""), getattr(e, "stderr", "")
        if on_line is None:
            return result(so or "", se or "", code)
        out.finish()
        return result(out.text(), err.text(), code)

    async def copy_in(self, host_path: str, sandbox_path: str) -> None:
        if os.path.isdir(host_path):
            tar = f"/tmp/sandcastle-copyin-{uuid.uuid4()}.tar.gz"
            await self._sb.files.write(tar, make_tarball(host_path))
            t, dest = shell_escape(tar), shell_escape(sandbox_path)
            await self._run_to_end(f"mkdir -p {dest} && tar -xzf {t} -C {dest}; s=$?; rm -f {t}; exit $s")
            return
        with open(host_path, "rb") as f:
            await self._sb.files.write(sandbox_path, f.read())
        mode = oct(os.stat(host_path).st_mode & 0o777)[2:]
        await self._run_to_end(f"chmod {mode} {shell_escape(sandbox_path)}")

    async def copy_file_out(self, sandbox_path: str, host_path: str) -> None:
        content = await self._sb.files.read(sandbox_path, format="bytes")
        os.makedirs(os.path.dirname(host_path) or ".", exist_ok=True)
        with open(host_path, "wb") as f:
            f.write(bytes(content))

    async def close(self) -> None:
        await self._sb.kill()


def e2b(
    *,
    template: Optional[str] = None,
    timeout_ms: Optional[int] = None,
    api_key: Optional[str] = None,
    metadata: Optional[Dict[str, str]] = None,
    env: Optional[Dict[str, str]] = None,
    max_output_tail_chars: int = MAX_TAIL_CHARS,
    **sdk_options: Any,
) -> IsolatedSandboxProvider:
    """Run the agent in an E2B sandbox. Extra keyword args go to ``AsyncSandbox.create``."""

    async def create(opts: IsolatedCreateOptions) -> E2BHandle:
        mod = require("e2b", "e2b")
        kwargs: Dict[str, Any] = dict(sdk_options)
        if template:
            kwargs["template"] = template
        if api_key:
            kwargs["api_key"] = api_key
        if metadata:
            kwargs["metadata"] = metadata
        kwargs["timeout"] = int((timeout_ms or DEFAULT_TIMEOUT_MS) / 1000)
        kwargs["envs"] = dict(opts.env)
        sandbox = await mod.AsyncSandbox.create(**kwargs)
        try:
            r = await sandbox.commands.run('mkdir -p "$HOME/workspace" && cd "$HOME/workspace" && pwd', timeout=0)
            worktree = r.stdout.strip()
        except Exception as e:  # noqa: BLE001
            try:
                await sandbox.kill()
            except Exception:  # noqa: BLE001
                pass
            why = (getattr(e, "stderr", "") or "").strip() or str(e)
            raise RuntimeError(f"e2b(): could not create $HOME/workspace: {why}") from e
        return E2BHandle(sandbox, worktree, max_output_tail_chars)

    return IsolatedSandboxProvider(name="e2b", create=create, env=dict(env or {}))

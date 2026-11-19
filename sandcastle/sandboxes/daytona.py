"""Daytona sandbox provider. Port of ``sandboxes/daytona.ts``,
``daytonaSession.ts`` and ``daytonaFiles.ts``.

Needs ``pip install daytona`` and ``DAYTONA_API_KEY`` (or ``api_key=``).
Streamed execs run in a Daytona session; a watcher notices if the session
shell dies before the command finishes (its exit code would be unknown).
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import uuid
from typing import Any, Dict, Optional

from ..display import MAX_TAIL_CHARS, BoundedTail
from ..sandbox_provider import ExecResult, IsolatedCreateOptions, IsolatedSandboxProvider
from ._cloud import require, result, shell_escape, walk_files

SESSION_WATCH_INTERVAL_S = 15.0


async def _maybe(value: Any) -> Any:
    return await value if asyncio.iscoroutine(value) else value


def _attr(obj: Any, *names: str) -> Any:
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return None


class DaytonaHandle:
    def __init__(self, client: Any, sandbox: Any, worktree_path: str, max_tail: int) -> None:
        self._client = client
        self._sb = sandbox
        self.worktree_path = worktree_path
        self.sandbox_id = getattr(sandbox, "id", None)
        self._max_tail = max_tail

    async def _prepare(self, command: str, sudo: bool, stdin: Optional[str]) -> str:
        eff = f"sudo sh -c {shell_escape(command)}" if sudo else command
        if stdin is None:
            return eff
        path = f"/tmp/sandcastle-stdin-{uuid.uuid4()}"
        await self._sb.fs.upload_file(stdin.encode("utf-8"), path)
        return f"{{ rm -f {path}; {eff}\n}} < {path}"

    async def _probe(self, session_id: str, cmd_id: str, pid_path: str) -> str:
        try:
            r = await self._sb.process.exec(f'test ! -f {pid_path} || kill -0 "$(cat {pid_path})"')
            alive = _attr(r, "exit_code", "exitCode") == 0
        except Exception:  # noqa: BLE001
            alive = True
        if alive:
            return "alive"
        try:
            cmd = await self._sb.process.get_session_command(session_id, cmd_id)
            return "exited" if isinstance(_attr(cmd, "exit_code", "exitCode"), int) else "lost"
        except Exception:  # noqa: BLE001
            return "lost"

    async def exec(self, command: str, *, on_line=None, cwd: Optional[str] = None, sudo: bool = False, stdin: Optional[str] = None) -> ExecResult:
        eff = await self._prepare(command, sudo, stdin)
        cwd = cwd or self.worktree_path
        proc = self._sb.process
        if on_line is None:
            r = await proc.exec(eff, cwd=cwd)
            code = _attr(r, "exit_code", "exitCode")
            out = _attr(r, "result") or ""
            return result(out, "" if code == 0 else out, code)

        daytona = require("daytona", "daytona")
        session_id = f"sandcastle-{uuid.uuid4()}"
        pid_path = f"/tmp/{session_id}.pid"
        await proc.create_session(session_id)
        out_tail = BoundedTail(self._max_tail, "\n")
        err_tail = BoundedTail(self._max_tail, "")
        partial = [""]

        def on_stdout(chunk: Any) -> None:
            text = chunk if isinstance(chunk, str) else getattr(chunk, "output", str(chunk))
            lines = (partial[0] + text).split("\n")
            partial[0] = lines.pop()
            for line in lines:
                out_tail.push(line)
                on_line(line)

        def on_stderr(chunk: Any) -> None:
            err_tail.push(chunk if isinstance(chunk, str) else str(chunk))

        try:
            req = daytona.SessionExecuteRequest(command=f"echo $$ > {pid_path}; ( cd {cwd} && {eff}\n)", run_async=True)
            resp = await proc.execute_session_command(session_id, req)
            cmd_id = _attr(resp, "cmd_id", "cmdId")
            logs = asyncio.ensure_future(proc.get_session_command_logs_async(session_id, cmd_id, on_stdout, on_stderr))

            async def watch() -> None:
                while True:
                    await asyncio.sleep(SESSION_WATCH_INTERVAL_S)
                    state = await self._probe(session_id, cmd_id, pid_path)
                    if state == "alive":
                        continue
                    if state == "exited":
                        return
                    raise RuntimeError(
                        f"Daytona session shell exited before the command finished, so its exit code is unknown: {command}"
                    )

            watcher = asyncio.ensure_future(watch())
            try:
                done, _ = await asyncio.wait({logs, watcher}, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t is watcher:
                        t.result()
            finally:
                watcher.cancel()
                if not logs.done():
                    logs.cancel()
            if partial[0]:
                out_tail.push(partial[0])
                on_line(partial[0])
            info = await proc.get_session_command(session_id, cmd_id)
            code = _attr(info, "exit_code", "exitCode")
            return result(str(out_tail), str(err_tail), 0 if code is None else code)
        finally:
            try:
                await proc.delete_session(session_id)
            except Exception:  # noqa: BLE001
                pass
            try:
                await proc.exec(f"rm -f {pid_path}")
            except Exception:  # noqa: BLE001
                pass

    async def copy_in(self, host_path: str, sandbox_path: str) -> None:
        if not os.path.isdir(host_path):
            await self._sb.fs.upload_file(host_path, sandbox_path)
            return
        files = walk_files(host_path)
        if not files:
            return
        daytona = require("daytona", "daytona")
        uploads = [daytona.FileUpload(source=f, destination=posixpath.join(sandbox_path, os.path.relpath(f, host_path).replace(os.sep, "/"))) for f in files]
        await self._sb.fs.upload_files(uploads)

    async def copy_file_out(self, sandbox_path: str, host_path: str) -> None:
        os.makedirs(os.path.dirname(host_path) or ".", exist_ok=True)
        await self._sb.fs.download_file(sandbox_path, host_path)

    async def close(self) -> None:
        await self._client.delete(self._sb)


def daytona(
    *,
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    target: Optional[str] = None,
    create: Any = None,
    env: Optional[Dict[str, str]] = None,
    max_output_tail_chars: int = MAX_TAIL_CHARS,
) -> IsolatedSandboxProvider:
    """Run the agent in a Daytona sandbox. ``create`` is a
    ``CreateSandboxFromImageParams`` / ``CreateSandboxFromSnapshotParams``."""

    async def make(opts: IsolatedCreateOptions) -> DaytonaHandle:
        mod = require("daytona", "daytona")
        cfg: Dict[str, Any] = {}
        if api_key:
            cfg["api_key"] = api_key
        if api_url:
            cfg["api_url"] = api_url
        if target:
            cfg["target"] = target
        client = mod.AsyncDaytona(mod.DaytonaConfig(**cfg)) if cfg else mod.AsyncDaytona()
        params = create
        if params is None:
            params = mod.CreateSandboxFromSnapshotParams(env_vars=dict(opts.env))
        else:
            merged = {**opts.env, **(getattr(params, "env_vars", None) or {})}
            try:
                params.env_vars = merged
            except Exception:  # noqa: BLE001
                pass
        sandbox = await client.create(params)
        work = None
        for name in ("get_work_dir", "get_user_home_dir", "get_user_root_dir"):
            fn = getattr(sandbox, name, None)
            if fn is not None:
                try:
                    work = await _maybe(fn())
                except Exception:  # noqa: BLE001
                    work = None
                if work:
                    break
        worktree = posixpath.join(work or "/home/daytona", "workspace")
        await sandbox.fs.create_folder(worktree, "755")
        return DaytonaHandle(client, sandbox, worktree, max_output_tail_chars)

    return IsolatedSandboxProvider(name="daytona", create=make, env=dict(env or {}))

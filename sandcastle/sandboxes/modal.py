"""Modal sandbox provider. Port of ``sandboxes/modal.ts`` and ``modalExec.ts``.

Needs ``pip install modal`` and a Modal token (``MODAL_TOKEN_ID`` /
``MODAL_TOKEN_SECRET``, the ``token_id`` / ``token_secret`` options, or
``~/.modal.toml``). Without ``image``, sandcastle builds ``node:24-bookworm``
with Claude Code installed (Modal caches it). Every command runs as root;
``IS_SANDBOX=1`` lets Claude Code skip its permission checks as root.
"""

from __future__ import annotations

import os
import posixpath
from typing import Any, Dict, Optional

from ..display import MAX_TAIL_CHARS, BoundedTail
from ..sandbox_provider import ExecResult, IsolatedCreateOptions, IsolatedSandboxProvider
from ._cloud import require, result, walk_files

MODAL_REPO_PATH = "/workspace"
DEFAULT_APP_NAME = "sandcastle"
DEFAULT_TIMEOUT_MS = 60 * 60 * 1000
DEFAULT_BASE_IMAGE = "node:24-bookworm"
DEFAULT_IMAGE_COMMANDS = ["RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force"]


class ModalHandle:
    def __init__(self, sandbox: Any, max_tail: int) -> None:
        self._sb = sandbox
        self.worktree_path = MODAL_REPO_PATH
        self.sandbox_id = getattr(sandbox, "object_id", None)
        self._max_tail = max_tail

    async def exec(self, command: str, *, on_line=None, cwd: Optional[str] = None, sudo: bool = False, stdin: Optional[str] = None) -> ExecResult:
        proc = await self._sb.exec.aio("sh", "-c", command, workdir=cwd or MODAL_REPO_PATH)
        if stdin:
            proc.stdin.write(stdin.encode("utf-8"))
        proc.stdin.write_eof()
        await proc.stdin.drain.aio()
        if on_line is None:
            out = await proc.stdout.read.aio()
            err = await proc.stderr.read.aio()
            code = await proc.wait.aio()
            return result(out, err, code)
        import asyncio

        out_tail = BoundedTail(self._max_tail, "\n")
        err_tail = BoundedTail(self._max_tail, "")

        async def lines() -> None:
            partial = ""
            async for chunk in proc.stdout:
                parts = (partial + chunk).split("\n")
                partial = parts.pop()
                for line in parts:
                    out_tail.push(line)
                    on_line(line)
            if partial:
                out_tail.push(partial)
                on_line(partial)

        async def drain() -> None:
            async for chunk in proc.stderr:
                err_tail.push(chunk)

        await asyncio.gather(lines(), drain())
        code = await proc.wait.aio()
        return result(str(out_tail), str(err_tail), code)

    async def _write_file(self, host_path: str, sandbox_path: str) -> None:
        with open(host_path, "rb") as src:
            data = src.read()
        parent = posixpath.dirname(sandbox_path)
        if parent:
            p = await self._sb.exec.aio("mkdir", "-p", parent)
            await p.wait.aio()
        f = await self._sb.open.aio(sandbox_path, "wb")
        try:
            await f.write.aio(data)
        finally:
            await f.close.aio()

    async def copy_in(self, host_path: str, sandbox_path: str) -> None:
        if not os.path.isdir(host_path):
            await self._write_file(host_path, sandbox_path)
            return
        p = await self._sb.exec.aio("mkdir", "-p", sandbox_path)
        await p.wait.aio()
        for f in walk_files(host_path):
            rel = os.path.relpath(f, host_path).replace(os.sep, "/")
            await self._write_file(f, posixpath.join(sandbox_path, rel))

    async def copy_file_out(self, sandbox_path: str, host_path: str) -> None:
        f = await self._sb.open.aio(sandbox_path, "rb")
        try:
            data = await f.read.aio()
        finally:
            await f.close.aio()
        os.makedirs(os.path.dirname(host_path) or ".", exist_ok=True)
        with open(host_path, "wb") as out:
            out.write(data)

    async def close(self) -> None:
        await self._sb.terminate.aio()


def modal(
    *,
    token_id: Optional[str] = None,
    token_secret: Optional[str] = None,
    environment: Optional[str] = None,
    app_name: str = DEFAULT_APP_NAME,
    image: Any = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    create: Optional[Dict[str, Any]] = None,
    env: Optional[Dict[str, str]] = None,
    max_output_tail_chars: int = MAX_TAIL_CHARS,
) -> IsolatedSandboxProvider:
    """Run the agent in a Modal sandbox. ``create`` passes extra
    ``Sandbox.create`` options (``cpu``, ``memory``, ``region``...)."""

    async def make(opts: IsolatedCreateOptions) -> ModalHandle:
        m = require("modal", "modal")
        if token_id:
            os.environ["MODAL_TOKEN_ID"] = token_id
        if token_secret:
            os.environ["MODAL_TOKEN_SECRET"] = token_secret
        if environment:
            os.environ["MODAL_ENVIRONMENT"] = environment
        app = await m.App.lookup.aio(app_name, create_if_missing=True)
        if isinstance(image, str) and image:
            img = m.Image.from_registry(image)
        elif image is not None and not isinstance(image, str):
            img = image
        else:
            img = m.Image.from_registry(DEFAULT_BASE_IMAGE).dockerfile_commands(DEFAULT_IMAGE_COMMANDS)
        extra = dict(create or {})
        sb_env = {**opts.env, **(extra.pop("env", None) or {})}
        sandbox = await m.Sandbox.create.aio(app=app, image=img, timeout=int(timeout_ms / 1000), secrets=[m.Secret.from_dict(sb_env)], **extra)
        try:
            p = await sandbox.exec.aio("mkdir", "-p", MODAL_REPO_PATH)
            code = await p.wait.aio()
            if code != 0:
                raise RuntimeError(f"mkdir exited with {code}")
        except Exception as e:  # noqa: BLE001
            try:
                await sandbox.terminate.aio()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"modal(): could not create {MODAL_REPO_PATH}: {e}") from e
        return ModalHandle(sandbox, max_output_tail_chars)

    return IsolatedSandboxProvider(name="modal", create=make, env={"IS_SANDBOX": "1", **(env or {})})

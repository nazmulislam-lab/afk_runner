"""Fly.io Sprites provider. Port of ``sandboxes/sprites.ts``,
``spritesExec.ts`` and ``spritesShutdown.ts``, on the official Python SDK.

Needs ``pip install sprites-py`` and ``SPRITES_TOKEN`` (or ``SPRITE_TOKEN``,
or ``token=``). Each sandbox is a new Sprite with git, Node and the
``claude`` CLI preinstalled; the repo lands in ``/home/sprite/workspace``.
A Sprite runs until deleted: ``close()`` deletes it, and so does an
interrupted process (a background ``curl`` DELETE).
"""

from __future__ import annotations

import io
import os
import posixpath
import re
import subprocess
import uuid
from typing import Any, Dict, Optional

from ..display import MAX_TAIL_CHARS
from ..sandbox_provider import ExecResult, IsolatedCreateOptions, IsolatedSandboxProvider
from ..shutdown import register_shutdown
from ._cloud import LineSink, require, result, shell_escape, walk_files

SPRITE_HOME = "/home/sprite"
SPRITES_REPO_PATH = posixpath.join(SPRITE_HOME, "workspace")
DEFAULT_BASE_URL = "https://api.sprites.dev"


def _resolve_token(token: Optional[str]) -> str:
    t = token or os.environ.get("SPRITES_TOKEN") or os.environ.get("SPRITE_TOKEN")
    if not t:
        raise ValueError("sprites(): no Sprites token. Set SPRITES_TOKEN (or SPRITE_TOKEN), or pass the token option.")
    return t


def _strip_url_queries(text: str) -> str:
    return re.sub(r"((?:wss?|https?)://[^\s?#)]*)[?#][^\s)]*", r"\1", text, flags=re.I)


def register_sprite_deletion(base_url: str, token: str, name: str):
    """On SIGINT/SIGTERM/exit, delete the Sprite from a detached process."""

    def teardown() -> None:
        url = f"{base_url}/v1/sprites/{name}"
        try:
            subprocess.Popen(
                ["curl", "-s", "-X", "DELETE", "-H", "@-", url],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            ).communicate(f"Authorization: Bearer {token}\n".encode(), timeout=2)
        except Exception:  # noqa: BLE001
            pass

    return register_shutdown(teardown)


class SpritesHandle:
    def __init__(self, sprite: Any, env: Dict[str, str], max_tail: int, unregister) -> None:
        self._sprite = sprite
        self.worktree_path = SPRITES_REPO_PATH
        self.sandbox_id = getattr(sprite, "name", None)
        self._env = env
        self._max_tail = max_tail
        self._unregister = unregister

    async def exec(self, command: str, *, on_line=None, cwd: Optional[str] = None, sudo: bool = False, stdin: Optional[str] = None) -> ExecResult:
        return await exec_in_sprite(self._sprite, command, cwd or SPRITES_REPO_PATH, self._env, on_line, sudo, stdin, self._max_tail)

    async def copy_in(self, host_path: str, sandbox_path: str) -> None:
        fs = self._sprite.filesystem()
        if not os.path.isdir(host_path):
            with open(host_path, "rb") as f:
                await fs.path(sandbox_path).write_bytes(f.read())
            return
        for f in walk_files(host_path):
            rel = os.path.relpath(f, host_path).replace(os.sep, "/")
            with open(f, "rb") as src:
                await fs.path(posixpath.join(sandbox_path, rel)).write_bytes(src.read())

    async def copy_file_out(self, sandbox_path: str, host_path: str) -> None:
        data = await self._sprite.filesystem().path(sandbox_path).read_bytes()
        os.makedirs(os.path.dirname(host_path) or ".", exist_ok=True)
        with open(host_path, "wb") as f:
            f.write(data)

    async def close(self) -> None:
        sprites = require("sprites", "sprites-py")
        try:
            await self._sprite.delete()
        except sprites.NotFoundError:
            pass
        self._unregister()


async def exec_in_sprite(sprite: Any, command: str, cwd: str, env: Dict[str, str], on_line, sudo: bool, stdin: Optional[str], max_tail: int) -> ExecResult:
    line = f"sudo sh -c {shell_escape(command)}" if sudo else command
    streamed = on_line is not None
    out = LineSink(on_line, max_tail if streamed else None)
    err = LineSink(None, max_tail if streamed else None, "")
    cmd = sprite.command("sh", "-c", line, cwd=cwd, env=env, stdin=io.BytesIO((stdin or "").encode("utf-8")), stdout=out, stderr=err)
    try:
        code = await cmd._execute()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"sprites(): the connection failed: {command} ({_strip_url_queries(str(e))})") from None
    out.finish()
    if not isinstance(code, int) or code < 0:
        raise RuntimeError(f"sprites(): the connection closed before the command exited: {command}")
    return result(out.text(), err.text(), code)


def sprites(*, token: Optional[str] = None, env: Optional[Dict[str, str]] = None, max_output_tail_chars: int = MAX_TAIL_CHARS) -> IsolatedSandboxProvider:
    """Run each sandbox in a new Fly.io Sprite."""

    async def create(opts: IsolatedCreateOptions) -> SpritesHandle:
        tok = _resolve_token(token)
        mod = require("sprites", "sprites-py")
        client = mod.AsyncSpritesClient(token=tok)
        name = f"sandcastle-{uuid.uuid4()}"
        unregister = register_sprite_deletion(getattr(client, "base_url", DEFAULT_BASE_URL), tok, name)
        try:
            sprite = await client.create_sprite(name)
            r = await exec_in_sprite(sprite, f"mkdir -p {SPRITES_REPO_PATH}", SPRITE_HOME, {}, None, False, None, MAX_TAIL_CHARS)
            if r.exit_code != 0:
                raise RuntimeError(f"sprites(): could not create {SPRITES_REPO_PATH}: {r.stderr.strip()}")
            return SpritesHandle(sprite, dict(opts.env), max_output_tail_chars, unregister)
        except BaseException:
            try:
                await client.delete_sprite(name)
            except Exception:  # noqa: BLE001
                pass
            unregister()
            raise

    return IsolatedSandboxProvider(name="sprites", create=create, env=dict(env or {}))

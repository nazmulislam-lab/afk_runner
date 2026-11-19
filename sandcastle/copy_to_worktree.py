"""Copy host files into a worktree. Port of ``CopyToWorktree.ts``.

Uses copy-on-write when the filesystem supports it (``cp -cR`` on macOS,
``cp -R --reflink=auto`` elsewhere), falling back to a plain ``cp -R``.
Each path lands at the same relative path; a directory merges into an
existing one. Missing paths are skipped.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import List, Optional, Sequence

from .errors import CopyToWorktreeError, CopyToWorktreeTimeoutError, with_timeout

COPY_TO_WORKTREE_TIMEOUT_MS = 60_000


def get_copy_on_write_flags(platform: str = sys.platform) -> List[str]:
    return ["-cR"] if platform == "darwin" else ["-R", "--reflink=auto"]


async def _run_cp(args: Sequence[str]) -> tuple:
    proc = await asyncio.create_subprocess_exec(
        "cp", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, err = await proc.communicate()
    except asyncio.CancelledError:
        # Killed if interrupted (e.g. by the timeout) so it can't keep writing.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise
    return proc.returncode, err.decode("utf-8", "replace")


async def copy_to_worktree(
    paths: Sequence[str], host_repo_dir: str, worktree_path: str, timeout_ms: Optional[float] = None
) -> None:
    effective = timeout_ms if timeout_ms is not None else COPY_TO_WORKTREE_TIMEOUT_MS

    async def work() -> None:
        cow = get_copy_on_write_flags()
        for rel in paths:
            src = os.path.join(host_repo_dir, rel)
            if not os.path.exists(src):
                continue
            dest = os.path.join(worktree_path, rel)
            is_dir = os.path.isdir(src)
            to_create = dest if is_dir else os.path.dirname(dest)
            cp_src = f"{src}{os.sep}." if is_dir else src
            try:
                os.makedirs(to_create, exist_ok=True)
            except OSError as e:
                raise CopyToWorktreeError(
                    f"Failed to copy {rel} to worktree: {e}", path=rel, stderr=str(e), exit_code=None
                ) from e
            code, _ = await _run_cp([*cow, cp_src, dest])
            if code == 0:
                continue
            code, err = await _run_cp(["-R", cp_src, dest])
            if code != 0:
                raise CopyToWorktreeError(
                    f"Failed to copy {rel} to worktree: {err or f'exit {code}'}",
                    path=rel,
                    stderr=err,
                    exit_code=code,
                )

    await with_timeout(
        effective,
        lambda: CopyToWorktreeTimeoutError(
            f"Copying files to worktree timed out after {effective}ms", timeout_ms=effective, paths=list(paths)
        ),
        work(),
    )

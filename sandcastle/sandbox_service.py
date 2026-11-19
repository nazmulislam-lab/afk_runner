"""Uniform async service over any sandbox handle, and sandbox start-up.

Port of ``sandboxService.ts``, ``startSandbox.ts`` and
``createClosingIfAbandoned.ts``.
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import sys
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from .errors import (
    ContainerStartTimeoutError,
    CopyError,
    CopyToWorktreeTimeoutError,
    ExecError,
    SyncInTimeoutError,
    WorktreeError,
    with_timeout,
)
from .mounts import SANDBOX_REPO_DIR, MountEntry, normalize_mounts, protect_git_control_files
from .sandbox_provider import (
    BindMountCreateOptions,
    ExecResult,
    IsolatedCreateOptions,
    Mount,
    NoSandboxCreateOptions,
)
from .sync import sync_in

CONTAINER_START_TIMEOUT_MS = 120_000
SYNC_IN_TIMEOUT_MS = 120_000
COPY_PATHS_TIMEOUT_MS = 120_000


class SandboxService:
    """Wraps a provider handle: exec / copy_in / copy_file_out with
    sandcastle errors (``ExecError``, ``CopyError``)."""

    def __init__(self, handle: Any) -> None:
        self.handle = handle

    async def exec(
        self,
        command: str,
        *,
        on_line: Optional[Callable[[str], None]] = None,
        cwd: Optional[str] = None,
        sudo: bool = False,
        stdin: Optional[str] = None,
    ) -> ExecResult:
        kwargs: Dict[str, Any] = {}
        if on_line is not None:
            kwargs["on_line"] = on_line
        if cwd is not None:
            kwargs["cwd"] = cwd
        if sudo:
            kwargs["sudo"] = True
        if stdin is not None:
            kwargs["stdin"] = stdin
        try:
            return await self.handle.exec(command, **kwargs)
        except (asyncio.CancelledError, ExecError):
            raise
        except Exception as e:  # noqa: BLE001
            raise ExecError(f"exec failed: {e}", command=command) from e

    async def copy_in(self, host_path: str, sandbox_path: str) -> None:
        if hasattr(self.handle, "copy_in"):
            fn, label = self.handle.copy_in, "copyIn"
        elif hasattr(self.handle, "copy_file_in"):
            fn, label = self.handle.copy_file_in, "copyFileIn"
        else:
            raise CopyError("copyIn is not supported for this sandbox provider")
        try:
            await fn(host_path, sandbox_path)
        except Exception as e:  # noqa: BLE001
            raise CopyError(f"{label} failed: {e}") from e

    async def copy_file_out(self, sandbox_path: str, host_path: str) -> None:
        if not hasattr(self.handle, "copy_file_out"):
            raise CopyError("copyFileOut is not supported for this sandbox provider")
        try:
            await self.handle.copy_file_out(sandbox_path, host_path)
        except Exception as e:  # noqa: BLE001
            raise CopyError(f"copyFileOut failed: {e}") from e


def make_sandbox_from_handle(handle: Any) -> SandboxService:
    return SandboxService(handle)


@dataclass
class StartSandboxResult:
    handle: Any
    sandbox: SandboxService
    worktree_path: str


def _to_result(handle: Any) -> StartSandboxResult:
    return StartSandboxResult(handle, SandboxService(handle), handle.worktree_path)


async def create_closing_if_abandoned(create: Callable[[], Awaitable[Any]], on_error: Callable[[BaseException], BaseException]) -> Any:
    """Run a provider's ``create()``. If we stop waiting (timeout or
    cancellation) the provider may still finish creating the sandbox; close
    that handle when it arrives so a late sandbox never outlives the run."""
    task = asyncio.ensure_future(create())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:

        def close_late(t: "asyncio.Future[Any]") -> None:
            if t.cancelled() or t.exception() is not None:
                return
            try:
                asyncio.ensure_future(t.result().close())
            except Exception:  # noqa: BLE001
                pass

        task.add_done_callback(close_late)
        raise
    except Exception as e:  # noqa: BLE001
        raise on_error(e) from e


async def start_no_sandbox(provider: Any, worktree_path: str, env: Dict[str, str]) -> StartSandboxResult:
    try:
        handle = await provider.create(NoSandboxCreateOptions(worktree_path=worktree_path, env=env))
    except Exception as e:  # noqa: BLE001
        raise WorktreeError(f"Provider '{provider.name}' create failed: {e}") from e
    return _to_result(handle)


async def start_bind_mount_sandbox(
    *,
    provider: Any,
    host_repo_dir: str,
    env: Dict[str, str],
    worktree_or_repo_path: str,
    git_mounts: Sequence[MountEntry],
    repo_dir: str = SANDBOX_REPO_DIR,
) -> StartSandboxResult:
    raw = [Mount(worktree_or_repo_path, repo_dir)] + [Mount(m.host_path, m.sandbox_path, m.readonly) for m in git_mounts]
    mounts = protect_git_control_files(normalize_mounts(raw, worktree_or_repo_path, SANDBOX_REPO_DIR), worktree_or_repo_path)
    wt = worktree_or_repo_path.replace("\\", "/") if sys.platform == "win32" else worktree_or_repo_path

    async def create() -> Any:
        return await provider.create(
            BindMountCreateOptions(worktree_path=wt, host_repo_path=host_repo_dir, mounts=mounts, env=env)
        )

    handle = await with_timeout(
        CONTAINER_START_TIMEOUT_MS,
        lambda: ContainerStartTimeoutError(
            f"Sandbox container start timed out after {CONTAINER_START_TIMEOUT_MS}ms", timeout_ms=CONTAINER_START_TIMEOUT_MS
        ),
        create_closing_if_abandoned(create, lambda e: WorktreeError(f"Provider '{provider.name}' create failed: {e}")),
    )
    return _to_result(handle)


async def _copy_paths_in(handle: Any, paths: Sequence[str], source_dir: str) -> None:
    async def work() -> None:
        for rel in paths:
            host_path = os.path.join(source_dir, rel)
            if not os.path.exists(host_path):
                continue
            try:
                await handle.copy_in(host_path, posixpath.join(handle.worktree_path, rel.replace("\\", "/")))
            except Exception as e:  # noqa: BLE001
                raise WorktreeError(f"Failed to copy {rel} into sandbox: {e}") from e

    await with_timeout(
        COPY_PATHS_TIMEOUT_MS,
        lambda: CopyToWorktreeTimeoutError(
            f"Copying paths to worktree timed out after {COPY_PATHS_TIMEOUT_MS}ms", timeout_ms=COPY_PATHS_TIMEOUT_MS, paths=list(paths)
        ),
        work(),
    )


async def start_isolated_sandbox(
    *,
    provider: Any,
    host_repo_dir: str,
    env: Dict[str, str],
    copy_paths: Optional[Sequence[str]] = None,
    copy_source_dir: Optional[str] = None,
) -> StartSandboxResult:
    """Create the handle, sync the repo in via git bundle, copy ``copy_paths``.
    The handle is closed on any failure so a remote sandbox never outlives a
    failed setup."""

    async def create() -> Any:
        return await provider.create(IsolatedCreateOptions(env=env))

    handle = await with_timeout(
        CONTAINER_START_TIMEOUT_MS,
        lambda: ContainerStartTimeoutError(
            f"Isolated sandbox container start timed out after {CONTAINER_START_TIMEOUT_MS}ms",
            timeout_ms=CONTAINER_START_TIMEOUT_MS,
        ),
        create_closing_if_abandoned(create, lambda e: WorktreeError(f"Isolated provider '{provider.name}' setup failed: {e}")),
    )
    try:
        await with_timeout(
            SYNC_IN_TIMEOUT_MS,
            lambda: SyncInTimeoutError(f"Sync-in timed out after {SYNC_IN_TIMEOUT_MS}ms", timeout_ms=SYNC_IN_TIMEOUT_MS),
            sync_in(host_repo_dir, handle),
        )
        if copy_paths:
            await _copy_paths_in(handle, copy_paths, copy_source_dir or host_repo_dir)
    except BaseException:
        try:
            await handle.close()
        except Exception:  # noqa: BLE001
            pass
        raise
    return _to_result(handle)


async def start_sandbox(provider: Any, **kwargs: Any) -> StartSandboxResult:
    """Dispatch on ``provider.tag``."""
    if provider.tag == "bind-mount":
        return await start_bind_mount_sandbox(provider=provider, **kwargs)
    if provider.tag == "none":
        return await start_no_sandbox(provider, kwargs["worktree_or_repo_path"], kwargs["env"])
    return await start_isolated_sandbox(provider=provider, **kwargs)

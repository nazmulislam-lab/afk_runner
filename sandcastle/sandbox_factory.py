"""SandboxFactory: worktree + sandbox acquisition and release.

Port of ``SandboxFactory.ts``. ``with_sandbox(make)`` creates (or reuses) a
worktree, starts the sandbox, calls ``make(info, sandbox)``, then closes the
sandbox and removes or preserves the worktree.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Generic, List, Optional, TypeVar

from . import worktree_manager as wm
from .abort import AbortSignal
from .copy_to_worktree import copy_to_worktree
from .display import Display
from .errors import AgentError, AgentIdleTimeoutError, WorktreeError
from .lifecycle import SandboxHooks, Timeouts, run_host_hooks
from .mounts import SANDBOX_REPO_DIR, patch_git_mounts_for_windows, remove_mount_temp_dirs, resolve_git_mounts
from .sandbox_service import (
    SandboxService,
    StartSandboxResult,
    start_bind_mount_sandbox,
    start_isolated_sandbox,
    start_no_sandbox,
)
from .shutdown import register_shutdown
from .sync import sync_out

A = TypeVar("A")


@dataclass
class SandboxInfo:
    sandbox_repo_path: str
    host_worktree_path: Optional[str] = None
    #: Isolated providers only: sync sandbox changes to the host worktree.
    apply_to_host: Optional[Callable[[], Awaitable[None]]] = None
    #: Bind-mount providers: the handle, used for session capture.
    bind_mount_handle: Any = None


@dataclass
class WithSandboxResult(Generic[A]):
    value: A
    preserved_worktree_path: Optional[str] = None


MakeFn = Callable[[SandboxInfo, SandboxService], Awaitable[A]]


class SandboxFactoryBase:
    async def with_sandbox(self, make: MakeFn) -> WithSandboxResult:  # pragma: no cover
        raise NotImplementedError


class ReuseSandboxFactory(SandboxFactoryBase):
    """Hands every caller the same, already-started sandbox (createSandbox
    and createWorktree handles)."""

    def __init__(self, info: SandboxInfo, sandbox: SandboxService) -> None:
        self.info = info
        self.sandbox = sandbox

    async def with_sandbox(self, make: MakeFn) -> WithSandboxResult:
        return WithSandboxResult(await make(self.info, self.sandbox), None)


@dataclass
class SandboxConfig:
    env: Dict[str, str]
    host_repo_dir: str
    sandbox_provider: Any
    branch_strategy: Any
    copy_to_worktree: Optional[List[str]] = None
    name: Optional[str] = None
    temp_branch: Optional[str] = None
    hooks: Optional[SandboxHooks] = None
    signal: Optional[AbortSignal] = None
    timeouts: Optional[Timeouts] = None


def print_worktree_preserved_message(worktree_path: str, reason: str) -> None:
    print(f"\n{reason}", file=sys.stderr)
    print(f"  To review: cd {worktree_path}", file=sys.stderr)
    print(f"  To clean up: git worktree remove --force {worktree_path}", file=sys.stderr)


async def cleanup_temp_branch(host_repo_dir: str, temp_branch: str) -> None:
    try:
        unmerged = await wm.delete_temp_branch_if_unused(host_repo_dir, temp_branch)
    except Exception:  # noqa: BLE001
        return
    if unmerged == 0:
        return
    s = "" if unmerged == 1 else "s"
    print(f"\nTemporary branch '{temp_branch}' kept: it has {unmerged} commit{s} not on any other branch.", file=sys.stderr)
    print(f"  To merge: git merge {temp_branch}", file=sys.stderr)
    print(f"  To discard: git branch -D {temp_branch}", file=sys.stderr)


async def cleanup_worktree(
    worktree_path: str, succeeded: bool, signal: Optional[AbortSignal], temp: Optional[Dict[str, str]]
) -> Optional[str]:
    """Preserve a dirty (or aborted) worktree; remove a clean one, and its
    merge-to-head temp branch unless that holds unmerged commits."""
    if not succeeded and signal is not None and signal.aborted:
        print_worktree_preserved_message(worktree_path, f"Worktree preserved at {worktree_path}")
        return worktree_path
    try:
        dirty = await wm.has_uncommitted_changes(worktree_path)
    except Exception:  # noqa: BLE001
        dirty = False
    if dirty:
        print_worktree_preserved_message(
            worktree_path,
            f"Run succeeded but worktree has uncommitted changes at {worktree_path}"
            if succeeded
            else f"Worktree preserved at {worktree_path}",
        )
        return worktree_path
    if not succeeded:
        print("\nWorktree removed (no uncommitted changes)", file=sys.stderr)
    await wm.remove(worktree_path)
    if temp:
        await cleanup_temp_branch(temp["host_repo_dir"], temp["branch"])
    return None


def attach_preserved_path(path: Optional[str], e: BaseException) -> BaseException:
    if path is not None:
        if isinstance(e, AgentIdleTimeoutError):
            return AgentIdleTimeoutError(e.message, timeout_ms=e.timeout_ms, preserved_worktree_path=path)
        if isinstance(e, AgentError):
            return AgentError(e.message, preserved_worktree_path=path)
    return e


class WorktreeSandboxFactory(SandboxFactoryBase):
    """Creates a worktree (unless ``head``), starts the provider's sandbox,
    and tears both down afterwards. Port of ``WorktreeDockerSandboxFactory``."""

    def __init__(self, config: SandboxConfig, display: Display) -> None:
        self.c = config
        self.display = display
        bs = config.branch_strategy
        self.is_head = bs.type == "head"
        self.branch = bs.branch if bs.type == "branch" else None
        self.base_branch = getattr(bs, "base_branch", None) if bs.type == "branch" else None
        self.on_worktree_ready = (config.hooks or SandboxHooks()).host_on_worktree_ready

    async def _prune_and_create(self) -> Dict[str, Any]:
        c = self.c
        try:
            await wm.prune_stale(c.host_repo_dir)
        except Exception as e:  # noqa: BLE001
            print("[sandcastle] Warning: failed to prune stale worktrees:", getattr(e, "message", str(e)), file=sys.stderr)
        if self.branch:
            info = await wm.create(c.host_repo_dir, branch=self.branch, base_branch=self.base_branch)
        else:
            info = await wm.create(c.host_repo_dir, name=c.name, temp_branch=c.temp_branch)
        path = info.path
        unregister = register_shutdown(lambda: print_worktree_preserved_message(path, f"Worktree preserved at {path}"))
        return {"path": info.path, "branch": info.branch, "unregister": unregister}

    async def _copy_and_hooks(self, worktree_path: str) -> None:
        c = self.c
        if c.copy_to_worktree:
            await self.display.spinner(
                "Copying to worktree",
                copy_to_worktree(c.copy_to_worktree, c.host_repo_dir, worktree_path, (c.timeouts or Timeouts()).copy_to_worktree_ms),
            )
        if self.on_worktree_ready:
            await run_host_hooks(self.on_worktree_ready, worktree_path, c.signal)

    def _git_mounts(self, worktree_path: str):
        try:
            mounts = resolve_git_mounts(os.path.join(self.c.host_repo_dir, ".git"))
        except OSError as e:
            raise WorktreeError(f"Failed to resolve git mounts: {e}") from e
        return patch_git_mounts_for_windows(mounts, worktree_path, SANDBOX_REPO_DIR)

    async def _release_worktree(self, wt: Dict[str, Any], succeeded: bool) -> Optional[str]:
        wt["unregister"]()
        temp = None if self.branch is not None else {"host_repo_dir": self.c.host_repo_dir, "branch": wt["branch"]}
        return await cleanup_worktree(wt["path"], succeeded, self.c.signal, temp)

    @staticmethod
    async def _close(handle: Any) -> None:
        try:
            await handle.close()
        except Exception:  # noqa: BLE001
            pass

    async def _in_worktree(self, start: Callable[[Dict[str, Any]], Awaitable[Any]]) -> WithSandboxResult:
        """Nested acquire/release: the worktree is always cleaned up, even when
        copying, hooks or sandbox start fail."""
        wt = await self._prune_and_create()
        preserved: Optional[str] = None
        succeeded = False
        try:
            value = await start(wt)
            succeeded = True
        except BaseException as e:
            try:
                preserved = await self._release_worktree(wt, False)
            except Exception:  # noqa: BLE001
                pass
            wrapped = attach_preserved_path(preserved, e)
            if wrapped is e:
                raise
            raise wrapped from e
        preserved = await self._release_worktree(wt, succeeded)
        return WithSandboxResult(value, preserved)

    async def with_sandbox(self, make: MakeFn) -> WithSandboxResult:
        c = self.c
        provider = c.sandbox_provider

        # No-sandbox providers: run directly on the host.
        if provider.tag == "none":
            if self.is_head:
                if self.on_worktree_ready:
                    await run_host_hooks(self.on_worktree_ready, c.host_repo_dir, c.signal)
                started = await start_no_sandbox(provider, c.host_repo_dir, c.env)
                try:
                    value = await make(
                        SandboxInfo(sandbox_repo_path=started.worktree_path, host_worktree_path=c.host_repo_dir), started.sandbox
                    )
                finally:
                    await self._close(started.handle)
                return WithSandboxResult(value, None)

            async def run_none(wt: Dict[str, Any]) -> Any:
                await self._copy_and_hooks(wt["path"])
                started = await start_no_sandbox(provider, wt["path"], c.env)
                try:
                    return await make(
                        SandboxInfo(sandbox_repo_path=started.worktree_path, host_worktree_path=wt["path"]), started.sandbox
                    )
                finally:
                    await self._close(started.handle)

            return await self._in_worktree(run_none)

        # Isolated providers: worktree on host, sync via git bundle.
        if provider.tag == "isolated":

            async def run_isolated(wt: Dict[str, Any]) -> Any:
                if self.on_worktree_ready:
                    await run_host_hooks(self.on_worktree_ready, wt["path"], c.signal)
                started = await start_isolated_sandbox(
                    provider=provider,
                    host_repo_dir=wt["path"],
                    env=c.env,
                    copy_paths=c.copy_to_worktree,
                    copy_source_dir=c.host_repo_dir,
                )
                handle = started.handle
                try:
                    return await make(
                        SandboxInfo(
                            sandbox_repo_path=started.worktree_path,
                            host_worktree_path=wt["path"],
                            apply_to_host=lambda: sync_out(wt["path"], handle),
                        ),
                        started.sandbox,
                    )
                finally:
                    await self._close(handle)

            return await self._in_worktree(run_isolated)

        # Bind-mount, head mode: mount the host repo directly.
        if self.is_head:
            if self.on_worktree_ready:
                await run_host_hooks(self.on_worktree_ready, c.host_repo_dir, c.signal)
            git_mounts = self._git_mounts(c.host_repo_dir)
            try:
                started = await start_bind_mount_sandbox(
                    provider=provider,
                    host_repo_dir=c.host_repo_dir,
                    env=c.env,
                    worktree_or_repo_path=c.host_repo_dir,
                    git_mounts=git_mounts,
                    repo_dir=SANDBOX_REPO_DIR,
                )
                try:
                    value = await make(
                        SandboxInfo(
                            sandbox_repo_path=started.worktree_path,
                            host_worktree_path=c.host_repo_dir,
                            bind_mount_handle=started.handle,
                        ),
                        started.sandbox,
                    )
                finally:
                    await self._close(started.handle)
            finally:
                remove_mount_temp_dirs(git_mounts)
            return WithSandboxResult(value, None)

        # Bind-mount, worktree mode (merge-to-head or branch).
        async def run_bind(wt: Dict[str, Any]) -> Any:
            await self._copy_and_hooks(wt["path"])
            git_mounts = self._git_mounts(wt["path"])
            try:
                started = await start_bind_mount_sandbox(
                    provider=provider,
                    host_repo_dir=c.host_repo_dir,
                    env=c.env,
                    worktree_or_repo_path=wt["path"],
                    git_mounts=git_mounts,
                    repo_dir=SANDBOX_REPO_DIR,
                )
                try:
                    return await make(
                        SandboxInfo(
                            sandbox_repo_path=started.worktree_path,
                            host_worktree_path=wt["path"],
                            bind_mount_handle=started.handle,
                        ),
                        started.sandbox,
                    )
                finally:
                    await self._close(started.handle)
            finally:
                remove_mount_temp_dirs(git_mounts)

        return await self._in_worktree(run_bind)

"""Git worktree management. Port of ``WorktreeManager.ts``."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional, Sequence

from .errors import HostGitError, WorktreeError, WorktreeTimeoutError, with_timeout
from .host_git import host_git

WORKTREE_TIMEOUT_MS = 30_000

#: Stop ``git worktree add -b`` from writing upstream tracking config.
NO_CONFIG_LOCK_FLAGS = ["-c", "branch.autoSetupMerge=false", "-c", "push.autoSetupRemote=false"]


def _format_timestamp(d: datetime) -> str:
    return d.strftime("%Y%m%d-%H%M%S")


def _random_branch_suffix() -> str:
    return secrets.token_hex(3)


def sanitize_name(name: str) -> str:
    """Lowercase; every non ``[a-z0-9]`` char becomes one dash per UTF-16 unit."""
    out = []
    for ch in name.lower():
        if re.match(r"[a-z0-9]", ch):
            out.append(ch)
        else:
            out.append("-" * (2 if ord(ch) > 0xFFFF else 1))
    return "".join(out)


async def _exec_git(args: Sequence[str], cwd: str) -> str:
    try:
        return await host_git(list(args), cwd)
    except HostGitError as e:
        raise WorktreeError(e.message) from e


def generate_temp_branch_name(name: Optional[str] = None) -> str:
    """``sandcastle/<name>/<YYYYMMDD-HHMMSS>-<hex>`` or ``sandcastle/<ts>-<hex>``."""
    ts = _format_timestamp(datetime.now())
    suffix = _random_branch_suffix()
    if name:
        return f"sandcastle/{sanitize_name(name)}/{ts}-{suffix}"
    return f"sandcastle/{ts}-{suffix}"


async def get_current_branch(repo_dir: str) -> str:
    return (await _exec_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_dir)).strip()


@dataclass
class WorktreeInfo:
    path: str
    branch: str


@dataclass
class WorktreeEntry:
    path: str
    branch: Optional[str]


def _norm(p: str) -> str:
    return p.replace("\\", "/")


def find_colliding_worktree(existing: Sequence[WorktreeEntry], branch: str, worktree_path: str) -> Optional[WorktreeEntry]:
    for wt in existing:
        if wt.branch == branch:
            return wt
    for wt in existing:
        if _norm(wt.path) == _norm(worktree_path):
            return wt
    return None


def is_managed_worktree_path(worktree_path: str, worktrees_dir: str) -> bool:
    return _norm(worktree_path).startswith(_norm(worktrees_dir))


def is_orphaned_worktree_path(entry_path: str, active: Iterable[str]) -> bool:
    n = _norm(entry_path)
    return all(_norm(a) != n for a in active)


async def _list_worktrees(repo_dir: str) -> List[WorktreeEntry]:
    output = await _exec_git(["worktree", "list", "--porcelain"], repo_dir)
    entries: List[WorktreeEntry] = []
    path: Optional[str] = None
    br: Optional[str] = None
    for line in output.split("\n"):
        if line.startswith("worktree "):
            if path is not None:
                entries.append(WorktreeEntry(path, br))
            path = line[len("worktree ") :].strip()
            br = None
        elif line.startswith("branch "):
            br = line[len("branch refs/heads/") :].strip()
    if path is not None:
        entries.append(WorktreeEntry(path, br))
    return entries


def _worktree_timeout(repo_dir: str, operation: str, noun: str) -> WorktreeTimeoutError:
    return WorktreeTimeoutError(
        f"Worktree {noun} timed out after {WORKTREE_TIMEOUT_MS}ms",
        timeout_ms=WORKTREE_TIMEOUT_MS,
        path=repo_dir,
        operation=operation,
    )


async def _head_sha(worktree_path: str) -> str:
    try:
        return (await _exec_git(["rev-parse", "HEAD"], worktree_path)).strip()
    except WorktreeError:
        return ""


async def _try_unlocked_git(args: Sequence[str], worktree_path: str) -> bool:
    try:
        await _exec_git([*NO_CONFIG_LOCK_FLAGS, *args], worktree_path)
        return True
    except WorktreeError:
        return False


async def _fast_forward_from_origin(worktree_path: str, branch: str) -> None:
    """Clean-reuse path: fetch ``origin/<branch>`` and fast-forward when safe.
    Every failure is non-fatal (ADR 0003)."""
    try:
        head_ref = (await _exec_git(["symbolic-ref", "--quiet", "HEAD"], worktree_path)).strip()
    except WorktreeError:
        head_ref = ""
    if head_ref != f"refs/heads/{branch}":
        print(
            f"Reusing worktree at {worktree_path} (branch '{branch}') — HEAD is not on '{branch}', skipping origin refresh"
        )
        return
    if not await _try_unlocked_git(["fetch", "origin", branch], worktree_path):
        print(f"Could not fetch from origin (reusing worktree at {worktree_path} as-is, branch '{branch}')")
        return
    before = await _head_sha(worktree_path)
    if not await _try_unlocked_git(["merge", "--ff-only", f"origin/{branch}"], worktree_path):
        print(f"Branch '{branch}' has diverged from origin (reusing worktree at {worktree_path} as-is)")
        return
    after = await _head_sha(worktree_path)
    if before and after and before != after:
        print(f"Fast-forwarded worktree at {worktree_path} (branch '{branch}') to origin/{branch}")
    else:
        print(f"Reusing existing worktree at {worktree_path} (branch '{branch}')")


async def _reuse_colliding_worktree(collision: WorktreeEntry, branch: str, worktrees_dir: str) -> WorktreeInfo:
    if is_managed_worktree_path(collision.path, worktrees_dir):
        if await has_uncommitted_changes(collision.path):
            print(
                f"Reusing worktree at {collision.path} (branch '{branch}') — worktree has uncommitted changes",
                file=sys.stderr,
            )
        else:
            await _fast_forward_from_origin(collision.path, branch)
        return WorktreeInfo(os.path.normpath(collision.path), branch)
    raise WorktreeError(
        f"Branch '{branch}' is already checked out in worktree at '{collision.path}'. "
        "Sandcastle's branch and merge-to-head strategies run the agent in a git worktree under "
        ".sandcastle/worktrees/, and git refuses to check out the same branch in two worktrees at once "
        "(HEAD would become ambiguous). Pick a different branch, or switch the main working tree to a "
        "different branch before re-running."
    )


async def _add_branch_worktree(repo_dir: str, worktree_path: str, branch: str, base_branch: Optional[str]) -> None:
    try:
        await _exec_git([*NO_CONFIG_LOCK_FLAGS, "worktree", "add", worktree_path, branch], repo_dir)
    except WorktreeError as e:
        if "invalid reference" not in e.message:
            raise
        await _exec_git(
            [*NO_CONFIG_LOCK_FLAGS, "worktree", "add", "-b", branch, worktree_path, base_branch or "HEAD"],
            repo_dir,
        )


async def _add_temp_branch_worktree(repo_dir: str, worktree_path: str, branch: str) -> None:
    try:
        await _exec_git([*NO_CONFIG_LOCK_FLAGS, "worktree", "add", "-b", branch, worktree_path, "HEAD"], repo_dir)
    except WorktreeError as e:
        if "already checked out" in e.message or "already exists" in e.message:
            raise WorktreeError(
                f"Branch '{branch}' is already checked out in another worktree. "
                "Use a different branch name, or wait for the other run to finish."
            ) from e
        raise


async def create(
    repo_dir: str,
    *,
    branch: Optional[str] = None,
    base_branch: Optional[str] = None,
    name: Optional[str] = None,
    temp_branch: Optional[str] = None,
) -> WorktreeInfo:
    """Create (or reuse) a worktree at ``.sandcastle/worktrees/<name>/``."""

    async def work() -> WorktreeInfo:
        worktrees_dir = os.path.join(repo_dir, ".sandcastle", "worktrees")
        try:
            os.makedirs(worktrees_dir, exist_ok=True)
        except OSError as e:
            raise WorktreeError(str(e)) from e
        explicit = bool(branch)
        br = branch if explicit else (temp_branch or generate_temp_branch_name(name))
        worktree_path = os.path.join(worktrees_dir, br.replace("/", "-"))
        if explicit:
            existing = await _list_worktrees(repo_dir)
            collision = find_colliding_worktree(existing, br, worktree_path)
            if collision:
                return await _reuse_colliding_worktree(collision, br, worktrees_dir)
            await _add_branch_worktree(repo_dir, worktree_path, br, base_branch)
            return WorktreeInfo(worktree_path, br)
        await _add_temp_branch_worktree(repo_dir, worktree_path, br)
        return WorktreeInfo(worktree_path, br)

    return await with_timeout(WORKTREE_TIMEOUT_MS, lambda: _worktree_timeout(repo_dir, "create", "creation"), work())


async def has_uncommitted_changes(worktree_path: str) -> bool:
    return len((await _exec_git(["status", "--porcelain"], worktree_path)).strip()) > 0


async def remove(worktree_path: str) -> None:
    """Remove a worktree under ``<repo>/.sandcastle/worktrees/`` and its metadata."""
    repo_dir = os.path.normpath(os.path.join(worktree_path, "..", "..", ".."))
    await _exec_git(["worktree", "remove", "--force", worktree_path], repo_dir)


async def delete_temp_branch_if_unused(repo_dir: str, branch: str) -> int:
    """Delete a merge-to-head temp branch unless it holds commits on no other
    branch; returns that count (0 when deleted or missing)."""
    ref = f"refs/heads/{branch}"
    try:
        await _exec_git(["show-ref", "--verify", "--quiet", ref], repo_dir)
    except WorktreeError:
        return 0
    count = await _exec_git(["rev-list", "--count", ref, "--not", f"--exclude={branch}", "--branches"], repo_dir)
    try:
        unmerged = int(count.strip())
    except ValueError:
        unmerged = 0
    if unmerged > 0:
        return unmerged
    await _exec_git(["branch", "-D", branch], repo_dir)
    return 0


async def _list_active_worktree_paths(repo_dir: str) -> set:
    out = await _exec_git(["worktree", "list", "--porcelain"], repo_dir)
    return {line[len("worktree ") :].strip() for line in out.split("\n") if line.startswith("worktree ")}


async def prune_stale(repo_dir: str) -> None:
    """Prune stale worktree metadata and remove orphaned dirs under
    ``.sandcastle/worktrees/``."""

    async def work() -> None:
        await _exec_git(["worktree", "prune"], repo_dir)
        worktrees_dir = os.path.join(repo_dir, ".sandcastle", "worktrees")
        try:
            entries = os.listdir(worktrees_dir)
        except FileNotFoundError:
            return
        except OSError as e:
            raise WorktreeError(str(e)) from e
        try:
            real_dir = os.path.realpath(worktrees_dir)
        except OSError:
            real_dir = worktrees_dir
        active = await _list_active_worktree_paths(repo_dir)
        for entry in entries:
            entry_path = os.path.join(real_dir, entry)
            if os.path.isdir(entry_path) and is_orphaned_worktree_path(entry_path, active):
                try:
                    shutil.rmtree(entry_path, ignore_errors=False)
                except OSError as e:
                    raise WorktreeError(f"Failed to remove {entry_path}: {e}") from e

    await with_timeout(WORKTREE_TIMEOUT_MS, lambda: _worktree_timeout(repo_dir, "prune", "prune"), work())

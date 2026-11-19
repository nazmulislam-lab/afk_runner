"""Sandbox lifecycle: git setup, hooks, then commit collection / merge-back.

Port of ``SandboxLifecycle.ts``, ``sandboxSetup.ts`` and
``lifecycleHostGit.ts``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Generic, List, Optional, Sequence, TypeVar, Union

from .abort import AbortController, AbortSignal
from .display import Display
from .errors import (
    CommitCollectionTimeoutError,
    ExecError,
    GitSetupTimeoutError,
    HookTimeoutError,
    HostGitError,
    MergeToHostTimeoutError,
    SyncError,
    with_timeout,
)
from .host_git import host_git, host_git_output
from .host_shell import HostShellAbortError, HostShellError, exec_host_shell
from .sandbox_service import SandboxService
from .sync import count_commits_to_sync

A = TypeVar("A")

GIT_SETUP_TIMEOUT_MS = 10_000
COMMIT_COLLECTION_TIMEOUT_MS = 30_000
MERGE_TO_HOST_TIMEOUT_MS = 30_000
HOOK_TIMEOUT_MS = 60_000
GIT_SETUP_MAX_RETRIES = 2
GIT_SETUP_RETRY_DELAY_S = 0.25
TRANSIENT_EXEC_EXIT_CODES = {126, 137}

# ---------------------------------------------------------------------------
# Hooks / timeouts types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostHook:
    command: str
    timeout_ms: Optional[float] = None


@dataclass(frozen=True)
class SandboxHook:
    command: str
    sudo: bool = False
    timeout_ms: Optional[float] = None


def _hook(raw: Any, cls: type) -> Any:
    if isinstance(raw, cls):
        return raw
    if isinstance(raw, str):
        return cls(command=raw)
    if isinstance(raw, dict):
        kw = {"command": raw["command"]}
        t = raw.get("timeout_ms", raw.get("timeoutMs"))
        if t is not None:
            kw["timeout_ms"] = t
        if cls is SandboxHook and raw.get("sudo"):
            kw["sudo"] = True
        return cls(**kw)
    if isinstance(raw, (HostHook, SandboxHook)):
        return cls(command=raw.command, timeout_ms=raw.timeout_ms)
    raise TypeError(f"Invalid hook: {raw!r}")


@dataclass
class SandboxHooks:
    """Lifecycle hooks grouped by where they run.

    * ``host_on_worktree_ready`` - host, after copy_to_worktree, sequential
    * ``host_on_sandbox_ready`` - host, after the sandbox is up, parallel
    * ``sandbox_on_sandbox_ready`` - in the sandbox, parallel (``sudo`` ok)

    ``SandboxHooks.from_any`` also accepts the TS-shaped dict
    ``{"host": {"onWorktreeReady": [...], "onSandboxReady": [...]},
    "sandbox": {"onSandboxReady": [...]}}``.
    """

    host_on_worktree_ready: List[HostHook] = field(default_factory=list)
    host_on_sandbox_ready: List[HostHook] = field(default_factory=list)
    sandbox_on_sandbox_ready: List[SandboxHook] = field(default_factory=list)

    @staticmethod
    def from_any(value: Any) -> "SandboxHooks":
        if value is None:
            return SandboxHooks()
        if isinstance(value, SandboxHooks):
            return value
        host = value.get("host") or {}
        sandbox = value.get("sandbox") or {}
        return SandboxHooks(
            host_on_worktree_ready=[_hook(h, HostHook) for h in host.get("onWorktreeReady", host.get("on_worktree_ready", []))],
            host_on_sandbox_ready=[_hook(h, HostHook) for h in host.get("onSandboxReady", host.get("on_sandbox_ready", []))],
            sandbox_on_sandbox_ready=[
                _hook(h, SandboxHook) for h in sandbox.get("onSandboxReady", sandbox.get("on_sandbox_ready", []))
            ],
        )


@dataclass(frozen=True)
class Timeouts:
    """Override default timeouts for built-in lifecycle steps (milliseconds)."""

    copy_to_worktree_ms: Optional[float] = None
    git_setup_ms: Optional[float] = None
    commit_collection_ms: Optional[float] = None
    merge_to_host_ms: Optional[float] = None

    @staticmethod
    def from_any(value: Any) -> Optional["Timeouts"]:
        if value is None or isinstance(value, Timeouts):
            return value
        return Timeouts(
            copy_to_worktree_ms=value.get("copy_to_worktree_ms", value.get("copyToWorktreeMs")),
            git_setup_ms=value.get("git_setup_ms", value.get("gitSetupMs")),
            commit_collection_ms=value.get("commit_collection_ms", value.get("commitCollectionMs")),
            merge_to_host_ms=value.get("merge_to_host_ms", value.get("mergeToHostMs")),
        )


# ---------------------------------------------------------------------------
# Sandbox setup (sandboxSetup.ts)
# ---------------------------------------------------------------------------


async def exec_ok(sandbox: SandboxService, command: str, cwd: Optional[str] = None, sudo: bool = False):
    result = await sandbox.exec(command, cwd=cwd, sudo=sudo)
    if result.exit_code != 0:
        raise ExecError(
            f"Command failed (exit {result.exit_code}): {command}\n{result.stderr}", command=command, exit_code=result.exit_code
        )
    return result


async def _exec_ok_with_git_timeout(sandbox: SandboxService, timeout_ms: float, command: str, cwd: Optional[str] = None):
    attempt = 0
    while True:
        try:
            return await with_timeout(
                timeout_ms,
                lambda: GitSetupTimeoutError(
                    f"Git command timed out after {timeout_ms}ms: {command}", timeout_ms=timeout_ms, command=command
                ),
                exec_ok(sandbox, command, cwd=cwd),
            )
        except ExecError as e:
            if attempt < GIT_SETUP_MAX_RETRIES and e.exit_code in TRANSIENT_EXEC_EXIT_CODES:
                attempt += 1
                await asyncio.sleep(GIT_SETUP_RETRY_DELAY_S)
                continue
            raise


def _escape_dq(v: str) -> str:
    return v.replace('"', '\\"')


async def set_up_sandbox_git(sandbox: SandboxService, sandbox_repo_dir: str, git_setup_timeout_ms: float, identity: Dict[str, str]) -> str:
    """Mark the repo safe, copy the host git identity in, return the branch."""
    t = git_setup_timeout_ms
    await _exec_ok_with_git_timeout(sandbox, t, f'git config --global --add safe.directory "{sandbox_repo_dir}"')
    if identity.get("name"):
        await _exec_ok_with_git_timeout(sandbox, t, f'git config --global user.name "{_escape_dq(identity["name"])}"')
    if identity.get("email"):
        await _exec_ok_with_git_timeout(sandbox, t, f'git config --global user.email "{_escape_dq(identity["email"])}"')
    head = await _exec_ok_with_git_timeout(sandbox, t, "git rev-parse --abbrev-ref HEAD", cwd=sandbox_repo_dir)
    return head.stdout.strip()


async def run_host_hook(hook: HostHook, cwd: str, signal: Optional[AbortSignal] = None) -> None:
    timeout = hook.timeout_ms or HOOK_TIMEOUT_MS

    async def run() -> None:
        try:
            await exec_host_shell(hook.command, cwd=cwd, signals=[signal])
        except (HostShellError, HostShellAbortError, OSError) as e:
            raise ExecError(f"Host hook failed: {hook.command}\n{e}", command=hook.command) from e

    await with_timeout(
        timeout,
        lambda: HookTimeoutError(f"Host hook '{hook.command}' timed out after {timeout}ms", timeout_ms=timeout, command=hook.command),
        run(),
    )


async def run_host_hooks(hooks: Sequence[HostHook], cwd: str, signal: Optional[AbortSignal] = None) -> None:
    """Run host hooks sequentially, failing fast."""
    for hook in hooks:
        await run_host_hook(hook, cwd, signal)


async def _run_sandbox_hook(hook: SandboxHook, sandbox: SandboxService, repo_dir: str) -> None:
    timeout = hook.timeout_ms or HOOK_TIMEOUT_MS
    await with_timeout(
        timeout,
        lambda: HookTimeoutError(f"Hook '{hook.command}' timed out after {timeout}ms", timeout_ms=timeout, command=hook.command),
        exec_ok(sandbox, hook.command, cwd=repo_dir, sudo=hook.sudo),
    )


async def run_on_sandbox_ready_hooks(
    *,
    sandbox: SandboxService,
    sandbox_repo_dir: str,
    host_worktree_path: str,
    signal: AbortSignal,
    sandbox_hooks: Sequence[SandboxHook],
    host_hooks: Sequence[HostHook],
) -> None:
    """Sandbox and host onSandboxReady hooks, all in parallel. Sandbox hooks
    stop when ``signal`` aborts."""
    if not sandbox_hooks and not host_hooks:
        return
    if signal.aborted:
        raise ExecError(f"Aborted: {signal.reason}", command="abort")
    tasks = [asyncio.ensure_future(_run_sandbox_hook(h, sandbox, sandbox_repo_dir)) for h in sandbox_hooks]
    tasks += [asyncio.ensure_future(run_host_hook(h, host_worktree_path, signal)) for h in host_hooks]
    abort_task = asyncio.ensure_future(signal.wait())
    try:
        pending = set(tasks)
        while pending:
            done, _ = await asyncio.wait(pending | {abort_task}, return_when=asyncio.FIRST_COMPLETED)
            if abort_task in done:
                raise ExecError(f"Aborted: {signal.reason}", command="abort")
            for t in done:
                pending.discard(t)
                t.result()  # re-raise the first failure
    finally:
        abort_task.cancel()
        for t in tasks:
            if not t.done():
                t.cancel()


# ---------------------------------------------------------------------------
# Host git steps (lifecycleHostGit.ts)
# ---------------------------------------------------------------------------


async def read_host_current_branch(branch: Optional[str], host_repo_dir: str) -> Optional[str]:
    if branch:
        return None
    return (await host_git(["rev-parse", "--abbrev-ref", "HEAD"], host_repo_dir)).strip()


async def read_host_identity(host_repo_dir: str) -> Dict[str, str]:
    async def read(key: str) -> str:
        try:
            return (await host_git(["config", key], host_repo_dir)).strip()
        except HostGitError:
            return ""

    name, email = await asyncio.gather(read("user.name"), read("user.email"))
    return {"name": name, "email": email}


async def _collect_commits(rng: str, host_repo_dir: str) -> List[Dict[str, str]]:
    try:
        out = (await host_git(["rev-list", rng, "--reverse"], host_repo_dir)).strip()
    except HostGitError:
        return []
    return [] if out == "" else [{"sha": s} for s in out.split("\n")]


async def collect_commits_logged(rng: str, host_repo_dir: str, timeout_ms: float, display: Display) -> List[Dict[str, str]]:
    async def work(_message: Any) -> List[Dict[str, str]]:
        return await with_timeout(
            timeout_ms,
            lambda: CommitCollectionTimeoutError(f"Commit collection timed out after {timeout_ms}ms", timeout_ms=timeout_ms),
            _collect_commits(rng, host_repo_dir),
        )

    return await display.task_log("Collecting commits", work)


async def _merge_to_host(source: str, target: str, host_repo_dir: str) -> None:
    try:
        await host_git(["merge", "--", source], host_repo_dir)
        return
    except HostGitError as e:
        git_output = "\n".join(s.strip() for s in (e.stdout, e.stderr) if s.strip())
        try:
            await host_git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], host_repo_dir)
            started = True
        except HostGitError:
            started = False
        if started:
            try:
                await host_git(["merge", "--abort"], host_repo_dir)
            except HostGitError:
                pass
        said = " " if git_output == "" else f"\ngit merge said:\n{git_output}\n"
        raise SyncError(
            f"Merge of '{source}' onto '{target}' failed{' and was aborted' if started else ''}, so '{target}' is unchanged."
            f"{said}The temporary branch '{source}' has been preserved. To retry: git merge {source} (resolve any "
            f"conflicts and commit), then clean up: git branch -D {source}"
        ) from e


async def merge_temp_branch(
    *,
    sandbox: SandboxService,
    sandbox_repo_dir: str,
    host_repo_dir: str,
    keep_source_branch: bool,
    merge_to_host_timeout_ms: float,
    has_new_commits: bool,
    temp_branch: str,
    host_branch: str,
    display: Display,
) -> None:
    """Merge (not cherry-pick: merge commits must survive) the temp branch
    into the host branch, then delete it."""
    if not keep_source_branch:
        await exec_ok(sandbox, "git checkout --detach", cwd=sandbox_repo_dir)
    if has_new_commits:

        async def work(_m: Any) -> None:
            await with_timeout(
                merge_to_host_timeout_ms,
                lambda: MergeToHostTimeoutError(
                    f"Merge of '{temp_branch}' to '{host_branch}' timed out after {merge_to_host_timeout_ms}ms",
                    timeout_ms=merge_to_host_timeout_ms,
                    source_branch=temp_branch,
                    target_branch=host_branch,
                ),
                _merge_to_host(temp_branch, host_branch, host_repo_dir),
            )

        await display.task_log(f"Merging to {host_branch}", work)
    if not keep_source_branch:
        try:
            await host_git(["branch", "-D", "--", temp_branch], host_repo_dir)
        except HostGitError:
            pass


# ---------------------------------------------------------------------------
# withSandboxLifecycle
# ---------------------------------------------------------------------------


@dataclass
class SandboxContext:
    sandbox: SandboxService
    sandbox_repo_dir: str
    base_head: str


@dataclass
class SandboxLifecycleResult(Generic[A]):
    result: A
    branch: str
    commits: List[Dict[str, str]]


@dataclass
class SandboxLifecycleOptions:
    host_repo_dir: str
    sandbox_repo_dir: str
    hooks: Optional[SandboxHooks] = None
    branch: Optional[str] = None
    host_worktree_path: Optional[str] = None
    apply_to_host: Optional[Callable[[], Awaitable[None]]] = None
    signal: Optional[AbortSignal] = None
    timeouts: Optional[Timeouts] = None
    keep_source_branch: bool = False


async def with_sandbox_lifecycle(
    options: SandboxLifecycleOptions,
    sandbox: SandboxService,
    work: Callable[[SandboxContext], Awaitable[A]],
    display: Display,
) -> SandboxLifecycleResult[A]:
    t = options.timeouts or Timeouts()
    git_setup_ms = t.git_setup_ms or GIT_SETUP_TIMEOUT_MS
    commit_ms = t.commit_collection_ms or COMMIT_COLLECTION_TIMEOUT_MS
    merge_ms = t.merge_to_host_ms or MERGE_TO_HOST_TIMEOUT_MS
    signal = options.signal or AbortController().signal
    host_side = options.host_worktree_path or options.sandbox_repo_dir
    hooks = options.hooks or SandboxHooks()

    host_current_branch = await read_host_current_branch(options.branch, options.host_repo_dir)
    identity = await read_host_identity(options.host_repo_dir)

    async def setup(message: Callable[[str], None]) -> str:
        resolved = await set_up_sandbox_git(sandbox, options.sandbox_repo_dir, git_setup_ms, identity)
        for h in hooks.sandbox_on_sandbox_ready:
            message(h.command)
        for h in hooks.host_on_sandbox_ready:
            message(f"[host] {h.command}")
        await run_on_sandbox_ready_hooks(
            sandbox=sandbox,
            sandbox_repo_dir=options.sandbox_repo_dir,
            host_worktree_path=host_side,
            signal=signal,
            sandbox_hooks=hooks.sandbox_on_sandbox_ready,
            host_hooks=hooks.host_on_sandbox_ready,
        )
        return resolved

    resolved_branch = await display.task_log("Setting up sandbox", setup)
    target_branch = options.branch or resolved_branch
    base_head = (await host_git(["rev-parse", "HEAD"], host_side)).strip()

    result = await work(SandboxContext(sandbox, options.sandbox_repo_dir, base_head))

    if options.apply_to_host is not None:
        count = await count_commits_to_sync(sandbox, options.sandbox_repo_dir, base_head)
        title = f"Syncing {count} commit{'' if count == 1 else 's'} to host" if count > 0 else "No commits to sync out"
        apply = options.apply_to_host

        async def do_sync(_m: Any) -> None:
            await apply()

        await display.task_log(title, do_sync)

    if host_current_branch is None:
        commits = await collect_commits_logged(f"{base_head}..refs/heads/{target_branch}", options.host_repo_dir, commit_ms, display)
        return SandboxLifecycleResult(result, target_branch, commits)

    try:
        has_new = int((await host_git(["rev-list", f"{base_head}..HEAD", "--count"], host_side)).strip()) > 0
    except (HostGitError, ValueError):
        has_new = False
    await merge_temp_branch(
        sandbox=sandbox,
        sandbox_repo_dir=options.sandbox_repo_dir,
        host_repo_dir=options.host_repo_dir,
        keep_source_branch=options.keep_source_branch,
        merge_to_host_timeout_ms=merge_ms,
        has_new_commits=has_new,
        temp_branch=resolved_branch,
        host_branch=host_current_branch,
        display=display,
    )
    commits = await collect_commits_logged(f"{base_head}..HEAD", options.host_repo_dir, commit_ms, display)
    return SandboxLifecycleResult(result, host_current_branch, commits)

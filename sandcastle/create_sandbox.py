"""``create_sandbox()``: one long-lived sandbox for many runs. Port of
``createSandbox.ts``.

```python
async with await create_sandbox(branch="agent/fix-42", sandbox=docker()) as sb:
    await sb.run(agent=claude_code("claude-opus-4-8"), prompt_file=".sandcastle/implement.md", max_iterations=5)
    tests = await sb.exec("npm test")
    await sb.run(agent=claude_code("claude-sonnet-4-6"), prompt="Review the changes and fix any issues.")
```
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Union

from . import worktree_manager as wm
from .abort import AbortSignal, race_abort_signal
from .agent_provider import AgentCommandOptions, PrintCommand, shell_escape
from .copy_to_worktree import copy_to_worktree
from .display import AgentStreamEmitter, Display, FileDisplay, SilentDisplay, TerminalDisplay
from .context_window import build_context_window_lines
from .errors import NativeInteractiveNotSupportedError
from .lifecycle import SandboxHooks, SandboxLifecycleOptions, Timeouts, exec_ok, run_host_hooks, with_sandbox_lifecycle
from .mounts import SANDBOX_REPO_DIR, MountEntry, patch_git_mounts_for_windows, remove_mount_temp_dirs, resolve_git_mounts
from .orchestrator import IterationResult, OrchestrateOptions, orchestrate
from .prompts import PromptArgs, preprocess_prompt, resolve_sandbox_prompt
from .run import FileLogging, LoggingOption, StdoutLogging, build_agent_stream_handler, build_completion_message, build_log_filename, print_file_display_startup, to_logging
from .sandbox_factory import ReuseSandboxFactory, SandboxInfo
from .sandbox_provider import ExecResult, InteractiveExecOptions
from .sandbox_service import SandboxService, start_bind_mount_sandbox, start_isolated_sandbox, start_no_sandbox
from .shutdown import register_shutdown
from .sync import sync_out
from .usage_limit import UsageLimitConfig
from .util import (
    assert_fork_supported,
    assert_resume_session_exists,
    merge_provider_env,
    resolve_cwd,
    resolve_env,
    validate_timeout_options,
)


@dataclass
class CloseResult:
    #: Host path to the preserved worktree, set when it had uncommitted changes.
    preserved_worktree_path: Optional[str] = None


@dataclass
class SandboxRunResult:
    iterations: List[IterationResult]
    stdout: str
    commits: List[Dict[str, str]]
    completion_signal: Optional[str] = None
    log_file_path: Optional[str] = None
    _resume: Optional[Callable[..., Awaitable["SandboxRunResult"]]] = field(default=None, repr=False)
    _fork: Optional[Callable[..., Awaitable["SandboxRunResult"]]] = field(default=None, repr=False)

    async def resume(self, prompt: str, **options: Any) -> "SandboxRunResult":
        """Continue the captured session for one iteration in the same warm sandbox."""
        if self._resume is None:
            raise RuntimeError("resume is only available when the provider captured a session id")
        return await self._resume(prompt, **options)

    async def fork(self, prompt: str, **options: Any) -> "SandboxRunResult":
        """Fork the captured session for one iteration in the same warm sandbox."""
        if self._fork is None:
            raise RuntimeError("fork is only available when the provider captured a session id")
        return await self._fork(prompt, **options)


@dataclass
class SandboxInteractiveResult:
    commits: List[Dict[str, str]]
    exit_code: int


class _AgentWithEnv:
    """An agent whose print command exports its ``env`` first (a long-lived
    sandbox starts before any agent is chosen)."""

    def __init__(self, agent: Any, exports: str) -> None:
        self._agent = agent
        self._exports = exports

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)

    def build_print_command(self, options: AgentCommandOptions) -> PrintCommand:
        pc = self._agent.build_print_command(options)
        return PrintCommand(command=self._exports + pc.command, stdin=pc.stdin)


def with_agent_env(agent: Any, sandbox_provider_env: Dict[str, str]) -> Any:
    entries = list(agent.env.items())
    if not entries:
        return agent
    merge_provider_env({}, agent.env, sandbox_provider_env)
    invalid = [k for k, _ in entries if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k)]
    if invalid:
        raise ValueError(f"Invalid agent provider env key(s): {', '.join(invalid)}. Keys must match [A-Za-z_][A-Za-z0-9_]*.")
    exports = "".join(f"export {k}={shell_escape(v)}; " for k, v in entries)
    return _AgentWithEnv(agent, exports)


def assert_interactive_supported(provider: Any) -> None:
    if getattr(provider, "native", False):
        raise NativeInteractiveNotSupportedError(
            f"interactive() is not supported on native providers (got {provider.name}()). "
            "Use run(), or pass docker() or no_sandbox() to interactive().",
            provider=provider.name,
        )


@dataclass
class _Ctx:
    branch: str
    worktree_path: str
    host_repo_dir: str
    sandbox_repo_dir: str
    sandbox: SandboxService
    provider_handle: Any
    bind_mount_handle: Any
    provider_tag: str
    sandbox_provider: Any
    sandbox_provider_env: Dict[str, str]
    apply_to_host: Optional[Callable[[], Awaitable[None]]]
    timeouts: Optional[Timeouts] = None
    branch_strategy: Any = None


class Sandbox:
    """A long-lived sandbox. Use ``async with``, or call ``close()``."""

    def __init__(self, ctx: _Ctx, close: Callable[[], Awaitable[CloseResult]]) -> None:
        self._ctx = ctx
        self._close = close
        self.branch = ctx.branch
        self.worktree_path = ctx.worktree_path
        self.sandbox_id: Optional[str] = getattr(ctx.provider_handle, "sandbox_id", None)

    async def __aenter__(self) -> "Sandbox":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # -- run ------------------------------------------------------------
    async def run(
        self,
        *,
        agent: Any,
        prompt: Optional[str] = None,
        prompt_file: Optional[str] = None,
        max_iterations: int = 1,
        prompt_args: Optional[PromptArgs] = None,
        completion_signal: Union[None, str, Sequence[str]] = None,
        idle_timeout_seconds: Optional[float] = None,
        completion_timeout_seconds: Optional[float] = None,
        name: Optional[str] = None,
        logging: Any = None,
        resume_session: Optional[str] = None,
        fork_session: bool = False,
        signal: Optional[AbortSignal] = None,
        wait_for_usage_reset: bool = True,
        usage_limit: Optional[UsageLimitConfig] = None,
    ) -> SandboxRunResult:
        ctx = self._ctx
        run_kwargs = dict(locals())
        run_kwargs.pop("self")
        run_kwargs.pop("ctx")
        if signal is not None:
            signal.throw_if_aborted()
        provider = with_agent_env(agent, ctx.sandbox_provider_env)
        validate_timeout_options(idle_timeout_seconds, completion_timeout_seconds)
        is_resuming = bool(resume_session)
        if is_resuming and max_iterations > 1:
            raise ValueError(
                "resumeSession cannot be combined with maxIterations > 1. "
                "Resume applies to iteration 1 only; multi-iteration resume semantics are not supported."
            )
        if fork_session and not is_resuming:
            raise ValueError("forkSession requires resumeSession. Use sandboxRunResult.fork(prompt) to fork the most recent captured session.")
        if fork_session:
            assert_fork_supported(provider)
        if is_resuming and resume_session:
            await assert_resume_session_exists(provider, ctx.provider_tag, ctx.host_repo_dir, resume_session)

        resolved = await resolve_sandbox_prompt(
            prompt=prompt, prompt_file=prompt_file, prompt_args=prompt_args, branch=ctx.branch, host_repo_dir=ctx.host_repo_dir, display=SilentDisplay()
        )
        log = to_logging(logging) or FileLogging(
            path=os.path.join(ctx.host_repo_dir, ".sandcastle", "logs", build_log_filename(ctx.branch, None, name))
        )
        if isinstance(log, FileLogging):
            print_file_display_startup(log.path, name, ctx.branch, None)
            display: Display = FileDisplay(log.path)
        else:
            display = TerminalDisplay()
        factory = ReuseSandboxFactory(
            SandboxInfo(
                sandbox_repo_path=ctx.sandbox_repo_dir,
                host_worktree_path=ctx.worktree_path,
                apply_to_host=ctx.apply_to_host,
                bind_mount_handle=ctx.bind_mount_handle,
            ),
            ctx.sandbox,
        )
        merge_to_head = getattr(ctx.branch_strategy, "type", None) == "merge-to-head"
        ul = usage_limit or UsageLimitConfig()
        if not wait_for_usage_reset:
            ul = replace(ul, enabled=False)
        try:
            display.intro(name or "sandcastle")
            result = await orchestrate(
                OrchestrateOptions(
                    host_repo_dir=ctx.host_repo_dir,
                    iterations=max_iterations,
                    prompt=resolved.text,
                    branch=None if merge_to_head else ctx.branch,
                    provider=provider,
                    completion_signal=completion_signal,
                    idle_timeout_seconds=idle_timeout_seconds,
                    completion_timeout_seconds=completion_timeout_seconds,
                    name=name,
                    resume_session=resume_session,
                    fork_session=fork_session,
                    signal=signal,
                    skip_prompt_expansion=resolved.is_inline,
                    timeouts=ctx.timeouts,
                    keep_source_branch=merge_to_head,
                    usage_limit=ul,
                ),
                factory,
                display,
                AgentStreamEmitter(build_agent_stream_handler(log)),
            )
            msg, sev = build_completion_message(result.completion_signal, len(result.iterations))
            display.status(msg, sev)  # type: ignore[arg-type]
            for line in build_context_window_lines(result.iterations):
                display.text(line)
        except BaseException:
            if signal is not None:
                signal.throw_if_aborted()
            raise
        finally:
            display.close()

        out = SandboxRunResult(
            iterations=result.iterations,
            stdout=result.stdout,
            commits=result.commits,
            completion_signal=result.completion_signal,
            log_file_path=log.path if isinstance(log, FileLogging) else None,
        )
        last = result.iterations[-1] if result.iterations else None
        if getattr(agent, "session_storage", None) is not None and last and last.session_id:
            sid = last.session_id

            async def rerun(fork: bool, next_prompt: str, **overrides: Any) -> SandboxRunResult:
                kw = dict(run_kwargs)
                kw["prompt_args"] = None
                kw.update(overrides)
                kw.update({"prompt": next_prompt, "prompt_file": None, "max_iterations": 1, "resume_session": sid, "fork_session": fork})
                return await self.run(**kw)

            out._resume = lambda p, **o: rerun(False, p, **o)
            out._fork = lambda p, **o: rerun(True, p, **o)
        return out

    # -- interactive ----------------------------------------------------
    async def interactive(
        self,
        *,
        agent: Any,
        prompt: Optional[str] = None,
        prompt_file: Optional[str] = None,
        prompt_args: Optional[PromptArgs] = None,
        name: Optional[str] = None,
        signal: Optional[AbortSignal] = None,
    ) -> SandboxInteractiveResult:
        ctx = self._ctx
        if signal is not None:
            signal.throw_if_aborted()
        assert_interactive_supported(ctx.sandbox_provider)
        if not getattr(agent, "supports_interactive", hasattr(agent, "build_interactive_args")):
            raise ValueError(f'Agent provider "{agent.name}" does not support buildInteractiveArgs, required for interactive sessions.')
        handle = ctx.provider_handle
        if handle is None or not hasattr(handle, "interactive_exec"):
            raise ValueError(
                "Sandbox provider does not support interactiveExec. The provider must implement the optional "
                "interactive_exec method to use interactive()."
            )
        display = TerminalDisplay()
        merge_to_head = getattr(ctx.branch_strategy, "type", None) == "merge-to-head"
        try:
            resolved = await resolve_sandbox_prompt(
                prompt=prompt, prompt_file=prompt_file, prompt_args=prompt_args, branch=ctx.branch, host_repo_dir=ctx.host_repo_dir, display=display
            )

            async def work(sctx: Any) -> int:
                full = resolved.text if resolved.is_inline else await preprocess_prompt(resolved.text, sctx.sandbox, sctx.sandbox_repo_dir, display)
                args = agent.build_interactive_args(AgentCommandOptions(prompt=full, dangerously_skip_permissions=True))
                return await race_abort_signal(
                    handle.interactive_exec(args, InteractiveExecOptions(sys.stdin, sys.stdout, sys.stderr, ctx.sandbox_repo_dir)),
                    signal,
                )

            lr = await with_sandbox_lifecycle(
                SandboxLifecycleOptions(
                    host_repo_dir=ctx.host_repo_dir,
                    sandbox_repo_dir=ctx.sandbox_repo_dir,
                    branch=None if merge_to_head else ctx.branch,
                    host_worktree_path=ctx.worktree_path,
                    apply_to_host=ctx.apply_to_host,
                    timeouts=ctx.timeouts,
                    keep_source_branch=merge_to_head,
                ),
                ctx.sandbox,
                work,
                display,
            )
        except BaseException:
            if signal is not None:
                signal.throw_if_aborted()
            raise
        finally:
            display.close()
        return SandboxInteractiveResult(commits=lr.commits, exit_code=lr.result)

    # -- exec / close ---------------------------------------------------
    async def exec(
        self, command: str, *, on_line=None, cwd: Optional[str] = None, sudo: bool = False, stdin: Optional[str] = None
    ) -> ExecResult:
        """Run a shell command in the sandbox (``cwd`` defaults to the repo).
        A non-zero exit code is returned, not raised."""
        kw: Dict[str, Any] = {"cwd": cwd or self._ctx.sandbox_repo_dir}
        if on_line is not None:
            kw["on_line"] = on_line
        if sudo:
            kw["sudo"] = True
        if stdin is not None:
            kw["stdin"] = stdin
        if self._ctx.provider_handle is not None:
            return await self._ctx.provider_handle.exec(command, **kw)
        return await self._ctx.sandbox.exec(command, **kw)

    async def close(self) -> CloseResult:
        return await self._close()


# ---------------------------------------------------------------------------
# Starting the sandbox
# ---------------------------------------------------------------------------


async def _start_provider_sandbox(provider: Any, host_repo_dir: str, worktree_path: str, env: Dict[str, str], copy_paths: Optional[List[str]], mounts_to_clean: List[MountEntry]):
    if provider.tag == "isolated":
        return await start_isolated_sandbox(provider=provider, host_repo_dir=worktree_path, env=env, copy_paths=copy_paths, copy_source_dir=host_repo_dir)
    if provider.tag == "none":
        return await start_no_sandbox(provider, worktree_path, env)
    try:
        git_mounts = resolve_git_mounts(os.path.join(host_repo_dir, ".git"))
    except OSError:
        git_mounts = []
    git_mounts = patch_git_mounts_for_windows(git_mounts, worktree_path, SANDBOX_REPO_DIR)
    mounts_to_clean.extend(git_mounts)
    return await start_bind_mount_sandbox(
        provider=provider, host_repo_dir=host_repo_dir, env=env, worktree_or_repo_path=worktree_path, git_mounts=git_mounts, repo_dir=SANDBOX_REPO_DIR
    )


async def _run_on_sandbox_ready_hooks(sandbox: SandboxService, sandbox_repo_dir: str, worktree_path: str, hooks: SandboxHooks) -> None:
    import asyncio

    if not hooks.sandbox_on_sandbox_ready and not hooks.host_on_sandbox_ready:
        return
    await sandbox.exec(f'git config --global --add safe.directory "{sandbox_repo_dir}"')
    aws = [exec_ok(sandbox, h.command, cwd=sandbox_repo_dir, sudo=h.sudo) for h in hooks.sandbox_on_sandbox_ready]
    if hooks.host_on_sandbox_ready:
        aws.append(run_host_hooks(hooks.host_on_sandbox_ready, worktree_path))
    await asyncio.gather(*aws)


def _provider_fields(provider: Any, handle: Any, worktree_path: str) -> Dict[str, Any]:
    return {
        "provider_handle": handle,
        "bind_mount_handle": handle if provider.tag == "bind-mount" else None,
        "provider_tag": provider.tag,
        "sandbox_provider": provider,
        "sandbox_provider_env": provider.env,
        "apply_to_host": (lambda: sync_out(worktree_path, handle)) if provider.tag == "isolated" and handle is not None else None,
    }


async def _close_quietly(handle: Any) -> None:
    if handle is None:
        return
    try:
        await handle.close()
    except Exception:  # noqa: BLE001
        pass


async def _start_with_ready_hooks(provider: Any, host_repo_dir: str, worktree_path: str, hooks: SandboxHooks, copy_paths, mounts_to_clean, test_build_sandbox=None, test_bind_mount_handle=None):
    if test_build_sandbox is not None:
        sandbox = test_build_sandbox(worktree_path)
        handle = test_bind_mount_handle
        repo_dir = worktree_path
    else:
        env = merge_provider_env(resolve_env(host_repo_dir), {}, provider.env)
        try:
            started = await _start_provider_sandbox(provider, host_repo_dir, worktree_path, env, copy_paths, mounts_to_clean)
        except BaseException:
            remove_mount_temp_dirs(mounts_to_clean)
            raise
        sandbox, handle, repo_dir = started.sandbox, started.handle, started.worktree_path
    try:
        await _run_on_sandbox_ready_hooks(sandbox, repo_dir, worktree_path, hooks)
    except BaseException:
        await _close_quietly(handle)
        remove_mount_temp_dirs(mounts_to_clean)
        raise
    return sandbox, handle, repo_dir


async def create_sandbox_from_worktree(
    *,
    branch: str,
    worktree_path: str,
    host_repo_dir: str,
    sandbox: Any,
    hooks: Any = None,
    copy_to_worktree_paths: Optional[List[str]] = None,
    timeouts: Any = None,
    branch_strategy: Any = None,
    _test: Optional[Dict[str, Any]] = None,
) -> Sandbox:
    """A sandbox on an existing worktree; ``close()`` tears down the container
    only (the worktree's owner removes the worktree)."""
    t = Timeouts.from_any(timeouts)
    validate_timeout_options(timeouts=t)
    hooks_obj = SandboxHooks.from_any(hooks)
    if copy_to_worktree_paths and sandbox.tag != "isolated":
        await copy_to_worktree(copy_to_worktree_paths, host_repo_dir, worktree_path, (t or Timeouts()).copy_to_worktree_ms)
    mounts: List[MountEntry] = []
    tb = (_test or {}).get("build_sandbox")
    th = (_test or {}).get("bind_mount_handle")
    sb, handle, repo_dir = await _start_with_ready_hooks(sandbox, host_repo_dir, worktree_path, hooks_obj, copy_to_worktree_paths, mounts, tb, th)
    closed = [False]

    async def close() -> CloseResult:
        if closed[0]:
            return CloseResult()
        closed[0] = True
        if handle is not None:
            await handle.close()
        remove_mount_temp_dirs(mounts)
        return CloseResult()

    return Sandbox(
        _Ctx(
            branch=branch,
            worktree_path=worktree_path,
            host_repo_dir=host_repo_dir,
            sandbox_repo_dir=repo_dir,
            sandbox=sb,
            timeouts=t,
            branch_strategy=branch_strategy,
            **_provider_fields(sandbox, handle, worktree_path),
        ),
        close,
    )


async def create_sandbox(
    *,
    branch: str,
    sandbox: Any,
    base_branch: Optional[str] = None,
    cwd: Optional[str] = None,
    hooks: Any = None,
    copy_to_worktree: Optional[List[str]] = None,
    timeouts: Any = None,
    _test: Optional[Dict[str, Any]] = None,
) -> Sandbox:
    """Create a worktree on ``branch`` and start a sandbox on it, for reuse
    across several ``run()`` calls."""
    t = Timeouts.from_any(timeouts)
    validate_timeout_options(timeouts=t)
    hooks_obj = SandboxHooks.from_any(hooks)
    mounts: List[MountEntry] = []
    host_repo_dir = resolve_cwd(cwd)
    try:
        await wm.prune_stale(host_repo_dir)
    except Exception:  # noqa: BLE001
        pass
    info = await wm.create(host_repo_dir, branch=branch, base_branch=base_branch)
    worktree_path = info.path
    try:
        if copy_to_worktree and sandbox.tag != "isolated":
            from .copy_to_worktree import copy_to_worktree as _copy

            await _copy(copy_to_worktree, host_repo_dir, worktree_path, (t or Timeouts()).copy_to_worktree_ms)
        if hooks_obj.host_on_worktree_ready:
            await run_host_hooks(hooks_obj.host_on_worktree_ready, worktree_path)
        tb = (_test or {}).get("build_sandbox")
        th = (_test or {}).get("bind_mount_handle")
        sb, handle, repo_dir = await _start_with_ready_hooks(sandbox, host_repo_dir, worktree_path, hooks_obj, copy_to_worktree, mounts, tb, th)
    except BaseException:
        try:
            await wm.remove(worktree_path)
        except Exception:  # noqa: BLE001
            pass
        remove_mount_temp_dirs(mounts)
        raise

    def print_preserved() -> None:
        print(f"\nWorktree preserved at {worktree_path}", file=sys.stderr)
        print(f"  To review: cd {worktree_path}", file=sys.stderr)
        print(f"  To clean up: git worktree remove --force {worktree_path}", file=sys.stderr)

    unregister = register_shutdown(print_preserved)
    closed = [False]

    async def close() -> CloseResult:
        unregister()
        if closed[0]:
            return CloseResult()
        closed[0] = True
        if handle is not None:
            await handle.close()
        remove_mount_temp_dirs(mounts)
        try:
            dirty = await wm.has_uncommitted_changes(worktree_path)
        except Exception:  # noqa: BLE001
            dirty = False
        if dirty:
            return CloseResult(preserved_worktree_path=worktree_path)
        try:
            await wm.remove(worktree_path)
        except Exception:  # noqa: BLE001
            pass
        return CloseResult()

    return Sandbox(
        _Ctx(
            branch=branch,
            worktree_path=worktree_path,
            host_repo_dir=host_repo_dir,
            sandbox_repo_dir=repo_dir,
            sandbox=sb,
            timeouts=t,
            **_provider_fields(sandbox, handle, worktree_path),
        ),
        close,
    )

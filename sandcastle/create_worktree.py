"""``create_worktree()``: a worktree as a first-class object. Port of
``createWorktree.ts``.

```python
async with await create_worktree(branch_strategy=branch("agent/fix-42")) as wt:
    await wt.interactive(agent=claude_code("claude-opus-4-8"), prompt="Explore the bug.")
    result = await wt.run(agent=claude_code("claude-opus-4-8"), sandbox=docker(), prompt="Fix issue #42.")
```

Only ``branch`` and ``merge-to-head`` strategies are accepted. With
``merge-to-head`` each ``wt.run()`` / ``wt.interactive()`` merges back to the
host's current branch and keeps the worktree's branch for the next call.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Union

from . import worktree_manager as wm
from .abort import AbortSignal, race_abort_signal
from .agent_provider import AgentCommandOptions
from .context_window import build_context_window_lines
from .copy_to_worktree import copy_to_worktree
from .create_sandbox import CloseResult, Sandbox, assert_interactive_supported, create_sandbox_from_worktree
from .display import AgentStreamEmitter, Display, FileDisplay, TerminalDisplay
from .interactive import InteractiveResult
from .lifecycle import SandboxHooks, SandboxLifecycleOptions, Timeouts, run_host_hooks, with_sandbox_lifecycle
from .mounts import SANDBOX_REPO_DIR, MountEntry, patch_git_mounts_for_windows, remove_mount_temp_dirs, resolve_git_mounts
from .orchestrator import IterationResult, OrchestrateOptions, orchestrate
from .prompts import (
    BUILT_IN_PROMPT_ARG_KEYS,
    PromptArgs,
    preprocess_prompt,
    resolve_prompt,
    substitute_prompt_args,
    validate_no_args_with_inline_prompt,
    validate_no_built_in_arg_override,
)
from .run import FileLogging, build_agent_stream_handler, build_completion_message, build_log_filename, print_file_display_startup, to_logging
from .sandbox_factory import ReuseSandboxFactory, SandboxInfo
from .sandbox_provider import InteractiveExecOptions, NoSandboxCreateOptions, to_branch_strategy
from .sandbox_service import SandboxService, start_bind_mount_sandbox, start_isolated_sandbox, start_no_sandbox
from .sandboxes.no_sandbox import no_sandbox
from .sync import sync_out
from .usage_limit import UsageLimitConfig
from .util import assert_resume_session_exists, merge_provider_env, resolve_cwd, resolve_env, validate_timeout_options


@dataclass
class WorktreeRunResult:
    iterations: List[IterationResult]
    stdout: str
    commits: List[Dict[str, str]]
    branch: str
    completion_signal: Optional[str] = None
    log_file_path: Optional[str] = None


def _env(host_repo_dir: str, agent_env: Dict[str, str], sandbox_env: Dict[str, str], overrides: Optional[Dict[str, str]]) -> Dict[str, str]:
    return {**merge_provider_env(resolve_env(host_repo_dir), agent_env, sandbox_env), **(overrides or {})}


def _git_mounts(host_repo_dir: str, worktree_path: str) -> List[MountEntry]:
    return patch_git_mounts_for_windows(resolve_git_mounts(os.path.join(host_repo_dir, ".git")), worktree_path, SANDBOX_REPO_DIR)


class Worktree:
    def __init__(self, host_repo_dir: str, info: wm.WorktreeInfo, strategy: Any, copy_paths: Optional[List[str]], timeouts: Optional[Timeouts]) -> None:
        self._host = host_repo_dir
        self._info = info
        self._strategy = strategy
        self._copy = copy_paths
        self._timeouts = timeouts
        self._closed = False
        self._merge_to_head = strategy.type == "merge-to-head"
        self.branch = info.branch
        self.worktree_path = info.path

    async def __aenter__(self) -> "Worktree":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> CloseResult:
        """Remove the worktree unless it has uncommitted changes."""
        if self._closed:
            return CloseResult()
        self._closed = True
        try:
            dirty = await wm.has_uncommitted_changes(self._info.path)
        except Exception:  # noqa: BLE001
            dirty = False
        if dirty:
            return CloseResult(preserved_worktree_path=self._info.path)
        try:
            await wm.remove(self._info.path)
        except Exception:  # noqa: BLE001
            pass
        if self._merge_to_head:
            try:
                await wm.delete_temp_branch_if_unused(self._host, self._info.branch)
            except Exception:  # noqa: BLE001
                pass
        return CloseResult()

    def _substitute(self, resolved: Any, prompt_args: Optional[PromptArgs], current: str, display: Display) -> str:
        if resolved is None:
            return ""
        if resolved.source == "inline":
            validate_no_args_with_inline_prompt(prompt_args)
            return resolved.text
        user = dict(prompt_args or {})
        validate_no_built_in_arg_override(user)
        return substitute_prompt_args(
            resolved.text, {"SOURCE_BRANCH": self._info.branch, "TARGET_BRANCH": current, **user}, display, BUILT_IN_PROMPT_ARG_KEYS
        )

    # -- interactive ----------------------------------------------------
    async def interactive(
        self,
        *,
        agent: Any,
        sandbox: Any = None,
        prompt: Optional[str] = None,
        prompt_file: Optional[str] = None,
        name: Optional[str] = None,
        hooks: Any = None,
        prompt_args: Optional[PromptArgs] = None,
        env: Optional[Dict[str, str]] = None,
        signal: Optional[AbortSignal] = None,
    ) -> InteractiveResult:
        if signal is not None:
            signal.throw_if_aborted()
        provider = sandbox or no_sandbox()
        assert_interactive_supported(provider)
        if not hasattr(agent, "build_interactive_args"):
            raise ValueError(f'Agent provider "{agent.name}" does not support buildInteractiveArgs, required for interactive sessions.')
        d = TerminalDisplay()
        try:
            resolved = resolve_prompt(prompt, prompt_file) if (prompt is not None or prompt_file is not None) else None
            eff_env = _env(self._host, agent.env, provider.env, env)
            current = await wm.get_current_branch(self._host) if resolved is not None and resolved.source == "template" else ""
            substituted = self._substitute(resolved, prompt_args, current, d)
            d.intro(name or "sandcastle interactive")
            d.summary("Interactive Session", {"Agent": name or agent.name, "Sandbox": provider.name, "Branch": self._info.branch})
            mounts: List[MountEntry] = []
            apply = None
            if provider.tag == "none":
                handle = await provider.create(NoSandboxCreateOptions(worktree_path=self._info.path, env=eff_env))
            elif provider.tag == "isolated":

                async def st(_m: Any) -> Any:
                    return await start_isolated_sandbox(
                        provider=provider, host_repo_dir=self._info.path, env=eff_env, copy_paths=self._copy, copy_source_dir=self._host
                    )

                handle = (await d.task_log("Starting sandbox", st)).handle
                h = handle

                async def apply_fn() -> None:
                    await sync_out(self._info.path, h)

                apply = apply_fn
            else:
                gm = _git_mounts(self._host, self._info.path)
                mounts.extend(gm)

                async def stb(_m: Any) -> Any:
                    return await start_bind_mount_sandbox(
                        provider=provider, host_repo_dir=self._host, env=eff_env, worktree_or_repo_path=self._info.path, git_mounts=gm
                    )

                handle = (await d.task_log("Starting sandbox", stb)).handle
            try:
                if not hasattr(handle, "interactive_exec"):
                    raise ValueError(
                        "Sandbox provider does not support interactiveExec. The provider must implement the optional "
                        "interactive_exec method to use interactive()."
                    )
                wt_path = handle.worktree_path

                async def work(ctx: Any) -> int:
                    full = (
                        await preprocess_prompt(substituted, ctx.sandbox, ctx.sandbox_repo_dir, d)
                        if resolved is not None and resolved.source == "template"
                        else substituted
                    )
                    args = agent.build_interactive_args(AgentCommandOptions(prompt=full, dangerously_skip_permissions=provider.tag != "none"))
                    return await race_abort_signal(
                        handle.interactive_exec(args, InteractiveExecOptions(sys.stdin, sys.stdout, sys.stderr, wt_path)), signal
                    )

                lr = await with_sandbox_lifecycle(
                    SandboxLifecycleOptions(
                        host_repo_dir=self._host,
                        sandbox_repo_dir=wt_path,
                        hooks=SandboxHooks.from_any(hooks),
                        branch=None if self._merge_to_head else self._info.branch,
                        host_worktree_path=self._info.path,
                        apply_to_host=apply,
                        timeouts=self._timeouts,
                        keep_source_branch=self._merge_to_head,
                    ),
                    SandboxService(handle),
                    work,
                    d,
                )
                d.summary("Session Complete", {"Commits": str(len(lr.commits)), "Branch": lr.branch, "Exit code": str(lr.result)})
                return InteractiveResult(commits=lr.commits, branch=lr.branch, exit_code=lr.result)
            finally:
                try:
                    await handle.close()
                except Exception:  # noqa: BLE001
                    pass
                remove_mount_temp_dirs(mounts)
        except BaseException:
            if signal is not None:
                signal.throw_if_aborted()
            raise
        finally:
            d.close()

    # -- run ------------------------------------------------------------
    async def run(
        self,
        *,
        agent: Any,
        sandbox: Any,
        prompt: Optional[str] = None,
        prompt_file: Optional[str] = None,
        prompt_args: Optional[PromptArgs] = None,
        max_iterations: int = 1,
        completion_signal: Union[None, str, Sequence[str]] = None,
        idle_timeout_seconds: Optional[float] = None,
        completion_timeout_seconds: Optional[float] = None,
        name: Optional[str] = None,
        logging: Any = None,
        hooks: Any = None,
        env: Optional[Dict[str, str]] = None,
        resume_session: Optional[str] = None,
        signal: Optional[AbortSignal] = None,
        wait_for_usage_reset: bool = True,
        usage_limit: Optional[UsageLimitConfig] = None,
    ) -> WorktreeRunResult:
        """Run an AFK agent in the worktree (a sandbox is required)."""
        if signal is not None:
            signal.throw_if_aborted()
        validate_timeout_options(idle_timeout_seconds, completion_timeout_seconds)
        if resume_session and max_iterations > 1:
            raise ValueError(
                "resumeSession cannot be combined with maxIterations > 1. "
                "Resume applies to iteration 1 only; multi-iteration resume semantics are not supported."
            )
        if resume_session:
            await assert_resume_session_exists(agent, sandbox.tag, self._host, resume_session)
        resolved = resolve_prompt(prompt, prompt_file)
        eff_env = _env(self._host, agent.env, sandbox.env, env)
        current = await wm.get_current_branch(self._host) if resolved.source == "template" else ""
        log = to_logging(logging) or FileLogging(
            path=os.path.join(self._host, ".sandcastle", "logs", build_log_filename(self._info.branch, None, name))
        )
        if isinstance(log, FileLogging):
            print_file_display_startup(log.path, name, self._info.branch, None)
            display: Display = FileDisplay(log.path)
        else:
            display = TerminalDisplay()
        resolved_prompt = self._substitute(resolved, prompt_args, current, display)
        mounts: List[MountEntry] = []
        info_kwargs: Dict[str, Any] = {}
        if sandbox.tag == "isolated":
            started = await start_isolated_sandbox(
                provider=sandbox, host_repo_dir=self._info.path, env=eff_env, copy_paths=self._copy, copy_source_dir=self._host
            )
            h = started.handle

            async def apply() -> None:
                await sync_out(self._info.path, h)

            info_kwargs["apply_to_host"] = apply
        elif sandbox.tag == "none":
            started = await start_no_sandbox(sandbox, self._info.path, eff_env)
        else:
            mounts = _git_mounts(self._host, self._info.path)
            started = await start_bind_mount_sandbox(
                provider=sandbox, host_repo_dir=self._host, env=eff_env, worktree_or_repo_path=self._info.path, git_mounts=mounts
            )
            info_kwargs["bind_mount_handle"] = started.handle
        ul = usage_limit or UsageLimitConfig()
        if not wait_for_usage_reset:
            ul = replace(ul, enabled=False)
        try:
            factory = ReuseSandboxFactory(
                SandboxInfo(sandbox_repo_path=started.worktree_path, host_worktree_path=self._info.path, **info_kwargs), started.sandbox
            )
            display.intro(name or "sandcastle")
            result = await orchestrate(
                OrchestrateOptions(
                    host_repo_dir=self._host,
                    iterations=max_iterations,
                    hooks=SandboxHooks.from_any(hooks),
                    prompt=resolved_prompt,
                    branch=None if self._merge_to_head else self._info.branch,
                    provider=agent,
                    completion_signal=completion_signal,
                    idle_timeout_seconds=idle_timeout_seconds,
                    completion_timeout_seconds=completion_timeout_seconds,
                    name=name,
                    resume_session=resume_session,
                    signal=signal,
                    skip_prompt_expansion=resolved.source == "inline",
                    timeouts=self._timeouts,
                    keep_source_branch=self._merge_to_head,
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
            try:
                await started.handle.close()
            except Exception:  # noqa: BLE001
                pass
            remove_mount_temp_dirs(mounts)
            display.close()
        return WorktreeRunResult(
            iterations=result.iterations,
            stdout=result.stdout,
            commits=result.commits,
            branch=result.branch,
            completion_signal=result.completion_signal,
            log_file_path=log.path if isinstance(log, FileLogging) else None,
        )

    # -- create_sandbox -------------------------------------------------
    async def create_sandbox(self, *, sandbox: Any, hooks: Any = None, copy_to_worktree: Optional[List[str]] = None, timeouts: Any = None, _test: Any = None) -> Sandbox:
        """A long-lived sandbox on this worktree; its ``close()`` leaves the
        worktree for ``wt.close()``."""
        paths = [*(self._copy or []), *(copy_to_worktree or [])] if sandbox.tag == "isolated" else copy_to_worktree
        return await create_sandbox_from_worktree(
            branch=self._info.branch,
            worktree_path=self._info.path,
            host_repo_dir=self._host,
            sandbox=sandbox,
            hooks=hooks,
            copy_to_worktree_paths=paths,
            timeouts=timeouts,
            branch_strategy=self._strategy,
            _test=_test,
        )


async def create_worktree(
    *,
    branch_strategy: Any,
    cwd: Optional[str] = None,
    copy_to_worktree: Optional[List[str]] = None,
    hooks: Any = None,
    timeouts: Any = None,
) -> Worktree:
    bs = to_branch_strategy(branch_strategy)
    if bs is None or bs.type == "head":
        raise ValueError("create_worktree() needs a 'branch' or 'merge-to-head' branch strategy ('head' means no worktree).")
    t = Timeouts.from_any(timeouts)
    validate_timeout_options(timeouts=t)
    host = resolve_cwd(cwd)
    try:
        await wm.prune_stale(host)
    except Exception:  # noqa: BLE001
        pass
    if bs.type == "branch":
        info = await wm.create(host, branch=bs.branch, base_branch=bs.base_branch)
    else:
        info = await wm.create(host)
    if copy_to_worktree:
        await copy_to_worktree_fn(copy_to_worktree, host, info.path, (t or Timeouts()).copy_to_worktree_ms)
    hk = SandboxHooks.from_any(hooks)
    if hk.host_on_worktree_ready:
        await run_host_hooks(hk.host_on_worktree_ready, info.path)
    return Worktree(host, info, bs, copy_to_worktree, t)


copy_to_worktree_fn = copy_to_worktree

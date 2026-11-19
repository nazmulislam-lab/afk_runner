"""``interactive()``: launch the agent's own TUI inside a sandbox. Port of
``interactive.ts``.

When the session ends, sandcastle collects commits and merges branches, as
``run()`` does. Missing ``{{KEY}}`` prompt arguments are asked for on the
terminal.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import worktree_manager as wm
from .abort import AbortSignal, race_abort_signal
from .agent_provider import AgentCommandOptions
from .copy_to_worktree import copy_to_worktree
from .create_sandbox import assert_interactive_supported
from .display import TerminalDisplay
from .lifecycle import SandboxHooks, SandboxLifecycleOptions, Timeouts, run_host_hooks, with_sandbox_lifecycle
from .mounts import SANDBOX_REPO_DIR, MountEntry, patch_git_mounts_for_windows, remove_mount_temp_dirs, resolve_git_mounts
from .prompts import (
    BUILT_IN_PROMPT_ARG_KEYS,
    PromptArgs,
    find_missing_prompt_arg_keys,
    preprocess_prompt,
    resolve_prompt,
    substitute_prompt_args,
    validate_no_args_with_inline_prompt,
    validate_no_built_in_arg_override,
)
from .sandbox_provider import HeadBranchStrategy, InteractiveExecOptions, MergeToHeadBranchStrategy, NoSandboxCreateOptions, to_branch_strategy
from .sandbox_service import SandboxService, start_bind_mount_sandbox, start_isolated_sandbox
from .sandboxes.no_sandbox import no_sandbox
from .sync import sync_out
from .util import merge_provider_env, resolve_cwd, resolve_env, validate_timeout_options
from .worktree_manager import generate_temp_branch_name, get_current_branch


@dataclass
class InteractiveResult:
    commits: List[Dict[str, str]]
    branch: str
    exit_code: int
    preserved_worktree_path: Optional[str] = None


async def _ask(key: str) -> str:
    while True:
        value = await asyncio.to_thread(input, f"◆  Enter value for {{{{{key}}}}}: ")
        if value:
            return value
        print(f"   A value is required for {{{{{key}}}}}", file=sys.stderr)


async def interactive(
    *,
    agent: Any,
    sandbox: Any = None,
    prompt: Optional[str] = None,
    prompt_file: Optional[str] = None,
    name: Optional[str] = None,
    branch_strategy: Any = None,
    hooks: Any = None,
    copy_to_worktree: Optional[List[str]] = None,
    prompt_args: Optional[PromptArgs] = None,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
    signal: Optional[AbortSignal] = None,
    timeouts: Any = None,
) -> InteractiveResult:
    """Launch an interactive agent session (defaults to ``no_sandbox()``)."""
    if signal is not None:
        signal.throw_if_aborted()
    t = Timeouts.from_any(timeouts)
    validate_timeout_options(timeouts=t)
    provider = sandbox or no_sandbox()
    assert_interactive_supported(provider)
    hooks_obj = SandboxHooks.from_any(hooks)
    bs = to_branch_strategy(branch_strategy) or (MergeToHeadBranchStrategy() if provider.tag == "isolated" else HeadBranchStrategy())
    if bs.type == "head" and provider.tag == "isolated":
        raise ValueError("head branch strategy is not supported with isolated providers")
    if bs.type == "head" and copy_to_worktree:
        raise ValueError(
            "copyToWorktree is not supported with head branch strategy. In head mode the host working directory is bind-mounted directly."
        )
    if not hasattr(agent, "build_interactive_args"):
        raise ValueError(f'Agent provider "{agent.name}" does not support buildInteractiveArgs, required for interactive sessions.')
    explicit = bs.branch if bs.type == "branch" else None
    is_head = bs.type == "head"
    d = TerminalDisplay()
    try:
        host_repo_dir = resolve_cwd(cwd)
        has_prompt = prompt is not None or prompt_file is not None
        resolved = resolve_prompt(prompt, prompt_file) if has_prompt else None
        raw = resolved.text if resolved else ""
        is_inline = resolved is not None and resolved.source == "inline"
        effective_env = {**merge_provider_env(resolve_env(host_repo_dir), agent.env, provider.env), **(env or {})}
        current = await get_current_branch(host_repo_dir)
        resolved_branch = current if is_head else (explicit or generate_temp_branch_name(name))

        substituted = raw
        if has_prompt and not is_inline:
            user_args = dict(prompt_args or {})
            validate_no_built_in_arg_override(user_args)
            for key in find_missing_prompt_arg_keys(raw, user_args):
                user_args[key] = await _ask(key)
            substituted = substitute_prompt_args(
                raw, {"SOURCE_BRANCH": resolved_branch, "TARGET_BRANCH": current, **user_args}, d, BUILT_IN_PROMPT_ARG_KEYS
            )
        elif is_inline:
            validate_no_args_with_inline_prompt(prompt_args)

        lifecycle_branch = current if is_head else explicit
        d.intro(name or "sandcastle interactive")
        d.summary("Interactive Session", {"Agent": name or agent.name, "Sandbox": provider.name, "Branch": resolved_branch})

        wt: Optional[wm.WorktreeInfo] = None
        if not is_head:

            async def mk(_m: Any) -> wm.WorktreeInfo:
                try:
                    await wm.prune_stale(host_repo_dir)
                except Exception:  # noqa: BLE001
                    pass
                if explicit:
                    return await wm.create(host_repo_dir, branch=explicit)
                return await wm.create(host_repo_dir, name=name, temp_branch=resolved_branch)

            wt = await d.task_log("Creating worktree", mk)
        host_work = wt.path if wt else host_repo_dir
        mounts: List[MountEntry] = []

        async def start() -> Any:
            if not is_head:
                if provider.tag in ("bind-mount", "none") and copy_to_worktree:

                    async def cp(_m: Any) -> None:
                        await copy_to_worktree_fn(copy_to_worktree, host_repo_dir, host_work, (t or Timeouts()).copy_to_worktree_ms)

                    await d.task_log("Copying files to worktree", cp)
                if hooks_obj.host_on_worktree_ready:
                    await run_host_hooks(hooks_obj.host_on_worktree_ready, host_work)
            elif hooks_obj.host_on_worktree_ready:
                await run_host_hooks(hooks_obj.host_on_worktree_ready, host_repo_dir)
            if provider.tag == "none":
                return await provider.create(NoSandboxCreateOptions(worktree_path=host_work, env=effective_env))
            if provider.tag == "isolated":

                async def st(_m: Any) -> Any:
                    return await start_isolated_sandbox(
                        provider=provider, host_repo_dir=host_work, env=effective_env, copy_paths=copy_to_worktree, copy_source_dir=host_repo_dir
                    )

                return (await d.task_log("Starting sandbox", st)).handle
            git_mounts = patch_git_mounts_for_windows(resolve_git_mounts(os.path.join(host_repo_dir, ".git")), host_work, SANDBOX_REPO_DIR)
            mounts.extend(git_mounts)

            async def stb(_m: Any) -> Any:
                return await start_bind_mount_sandbox(
                    provider=provider, host_repo_dir=host_repo_dir, env=effective_env, worktree_or_repo_path=host_work, git_mounts=git_mounts
                )

            return (await d.task_log("Starting sandbox", stb)).handle

        copy_to_worktree_fn = copy_to_worktree_impl
        try:
            handle = await start()
        except BaseException:
            if wt:
                try:
                    await wm.remove(wt.path)
                except Exception:  # noqa: BLE001
                    pass
            remove_mount_temp_dirs(mounts)
            raise

        try:
            if not hasattr(handle, "interactive_exec"):
                raise ValueError(
                    "Sandbox provider does not support interactiveExec. The provider must implement the optional "
                    "interactive_exec method to use interactive()."
                )
            sb = SandboxService(handle)
            wt_path = handle.worktree_path

            async def apply() -> None:
                await sync_out(host_work, handle)

            async def work(ctx: Any) -> int:
                full = substituted if (not has_prompt or is_inline) else await preprocess_prompt(substituted, ctx.sandbox, ctx.sandbox_repo_dir, d)
                args = agent.build_interactive_args(
                    AgentCommandOptions(prompt=full, dangerously_skip_permissions=provider.tag != "none")
                )
                return await race_abort_signal(
                    handle.interactive_exec(args, InteractiveExecOptions(sys.stdin, sys.stdout, sys.stderr, wt_path)), signal
                )

            lr = await with_sandbox_lifecycle(
                SandboxLifecycleOptions(
                    host_repo_dir=host_repo_dir,
                    sandbox_repo_dir=wt_path,
                    hooks=hooks_obj,
                    branch=lifecycle_branch,
                    host_worktree_path=host_repo_dir if is_head else (wt.path if wt else None),
                    apply_to_host=apply if provider.tag == "isolated" and wt else None,
                    timeouts=t,
                ),
                sb,
                work,
                d,
            )
            preserved = None
            if wt:
                try:
                    if await wm.has_uncommitted_changes(wt.path):
                        preserved = wt.path
                except Exception:  # noqa: BLE001
                    pass
                if preserved is None:
                    try:
                        await wm.remove(wt.path)
                    except Exception:  # noqa: BLE001
                        pass
            rows = {"Commits": str(len(lr.commits)), "Branch": lr.branch, "Exit code": str(lr.result)}
            if preserved:
                rows["Preserved worktree"] = preserved
            d.summary("Session Complete", rows)
            return InteractiveResult(commits=lr.commits, branch=lr.branch, exit_code=lr.result, preserved_worktree_path=preserved)
        except BaseException:
            if wt:
                try:
                    await wm.remove(wt.path)
                except Exception:  # noqa: BLE001
                    pass
            raise
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


copy_to_worktree_impl = copy_to_worktree

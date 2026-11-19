"""``run()``: the main entry point. Port of ``run.ts``.

```python
import asyncio
from sandcastle import run, claude_code
from sandcastle.sandboxes.docker import docker

result = asyncio.run(run(agent=claude_code("claude-opus-4-8"), sandbox=docker(),
                         prompt_file=".sandcastle/prompt.md", max_iterations=5))
print(result.commits, result.branch)
```

``run_sync(...)`` is the same without asyncio.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Sequence, Union

from .abort import AbortSignal
from .context_window import build_context_window_lines
from .display import AgentStreamEmitter, AgentStreamEvent, Display, FileDisplay, TerminalDisplay, _Style
from .lifecycle import SandboxHooks, Timeouts
from .orchestrator import IterationResult, OrchestrateOptions, OrchestrateResult, orchestrate
from .output import ExtractionContext, OutputDefinition, StructuredOutputError, extract_structured_output
from .prompts import (
    BUILT_IN_PROMPT_ARG_KEYS,
    PromptArgs,
    resolve_prompt,
    substitute_prompt_args,
    validate_no_args_with_inline_prompt,
    validate_no_built_in_arg_override,
)
from .sandbox_factory import SandboxConfig, WorktreeSandboxFactory
from .sandbox_provider import MergeToHeadBranchStrategy, HeadBranchStrategy, to_branch_strategy
from .usage_limit import UsageLimitConfig
from .util import (
    assert_fork_supported,
    assert_resume_session_exists,
    format_error_message,
    merge_provider_env,
    resolve_cwd,
    resolve_env,
    validate_timeout_options,
)
from .worktree_manager import generate_temp_branch_name, get_current_branch

DEFAULT_MAX_ITERATIONS = 1

# ---------------------------------------------------------------------------
# Logging option
# ---------------------------------------------------------------------------


@dataclass
class FileLogging:
    """Log-to-file mode (the default): progress goes to ``path``."""

    path: str
    on_agent_stream_event: Optional[Callable[[AgentStreamEvent], Any]] = None
    verbose: bool = False
    type: Literal["file"] = "file"


@dataclass
class StdoutLogging:
    """Terminal mode: progress shows in the terminal."""

    verbose: bool = False
    type: Literal["stdout"] = "stdout"


LoggingOption = Union[FileLogging, StdoutLogging]


def to_logging(value: Any) -> Optional[LoggingOption]:
    if value is None or isinstance(value, (FileLogging, StdoutLogging)):
        return value
    if value == "stdout":
        return StdoutLogging()
    if isinstance(value, dict):
        if value.get("type") == "stdout":
            return StdoutLogging(verbose=bool(value.get("verbose")))
        if value.get("type") == "file":
            return FileLogging(
                path=value["path"],
                on_agent_stream_event=value.get("on_agent_stream_event", value.get("onAgentStreamEvent")),
                verbose=bool(value.get("verbose")),
            )
    raise ValueError(f"Invalid logging option: {value!r}")


# ---------------------------------------------------------------------------
# Small builders (exported for tests, as in TS)
# ---------------------------------------------------------------------------


def _describe_parser_cause(cause: Any) -> str:
    if cause is None:
        return "(no parser detail)"
    if isinstance(cause, str):
        return cause
    try:
        return json.dumps(cause, indent=2, default=str)
    except (TypeError, ValueError):
        return str(cause)


def build_structured_output_retry_feedback(error: StructuredOutputError, retries_remaining: int) -> str:
    raw = error.raw_matched if error.raw_matched is not None else "(no matching tag was emitted)"
    return (
        "Your previous response did not produce valid structured output.\n\n"
        f"Retries remaining after this attempt: {retries_remaining}.\n\n"
        f"Problem:\n{error.message}\n\n"
        f"Parser detail:\n{_describe_parser_cause(error.cause)}\n\n"
        f"Previous matched output:\n{raw}\n\n"
        f"Emit only a corrected <{error.tag}> block. Do not change files or run commands."
    )


def sanitize_branch_for_filename(branch: str) -> str:
    return re.sub(r'[/\\:*?"<>|]', "-", branch)


def build_log_filename(resolved_branch: str, target_branch: Optional[str] = None, name: Optional[str] = None) -> str:
    sanitized = sanitize_branch_for_filename(resolved_branch)
    suffix = ""
    if name:
        suffix = "-" + "".join(
            ch if re.match(r"[a-z0-9_.-]", ch) else "-" * (2 if ord(ch) > 0xFFFF else 1) for ch in name.lower()
        )
    if target_branch:
        return f"{sanitize_branch_for_filename(target_branch)}-{sanitized}{suffix}.log"
    return f"{sanitized}{suffix}.log"


def build_run_summary_rows(*, name: Optional[str], agent_name: str, sandbox_name: str, max_iterations: int, branch: str) -> Dict[str, str]:
    return {"Agent": name or agent_name, "Sandbox": sandbox_name, "Max iterations": str(max_iterations), "Branch": branch}


def build_completion_message(completion_signal: Optional[str], iterations_run: int):
    if completion_signal is not None:
        return (f"Run complete: agent finished after {iterations_run} iteration(s).", "success")
    return (f"Run complete: reached {iterations_run} iteration(s) without completion signal.", "warn")


def print_file_display_startup(log_path: str, agent_name: Optional[str], branch: Optional[str], host_repo_dir: Optional[str]) -> None:
    s = _Style(sys.stdout.isatty() and not os.environ.get("NO_COLOR"))
    label = s.bold(f"[{agent_name or 'Agent'}]")
    branch_part = f" on branch {branch}" if branch else ""
    repo = host_repo_dir or os.getcwd()
    shown = os.path.relpath(log_path, os.getcwd()) if os.path.realpath(repo) == os.path.realpath(os.getcwd()) else log_path
    print(f"{label} Started{branch_part}")
    print(s.dim(f"  tail -f {shown}"), flush=True)


def _file_ends_mid_line(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                return False
            f.seek(-1, os.SEEK_END)
            return f.read(1) != b"\n"
    except OSError:
        return False


def build_agent_stream_handler(logging: LoggingOption) -> Optional[Callable[[AgentStreamEvent], None]]:
    user = logging.on_agent_stream_event if isinstance(logging, FileLogging) else None
    sink: Optional[Callable[[str], None]] = None
    if logging.verbose:
        if isinstance(logging, FileLogging):
            path = logging.path
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            except OSError:
                pass

            def file_sink(line: str) -> None:
                try:
                    prefix = "\n" if _file_ends_mid_line(path) else ""
                    with open(path, "a", encoding="utf-8") as f:
                        f.write(prefix + line + "\n")
                except OSError:
                    pass

            sink = file_sink
        else:

            def stdout_sink(line: str) -> None:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()

            sink = stdout_sink
    if user is None and sink is None:
        return None

    def handler(event: AgentStreamEvent) -> None:
        if user is not None:
            try:
                user(event)
            except Exception:  # noqa: BLE001
                pass
        if sink is not None and event.type == "raw":
            sink(event.line)  # type: ignore[union-attr]

    return handler


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    iterations: List[IterationResult]
    stdout: str
    commits: List[Dict[str, str]]
    branch: str
    completion_signal: Optional[str] = None
    log_file_path: Optional[str] = None
    preserved_worktree_path: Optional[str] = None
    #: Structured output (only when ``output`` was given).
    output: Any = None
    _resume: Optional[Callable[..., Awaitable["RunResult"]]] = field(default=None, repr=False)
    _fork: Optional[Callable[..., Awaitable["RunResult"]]] = field(default=None, repr=False)

    async def resume(self, prompt: str, **options: Any) -> "RunResult":
        """Continue the last captured agent session for one iteration."""
        if self._resume is None:
            raise RuntimeError("resume is not available for this result")
        return await self._resume(prompt, **options)

    async def fork(self, prompt: str, **options: Any) -> "RunResult":
        """Fork the last captured agent session for one iteration."""
        if self._fork is None:
            raise RuntimeError("fork is not available for this result")
        return await self._fork(prompt, **options)

    @property
    def can_resume(self) -> bool:
        return self._resume is not None


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


async def run(
    *,
    agent: Any,
    sandbox: Any,
    cwd: Optional[str] = None,
    prompt: Optional[str] = None,
    prompt_file: Optional[str] = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    hooks: Any = None,
    prompt_args: Optional[PromptArgs] = None,
    logging: Any = None,
    completion_signal: Union[None, str, Sequence[str]] = None,
    idle_timeout_seconds: Optional[float] = None,
    completion_timeout_seconds: Optional[float] = None,
    name: Optional[str] = None,
    copy_to_worktree: Optional[List[str]] = None,
    branch_strategy: Any = None,
    resume_session: Optional[str] = None,
    fork_session: bool = False,
    signal: Optional[AbortSignal] = None,
    timeouts: Any = None,
    output: Optional[OutputDefinition] = None,
    wait_for_usage_reset: bool = True,
    usage_limit: Optional[UsageLimitConfig] = None,
) -> RunResult:
    """Run an agent in a sandbox for up to ``max_iterations`` iterations.

    See the README for every option. Python-port extras:

    * ``wait_for_usage_reset`` (default ``True``): when the agent hits its
      usage limit (e.g. Claude's 5-hour window), wait for the reset and resume
      the same session instead of failing.
    * ``usage_limit``: fine-tune that behaviour (:class:`UsageLimitConfig`).
    """
    options = dict(locals())
    if signal is not None:
        signal.throw_if_aborted()

    hooks_obj = SandboxHooks.from_any(hooks)
    timeouts_obj = Timeouts.from_any(timeouts)
    logging_obj = to_logging(logging)
    bs = to_branch_strategy(branch_strategy) or (
        MergeToHeadBranchStrategy() if sandbox.tag == "isolated" else HeadBranchStrategy()
    )
    ul = usage_limit or UsageLimitConfig()
    if not wait_for_usage_reset:
        ul = replace(ul, enabled=False)

    validate_timeout_options(idle_timeout_seconds, completion_timeout_seconds, timeouts_obj)
    if bs.type == "head" and sandbox.tag == "isolated":
        raise ValueError("head branch strategy is not supported with isolated providers")
    if bs.type == "head" and copy_to_worktree:
        raise ValueError(
            "copyToWorktree is not supported with head branch strategy. "
            "In head mode the host working directory is bind-mounted directly."
        )
    is_resuming = bool(resume_session)
    if is_resuming and max_iterations > 1:
        raise ValueError(
            "resumeSession cannot be combined with maxIterations > 1. "
            "Resume applies to iteration 1 only; multi-iteration resume semantics are not supported."
        )
    if fork_session and not is_resuming:
        raise ValueError("forkSession requires resumeSession. Use RunResult.fork(prompt) to fork the last captured session.")
    if fork_session:
        assert_fork_supported(agent)
    if output is not None and max_iterations != 1:
        raise ValueError("output requires maxIterations to be 1. Structured output is only supported for single-iteration runs.")
    out_retries = getattr(output, "max_retries", 0) if output is not None else 0
    if not isinstance(out_retries, int) or out_retries < 0:
        raise ValueError(f"output.maxRetries must be a non-negative integer. Received: {out_retries}")
    if out_retries > 0 and getattr(agent, "session_storage", None) is None:
        raise ValueError(
            f'output.maxRetries requires an agent provider that supports session resumption. The "{agent.name}" '
            "provider does not. Use an agent provider that supports resume, or set maxRetries to 0."
        )

    host_repo_dir = resolve_cwd(cwd)
    if is_resuming and resume_session:
        await assert_resume_session_exists(agent, sandbox.tag, host_repo_dir, resume_session)

    resolved = resolve_prompt(prompt, prompt_file)
    if output is not None and f"<{output.tag}>" not in resolved.text:
        raise ValueError(
            f"output tag <{output.tag}> not found in the resolved prompt. The caller must instruct the agent to emit the configured tag."
        )
    is_inline = resolved.source == "inline"
    env = merge_provider_env(resolve_env(host_repo_dir), agent.env, sandbox.env)

    explicit_branch = bs.branch if bs.type == "branch" else None
    current = await get_current_branch(host_repo_dir)
    resolved_branch = current if bs.type == "head" else (explicit_branch or generate_temp_branch_name(name))
    target_branch = current if bs.type == "merge-to-head" else None
    orchestrate_branch = current if bs.type == "head" else explicit_branch

    if logging_obj is None:
        logging_obj = FileLogging(
            path=os.path.join(host_repo_dir, ".sandcastle", "logs", build_log_filename(resolved_branch, target_branch, name))
        )

    if isinstance(logging_obj, FileLogging):
        print_file_display_startup(logging_obj.path, name, resolved_branch, host_repo_dir)
        display: Display = FileDisplay(logging_obj.path)
    else:
        display = TerminalDisplay()
    emitter = AgentStreamEmitter(build_agent_stream_handler(logging_obj))
    factory = WorktreeSandboxFactory(
        SandboxConfig(
            env=env,
            host_repo_dir=host_repo_dir,
            sandbox_provider=sandbox,
            branch_strategy=bs,
            copy_to_worktree=copy_to_worktree,
            name=name,
            temp_branch=resolved_branch if bs.type == "merge-to-head" else None,
            hooks=hooks_obj,
            signal=signal,
            timeouts=timeouts_obj,
        ),
        display,
    )

    try:
        display.intro(name or "sandcastle")
        display.summary(
            "Sandcastle Run",
            build_run_summary_rows(
                name=name, agent_name=agent.name, sandbox_name=sandbox.name, max_iterations=max_iterations, branch=resolved_branch
            ),
        )
        user_args = dict(prompt_args or {})
        if is_inline:
            validate_no_args_with_inline_prompt(user_args)
            agent_prompt = resolved.text
        else:
            validate_no_built_in_arg_override(user_args)
            agent_prompt = substitute_prompt_args(
                resolved.text,
                {"SOURCE_BRANCH": resolved_branch, "TARGET_BRANCH": current, **user_args},
                display,
                BUILT_IN_PROMPT_ARG_KEYS,
            )
        result: OrchestrateResult = await orchestrate(
            OrchestrateOptions(
                host_repo_dir=host_repo_dir,
                iterations=max_iterations,
                hooks=hooks_obj,
                prompt=agent_prompt,
                branch=orchestrate_branch,
                provider=agent,
                completion_signal=completion_signal,
                idle_timeout_seconds=idle_timeout_seconds,
                completion_timeout_seconds=completion_timeout_seconds,
                name=name,
                resume_session=resume_session,
                fork_session=fork_session,
                signal=signal,
                skip_prompt_expansion=is_inline,
                timeouts=timeouts_obj,
                usage_limit=ul,
            ),
            factory,
            display,
            emitter,
        )
        msg, sev = build_completion_message(result.completion_signal, len(result.iterations))
        display.status(msg, sev)  # type: ignore[arg-type]
        for line in build_context_window_lines(result.iterations):
            display.text(line)
    except BaseException as error:
        if isinstance(logging_obj, FileLogging) and isinstance(error, Exception):
            display.status(format_error_message(error), "error")
        if signal is not None:
            signal.throw_if_aborted()
        raise
    finally:
        display.close()

    def follow_up(kind: str) -> Callable[..., Awaitable[RunResult]]:
        async def go(follow_prompt: str, **follow_options: Any) -> RunResult:
            last = result.iterations[-1] if result.iterations else None
            if last is None or not last.session_id:
                raise RuntimeError(f"Cannot {kind}: no sessionId was captured")
            opts = {k: v for k, v in options.items()}
            opts.update({"prompt_args": None, "output": None})
            opts.update(follow_options)
            opts.update(
                {
                    "prompt": follow_prompt,
                    "prompt_file": None,
                    "max_iterations": 1,
                    "resume_session": last.session_id,
                    "fork_session": kind == "fork",
                }
            )
            return await run(**opts)

        return go

    has_storage = getattr(agent, "session_storage", None) is not None
    base = RunResult(
        iterations=result.iterations,
        stdout=result.stdout,
        commits=result.commits,
        branch=result.branch,
        completion_signal=result.completion_signal,
        log_file_path=logging_obj.path if isinstance(logging_obj, FileLogging) else None,
        preserved_worktree_path=result.preserved_worktree_path,
        _resume=follow_up("resume") if has_storage else None,
        _fork=follow_up("fork") if has_storage else None,
    )
    if output is None:
        return base

    last = result.iterations[-1] if result.iterations else None
    try:
        base.output = await extract_structured_output(
            base.stdout,
            output,
            ExtractionContext(
                commits=base.commits,
                branch=base.branch,
                preserved_worktree_path=base.preserved_worktree_path,
                session_id=last.session_id if last else None,
                session_file_path=last.session_file_path if last else None,
            ),
        )
        return base
    except StructuredOutputError as error:
        if out_retries > 0 and error.session_id is not None:
            remaining = out_retries - 1
            opts = dict(options)
            opts.update(
                {
                    "prompt": build_structured_output_retry_feedback(error, remaining),
                    "prompt_file": None,
                    "prompt_args": None,
                    "resume_session": error.session_id,
                    "fork_session": False,
                    "output": replace(output, max_retries=remaining),
                }
            )
            return await run(**opts)
        raise


def run_sync(**kwargs: Any) -> RunResult:
    """Blocking ``run()`` for scripts that don't use asyncio."""
    return asyncio.run(run(**kwargs))

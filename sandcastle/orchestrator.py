"""The iteration loop. Port of ``Orchestrator.ts``.

Each iteration acquires a sandbox (``SandboxFactory.with_sandbox``), runs the
lifecycle (git setup, hooks), resumes a session if asked, expands the prompt,
invokes the agent with idle / completion timeouts, captures the session, and
checks for the completion signal.

Python-port addition: when the agent hits its usage limit, the iteration
waits for the window to reset and resumes the same session (see
:mod:`sandcastle.usage_limit`).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from .abort import AbortSignal, check_abort
from .agent_provider import (
    AgentCommandOptions,
    ContextWindowEvent,
    ErrorEvent,
    IterationUsage,
    ResultEvent,
    SessionIdEvent,
    TextEvent,
    ToolCallEvent,
    UsageEvent,
)
from .context_window import ContextWindow, build_context_window_lines
from .display import (
    AgentRawStreamEvent,
    AgentStreamEmitter,
    AgentTextStreamEvent,
    AgentToolCallStreamEvent,
    Display,
    TextDeltaBuffer,
    now,
)
from .errors import AgentError, AgentIdleTimeoutError, SessionCaptureError, UsageLimitWaitError
from .lifecycle import SandboxHooks, SandboxLifecycleOptions, Timeouts, with_sandbox_lifecycle
from .prompts import preprocess_prompt
from .usage_limit import UsageLimitConfig, UsageLimitReached, is_usage_limit_message, parse_reset_time, wait_for_reset

IDLE_WARNING_INTERVAL_S = 60.0
DEFAULT_COMPLETION_SIGNAL = "<promise>COMPLETE</promise>"
DEFAULT_IDLE_TIMEOUT_SECONDS = 10 * 60
DEFAULT_COMPLETION_TIMEOUT_SECONDS = 60


@dataclass
class InvokeResult:
    result: str
    session_id: Optional[str] = None
    usage: Optional[IterationUsage] = None


def _limit_check(texts: Sequence[str]) -> Optional[str]:
    for t in texts:
        if t and is_usage_limit_message(t):
            return t
    return None


async def invoke_agent(
    *,
    sandbox: Any,
    sandbox_repo_dir: str,
    prompt: str,
    provider: Any,
    idle_timeout_s: float,
    completion_timeout_s: float,
    completion_signals: Sequence[str],
    on_text: Callable[[str], None],
    on_tool_call: Callable[[str, str], None],
    on_raw_line: Callable[[str], None],
    on_idle_warning: Callable[[int], None],
    on_completion_timeout: Callable[[float], None],
    on_context_window: Callable[[ContextWindow], None],
    idle_warning_interval_s: float = IDLE_WARNING_INTERVAL_S,
    resume_session: Optional[str] = None,
    fork_session: bool = False,
    signal: Optional[AbortSignal] = None,
    detect_usage_limit: bool = True,
) -> InvokeResult:
    loop = asyncio.get_running_loop()
    st: Dict[str, Any] = {
        "result": "",
        "stream_error": None,
        "session_id": None,
        "usage": None,
        "acc": "",
        "completion": False,
        "settled": False,
        "idle_minutes": 0,
    }

    def get_result(fallback: str) -> str:
        r, acc = st["result"], st["acc"]
        if r != "" and acc.endswith(r):
            return acc
        return fallback if r == "" else r

    idle_fut: asyncio.Future = loop.create_future()
    completion_fut: asyncio.Future = loop.create_future()
    timers: Dict[str, Optional[asyncio.TimerHandle]] = {"timeout": None, "warning": None}

    def cancel(name: str) -> None:
        h = timers[name]
        if h is not None:
            h.cancel()
            timers[name] = None

    def warning_tick() -> None:
        if st["settled"]:
            return
        st["idle_minutes"] += 1
        on_idle_warning(st["idle_minutes"])
        timers["warning"] = loop.call_later(idle_warning_interval_s, warning_tick)

    def start_warning() -> None:
        cancel("warning")
        st["idle_minutes"] = 0
        timers["warning"] = loop.call_later(idle_warning_interval_s, warning_tick)

    def fire_completion() -> None:
        if completion_fut.done():
            return
        on_completion_timeout(completion_timeout_s)
        completion_fut.set_result(InvokeResult(get_result(st["acc"]), st["session_id"], st["usage"]))

    def fire_idle() -> None:
        if idle_fut.done():
            return
        idle_fut.set_exception(
            AgentIdleTimeoutError(
                f"Agent idle for {idle_timeout_s:g} seconds — no output received. "
                "Increase the idle_timeout_seconds option to allow longer silences.",
                timeout_ms=idle_timeout_s * 1000,
            )
        )

    def reset_timer() -> None:
        cancel("timeout")
        if st["completion"]:
            timers["timeout"] = loop.call_later(completion_timeout_s, fire_completion)
        else:
            timers["timeout"] = loop.call_later(idle_timeout_s, fire_idle)
            start_warning()

    if signal is not None:
        signal.throw_if_aborted()

    def on_line(line: str) -> None:
        if st["settled"]:
            return
        try:
            on_raw_line(line)
        except Exception:  # noqa: BLE001
            pass
        for ev in provider.parse_stream_line(line):
            if isinstance(ev, TextEvent):
                on_text(ev.text)
                st["acc"] += ev.text
            elif isinstance(ev, ContextWindowEvent):
                on_context_window(ev.context_window)
            elif isinstance(ev, ResultEvent):
                st["result"] = ev.result
            elif isinstance(ev, ToolCallEvent):
                on_tool_call(ev.name, ev.args)
            elif isinstance(ev, SessionIdEvent):
                st["session_id"] = ev.session_id
            elif isinstance(ev, UsageEvent):
                st["usage"] = ev.usage
            elif isinstance(ev, ErrorEvent):
                st["stream_error"] = ev.message
        if not st["completion"] and any(s in st["acc"] or s in st["result"] for s in completion_signals):
            st["completion"] = True
            cancel("warning")
        reset_timer()

    def usage_limit(text: str, partial: str) -> UsageLimitReached:
        return UsageLimitReached(text, st["session_id"] or resume_session, parse_reset_time(text), partial)

    async def run_exec() -> InvokeResult:
        cmd = provider.build_print_command(
            AgentCommandOptions(
                prompt=prompt, dangerously_skip_permissions=True, resume_session=resume_session, fork_session=fork_session
            )
        )
        r = await sandbox.exec(cmd.command, on_line=on_line, cwd=sandbox_repo_dir, stdin=cmd.stdin)
        if r.exit_code != 0:
            detail = r.stderr
            if not detail.strip():
                detail = st["stream_error"] or ""
            if not detail.strip():
                detail = st["result"]
            if not detail.strip():
                lines = [ln for ln in r.stdout.split("\n") if ln.strip()]
                detail = "\n".join(lines[-20:])
            if detect_usage_limit:
                hit = _limit_check([st["stream_error"] or "", st["result"], r.stderr, detail])
                if hit:
                    raise usage_limit(hit, st["acc"])
            raise AgentError(f"{provider.name} exited with code {r.exit_code}:\n{detail}")
        if st["stream_error"] is not None:
            if detect_usage_limit and is_usage_limit_message(st["stream_error"]):
                raise usage_limit(st["stream_error"], st["acc"])
            raise AgentError(f"{provider.name} reported an error:\n{st['stream_error']}")
        if detect_usage_limit and len(st["result"]) < 500 and is_usage_limit_message(st["result"]):
            raise usage_limit(st["result"], st["acc"])
        return InvokeResult(get_result(r.stdout), st["session_id"], st["usage"])

    reset_timer()
    exec_task = asyncio.ensure_future(run_exec())
    waiters = {exec_task, idle_fut, completion_fut}
    abort_task = asyncio.ensure_future(signal.wait()) if signal is not None else None
    if abort_task is not None:
        waiters.add(abort_task)
    try:
        done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if exec_task in done:
            return exec_task.result()
        if abort_task is not None and abort_task in done:
            signal.throw_if_aborted()  # type: ignore[union-attr]
        if idle_fut in done:
            return idle_fut.result()  # raises AgentIdleTimeoutError
        return completion_fut.result()
    finally:
        st["settled"] = True
        cancel("timeout")
        cancel("warning")
        if abort_task is not None:
            abort_task.cancel()
        if not exec_task.done():
            exec_task.cancel()
            try:
                await exec_task
            except BaseException:  # noqa: BLE001
                pass
        for f in (idle_fut, completion_fut):
            if not f.done():
                f.cancel()
            elif not f.cancelled():
                f.exception()  # mark retrieved


@dataclass
class IterationResult:
    """Per-iteration result."""

    session_id: Optional[str] = None
    session_file_path: Optional[str] = None
    usage: Optional[IterationUsage] = None
    context_window: Optional[ContextWindow] = None
    #: How many times this iteration waited for a usage-limit reset.
    usage_limit_waits: int = 0


@dataclass
class OrchestrateResult:
    iterations: List[IterationResult]
    completion_signal: Optional[str]
    stdout: str
    commits: List[Dict[str, str]]
    branch: str
    preserved_worktree_path: Optional[str] = None


@dataclass
class OrchestrateOptions:
    host_repo_dir: str
    iterations: int
    prompt: str
    provider: Any
    hooks: Optional[SandboxHooks] = None
    branch: Optional[str] = None
    completion_signal: Union[None, str, Sequence[str]] = None
    idle_timeout_seconds: Optional[float] = None
    completion_timeout_seconds: Optional[float] = None
    name: Optional[str] = None
    idle_warning_interval_s: Optional[float] = None
    resume_session: Optional[str] = None
    fork_session: bool = False
    signal: Optional[AbortSignal] = None
    skip_prompt_expansion: bool = False
    timeouts: Optional[Timeouts] = None
    keep_source_branch: bool = False
    usage_limit: UsageLimitConfig = field(default_factory=UsageLimitConfig)


def _signals(value: Union[None, str, Sequence[str]]) -> List[str]:
    if value is None:
        return [DEFAULT_COMPLETION_SIGNAL]
    if isinstance(value, str):
        return [value]
    return list(value)


async def orchestrate(
    options: OrchestrateOptions, factory: Any, display: Display, emitter: AgentStreamEmitter
) -> OrchestrateResult:
    idle_s = options.idle_timeout_seconds or DEFAULT_IDLE_TIMEOUT_SECONDS
    completion_s = options.completion_timeout_seconds or DEFAULT_COMPLETION_TIMEOUT_SECONDS
    provider = options.provider
    storage = getattr(provider, "session_storage", None)
    signals = _signals(options.completion_signal)
    ul = options.usage_limit
    total_waits = [0]

    def label(msg: str) -> str:
        return f"[{options.name}] {msg}" if options.name else msg

    all_commits: List[Dict[str, str]] = []
    all_iterations: List[IterationResult] = []
    all_stdout = ""
    resolved_branch = ""
    preserved: Optional[str] = None

    for i in range(1, options.iterations + 1):
        check_abort(options.signal)
        display.status(label(f"Iteration {i}/{options.iterations}"), "info")

        async def make(info: Any, sandbox: Any, i: int = i) -> Any:
            async def work(ctx: Any) -> Dict[str, Any]:
                it_resume = options.resume_session if i == 1 else None
                it_fork = options.fork_session if i == 1 else False
                handle = info.bind_mount_handle
                if it_resume and handle is not None and storage is not None:
                    display.status(label("Resuming session"), "info")
                    try:
                        await storage.resume_into_sandbox(
                            host_cwd=options.host_repo_dir, sandbox_cwd=ctx.sandbox_repo_dir, session_id=it_resume, handle=handle
                        )
                    except Exception as e:  # noqa: BLE001
                        raise SessionCaptureError(f"Session resume failed: {e}", session_id=it_resume) from e

                full_prompt = (
                    options.prompt
                    if options.skip_prompt_expansion
                    else await preprocess_prompt(options.prompt, ctx.sandbox, ctx.sandbox_repo_dir, display)
                )
                display.status(label("Agent started"), "success")

                def flush_chunk(chunk: str) -> None:
                    display.text_chunk(chunk)
                    emitter.emit(AgentTextStreamEvent(chunk, i, now()))

                buf = TextDeltaBuffer(flush_chunk)

                def on_tool_call(name: str, args: str) -> None:
                    buf.flush()
                    display.tool_call(name, args)
                    emitter.emit(AgentToolCallStreamEvent(name, args, i, now()))

                def on_idle_warning(minutes: int) -> None:
                    display.status(
                        label("Agent idle for 1 minute" if minutes == 1 else f"Agent idle for {minutes} minutes"), "warn"
                    )

                def on_completion_timeout(t: float) -> None:
                    display.status(
                        label(
                            f"Completion signal seen but agent process is hanging — force-completing after {t:g}s grace window."
                        ),
                        "warn",
                    )

                cw: List[Optional[ContextWindow]] = [None]
                attempt_prompt, attempt_resume, attempt_fork = full_prompt, it_resume, it_fork
                prefix_output = ""
                waits = 0
                while True:
                    try:
                        res = await invoke_agent(
                            sandbox=ctx.sandbox,
                            sandbox_repo_dir=ctx.sandbox_repo_dir,
                            prompt=attempt_prompt,
                            provider=provider,
                            idle_timeout_s=idle_s,
                            completion_timeout_s=completion_s,
                            completion_signals=signals,
                            on_text=buf.write,
                            on_tool_call=on_tool_call,
                            on_raw_line=lambda line: emitter.emit(AgentRawStreamEvent(line, i, now())),
                            on_idle_warning=on_idle_warning,
                            on_completion_timeout=on_completion_timeout,
                            on_context_window=lambda c: cw.__setitem__(0, c),
                            idle_warning_interval_s=options.idle_warning_interval_s or IDLE_WARNING_INTERVAL_S,
                            resume_session=attempt_resume,
                            fork_session=attempt_fork,
                            signal=options.signal,
                            detect_usage_limit=ul.enabled,
                        )
                        break
                    except UsageLimitReached as hit:
                        buf.flush()
                        if not ul.enabled:
                            raise AgentError(f"{provider.name} hit its usage limit:\n{hit.message}") from hit
                        if ul.max_waits is not None and total_waits[0] >= ul.max_waits:
                            raise UsageLimitWaitError(
                                f"{provider.name} hit its usage limit and the maximum of {ul.max_waits} wait(s) is used up:\n{hit.message}",
                                resets_at=hit.resets_at.timestamp() if hit.resets_at else None,
                            ) from hit
                        total_waits[0] += 1
                        waits += 1
                        prefix_output += hit.partial_output
                        await wait_for_reset(hit.resets_at, ul, lambda m, s: display.status(label(m), s), options.signal)
                        if hit.session_id and storage is not None:
                            attempt_resume, attempt_fork, attempt_prompt = hit.session_id, False, ul.continue_prompt
                        display.status(label("Agent restarted after usage-limit reset"), "success")
                    finally:
                        buf.dispose()

                agent_output = prefix_output + res.result
                display.status(label("Agent stopped"), "info")

                session_file_path = None
                usage = res.usage
                sid = res.session_id
                handle = info.bind_mount_handle
                if provider.capture_sessions and storage is not None and sid and handle is not None:
                    display.status(label("Capturing session"), "info")
                    try:
                        await storage.capture_to_host(
                            host_cwd=options.host_repo_dir, sandbox_cwd=ctx.sandbox_repo_dir, session_id=sid, handle=handle
                        )
                    except Exception as e:  # noqa: BLE001
                        raise SessionCaptureError(f"Session capture failed: {e}", session_id=sid) from e
                    session_file_path = storage.host_session_file_path(options.host_repo_dir, sid)
                    try:
                        content = await storage.read_host_session(options.host_repo_dir, sid)
                    except Exception:  # noqa: BLE001
                        content = None
                    if content:
                        pu = provider.parse_session_usage(content)
                        if pu:
                            usage = pu
                        pcw = provider.parse_session_context_window(content)
                        if pcw:
                            cw[0] = pcw

                matched = next((s for s in signals if s in agent_output), None)
                return {
                    "completion_signal": matched,
                    "stdout": agent_output,
                    "session_id": sid,
                    "session_file_path": session_file_path,
                    "usage": usage,
                    "context_window": cw[0],
                    "usage_limit_waits": waits,
                }

            return await with_sandbox_lifecycle(
                SandboxLifecycleOptions(
                    host_repo_dir=options.host_repo_dir,
                    sandbox_repo_dir=info.sandbox_repo_path,
                    hooks=options.hooks,
                    branch=options.branch,
                    host_worktree_path=info.host_worktree_path,
                    apply_to_host=info.apply_to_host,
                    signal=options.signal,
                    timeouts=options.timeouts,
                    keep_source_branch=options.keep_source_branch,
                ),
                sandbox,
                work,
                display,
            )

        try:
            sandbox_result = await factory.with_sandbox(make)
        except BaseException:
            for line in build_context_window_lines(all_iterations):
                display.text(line)
            raise

        lr = sandbox_result.value
        preserved = sandbox_result.preserved_worktree_path
        all_commits.extend(lr.commits)
        all_stdout += lr.result["stdout"]
        resolved_branch = lr.branch
        all_iterations.append(
            IterationResult(
                session_id=lr.result["session_id"],
                session_file_path=lr.result["session_file_path"],
                usage=lr.result["usage"],
                context_window=lr.result["context_window"],
                usage_limit_waits=lr.result["usage_limit_waits"],
            )
        )
        if lr.result["completion_signal"] is not None:
            display.status(label(f"Agent signaled completion after {i} iteration(s)."), "success")
            return OrchestrateResult(
                all_iterations, lr.result["completion_signal"], all_stdout, all_commits, resolved_branch, preserved
            )

    display.status(label(f"Reached max iterations ({options.iterations})."), "info")
    return OrchestrateResult(all_iterations, None, all_stdout, all_commits, resolved_branch, preserved)

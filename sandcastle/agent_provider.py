"""Agent providers. Port of ``AgentProvider.ts``.

An agent provider knows how to start one kind of agent CLI (Claude Code,
Codex, pi, Cursor, OpenCode, Copilot), how to read what it prints
(``parse_stream_line``), and where it keeps its sessions
(``session_storage``) so they can be captured, resumed and forked.
"""

from __future__ import annotations

import json
import os
import posixpath
import random
import string
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional, Protocol, Sequence, Union

from .context_window import ContextWindow
from .errors import ForkNotSupportedError
from .session_store import (
    HostSessionLookup,
    SessionFileLayout,
    SessionFileStorage,
    claude_host_session_path,
    claude_sandbox_session_path,
    claude_subagents_dir_on_host,
    copilot_host_sessions_dir,
    copilot_session_path,
    cursor_chat_dir_name,
    cursor_host_chats_dir,
    encode_pi_session_dir,
    find_claude_session_on_host,
    find_codex_session_on_host,
    find_pi_session_on_host,
    home_dir,
    json_dumps,
    list_claude_subagent_sessions_in_sandbox,
    locate_sandbox_session_file,
    opencode_host_database,
    opencode_host_sessions_dir,
    path_exists,
    pi_session_dir_path,
    require_host_session_path,
    sandbox_dirs_of,
    transfer_claude_session,
    transfer_codex_session,
    transfer_copilot_session,
    transfer_pi_session,
)

# ---------------------------------------------------------------------------
# Stream events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IterationUsage:
    """Per-iteration token usage snapshot."""

    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class TextEvent:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class ContextWindowEvent:
    context_window: ContextWindow
    type: Literal["context_window"] = "context_window"


@dataclass(frozen=True)
class ResultEvent:
    result: str
    type: Literal["result"] = "result"


@dataclass(frozen=True)
class ToolCallEvent:
    name: str
    args: str
    type: Literal["tool_call"] = "tool_call"


@dataclass(frozen=True)
class SessionIdEvent:
    session_id: str
    type: Literal["session_id"] = "session_id"


@dataclass(frozen=True)
class UsageEvent:
    usage: IterationUsage
    type: Literal["usage"] = "usage"


@dataclass(frozen=True)
class ErrorEvent:
    """The agent reported a failure in its own stream. Some agents exit 0
    after one, so the orchestrator fails the run on it whatever the exit code."""

    message: str
    type: Literal["error"] = "error"


ParsedStreamEvent = Union[
    TextEvent, ContextWindowEvent, ResultEvent, ToolCallEvent, SessionIdEvent, UsageEvent, ErrorEvent
]


def shell_escape(s: str) -> str:
    """Single-quote a string for safe use in a POSIX shell command."""
    return "'" + s.replace("'", "'\\''") + "'"


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _loads_obj(line: str) -> Any:
    if not line.startswith("{"):
        return None
    try:
        return json.loads(line)
    except ValueError:
        return None


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _context_window_event(input_tokens: Any, *cache_buckets: Any) -> List[ParsedStreamEvent]:
    """A context_window event from ONE model call's input-side buckets, or
    nothing when the input is missing or zero."""
    if not _is_num(input_tokens):
        return []
    tokens = input_tokens
    for n in cache_buckets:
        if _is_num(n):
            tokens += n
    return [ContextWindowEvent(ContextWindow(tokens=int(tokens)))] if tokens > 0 else []


def _parse_claude_context_window(line: str) -> List[ParsedStreamEvent]:
    obj = _loads_obj(line)
    if not isinstance(obj, dict) or obj.get("type") != "assistant" or obj.get("parent_tool_use_id"):
        return []
    u = _get(obj, "message", "usage")
    return _context_window_event(
        _get(u, "input_tokens"), _get(u, "cache_creation_input_tokens"), _get(u, "cache_read_input_tokens")
    )


#: Allowlisted tool names -> the input field containing the display arg.
TOOL_ARG_FIELDS: Dict[str, str] = {
    "Bash": "command",
    "WebSearch": "query",
    "WebFetch": "url",
    "Agent": "description",
    "Skill": "skill",
}


def _message_field(value: Any) -> Optional[str]:
    if isinstance(value, dict) and isinstance(value.get("message"), str):
        return value["message"]
    return None


def extract_error_message(obj: Any) -> Optional[str]:
    """Handles ``{error: "s"}``, ``{error: {message}}``, ``{error: {data: {message}}}``
    and ``{message}``."""
    if not isinstance(obj, dict):
        return None
    err = obj.get("error")
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        nested = _message_field(err) or _message_field(err.get("data"))
        if nested is not None:
            return nested
    return _message_field(obj)


def _tool_call_from_claude_block(block: Any) -> Optional[ParsedStreamEvent]:
    if not isinstance(block, dict) or block.get("type") != "tool_use":
        return None
    name = block.get("name")
    inp = block.get("input")
    if not isinstance(name, str) or inp is None:
        return None
    arg_field = TOOL_ARG_FIELDS.get(name)
    if arg_field is None or not isinstance(inp, dict):
        return None
    value = inp.get(arg_field)
    if not isinstance(value, str):
        return None
    return ToolCallEvent(name=name, args=value)


def _merge_adjacent_text(events: Sequence[ParsedStreamEvent]) -> List[ParsedStreamEvent]:
    merged: List[ParsedStreamEvent] = []
    for ev in events:
        if isinstance(ev, TextEvent) and merged and isinstance(merged[-1], TextEvent):
            merged[-1] = TextEvent(merged[-1].text + ev.text)
        else:
            merged.append(ev)
    return merged


def _parse_claude_assistant_content(content: Sequence[Any]) -> List[ParsedStreamEvent]:
    events: List[ParsedStreamEvent] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            events.append(TextEvent(block["text"]))
            continue
        tc = _tool_call_from_claude_block(block)
        if tc is not None:
            events.append(tc)
    return _merge_adjacent_text(events)


def parse_stream_json_line(line: str) -> List[ParsedStreamEvent]:
    """Claude Code ``--output-format stream-json`` line parser."""
    obj = _loads_obj(line)
    if not isinstance(obj, dict):
        return []
    content = _get(obj, "message", "content")
    if obj.get("type") == "assistant" and isinstance(content, list):
        return _parse_claude_assistant_content(content)
    if obj.get("type") == "result" and isinstance(obj.get("result"), str):
        if obj.get("is_error") is True:
            return [ErrorEvent(obj["result"])]
        return [ResultEvent(obj["result"])]
    if obj.get("type") == "system" and obj.get("subtype") == "init" and isinstance(obj.get("session_id"), str):
        return [SessionIdEvent(obj["session_id"])]
    return []


# ---------------------------------------------------------------------------
# Cursor stream parsing
# ---------------------------------------------------------------------------

CURSOR_PRINT_PROMPT_MAX_BYTES = 120 * 1024
COPILOT_PRINT_PROMPT_MAX_BYTES = 120 * 1024


def _assert_cursor_print_prompt_fits_argv(prompt: str) -> None:
    n = len(prompt.encode("utf-8"))
    if n > CURSOR_PRINT_PROMPT_MAX_BYTES:
        raise ValueError(
            f"Cursor print-mode prompt is {n} bytes (max {CURSOR_PRINT_PROMPT_MAX_BYTES} bytes). "
            "The Cursor CLI accepts the prompt only as a command-line argument; shorten the prompt "
            "or split the work. Other Sandcastle providers use stdin for large prompts."
        )


def _assert_copilot_print_prompt_fits_argv(prompt: str) -> None:
    n = len(prompt.encode("utf-8"))
    if n > COPILOT_PRINT_PROMPT_MAX_BYTES:
        raise ValueError(
            f"Copilot print-mode prompt is {n} bytes (max {COPILOT_PRINT_PROMPT_MAX_BYTES} bytes). "
            "This provider passes the prompt as a command-line argument; shorten the prompt or split "
            "the work. Other Sandcastle providers use stdin for large prompts."
        )


_CURSOR_TYPED_TOOL_CALLS = (
    ("readToolCall", "Read", "path"),
    ("writeToolCall", "Write", "path"),
    ("editToolCall", "Edit", "path"),
    ("shellToolCall", "Bash", "command"),
)


def _cursor_tool_call_arg(tool_call: Any, fld: str) -> Optional[str]:
    args = _get(tool_call, "args")
    if not isinstance(args, dict):
        return None
    value = args.get(fld)
    return value if isinstance(value, str) else None


def _parse_cursor_function_tool_call(fn: Any) -> List[ParsedStreamEvent]:
    if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
        return []
    name = fn["name"]
    raw = fn.get("arguments")
    raw_args = raw if isinstance(raw, str) else ""
    if raw_args == "":
        return [ToolCallEvent(name=name, args="")]
    try:
        parsed = json.loads(raw_args)
        command = parsed.get("command") if isinstance(parsed, dict) else None
        if isinstance(command, str):
            return [ToolCallEvent(name="Bash", args=command)]
    except ValueError:
        pass
    return [ToolCallEvent(name=name, args=raw_args)]


def _parse_cursor_tool_call_started(obj: dict) -> List[ParsedStreamEvent]:
    if obj.get("type") != "tool_call" or obj.get("subtype") != "started":
        return []
    tool_call = obj.get("tool_call")
    if not isinstance(tool_call, dict):
        return []
    for kind, name, fld in _CURSOR_TYPED_TOOL_CALLS:
        args = _cursor_tool_call_arg(tool_call.get(kind), fld)
        if args is not None:
            return [ToolCallEvent(name=name, args=args)]
    return _parse_cursor_function_tool_call(tool_call.get("function"))


def parse_cursor_stream_line(line: str) -> List[ParsedStreamEvent]:
    """Cursor emits no context_window events on purpose (no per-call figure)."""
    obj = _loads_obj(line)
    if not isinstance(obj, dict):
        return []
    if obj.get("type") == "tool_call":
        return _parse_cursor_tool_call_started(obj)
    return parse_stream_json_line(line)


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentCommandOptions:
    """Options passed to ``build_print_command`` and ``build_interactive_args``."""

    prompt: str
    dangerously_skip_permissions: bool
    #: When set, resume this session id instead of starting fresh.
    resume_session: Optional[str] = None
    #: With ``resume_session``: fork the session instead of mutating it.
    fork_session: bool = False


@dataclass(frozen=True)
class PrintCommand:
    """Command string plus optional stdin. When ``stdin`` is set the sandbox
    pipes it to the child instead of inlining the prompt in argv."""

    command: str
    stdin: Optional[str] = None


class AgentSessionStorage(Protocol):
    can_fork: bool

    async def capture_to_host(self, *, host_cwd: str, sandbox_cwd: str, session_id: str, handle: Any) -> None: ...

    async def resume_into_sandbox(self, *, host_cwd: str, sandbox_cwd: str, session_id: str, handle: Any) -> None: ...

    async def read_host_session(self, cwd: str, session_id: str) -> Optional[str]: ...

    async def exists_on_host(self, cwd: str, session_id: str) -> bool: ...

    def host_session_file_path(self, cwd: str, session_id: str) -> Optional[str]: ...

    async def find_by_id_on_host(self, session_id: str) -> HostSessionLookup: ...


class AgentProvider:
    """Base class for agent providers.

    Subclass it (or duck-type it) to plug in your own agent: implement
    ``build_print_command`` and ``parse_stream_line``; optionally
    ``build_interactive_args``, ``parse_session_usage``,
    ``parse_session_context_window`` and a ``session_storage``.
    """

    name: str = "agent"
    env: Dict[str, str]
    capture_sessions: bool = False
    session_storage: Optional[Any] = None

    def __init__(self, env: Optional[Dict[str, str]] = None, capture_sessions: bool = False) -> None:
        self.env = dict(env or {})
        self.capture_sessions = capture_sessions

    def build_print_command(self, options: AgentCommandOptions) -> PrintCommand:  # pragma: no cover
        raise NotImplementedError

    def build_interactive_args(self, options: AgentCommandOptions) -> List[str]:  # pragma: no cover
        raise NotImplementedError

    def parse_stream_line(self, line: str) -> List[ParsedStreamEvent]:  # pragma: no cover
        return []

    def parse_session_usage(self, content: str) -> Optional[IterationUsage]:
        return None

    def parse_session_context_window(self, content: str) -> Optional[ContextWindow]:
        return None

    @property
    def supports_interactive(self) -> bool:
        return type(self).build_interactive_args is not AgentProvider.build_interactive_args


DEFAULT_MODEL = "claude-opus-4-8"

# ---------------------------------------------------------------------------
# Session file helpers
# ---------------------------------------------------------------------------


def _tmp_path(tag: str) -> str:
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return os.path.join(tempfile.gettempdir(), f"sandcastle-{tag}-{int(time.time() * 1000)}-{rand}.jsonl")


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


async def read_sandbox_file(handle: Any, sandbox_path: str, tag: str) -> str:
    tmp = _tmp_path(tag)
    await handle.copy_file_out(sandbox_path, tmp)
    try:
        return _read_text(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


async def write_sandbox_file(handle: Any, sandbox_path: str, content: str, tag: str) -> None:
    tmp = _tmp_path(tag)
    _write_text(tmp, content)
    try:
        await handle.exec(f"mkdir -p {json_dumps(posixpath.dirname(sandbox_path))}")
        await handle.copy_file_in(tmp, sandbox_path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


async def _copy_claude_session_file(
    handle: Any, source_path: str, from_cwd: str, to_cwd: str, dest_path: str, tag: str
) -> None:
    jsonl = await read_sandbox_file(handle, source_path, tag)
    _write_text(dest_path, transfer_claude_session(jsonl, from_cwd, to_cwd))


class ClaudeSessionStorage:
    can_fork = True

    def __init__(self, host_projects_dir: Optional[str], sandbox_projects_dir: Optional[str]) -> None:
        self.host_projects_dir = host_projects_dir
        self._sandbox_projects_dir = sandbox_projects_dir

    def _sandbox_projects_dir_of(self, handle: Any) -> str:
        return self._sandbox_projects_dir or posixpath.join(sandbox_dirs_of(handle).home, ".claude", "projects")

    def host_session_file_path(self, cwd: str, id: str) -> Optional[str]:
        return claude_host_session_path(cwd, id, self.host_projects_dir)

    async def exists_on_host(self, cwd: str, id: str) -> bool:
        return path_exists(claude_host_session_path(cwd, id, self.host_projects_dir))

    async def read_host_session(self, cwd: str, id: str) -> Optional[str]:
        p = claude_host_session_path(cwd, id, self.host_projects_dir)
        return _read_text(p) if path_exists(p) else None

    async def capture_to_host(self, *, host_cwd: str, sandbox_cwd: str, session_id: str, handle: Any) -> None:
        sandbox_projects = self._sandbox_projects_dir_of(handle)
        # Main session: failure is fatal; the user expects their session.
        await _copy_claude_session_file(
            handle,
            claude_sandbox_session_path(sandbox_cwd, session_id, sandbox_projects),
            sandbox_cwd,
            host_cwd,
            claude_host_session_path(host_cwd, session_id, self.host_projects_dir),
            "claude-cap",
        )
        # Subagent / workflow transcripts: best-effort.
        sub_paths = await list_claude_subagent_sessions_in_sandbox(sandbox_cwd, session_id, handle, sandbox_projects)
        host_sub_dir = claude_subagents_dir_on_host(host_cwd, session_id, self.host_projects_dir)
        for sub in sub_paths:
            try:
                await _copy_claude_session_file(
                    handle, sub, sandbox_cwd, host_cwd, os.path.join(host_sub_dir, posixpath.basename(sub)), "claude-sub"
                )
            except Exception as error:  # noqa: BLE001
                print(f"sandcastle: failed to capture Claude subagent transcript {sub}: {error}", file=sys.stderr)

    async def resume_into_sandbox(self, *, host_cwd: str, sandbox_cwd: str, session_id: str, handle: Any) -> None:
        jsonl = _read_text(claude_host_session_path(host_cwd, session_id, self.host_projects_dir))
        await write_sandbox_file(
            handle,
            claude_sandbox_session_path(sandbox_cwd, session_id, self._sandbox_projects_dir_of(handle)),
            transfer_claude_session(jsonl, host_cwd, sandbox_cwd),
            "claude-res",
        )

    async def find_by_id_on_host(self, id: str) -> HostSessionLookup:
        return find_claude_session_on_host(id, self.host_projects_dir)


class CodexSessionStorage:
    """Codex sessions live at ``YYYY/MM/DD/rollout-*-<id>.jsonl``: not
    derivable from (cwd, id), so captured paths are cached."""

    can_fork = True

    def __init__(self, host_sessions_dir: Optional[str], sandbox_sessions_dir: Optional[str]) -> None:
        self.host_sessions_dir = host_sessions_dir
        self._sandbox_sessions_dir = sandbox_sessions_dir
        self._captured: Dict[str, str] = {}

    def _sandbox_dir_of(self, handle: Any) -> str:
        return self._sandbox_sessions_dir or posixpath.join(sandbox_dirs_of(handle).home, ".codex", "sessions")

    def host_session_file_path(self, cwd: str, id: str) -> Optional[str]:
        return self._captured.get(id)

    async def exists_on_host(self, cwd: str, id: str) -> bool:
        return find_codex_session_on_host(id, self.host_sessions_dir).path is not None

    async def read_host_session(self, cwd: str, id: str) -> Optional[str]:
        found = find_codex_session_on_host(id, self.host_sessions_dir)
        return _read_text(found.path) if found.path else None

    async def capture_to_host(self, *, host_cwd: str, sandbox_cwd: str, session_id: str, handle: Any) -> None:
        located = await locate_sandbox_session_file(handle, self._sandbox_dir_of(handle), f"rollout-*-{session_id}.jsonl")
        jsonl = await read_sandbox_file(handle, located.path, "codex-cap")
        root = self.host_sessions_dir or os.path.join(home_dir(), ".codex", "sessions")
        target = os.path.join(root, located.relative_path)
        _write_text(target, transfer_codex_session(jsonl, sandbox_cwd, host_cwd))
        self._captured[session_id] = target

    async def resume_into_sandbox(self, *, host_cwd: str, sandbox_cwd: str, session_id: str, handle: Any) -> None:
        found = find_codex_session_on_host(session_id, self.host_sessions_dir)
        path = require_host_session_path(found, session_id)
        rel = os.path.relpath(path, found.searched_root).replace("\\", "/").split("/")
        await write_sandbox_file(
            handle,
            posixpath.join(self._sandbox_dir_of(handle), *rel),
            transfer_codex_session(_read_text(path), host_cwd, sandbox_cwd),
            "codex-res",
        )

    async def find_by_id_on_host(self, id: str) -> HostSessionLookup:
        return find_codex_session_on_host(id, self.host_sessions_dir)


class PiSessionStorage:
    """Pi names its JSONL ``<timestamp>_<id>.jsonl``; captured paths are cached.
    Captured files land in the *host* cwd's encoded dir so ``--session <id>``
    doesn't hit pi's interactive "fork session?" prompt."""

    can_fork = True

    def __init__(self, host_sessions_dir: Optional[str], sandbox_sessions_dir: Optional[str]) -> None:
        self.host_sessions_dir = host_sessions_dir
        self._sandbox_sessions_dir = sandbox_sessions_dir
        self._captured: Dict[str, str] = {}

    def _sandbox_dir_of(self, handle: Any) -> str:
        return self._sandbox_sessions_dir or posixpath.join(sandbox_dirs_of(handle).home, ".pi", "agent", "sessions")

    def host_session_file_path(self, cwd: str, id: str) -> Optional[str]:
        return self._captured.get(id) or pi_session_dir_path(cwd, self.host_sessions_dir)

    async def exists_on_host(self, cwd: str, id: str) -> bool:
        return find_pi_session_on_host(id, self.host_sessions_dir).path is not None

    async def read_host_session(self, cwd: str, id: str) -> Optional[str]:
        p = find_pi_session_on_host(id, self.host_sessions_dir).path
        return None if p is None else _read_text(p)

    async def capture_to_host(self, *, host_cwd: str, sandbox_cwd: str, session_id: str, handle: Any) -> None:
        located = await locate_sandbox_session_file(handle, self._sandbox_dir_of(handle), f"*_{session_id}.jsonl")
        jsonl = await read_sandbox_file(handle, located.path, "pi-cap")
        target = os.path.join(pi_session_dir_path(host_cwd, self.host_sessions_dir), posixpath.basename(located.path))
        _write_text(target, transfer_pi_session(jsonl, sandbox_cwd, host_cwd))
        self._captured[session_id] = target

    async def resume_into_sandbox(self, *, host_cwd: str, sandbox_cwd: str, session_id: str, handle: Any) -> None:
        found = find_pi_session_on_host(session_id, self.host_sessions_dir)
        path = require_host_session_path(found, session_id)
        filename = posixpath.basename(path.replace("\\", "/"))
        await write_sandbox_file(
            handle,
            posixpath.join(self._sandbox_dir_of(handle), encode_pi_session_dir(sandbox_cwd), filename),
            transfer_pi_session(_read_text(path), host_cwd, sandbox_cwd),
            "pi-res",
        )

    async def find_by_id_on_host(self, id: str) -> HostSessionLookup:
        return find_pi_session_on_host(id, self.host_sessions_dir)


# ---------------------------------------------------------------------------
# Pi
# ---------------------------------------------------------------------------


def _last_assistant_message(messages: Sequence[Any]) -> Optional[dict]:
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            return msg
    return None


def _parse_pi_agent_end(messages: Sequence[Any]) -> List[ParsedStreamEvent]:
    msg = _last_assistant_message(messages)
    if msg is None:
        return []
    if msg.get("stopReason") == "error":
        em = msg.get("errorMessage")
        return [ErrorEvent(em if isinstance(em, str) and em else 'pi stopped with stopReason "error"')]
    texts = [
        b["text"]
        for b in (msg.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    ]
    return [ResultEvent("".join(texts))] if texts else []


def _parse_pi_usage(u: Any) -> Optional[IterationUsage]:
    if not isinstance(u, dict):
        return None
    vals = [u.get(k) for k in ("input", "output", "cacheRead", "cacheWrite")]
    if not all(_is_num(v) for v in vals):
        return None
    inp, out, cr, cw = vals
    return IterationUsage(
        input_tokens=inp, cache_creation_input_tokens=cw, cache_read_input_tokens=cr, output_tokens=out
    )


def parse_pi_stream_line(line: str) -> List[ParsedStreamEvent]:
    obj = _loads_obj(line)
    if not isinstance(obj, dict):
        return []
    t = obj.get("type")
    if t == "session" and isinstance(obj.get("id"), str):
        return [SessionIdEvent(obj["id"])]
    if t == "message_end" and _get(obj, "message", "role") == "assistant":
        u = _get(obj, "message", "usage")
        usage = _parse_pi_usage(u)
        events = _context_window_event(_get(u, "input"), _get(u, "cacheRead"), _get(u, "cacheWrite"))
        if usage:
            events.append(UsageEvent(usage))
        return events
    if t == "message_update" and obj.get("assistantMessageEvent"):
        evt = obj["assistantMessageEvent"]
        if isinstance(evt, dict) and evt.get("type") == "text_delta" and isinstance(evt.get("delta"), str):
            return [TextEvent(evt["delta"])]
        return []
    if t == "tool_execution_start":
        raw = obj.get("toolName")
        if not isinstance(raw, str):
            return []
        tool_name = next((n for n in TOOL_ARG_FIELDS if n.lower() == raw.lower()), None)
        if tool_name is None:
            return []
        args = obj.get("args")
        if not isinstance(args, dict):
            return []
        value = args.get(TOOL_ARG_FIELDS[tool_name])
        return [ToolCallEvent(name=tool_name, args=value)] if isinstance(value, str) else []
    if t in ("agent_error", "error"):
        msg = extract_error_message(obj)
        return [ErrorEvent(msg)] if msg else []
    if t == "agent_end" and isinstance(obj.get("messages"), list):
        return _parse_pi_agent_end(obj["messages"])
    return []


ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class PiAgent(AgentProvider):
    name = "pi"

    def __init__(
        self,
        model: str,
        thinking: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        capture_sessions: bool = True,
        session_storage: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(env, capture_sessions)
        self.model = model
        self.thinking = thinking
        ss = session_storage or {}
        self.session_storage = PiSessionStorage(ss.get("host_sessions_dir"), ss.get("sandbox_sessions_dir"))

    def build_print_command(self, o: AgentCommandOptions) -> PrintCommand:
        thinking_flag = f" --thinking {self.thinking}" if self.thinking else ""
        # `--fork <id>` copies the session to a new file under a fresh id; pi
        # rejects `--fork` combined with `--session`.
        session_flag = (
            f" --{'fork' if o.fork_session else 'session'} {shell_escape(o.resume_session)}" if o.resume_session else ""
        )
        return PrintCommand(
            command=f"pi -p --mode json --model {shell_escape(self.model)}{thinking_flag}{session_flag}",
            stdin=o.prompt,
        )

    def build_interactive_args(self, o: AgentCommandOptions) -> List[str]:
        args = ["pi", "--model", self.model]
        if o.prompt:
            args.append(o.prompt)
        return args

    def parse_stream_line(self, line: str) -> List[ParsedStreamEvent]:
        return parse_pi_stream_line(line)


def pi(
    model: str,
    *,
    thinking: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    capture_sessions: bool = True,
    session_storage: Optional[Dict[str, str]] = None,
) -> PiAgent:
    """The pi coding agent. ``thinking`` maps to ``--thinking``."""
    return PiAgent(model, thinking, env, capture_sessions, session_storage)


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def _parse_codex_usage(usage: Any) -> Optional[IterationUsage]:
    if not isinstance(usage, dict):
        return None
    it, ct, ot = usage.get("input_tokens"), usage.get("cached_input_tokens"), usage.get("output_tokens")
    if not (_is_num(it) and _is_num(ct) and _is_num(ot)):
        return None
    return IterationUsage(
        input_tokens=it - ct, cache_creation_input_tokens=0, cache_read_input_tokens=ct, output_tokens=ot
    )


def parse_codex_session_context_window(content: str) -> Optional[ContextWindow]:
    for line in reversed(content.split("\n")):
        obj = _loads_obj(line)
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "event_msg" or _get(obj, "payload", "type") != "token_count":
            continue
        info = _get(obj, "payload", "info")
        tokens = _get(info, "last_token_usage", "input_tokens")
        if not _is_num(tokens) or tokens <= 0:
            continue
        limit = _get(info, "model_context_window")
        return ContextWindow(int(tokens), int(limit)) if _is_num(limit) and limit > 0 else ContextWindow(int(tokens))
    return None


def parse_codex_stream_line(line: str) -> List[ParsedStreamEvent]:
    obj = _loads_obj(line)
    if not isinstance(obj, dict):
        return []
    t = obj.get("type")
    item = obj.get("item")
    if t == "thread.started" and isinstance(obj.get("thread_id"), str):
        return [SessionIdEvent(obj["thread_id"])]
    if t == "item.completed" and _get(item, "type") == "agent_message" and isinstance(_get(item, "text"), str):
        return [TextEvent(item["text"]), ResultEvent(item["text"])]
    if t == "item.started" and _get(item, "type") == "command_execution" and isinstance(_get(item, "command"), str):
        return [ToolCallEvent(name="Bash", args=item["command"])]
    if t == "error":
        msg = extract_error_message(obj)
        return [ResultEvent(msg)] if msg else []
    if t == "turn.failed":
        return [ErrorEvent(extract_error_message(obj) or "codex turn failed")]
    if t == "turn.completed":
        usage = _parse_codex_usage(obj.get("usage"))
        return [UsageEvent(usage)] if usage else []
    return []


class CodexAgent(AgentProvider):
    name = "codex"

    def __init__(
        self,
        model: str,
        effort: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        capture_sessions: bool = True,
        session_storage: Optional[Dict[str, str]] = None,
        approvals_reviewer: Optional[str] = None,
    ) -> None:
        super().__init__(env, capture_sessions)
        self.model = model
        self.effort = effort
        self.approvals_reviewer = approvals_reviewer
        ss = session_storage or {}
        self.session_storage = CodexSessionStorage(ss.get("host_sessions_dir"), ss.get("sandbox_sessions_dir"))

    def build_print_command(self, o: AgentCommandOptions) -> PrintCommand:
        effort_value = 'model_reasoning_effort="' + str(self.effort) + '"'
        effort_flag = f" -c {shell_escape(effort_value)}" if self.effort else ""
        if self.approvals_reviewer == "auto_review":
            reviewer_value = 'approvals_reviewer="auto_review"'
            approvals = f" -a on-request -s danger-full-access -c {shell_escape(reviewer_value)}"
        else:
            approvals = " --dangerously-bypass-approvals-and-sandbox"
        if o.resume_session and o.fork_session:
            base = f"codex exec fork {shell_escape(o.resume_session)}"
        elif o.resume_session:
            base = f"codex exec resume {shell_escape(o.resume_session)}"
        else:
            base = "codex exec"
        stdin_arg = " -" if o.resume_session else ""
        return PrintCommand(
            command=f"{base} --json{approvals} -m {shell_escape(self.model)}{effort_flag}{stdin_arg}",
            stdin=o.prompt,
        )

    def build_interactive_args(self, o: AgentCommandOptions) -> List[str]:
        args = ["codex", "--model", self.model]
        if o.prompt:
            args.append(o.prompt)
        return args

    def parse_stream_line(self, line: str) -> List[ParsedStreamEvent]:
        return parse_codex_stream_line(line)

    def parse_session_context_window(self, content: str) -> Optional[ContextWindow]:
        return parse_codex_session_context_window(content)


def codex(
    model: str,
    *,
    effort: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    capture_sessions: bool = True,
    session_storage: Optional[Dict[str, str]] = None,
    approvals_reviewer: Optional[str] = None,
) -> CodexAgent:
    """OpenAI Codex CLI. ``approvals_reviewer="auto_review"`` swaps the
    approval bypass for ``-a on-request -s danger-full-access``."""
    return CodexAgent(model, effort, env, capture_sessions, session_storage, approvals_reviewer)


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


class CursorAgent(AgentProvider):
    name = "cursor"

    def __init__(
        self,
        model: str,
        env: Optional[Dict[str, str]] = None,
        capture_sessions: bool = True,
        session_storage: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(env, capture_sessions)
        self.model = model
        ss = session_storage or {}
        host_dir = ss.get("host_chats_dir") or cursor_host_chats_dir()
        sandbox_override = ss.get("sandbox_chats_dir")
        self.session_storage = SessionFileStorage(
            SessionFileLayout(
                host_root=host_dir,
                host_path=lambda cwd, id: os.path.join(host_dir, cursor_chat_dir_name(cwd), id, "store.db"),
                host_paths_by_id=lambda id, chat_dirs: [os.path.join(host_dir, d, id, "store.db") for d in chat_dirs],
                sandbox_path=lambda cwd, id, dirs: posixpath.join(
                    sandbox_override or posixpath.join(dirs.home, ".cursor", "chats"),
                    cursor_chat_dir_name(cwd),
                    id,
                    "store.db",
                ),
            ),
            can_fork=False,
        )

    def build_print_command(self, o: AgentCommandOptions) -> PrintCommand:
        if o.resume_session is not None and o.fork_session:
            raise ForkNotSupportedError(
                "cursor cannot fork a session: its CLI resumes but has no fork. Use resume() to continue the session in place.",
                agent_provider="cursor",
            )
        _assert_cursor_print_prompt_fits_argv(o.prompt)
        force = " --force" if o.dangerously_skip_permissions else ""
        resume = "" if o.resume_session is None else f" --resume={shell_escape(o.resume_session)}"
        return PrintCommand(
            command=f"agent --print --output-format stream-json --model {shell_escape(self.model)} {force}{resume} {shell_escape(o.prompt)}"
        )

    def build_interactive_args(self, o: AgentCommandOptions) -> List[str]:
        args = ["agent", "--model", self.model]
        if o.dangerously_skip_permissions:
            args.append("--force")
        if o.prompt:
            args.append(o.prompt)
        return args

    def parse_stream_line(self, line: str) -> List[ParsedStreamEvent]:
        return parse_cursor_stream_line(line)


def cursor(
    model: str,
    *,
    env: Optional[Dict[str, str]] = None,
    capture_sessions: bool = True,
    session_storage: Optional[Dict[str, str]] = None,
) -> CursorAgent:
    """Cursor Agent CLI (``agent``). Resumes, but cannot fork."""
    return CursorAgent(model, env, capture_sessions, session_storage)


# ---------------------------------------------------------------------------
# OpenCode
# ---------------------------------------------------------------------------

OPENCODE_TOOL_ARG_FIELDS = {"bash": "command", "webfetch": "url", "task": "description"}


def parse_opencode_stream_line(line: str) -> List[ParsedStreamEvent]:
    obj = _loads_obj(line)
    if not isinstance(obj, dict):
        return []
    t = obj.get("type")
    part = obj.get("part")
    if t == "step_start" and isinstance(obj.get("sessionID"), str):
        return [SessionIdEvent(obj["sessionID"])]
    if t == "step_finish" and isinstance(part, dict) and part.get("tokens"):
        tk = part["tokens"]
        return _context_window_event(_get(tk, "input"), _get(tk, "cache", "read"), _get(tk, "cache", "write"))
    if t == "text" and _get(part, "type") == "text" and isinstance(_get(part, "text"), str):
        return [TextEvent(part["text"]), ResultEvent(part["text"])]
    if t == "tool_use" and _get(part, "type") == "tool":
        tool = part.get("tool")
        if not isinstance(tool, str):
            return []
        state = part.get("state")
        if _get(state, "status") != "completed":
            return []
        inp = state.get("input")
        if not inp:
            return []
        arg_field = OPENCODE_TOOL_ARG_FIELDS.get(tool)
        value = inp.get(arg_field) if arg_field and isinstance(inp, dict) else None
        return [ToolCallEvent(name=tool, args=value if isinstance(value, str) else json_dumps(inp))]
    if t == "error":
        msg = extract_error_message(obj)
        return [ErrorEvent(msg)] if msg else []
    return []


class OpenCodeAgent(AgentProvider):
    name = "opencode"

    def __init__(
        self,
        model: str,
        variant: Optional[str] = None,
        agent: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        capture_sessions: bool = True,
        session_storage: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(env, capture_sessions)
        self.model = model
        self.variant = variant
        self.agent = agent
        ss = session_storage or {}
        host_dir = ss.get("host_sessions_dir") or opencode_host_sessions_dir()
        export_override = ss.get("sandbox_export_dir")
        host_path = lambda id: os.path.join(host_dir, f"{id}.json")  # noqa: E731
        self.session_storage = SessionFileStorage(
            SessionFileLayout(
                host_root=host_dir,
                host_path=lambda _cwd, id: host_path(id),
                host_paths_by_id=lambda id, _subdirs: [host_path(id), opencode_host_database()],
                sandbox_path=lambda _cwd, id, dirs: posixpath.join(export_override or dirs.tmp, f"{id}.json"),
                sandbox_export=lambda id, p: f"opencode export {shell_escape(id)} > {shell_escape(p)}",
                sandbox_import=lambda _id, p: f"opencode import {shell_escape(p)}",
            )
        )

    def build_print_command(self, o: AgentCommandOptions) -> PrintCommand:
        variant = f" --variant {shell_escape(self.variant)}" if self.variant else ""
        agent = f" --agent {shell_escape(self.agent)}" if self.agent else ""
        perms = " --dangerously-skip-permissions" if o.dangerously_skip_permissions else ""
        session = (
            ""
            if o.resume_session is None
            else f" --session {shell_escape(o.resume_session)}{' --fork' if o.fork_session else ''}"
        )
        return PrintCommand(
            command=f"opencode run --format json --model {shell_escape(self.model)}{variant}{agent}{perms}{session} {shell_escape(o.prompt)}"
        )

    def build_interactive_args(self, o: AgentCommandOptions) -> List[str]:
        args = ["opencode", "--model", self.model]
        if self.agent:
            args += ["--agent", self.agent]
        if o.prompt:
            args += ["--prompt", o.prompt]
        return args

    def parse_stream_line(self, line: str) -> List[ParsedStreamEvent]:
        return parse_opencode_stream_line(line)


def opencode(
    model: str,
    *,
    variant: Optional[str] = None,
    agent: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    capture_sessions: bool = True,
    session_storage: Optional[Dict[str, str]] = None,
) -> OpenCodeAgent:
    """OpenCode. ``agent`` picks a named OpenCode agent/mode (``--agent``)."""
    return OpenCodeAgent(model, variant, agent, env, capture_sessions, session_storage)


# ---------------------------------------------------------------------------
# Copilot
# ---------------------------------------------------------------------------


def parse_copilot_stream_line(line: str) -> List[ParsedStreamEvent]:
    obj = _loads_obj(line)
    if not isinstance(obj, dict):
        return []
    t = obj.get("type")
    data = obj.get("data")
    if t == "assistant.message_delta" and isinstance(_get(data, "deltaContent"), str):
        return [TextEvent(data["deltaContent"])]
    if t == "tool.execution_start":
        raw = _get(data, "toolName")
        if not isinstance(raw, str):
            return []
        tool_name = "Bash" if raw == "bash" else raw
        arg_field = TOOL_ARG_FIELDS.get(tool_name)
        if arg_field is None:
            return []
        args = _get(data, "arguments")
        if not isinstance(args, dict):
            return []
        value = args.get(arg_field)
        return [ToolCallEvent(name=tool_name, args=value)] if isinstance(value, str) else []
    if t == "assistant.message" and isinstance(_get(data, "content"), str) and data["content"]:
        return [ResultEvent(data["content"])]
    if t == "session.usage_checkpoint":
        return _context_window_event(_get(data, "accountingSnapshot", "lastCallInputTokens"))
    if t == "result" and isinstance(obj.get("sessionId"), str):
        return [SessionIdEvent(obj["sessionId"])]
    if t in ("error", "agent_error"):
        msg = extract_error_message(obj)
        return [ErrorEvent(msg)] if msg else []
    return []


class CopilotAgent(AgentProvider):
    name = "copilot"

    def __init__(
        self,
        model: str,
        effort: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        capture_sessions: bool = True,
        session_storage: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(env, capture_sessions)
        self.model = model
        self.effort = effort
        ss = session_storage or {}
        host_dir = ss.get("host_sessions_dir") or copilot_host_sessions_dir()
        sandbox_override = ss.get("sandbox_sessions_dir")
        host_path = lambda id: os.path.join(host_dir, id, "events.jsonl")  # noqa: E731
        self.session_storage = SessionFileStorage(
            SessionFileLayout(
                host_root=host_dir,
                host_path=lambda _cwd, id: host_path(id),
                host_paths_by_id=lambda id, _s: [host_path(id)],
                sandbox_path=lambda _cwd, id, dirs: copilot_session_path(
                    sandbox_override or posixpath.join(dirs.home, ".copilot", "session-state"), id
                ),
                rewrite=transfer_copilot_session,
            ),
            can_fork=False,
        )

    def build_print_command(self, o: AgentCommandOptions) -> PrintCommand:
        if o.resume_session is not None and o.fork_session:
            raise ForkNotSupportedError(
                "copilot cannot fork a session: its CLI resumes but has no fork. Use resume() to continue the session in place.",
                agent_provider="copilot",
            )
        _assert_copilot_print_prompt_fits_argv(o.prompt)
        allow_all = " --allow-all-tools" if o.dangerously_skip_permissions else ""
        effort = f" --effort {self.effort}" if self.effort else ""
        resume = "" if o.resume_session is None else f" --resume={shell_escape(o.resume_session)}"
        return PrintCommand(
            command=f"copilot -p {shell_escape(o.prompt)} --output-format json --model {shell_escape(self.model)}{allow_all}{effort}{resume}"
        )

    def build_interactive_args(self, o: AgentCommandOptions) -> List[str]:
        args = ["copilot", "--model", self.model]
        if o.prompt:
            args += ["-i", o.prompt]
        return args

    def parse_stream_line(self, line: str) -> List[ParsedStreamEvent]:
        return parse_copilot_stream_line(line)


def copilot(
    model: str,
    *,
    effort: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    capture_sessions: bool = True,
    session_storage: Optional[Dict[str, str]] = None,
) -> CopilotAgent:
    """GitHub Copilot CLI. Resumes, but cannot fork."""
    return CopilotAgent(model, effort, env, capture_sessions, session_storage)


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

PermissionMode = Literal["default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"]


def parse_claude_session_usage(content: str) -> Optional[IterationUsage]:
    for line in reversed(content.split("\n")):
        obj = _loads_obj(line)
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        u = _get(obj, "message", "usage")
        if not isinstance(u, dict):
            continue
        keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
        if all(_is_num(u.get(k)) for k in keys):
            return IterationUsage(*(u[k] for k in keys))
    return None


class ClaudeCodeAgent(AgentProvider):
    name = "claude-code"

    def __init__(
        self,
        model: str,
        effort: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        capture_sessions: bool = True,
        session_storage: Optional[Dict[str, str]] = None,
        permission_mode: Optional[str] = None,
    ) -> None:
        super().__init__(env, capture_sessions)
        self.model = model
        self.effort = effort
        self.permission_mode = permission_mode
        ss = session_storage or {}
        self.session_storage = ClaudeSessionStorage(ss.get("host_projects_dir"), ss.get("sandbox_projects_dir"))

    def build_print_command(self, o: AgentCommandOptions) -> PrintCommand:
        skip = " --dangerously-skip-permissions" if o.dangerously_skip_permissions else ""
        permission = f" --permission-mode {self.permission_mode}" if self.permission_mode else skip
        effort = f" --effort {self.effort}" if self.effort else ""
        resume = f" --resume {shell_escape(o.resume_session)}" if o.resume_session else ""
        fork = " --fork-session" if o.resume_session and o.fork_session else ""
        return PrintCommand(
            command=f"claude --print --verbose{permission} --output-format stream-json --model {shell_escape(self.model)}{effort}{resume}{fork}",
            stdin=o.prompt,
        )

    def build_interactive_args(self, o: AgentCommandOptions) -> List[str]:
        args = ["claude"]
        if self.permission_mode:
            args += ["--permission-mode", self.permission_mode]
        elif o.dangerously_skip_permissions:
            args.append("--dangerously-skip-permissions")
        args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]
        if o.prompt:
            args.append(o.prompt)
        return args

    def parse_stream_line(self, line: str) -> List[ParsedStreamEvent]:
        return parse_stream_json_line(line) + _parse_claude_context_window(line)

    def parse_session_usage(self, content: str) -> Optional[IterationUsage]:
        return parse_claude_session_usage(content)


def claude_code(
    model: str = DEFAULT_MODEL,
    *,
    effort: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    capture_sessions: bool = True,
    session_storage: Optional[Dict[str, str]] = None,
    permission_mode: Optional[str] = None,
) -> ClaudeCodeAgent:
    """Claude Code. ``permission_mode`` replaces the default
    ``--dangerously-skip-permissions`` on AFK runs."""
    return ClaudeCodeAgent(model, effort, env, capture_sessions, session_storage, permission_mode)


# TS-compatible alias
claudeCode = claude_code

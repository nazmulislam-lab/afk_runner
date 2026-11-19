"""Session JSONL transfer primitives. Port of ``SessionStore.ts``.

The transfer functions are pure: they take a JSONL string and the
source/target cwds and return the rewritten JSONL. Per ADR 0012 the cwd
rewrite is specific to each agent's JSONL format, so each agent owns its own
transfer function.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence

from .sandbox_provider import ExecResult


def json_dumps(value: Any) -> str:
    """``JSON.stringify`` equivalent: compact, non-ASCII kept."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def home_dir() -> str:
    """The host user's home directory, where every agent keeps its sessions."""
    return os.environ.get("HOME") or os.path.expanduser("~")


# ---------------------------------------------------------------------------
# Host session lookup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostSessionLookup:
    #: Absolute path to the located session file, or ``None``.
    path: Optional[str]
    #: The host directory that was scanned (named in not-found errors).
    searched_root: str


def path_exists(path: str) -> bool:
    return os.path.exists(path)


def first_existing_path(paths: Sequence[str]) -> Optional[str]:
    for p in paths:
        if path_exists(p):
            return p
    return None


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

MAX_PROJECT_DIR_LENGTH = 200


def _to_int32(n: int) -> int:
    n &= 0xFFFFFFFF
    return n - 0x100000000 if n & 0x80000000 else n


def _imul(a: int, b: int) -> int:
    return _to_int32(_to_int32(a) * _to_int32(b))


def _to_base36(n: int) -> str:
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    return "".join(reversed(out))


def _claude_path_hash(value: str) -> str:
    """Claude Code's 32-bit string hash over UTF-16 code units, base 36."""
    data = value.encode("utf-16-le", "surrogatepass")
    acc = 0
    for i in range(0, len(data), 2):
        unit = data[i] | (data[i + 1] << 8)
        acc = _imul(31, acc) + unit  # the addition is not wrapped in JS
    return _to_base36(abs(_imul(acc, 1)))


def encode_project_path(cwd: str) -> str:
    """Encode a cwd into Claude Code's ``~/.claude/projects/<encoded>/`` name.

    Every character that isn't an ASCII letter or digit becomes ``-``; names
    longer than 200 chars are cut and suffixed with a hash of the full path.
    A trailing path separator is ignored.
    """
    is_root = cwd == "/" or re.fullmatch(r"[A-Za-z]:[\\/]?", cwd) is not None
    normalized = cwd if is_root else re.sub(r"[\\/]+$", "", cwd)
    encoded = re.sub(r"[^A-Za-z0-9]", "-", normalized)
    if len(encoded) <= MAX_PROJECT_DIR_LENGTH:
        return encoded
    return f"{encoded[:MAX_PROJECT_DIR_LENGTH]}-{_claude_path_hash(normalized)}"


@dataclass(frozen=True)
class SandboxDirs:
    home: str
    tmp: str


DEFAULT_SANDBOX_DIRS = SandboxDirs(home="/home/agent", tmp="/tmp")


def sandbox_dirs_of(handle: Any) -> SandboxDirs:
    """The home and temp dir a sandbox handle reports, or the image defaults."""
    return SandboxDirs(
        home=getattr(handle, "sandbox_homedir", None) or DEFAULT_SANDBOX_DIRS.home,
        tmp=getattr(handle, "sandbox_tmpdir", None) or DEFAULT_SANDBOX_DIRS.tmp,
    )


def _claude_projects_root(projects_dir: Optional[str]) -> str:
    return projects_dir or os.path.join(home_dir(), ".claude", "projects")


def claude_host_session_path(cwd: str, id: str, projects_dir: Optional[str] = None) -> str:
    return os.path.join(_claude_projects_root(projects_dir), encode_project_path(cwd), f"{id}.jsonl")


def claude_sandbox_session_path(cwd: str, id: str, projects_dir: str) -> str:
    return posixpath.join(projects_dir, encode_project_path(cwd), f"{id}.jsonl")


def claude_subagents_dir_in_sandbox(cwd: str, id: str, projects_dir: str) -> str:
    return posixpath.join(projects_dir, encode_project_path(cwd), id, "subagents")


def claude_subagents_dir_on_host(cwd: str, id: str, projects_dir: Optional[str] = None) -> str:
    return os.path.join(_claude_projects_root(projects_dir), encode_project_path(cwd), id, "subagents")


async def list_claude_subagent_sessions_in_sandbox(
    cwd: str, id: str, handle: Any, sandbox_projects_dir: str
) -> List[str]:
    """Every ``agent-*.jsonl`` under the session's ``subagents/`` dir. Never raises."""
    d = claude_subagents_dir_in_sandbox(cwd, id, sandbox_projects_dir)
    try:
        result = await handle.exec(
            f"find {json_dumps(d)} -type f -name {json_dumps('agent-*.jsonl')} 2>/dev/null"
        )
    except Exception:
        return []
    if result.exit_code != 0:
        return []
    out = result.stdout.strip()
    return [line for line in out.split("\n") if line] if out else []


def find_claude_session_on_host(id: str, projects_dir: Optional[str] = None) -> HostSessionLookup:
    """Locate a Claude session by id across every project dir; first match wins."""
    root = _claude_projects_root(projects_dir)
    try:
        entries = list(os.scandir(root))
    except OSError:
        return HostSessionLookup(None, root)
    candidates = [os.path.join(root, e.name, f"{id}.jsonl") for e in entries if e.is_dir()]
    return HostSessionLookup(first_existing_path(candidates), root)


def _rewrite_entry_cwd(entry: dict, from_cwd: str, to_cwd: str) -> None:
    if isinstance(entry.get("cwd"), str) and entry["cwd"] == from_cwd:
        entry["cwd"] = to_cwd
    payload = entry.get("payload")
    if (
        entry.get("type") == "session_meta"
        and isinstance(payload, dict)
        and isinstance(payload.get("cwd"), str)
        and payload["cwd"] == from_cwd
    ):
        payload["cwd"] = to_cwd


def _rewrite_session_cwd(content: str, from_cwd: str, to_cwd: str) -> str:
    if content == "":
        return ""
    out = []
    for line in content.split("\n"):
        if line == "":
            out.append(line)
            continue
        # A torn final line must not abort the transfer: keep it verbatim.
        try:
            entry = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        if not isinstance(entry, (dict, list)):
            out.append(line)
            continue
        if isinstance(entry, dict):
            _rewrite_entry_cwd(entry, from_cwd, to_cwd)
        out.append(json_dumps(entry))
    return "\n".join(out)


def transfer_claude_session(jsonl: str, from_cwd: str, to_cwd: str) -> str:
    """Rewrite a Claude session JSONL's ``cwd`` fields. Pure function."""
    return _rewrite_session_cwd(jsonl, from_cwd, to_cwd)


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def _is_codex_session_filename(filename: str, id: str) -> bool:
    return filename.startswith("rollout-") and filename.endswith(f"-{id}.jsonl")


def find_codex_session_on_host(id: str, sessions_dir: Optional[str] = None) -> HostSessionLookup:
    """Locate a Codex rollout file by id, scanning ``YYYY/MM/DD/`` depth-first."""
    root = sessions_dir or os.path.join(home_dir(), ".codex", "sessions")
    try:
        entries = list(os.scandir(root))
    except OSError:
        return HostSessionLookup(None, root)
    for entry in entries:
        child = os.path.join(root, entry.name)
        if entry.is_file() and _is_codex_session_filename(entry.name, id):
            return HostSessionLookup(child, root)
        if entry.is_dir():
            found = find_codex_session_on_host(id, child)
            if found.path is not None:
                return HostSessionLookup(found.path, root)
    return HostSessionLookup(None, root)


def require_host_session_path(found: HostSessionLookup, id: str) -> str:
    if found.path is None:
        raise RuntimeError(f"session {id} not found in {found.searched_root}")
    return found.path


@dataclass(frozen=True)
class SandboxSessionLocation:
    path: str
    relative_path: str


async def locate_sandbox_session_file(handle: Any, sessions_dir: str, name_glob: str) -> SandboxSessionLocation:
    result = await handle.exec(
        f"find {json_dumps(sessions_dir)} -type f -name {json_dumps(name_glob)} -print -quit"
    )
    path = (result.stdout.strip().split("\n") or [""])[0]
    if result.exit_code != 0 or path == "":
        raise RuntimeError(f"session {name_glob} not found in {sessions_dir}")
    return SandboxSessionLocation(path, posixpath.relpath(path, sessions_dir))


def transfer_codex_session(jsonl: str, from_cwd: str, to_cwd: str) -> str:
    """Rewrite top-level ``cwd`` and ``session_meta.payload.cwd``. Pure."""
    return _rewrite_session_cwd(jsonl, from_cwd, to_cwd)


# ---------------------------------------------------------------------------
# Pi
# ---------------------------------------------------------------------------


def encode_pi_session_dir(cwd: str) -> str:
    """Pi's ``--<encoded-cwd>--`` session directory name."""
    stripped = re.sub(r"^[/\\]", "", cwd)
    replaced = re.sub(r"[/\\:]", "-", stripped)
    return f"--{replaced}--"


def pi_session_dir_path(cwd: str, sessions_dir: Optional[str] = None) -> str:
    base = sessions_dir or os.path.join(home_dir(), ".pi", "agent", "sessions")
    return os.path.join(base, encode_pi_session_dir(cwd))


def find_pi_session_on_host(id: str, sessions_dir: Optional[str] = None) -> HostSessionLookup:
    root = sessions_dir or os.path.join(home_dir(), ".pi", "agent", "sessions")
    try:
        entries = list(os.scandir(root))
    except OSError:
        return HostSessionLookup(None, root)
    for entry in entries:
        if not entry.is_dir():
            continue
        dir_abs = os.path.join(root, entry.name)
        try:
            files = os.listdir(dir_abs)
        except OSError:
            continue
        for name in files:
            if name.endswith(f"_{id}.jsonl"):
                return HostSessionLookup(os.path.join(dir_abs, name), root)
    return HostSessionLookup(None, root)


def transfer_pi_session(jsonl: str, from_cwd: str, to_cwd: str) -> str:
    """Rewrite the ``cwd`` on pi's header ``session`` entry. Pure."""
    if jsonl == "":
        return ""
    out = []
    for line in jsonl.split("\n"):
        if line == "":
            out.append(line)
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        if (
            isinstance(entry, dict)
            and entry.get("type") == "session"
            and isinstance(entry.get("cwd"), str)
            and entry["cwd"] == from_cwd
        ):
            entry["cwd"] = to_cwd
            out.append(json_dumps(entry))
        else:
            out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Copilot
# ---------------------------------------------------------------------------


def copilot_session_path(sessions_dir: str, id: str) -> str:
    return posixpath.join(sessions_dir, id, "events.jsonl")


def copilot_host_sessions_dir() -> str:
    return os.path.join(home_dir(), ".copilot", "session-state")


def _rewrite_copilot_event(line: str, from_cwd: str, to_cwd: str) -> str:
    try:
        entry = json.loads(line)
    except ValueError:
        return line
    data = entry.get("data") if isinstance(entry, dict) else None
    context = data.get("context") if isinstance(data, dict) else None
    if not isinstance(context, dict):
        return line
    keys = [k for k in ("cwd", "gitRoot") if context.get(k) == from_cwd]
    for k in keys:
        context[k] = to_cwd
    return json_dumps(entry) if keys else line


def transfer_copilot_session(jsonl: str, from_cwd: str, to_cwd: str) -> str:
    """Rewrite ``data.context.cwd`` / ``gitRoot`` in Copilot's events.jsonl. Pure."""
    return "\n".join(
        line if line == "" else _rewrite_copilot_event(line, from_cwd, to_cwd)
        for line in jsonl.split("\n")
    )


# ---------------------------------------------------------------------------
# Cursor / OpenCode
# ---------------------------------------------------------------------------


def cursor_chat_dir_name(cwd: str) -> str:
    return hashlib.md5(cwd.encode("utf-8")).hexdigest()


def cursor_host_chats_dir() -> str:
    return os.path.join(home_dir(), ".cursor", "chats")


def opencode_host_sessions_dir() -> str:
    return os.path.join(home_dir(), ".local", "share", "opencode", "sandcastle-sessions")


def opencode_host_database() -> str:
    return os.path.join(home_dir(), ".local", "share", "opencode", "opencode.db")


# ---------------------------------------------------------------------------
# Session storage for agents that keep each session in one file
# ---------------------------------------------------------------------------


@dataclass
class SessionFileLayout:
    host_root: str
    host_path: Callable[[str, str], str]
    host_paths_by_id: Callable[[str, Sequence[str]], Sequence[str]]
    sandbox_path: Callable[[str, str, SandboxDirs], str]
    sandbox_export: Optional[Callable[[str, str], str]] = None
    sandbox_import: Optional[Callable[[str, str], str]] = None
    rewrite: Optional[Callable[[str, str, str], str]] = None


def _sandbox_command_error(command: str, result: ExecResult) -> RuntimeError:
    return RuntimeError(f"`{command}` exited with code {result.exit_code}: {result.stderr.strip()}")


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class SessionFileStorage:
    """Session storage operations for a :class:`SessionFileLayout`.

    The file is copied between host and sandbox as is, and rewritten only when
    the layout says how. Binary files (an SQLite store) survive when
    ``rewrite`` is omitted.
    """

    can_fork: bool = True

    def __init__(self, layout: SessionFileLayout, can_fork: bool = True) -> None:
        self.layout = layout
        self.can_fork = can_fork

    def host_session_file_path(self, cwd: str, id: str) -> Optional[str]:
        return self.layout.host_path(cwd, id)

    async def exists_on_host(self, cwd: str, id: str) -> bool:
        return path_exists(self.layout.host_path(cwd, id))

    async def read_host_session(self, cwd: str, id: str) -> Optional[str]:
        p = self.layout.host_path(cwd, id)
        return _read_text(p) if path_exists(p) else None

    async def find_by_id_on_host(self, id: str) -> HostSessionLookup:
        try:
            subdirs = os.listdir(self.layout.host_root)
        except OSError:
            subdirs = []
        return HostSessionLookup(
            first_existing_path(list(self.layout.host_paths_by_id(id, subdirs))),
            self.layout.host_root,
        )

    async def capture_to_host(self, *, host_cwd: str, sandbox_cwd: str, session_id: str, handle: Any) -> None:
        layout = self.layout
        source = layout.sandbox_path(sandbox_cwd, session_id, sandbox_dirs_of(handle))
        if layout.sandbox_export is not None:
            command = layout.sandbox_export(session_id, source)
            result = await handle.exec(command, cwd=sandbox_cwd)
            if result.exit_code != 0:
                raise _sandbox_command_error(command, result)
        target = layout.host_path(host_cwd, session_id)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        await handle.copy_file_out(source, target)
        if layout.rewrite is not None:
            _write_text(target, layout.rewrite(_read_text(target), sandbox_cwd, host_cwd))

    async def resume_into_sandbox(self, *, host_cwd: str, sandbox_cwd: str, session_id: str, handle: Any) -> None:
        layout = self.layout
        source = layout.host_path(host_cwd, session_id)
        target = layout.sandbox_path(sandbox_cwd, session_id, sandbox_dirs_of(handle))
        d = json_dumps(posixpath.dirname(target))
        if layout.rewrite is None:
            await handle.exec(f"mkdir -p {d}")
            await handle.copy_file_in(source, target)
            if layout.sandbox_import is not None:
                command = layout.sandbox_import(session_id, target)
                result = await handle.exec(command, cwd=sandbox_cwd)
                if result.exit_code != 0:
                    raise _sandbox_command_error(command, result)
            return
        content = _read_text(source)
        result = await handle.exec(
            f"mkdir -p {d} && cat > {json_dumps(target)}",
            stdin=layout.rewrite(content, host_cwd, sandbox_cwd),
        )
        if result.exit_code != 0:
            raise RuntimeError(f"writing {target} failed: {result.stderr}")


# TS-compatible aliases for the public exports
transferClaudeSession = transfer_claude_session
transferCodexSession = transfer_codex_session
encodeProjectPath = encode_project_path
claudeHostSessionPath = claude_host_session_path
claudeSandboxSessionPath = claude_sandbox_session_path
findClaudeSessionOnHost = find_claude_session_on_host
findCodexSessionOnHost = find_codex_session_on_host

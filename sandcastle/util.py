"""Small shared helpers. Port of ``EnvResolver.ts``, ``ErrorHandler.ts``,
``resumePrecheck.ts``, ``resolveCwd.ts``, ``validateTimeouts.ts`` and
``mergeProviderEnv.ts``."""

from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, Mapping, Optional

from .errors import CwdError, ForkNotSupportedError, SandcastleError

# ---------------------------------------------------------------------------
# .sandcastle/.env
# ---------------------------------------------------------------------------

_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\"}


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1]
    if not (len(value) >= 2 and value[0] == '"' and value[-1] == '"'):
        return value
    return re.sub(r"\\([nrt\\])", lambda m: _ESCAPES[m.group(1)], value[1:-1])


def parse_env_text(content: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in content.split("\n"):
        t = line.strip()
        if not t or t.startswith("#") or "=" not in t:
            continue
        i = t.index("=")
        out[t[:i].strip()] = _unquote(t[i + 1 :].strip())
    return out


def resolve_env(repo_dir: str) -> Dict[str, str]:
    """Env vars declared in ``.sandcastle/.env``; an empty value falls back to
    the process env. Only declared keys are resolved."""
    try:
        with open(os.path.join(repo_dir, ".sandcastle", ".env"), "r", encoding="utf-8") as f:
            declared = parse_env_text(f.read())
    except OSError:
        return {}
    result: Dict[str, str] = {}
    for key, file_value in declared.items():
        value = file_value or os.environ.get(key, "")
        if value:
            result[key] = value
    return result


def merge_provider_env(
    resolved_env: Mapping[str, str], agent_provider_env: Mapping[str, str], sandbox_provider_env: Mapping[str, str]
) -> Dict[str, str]:
    """Provider env overrides the resolved env; agent and sandbox provider env
    must not share keys."""
    overlap = [k for k in agent_provider_env if k in sandbox_provider_env]
    if overlap:
        raise ValueError(f"Overlapping env keys between agent provider and sandbox provider: {', '.join(overlap)}")
    return {**resolved_env, **sandbox_provider_env, **agent_provider_env}


# ---------------------------------------------------------------------------
# cwd / timeouts
# ---------------------------------------------------------------------------


def resolve_cwd(cwd: Optional[str]) -> str:
    """Absolute, existing, symlink-resolved host repo directory."""
    resolved = os.path.abspath(os.getcwd() if cwd is None else os.path.join(os.getcwd(), os.path.expanduser(cwd)))
    if not os.path.exists(resolved):
        raise CwdError(f"cwd does not exist: {resolved}", cwd=resolved)
    if not os.path.isdir(resolved):
        raise CwdError(f"cwd is not a directory: {resolved}", cwd=resolved)
    try:
        return os.path.realpath(resolved)
    except OSError:
        raise CwdError(f"cwd could not be resolved: {resolved}", cwd=resolved) from None


def _assert_positive(name: str, unit: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not (value > 0) or (isinstance(value, float) and math.isnan(value)):
        raise ValueError(f"{name} must be a positive number of {unit}. Received: {value!r}")


def validate_timeout_options(
    idle_timeout_seconds: Any = None, completion_timeout_seconds: Any = None, timeouts: Any = None
) -> None:
    _assert_positive("idleTimeoutSeconds", "seconds", idle_timeout_seconds)
    _assert_positive("completionTimeoutSeconds", "seconds", completion_timeout_seconds)
    if timeouts is not None:
        for attr, label in (
            ("copy_to_worktree_ms", "copyToWorktreeMs"),
            ("git_setup_ms", "gitSetupMs"),
            ("commit_collection_ms", "commitCollectionMs"),
            ("merge_to_host_ms", "mergeToHostMs"),
        ):
            _assert_positive(f"timeouts.{label}", "milliseconds", getattr(timeouts, attr, None))


# ---------------------------------------------------------------------------
# Resume prechecks
# ---------------------------------------------------------------------------


def assert_fork_supported(provider: Any) -> None:
    storage = getattr(provider, "session_storage", None)
    if storage is not None and getattr(storage, "can_fork", True) is False:
        raise ForkNotSupportedError(
            f"{provider.name} cannot fork a session: its CLI resumes but has no fork. "
            "Use resume() to continue the session in place.",
            agent_provider=provider.name,
        )


async def assert_resume_session_exists(provider: Any, sandbox_tag: str, host_repo_dir: str, resume_session: str) -> None:
    storage = getattr(provider, "session_storage", None)
    if storage is None:
        raise ValueError(f"{provider.name} does not support resumeSession")
    if sandbox_tag == "none":
        found = await storage.find_by_id_on_host(resume_session)
        if found.path is None:
            raise ValueError(f'resumeSession "{resume_session}" not found under {found.searched_root}')
        return
    if not await storage.exists_on_host(host_repo_dir, resume_session):
        p = storage.host_session_file_path(host_repo_dir, resume_session)
        raise ValueError(
            f'resumeSession "{resume_session}" not found'
            if p is None
            else f'resumeSession "{resume_session}" not found: expected session file at {p}'
        )


# ---------------------------------------------------------------------------
# Friendly error messages
# ---------------------------------------------------------------------------


def format_error_message(error: BaseException) -> str:
    tag = getattr(error, "_tag", None)
    msg = getattr(error, "message", str(error))
    prefixes = {
        "ExecError": lambda: f"Command failed in sandbox ({getattr(error, 'command', '')}): {msg}",
        "ExecHostError": lambda: f"Command failed on host ({getattr(error, 'command', '')}): {msg}",
        "CopyError": lambda: f"File copy failed: {msg}",
        "DockerError": lambda: f"Docker operation failed: {msg}. Is Docker running?",
        "PodmanError": lambda: f"Podman operation failed: {msg}. Is Podman running?",
        "SyncError": lambda: f"Git sync failed: {msg}",
        "WorktreeError": lambda: f"Git worktree operation failed: {msg}",
        "PromptError": lambda: f"Failed to resolve prompt: {msg}",
        "AgentError": lambda: f"Agent invocation failed: {msg}",
    }
    if tag in prefixes:
        return prefixes[tag]()
    return msg


def is_sandbox_error(error: BaseException) -> bool:
    return isinstance(error, SandcastleError)

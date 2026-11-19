"""Error types for sandcastle.

Python port of ``src/errors.ts`` and the public error re-exports
(``CwdError``, ``ForkNotSupportedError``, ``SyncOutPathError``,
``NativeSandboxUnavailableError``, ``NativeInteractiveNotSupportedError``).

Every error carries a ``_tag`` class attribute matching the TypeScript
``Data.TaggedError`` tag, so callers can match on ``error._tag`` exactly as
they would in the original library, or simply use ``isinstance``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional, Sequence, TypeVar

T = TypeVar("T")


class SandcastleError(Exception):
    """Base class for every sandcastle error."""

    _tag: str = "SandcastleError"

    def __init__(self, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.message = message
        for key, value in fields.items():
            setattr(self, key, value)

    @property
    def name(self) -> str:
        return self._tag

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


def _tagged(tag: str, doc: str, *field_names: str) -> type:
    """Build a tagged error subclass with keyword-only extra fields."""

    def __init__(self: SandcastleError, message: str, **fields: Any) -> None:
        for name in field_names:
            fields.setdefault(name, None)
        SandcastleError.__init__(self, message, **fields)

    return type(tag, (SandcastleError,), {"_tag": tag, "__doc__": doc, "__init__": __init__})


class ExecError(SandcastleError):
    """Command execution failed in the sandbox.

    ``exit_code`` is set when the command ran but returned non-zero; it is
    ``None`` when the exec itself failed (e.g. an SDK threw first).
    """

    _tag = "ExecError"

    def __init__(self, message: str, command: str = "", exit_code: Optional[int] = None) -> None:
        super().__init__(message, command=command, exit_code=exit_code)


class ExecHostError(SandcastleError):
    """Command execution failed on the host."""

    _tag = "ExecHostError"

    def __init__(self, message: str, command: str = "") -> None:
        super().__init__(message, command=command)


class CopyError(SandcastleError):
    """File copy between host and sandbox failed."""

    _tag = "CopyError"


class BwrapError(SandcastleError):
    """Setting up a ``bwrap()`` sandbox's private dirs failed."""

    _tag = "BwrapError"


class SeatbeltError(SandcastleError):
    """Setting up a ``seatbelt()`` sandbox's private dirs failed."""

    _tag = "SeatbeltError"


class DockerError(SandcastleError):
    """Docker infrastructure operation failed."""

    _tag = "DockerError"


class PodmanError(SandcastleError):
    """Podman infrastructure operation failed."""

    _tag = "PodmanError"


class ImageNotFoundError(SandcastleError):
    """A container image to act on does not exist."""

    _tag = "ImageNotFoundError"


class SyncError(SandcastleError):
    """Git sync-in or sync-out operation failed."""

    _tag = "SyncError"


class SyncOutPathError(SandcastleError):
    """Sync-out refused to write a file on the host because its path, as
    reported by the sandbox, would land outside the host worktree (or in its
    ``.git``): an absolute path, a ``..`` segment, or a symlink leading out."""

    _tag = "SyncOutPathError"

    def __init__(self, message: str, path: str = "") -> None:
        super().__init__(message, path=path)


class NativeInteractiveNotSupportedError(SandcastleError):
    """``interactive()`` was called with a native sandbox provider."""

    _tag = "NativeInteractiveNotSupportedError"

    def __init__(self, message: str, provider: str = "") -> None:
        super().__init__(message, provider=provider)


class NativeSandboxUnavailableError(SandcastleError):
    """A native sandbox provider's OS mechanism isn't usable on this host.

    Raised when the provider is constructed, so a run never falls back to
    running unsandboxed. ``fix_command`` is a shell command that fixes the
    host, when there is one.
    """

    _tag = "NativeSandboxUnavailableError"

    def __init__(self, message: str, fix_command: Optional[str] = None) -> None:
        super().__init__(message, fix_command=fix_command)

    @property
    def fixCommand(self) -> Optional[str]:  # noqa: N802 - TS-compatible alias
        return self.fix_command


class HostGitError(SandcastleError):
    """A host-side git command failed, or was refused because the
    repository's ``.git`` did not lead where it should."""

    _tag = "HostGitError"

    def __init__(
        self,
        message: str,
        args: Sequence[str] = (),
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message, args=list(args), stdout=stdout, stderr=stderr)


class WorktreeError(SandcastleError):
    """Git worktree operation failed."""

    _tag = "WorktreeError"


class PromptError(SandcastleError):
    """Prompt resolution or preprocessing failed.

    ``exit_code`` is set when the failure is a non-zero exit from a
    ``!`command``` expansion.
    """

    _tag = "PromptError"

    def __init__(self, message: str, exit_code: Optional[int] = None) -> None:
        super().__init__(message, exit_code=exit_code)


class AgentError(SandcastleError):
    """Agent invocation failed."""

    _tag = "AgentError"

    def __init__(self, message: str, preserved_worktree_path: Optional[str] = None) -> None:
        super().__init__(message, preserved_worktree_path=preserved_worktree_path)


class ConfigDirError(SandcastleError):
    """``.sandcastle/`` config directory missing."""

    _tag = "ConfigDirError"


class InitError(SandcastleError):
    """Initialization or setup operation failed."""

    _tag = "InitError"


class AgentIdleTimeoutError(SandcastleError):
    """Run exceeded the configured agent idle timeout."""

    _tag = "AgentIdleTimeoutError"

    def __init__(
        self, message: str, timeout_ms: float = 0, preserved_worktree_path: Optional[str] = None
    ) -> None:
        super().__init__(
            message, timeout_ms=timeout_ms, preserved_worktree_path=preserved_worktree_path
        )


class WorktreeTimeoutError(SandcastleError):
    """Git worktree create or prune timed out."""

    _tag = "WorktreeTimeoutError"

    def __init__(self, message: str, timeout_ms: float = 0, path: str = "", operation: str = "create") -> None:
        super().__init__(message, timeout_ms=timeout_ms, path=path, operation=operation)


class ContainerStartTimeoutError(SandcastleError):
    """Sandbox container start timed out."""

    _tag = "ContainerStartTimeoutError"

    def __init__(self, message: str, timeout_ms: float = 0) -> None:
        super().__init__(message, timeout_ms=timeout_ms)


class CopyToWorktreeTimeoutError(SandcastleError):
    """Copying files to worktree timed out."""

    _tag = "CopyToWorktreeTimeoutError"

    def __init__(self, message: str, timeout_ms: float = 0, paths: Sequence[str] = ()) -> None:
        super().__init__(message, timeout_ms=timeout_ms, paths=list(paths))


class CopyToWorktreeError(SandcastleError):
    """Fallback ``cp -R`` to worktree failed."""

    _tag = "CopyToWorktreeError"

    def __init__(self, message: str, path: str = "", stderr: str = "", exit_code: Optional[int] = None) -> None:
        super().__init__(message, path=path, stderr=stderr, exit_code=exit_code)


class SyncInTimeoutError(SandcastleError):
    """Git sync-in for isolated providers timed out."""

    _tag = "SyncInTimeoutError"

    def __init__(self, message: str, timeout_ms: float = 0) -> None:
        super().__init__(message, timeout_ms=timeout_ms)


class HookTimeoutError(SandcastleError):
    """onSandboxReady hook command timed out."""

    _tag = "HookTimeoutError"

    def __init__(self, message: str, timeout_ms: float = 0, command: str = "") -> None:
        super().__init__(message, timeout_ms=timeout_ms, command=command)


class GitSetupTimeoutError(SandcastleError):
    """Git config setup command timed out."""

    _tag = "GitSetupTimeoutError"

    def __init__(self, message: str, timeout_ms: float = 0, command: str = "") -> None:
        super().__init__(message, timeout_ms=timeout_ms, command=command)


class PromptExpansionTimeoutError(SandcastleError):
    """Prompt shell expression expansion timed out.

    ``elapsed_ms`` is the wall-clock time the expression actually ran,
    distinct from ``timeout_ms`` (the configured deadline).
    """

    _tag = "PromptExpansionTimeoutError"

    def __init__(self, message: str, timeout_ms: float = 0, expression: str = "", elapsed_ms: float = 0) -> None:
        super().__init__(message, timeout_ms=timeout_ms, expression=expression, elapsed_ms=elapsed_ms)


class CommitCollectionTimeoutError(SandcastleError):
    """Commit collection timed out."""

    _tag = "CommitCollectionTimeoutError"

    def __init__(self, message: str, timeout_ms: float = 0) -> None:
        super().__init__(message, timeout_ms=timeout_ms)


class MergeToHostTimeoutError(SandcastleError):
    """Merge-to-host branch timed out."""

    _tag = "MergeToHostTimeoutError"

    def __init__(self, message: str, timeout_ms: float = 0, source_branch: str = "", target_branch: str = "") -> None:
        super().__init__(
            message, timeout_ms=timeout_ms, source_branch=source_branch, target_branch=target_branch
        )


class ForkNotSupportedError(SandcastleError):
    """``.fork()`` was called on an agent provider whose CLI resumes a
    session but has no native fork. Raised before any sandbox starts."""

    _tag = "ForkNotSupportedError"

    def __init__(self, message: str, agent_provider: str = "") -> None:
        super().__init__(message, agent_provider=agent_provider)

    @property
    def agentProvider(self) -> str:  # noqa: N802 - TS-compatible alias
        return self.agent_provider


class SessionCaptureError(SandcastleError):
    """Session capture (read, rewrite, or write) failed."""

    _tag = "SessionCaptureError"

    def __init__(self, message: str, session_id: str = "") -> None:
        super().__init__(message, session_id=session_id)


class CwdError(SandcastleError):
    """The provided ``cwd`` path does not exist or is not a directory."""

    _tag = "CwdError"

    def __init__(self, message: str, cwd: str = "") -> None:
        super().__init__(message, cwd=cwd)


class UsageLimitWaitError(SandcastleError):
    """The agent hit its usage limit and ``wait_for_usage_reset`` is off, or
    the configured maximum number of usage-limit waits was exhausted."""

    _tag = "UsageLimitWaitError"

    def __init__(self, message: str, resets_at: Optional[float] = None) -> None:
        super().__init__(message, resets_at=resets_at)


SandboxError = SandcastleError
"""Union of all sandbox-related errors (every error derives from this)."""


async def with_timeout(
    timeout_ms: Optional[float],
    on_timeout: Callable[[], BaseException],
    awaitable: Awaitable[T],
) -> T:
    """Await ``awaitable``; raise ``on_timeout()`` if it takes longer than
    ``timeout_ms`` milliseconds. ``None`` or ``inf`` means no timeout.

    Port of ``withTimeout`` (``Effect.timeoutFail``).
    """
    if timeout_ms is None or timeout_ms == float("inf"):
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout_ms / 1000.0)
    except asyncio.TimeoutError:
        raise on_timeout() from None


__all__ = [name for name in dir() if name.endswith("Error")] + ["with_timeout", "SandcastleError"]

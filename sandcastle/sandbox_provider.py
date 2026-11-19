"""Sandbox provider types: the pluggable interface for sandbox runtimes.

Port of ``SandboxProvider.ts``, ``BranchStrategy.ts``, ``MountConfig.ts`` and
``createSandboxProvider.ts``.

Provider authors implement a small async interface. Sandcastle handles
worktree creation, git mount resolution, and commit extraction.

A *handle* is any object with the async methods described by the protocols
below. ``exec`` MUST stream stdout line by line through ``on_line``: that is
how sandcastle shows live progress and enforces idle timeouts. When ``stdin``
is given, the implementation pipes the string to the child's stdin and closes
it (this avoids the Linux 128 KB per-argument limit).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    IO,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Protocol,
    Sequence,
    Union,
    runtime_checkable,
)

# ---------------------------------------------------------------------------
# Branch strategies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeadBranchStrategy:
    """Agent writes directly to the host working directory. Bind-mount only."""

    type: Literal["head"] = "head"


@dataclass(frozen=True)
class MergeToHeadBranchStrategy:
    """Temp branch in a worktree, merged back to HEAD, then deleted."""

    type: Literal["merge-to-head"] = "merge-to-head"


@dataclass(frozen=True)
class NamedBranchStrategy:
    """Commits land on an explicit named branch in a worktree.

    ``base_branch`` is the git ref used as the starting point when the branch
    doesn't already exist (ignored otherwise). Defaults to ``HEAD``.
    """

    branch: str = ""
    base_branch: Optional[str] = None
    type: Literal["branch"] = "branch"


BranchStrategy = Union[HeadBranchStrategy, MergeToHeadBranchStrategy, NamedBranchStrategy]
BindMountBranchStrategy = BranchStrategy
NoSandboxBranchStrategy = BranchStrategy
IsolatedBranchStrategy = Union[MergeToHeadBranchStrategy, NamedBranchStrategy]
WorktreeBranchStrategy = Union[MergeToHeadBranchStrategy, NamedBranchStrategy]


def head() -> HeadBranchStrategy:
    """Shorthand for ``HeadBranchStrategy()``."""
    return HeadBranchStrategy()


def merge_to_head() -> MergeToHeadBranchStrategy:
    """Shorthand for ``MergeToHeadBranchStrategy()``."""
    return MergeToHeadBranchStrategy()


def branch(name: str, base_branch: Optional[str] = None) -> NamedBranchStrategy:
    """Shorthand for ``NamedBranchStrategy(branch=name, base_branch=...)``."""
    return NamedBranchStrategy(branch=name, base_branch=base_branch)


def to_branch_strategy(value: Any) -> Optional[BranchStrategy]:
    """Accept a strategy object or a TS-style dict (``{"type": "branch", "branch": "x"}``)."""
    if value is None:
        return None
    if isinstance(value, (HeadBranchStrategy, MergeToHeadBranchStrategy, NamedBranchStrategy)):
        return value
    if isinstance(value, dict):
        kind = value.get("type")
        if kind == "head":
            return HeadBranchStrategy()
        if kind == "merge-to-head":
            return MergeToHeadBranchStrategy()
        if kind == "branch":
            return NamedBranchStrategy(
                branch=value["branch"],
                base_branch=value.get("base_branch", value.get("baseBranch")),
            )
    raise ValueError(f"Invalid branch strategy: {value!r}")


# ---------------------------------------------------------------------------
# Mounts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MountConfig:
    """A single bind-mount descriptor for docker()/podman()/native providers.

    ``host_path`` supports absolute, ``~``-expanded and relative paths
    (resolved from the current directory). ``sandbox_path`` supports absolute,
    ``~``-expanded (using the provider's sandbox home) and relative paths
    (resolved from the sandbox repo directory).
    """

    host_path: str
    sandbox_path: str
    readonly: bool = False


@dataclass
class Mount:
    """A resolved host:sandbox volume mount passed to ``create``."""

    host_path: str
    sandbox_path: str
    readonly: bool = False


# ---------------------------------------------------------------------------
# Exec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecResult:
    """Result of executing a command inside a sandbox."""

    stdout: str
    stderr: str
    exit_code: int

    @property
    def exitCode(self) -> int:  # noqa: N802 - TS-compatible alias
        return self.exit_code


OnLine = Callable[[str], None]


@dataclass
class InteractiveExecOptions:
    """The streams the provider should wire to an interactive process."""

    stdin: IO[Any]
    stdout: IO[Any]
    stderr: IO[Any]
    cwd: Optional[str] = None


@runtime_checkable
class SandboxHandleBase(Protocol):
    worktree_path: str

    async def exec(
        self,
        command: str,
        *,
        on_line: Optional[OnLine] = None,
        cwd: Optional[str] = None,
        sudo: bool = False,
        stdin: Optional[str] = None,
    ) -> ExecResult: ...

    async def close(self) -> None: ...


class BindMountSandboxHandle(SandboxHandleBase, Protocol):
    """Handle to a running bind-mount sandbox.

    Optional attributes: ``sandbox_id`` (an id outside tools can address the
    sandbox by), ``sandbox_homedir`` (default ``/home/agent``),
    ``sandbox_tmpdir`` (default ``/tmp``) and an ``interactive_exec`` method.
    """

    async def copy_file_in(self, host_path: str, sandbox_path: str) -> None: ...

    async def copy_file_out(self, sandbox_path: str, host_path: str) -> None: ...


class IsolatedSandboxHandle(SandboxHandleBase, Protocol):
    """Handle to a running isolated sandbox (its own filesystem)."""

    async def copy_in(self, host_path: str, sandbox_path: str) -> None: ...

    async def copy_file_out(self, sandbox_path: str, host_path: str) -> None: ...


class NoSandboxHandle(SandboxHandleBase, Protocol):
    """Handle to a no-sandbox session: runs commands directly on the host."""

    async def interactive_exec(self, args: List[str], options: InteractiveExecOptions) -> int: ...


@dataclass
class BindMountCreateOptions:
    """Options passed to a bind-mount provider's ``create`` function."""

    worktree_path: str
    host_repo_path: str
    mounts: List[Mount]
    env: Dict[str, str]


@dataclass
class IsolatedCreateOptions:
    """Options passed to an isolated provider's ``create`` function."""

    env: Dict[str, str]


@dataclass
class NoSandboxCreateOptions:
    worktree_path: str
    env: Dict[str, str]


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


@dataclass
class BindMountSandboxProvider:
    """A bind-mount sandbox provider (Docker, Podman, bwrap, seatbelt...)."""

    name: str
    create: Callable[[BindMountCreateOptions], Awaitable[Any]]
    env: Dict[str, str] = field(default_factory=dict)
    sandbox_homedir: Optional[str] = None
    tag: Literal["bind-mount"] = "bind-mount"
    #: Set by native providers (bwrap, seatbelt): no root, no interactive().
    native: bool = False
    #: Extra provider-specific data (e.g. image name for the CLI).
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IsolatedSandboxProvider:
    """An isolated sandbox provider (E2B, Daytona, Modal, Vercel, Sprites...)."""

    name: str
    create: Callable[[IsolatedCreateOptions], Awaitable[Any]]
    env: Dict[str, str] = field(default_factory=dict)
    tag: Literal["isolated"] = "isolated"
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NoSandboxProvider:
    """Runs the agent directly on the host with no isolation."""

    name: str
    create: Callable[[NoSandboxCreateOptions], Awaitable[Any]]
    env: Dict[str, str] = field(default_factory=dict)
    tag: Literal["none"] = "none"
    meta: Dict[str, Any] = field(default_factory=dict)


SandboxProvider = Union[BindMountSandboxProvider, IsolatedSandboxProvider, NoSandboxProvider]
AnySandboxProvider = SandboxProvider


def create_bind_mount_sandbox_provider(
    name: str,
    create: Callable[[BindMountCreateOptions], Awaitable[Any]],
    env: Optional[Dict[str, str]] = None,
    sandbox_homedir: Optional[str] = None,
) -> BindMountSandboxProvider:
    """Create a bind-mount sandbox provider from its parts."""
    return BindMountSandboxProvider(
        name=name, create=create, env=dict(env or {}), sandbox_homedir=sandbox_homedir
    )


def create_isolated_sandbox_provider(
    name: str,
    create: Callable[[IsolatedCreateOptions], Awaitable[Any]],
    env: Optional[Dict[str, str]] = None,
) -> IsolatedSandboxProvider:
    """Create an isolated sandbox provider from its parts."""
    return IsolatedSandboxProvider(name=name, create=create, env=dict(env or {}))


# TS-compatible aliases
createBindMountSandboxProvider = create_bind_mount_sandbox_provider
createIsolatedSandboxProvider = create_isolated_sandbox_provider


def handle_attr(handle: Any, name: str, default: Any = None) -> Any:
    """Read an optional handle attribute (``sandbox_id``, ``sandbox_homedir``...)."""
    return getattr(handle, name, default)


def is_native(provider: Any) -> bool:
    return bool(getattr(provider, "native", False))


__all__ = [
    "HeadBranchStrategy",
    "MergeToHeadBranchStrategy",
    "NamedBranchStrategy",
    "BranchStrategy",
    "BindMountBranchStrategy",
    "IsolatedBranchStrategy",
    "NoSandboxBranchStrategy",
    "WorktreeBranchStrategy",
    "head",
    "merge_to_head",
    "branch",
    "to_branch_strategy",
    "MountConfig",
    "Mount",
    "ExecResult",
    "InteractiveExecOptions",
    "BindMountSandboxHandle",
    "IsolatedSandboxHandle",
    "NoSandboxHandle",
    "BindMountCreateOptions",
    "IsolatedCreateOptions",
    "NoSandboxCreateOptions",
    "BindMountSandboxProvider",
    "IsolatedSandboxProvider",
    "NoSandboxProvider",
    "SandboxProvider",
    "AnySandboxProvider",
    "create_bind_mount_sandbox_provider",
    "create_isolated_sandbox_provider",
    "createBindMountSandboxProvider",
    "createIsolatedSandboxProvider",
]

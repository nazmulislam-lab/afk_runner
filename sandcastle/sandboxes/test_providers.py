"""Filesystem-backed test providers. Port of ``test-shared.ts``,
``test-bind-mount.ts`` and ``test-isolated.ts``.

The "sandbox" is a temp directory: ``exec`` runs shell commands there.
Useful for tests and for trying sandcastle without Docker.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any, List, Optional

from ..sandbox_provider import (
    BindMountCreateOptions,
    BindMountSandboxProvider,
    ExecResult,
    IsolatedSandboxProvider,
    create_bind_mount_sandbox_provider,
    create_isolated_sandbox_provider,
)
from ._proc import run_process


class TempSandbox:
    def __init__(self, prefix: str) -> None:
        self.root = tempfile.mkdtemp(prefix=prefix)
        self.worktree_path = os.path.join(self.root, "workspace")
        os.makedirs(self.worktree_path, exist_ok=True)

    async def exec(self, command: str, *, on_line=None, cwd: Optional[str] = None, sudo: bool = False, stdin: Optional[str] = None) -> ExecResult:
        return await run_process(["sh", "-c", command], cwd=cwd or self.worktree_path, stdin=stdin, on_line=on_line, new_session=True)

    async def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class _BindHandle(TempSandbox):
    async def copy_file_in(self, host_path: str, sandbox_path: str) -> None:
        os.makedirs(os.path.dirname(sandbox_path), exist_ok=True)
        shutil.copyfile(host_path, sandbox_path)

    async def copy_file_out(self, sandbox_path: str, host_path: str) -> None:
        os.makedirs(os.path.dirname(host_path), exist_ok=True)
        shutil.copyfile(sandbox_path, host_path)


class _IsolatedHandle(TempSandbox):
    async def copy_in(self, host_path: str, sandbox_path: str) -> None:
        if os.path.isdir(host_path):
            shutil.copytree(host_path, sandbox_path, dirs_exist_ok=True, symlinks=True)
        else:
            os.makedirs(os.path.dirname(sandbox_path), exist_ok=True)
            shutil.copyfile(host_path, sandbox_path)

    async def copy_file_out(self, sandbox_path: str, host_path: str) -> None:
        os.makedirs(os.path.dirname(host_path), exist_ok=True)
        shutil.copyfile(sandbox_path, host_path)


def test_bind_mount() -> BindMountSandboxProvider:
    async def create(_opts: Any) -> _BindHandle:
        return _BindHandle("sandcastle-test-bm-")

    return create_bind_mount_sandbox_provider("test-bind-mount", create)


def test_isolated() -> IsolatedSandboxProvider:
    async def create(_opts: Any) -> _IsolatedHandle:
        return _IsolatedHandle("sandcastle-test-")

    return create_isolated_sandbox_provider("test-isolated", create)


@dataclass
class StubProviderRecord:
    provider: BindMountSandboxProvider
    create_calls: List[BindMountCreateOptions] = field(default_factory=list)
    close_calls: List[int] = field(default_factory=lambda: [0])


def test_stub_provider(name: str = "test-stub", worktree_path: str = "/home/agent/workspace") -> StubProviderRecord:
    """A no-op bind-mount provider that records create/close calls."""
    record = StubProviderRecord(provider=None)  # type: ignore[arg-type]

    class _Stub:
        def __init__(self) -> None:
            self.worktree_path = worktree_path

        async def exec(self, command: str, **_: Any) -> ExecResult:
            return ExecResult("", "", 0)

        async def copy_file_in(self, *_: Any) -> None:
            return None

        async def copy_file_out(self, *_: Any) -> None:
            return None

        async def close(self) -> None:
            record.close_calls[0] += 1

    async def create(opts: BindMountCreateOptions) -> _Stub:
        record.create_calls.append(opts)
        return _Stub()

    record.provider = create_bind_mount_sandbox_provider(name, create)
    return record


# pytest must not collect these factory functions as tests
test_bind_mount.__test__ = False  # type: ignore[attr-defined]
test_isolated.__test__ = False  # type: ignore[attr-defined]
test_stub_provider.__test__ = False  # type: ignore[attr-defined]
testBindMount = test_bind_mount
testIsolated = test_isolated

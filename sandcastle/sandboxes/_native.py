"""Shared pieces of the native sandbox providers (bwrap, seatbelt).

Port of ``hostPathToolchain.ts``, ``nativeSandboxIo.ts`` and
``nativeSandboxShutdown.ts``.

The toolchain is resolved from the host ``PATH``: every ``PATH`` dir plus the
install root of each executable symlinked from one (an nvm ``codex``, a
``claude`` in ``~/.local/bin``). The home dir and its ancestors are never
exposed, nor an install root holding a ``.git``.
"""

from __future__ import annotations

import os
import posixpath
import shutil
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from ..errors import CopyError
from ..shutdown import register_shutdown


@dataclass
class HostPathToolchain:
    #: The ``PATH`` for the sandboxed child, in order.
    path_entries: List[str] = field(default_factory=list)
    #: Real dirs to make readable at their own paths.
    readable_dirs: List[str] = field(default_factory=list)
    #: ``(path, target)``: PATH dirs that are symlinks, to recreate.
    symlinks: List[Tuple[str, str]] = field(default_factory=list)


def _is_within(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(root + "/")


def _is_within_any(path: str, roots: Sequence[str]) -> bool:
    return any(_is_within(path, r) for r in roots)


def _is_home_or_ancestor(path: str, home: str) -> bool:
    return _is_within(home, path)


def _normalize(entry: str) -> str:
    n = posixpath.normpath(entry)
    return n[:-1] if len(n) > 1 and n.endswith("/") else n


def _real(path: str) -> Optional[str]:
    return os.path.realpath(path) if os.path.exists(path) else None


def _install_root_of(file: str, home: str) -> str:
    file_dir = posixpath.dirname(file)
    d = file_dir
    while "node_modules" in d.split("/") and not _is_home_or_ancestor(d, home):
        if os.path.exists(posixpath.join(d, "package.json")):
            return d
        d = posixpath.dirname(d)
    return file_dir


def _symlink_targets_in(d: str) -> List[str]:
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for name in names:
        entry = posixpath.join(d, name)
        if os.path.islink(entry):
            r = _real(entry)
            if r:
                out.append(r)
    return out


def _outermost(dirs: Sequence[str]) -> List[str]:
    unique = list(dict.fromkeys(dirs))
    return [d for d in unique if not any(o != d and _is_within(d, o) for o in unique)]


def resolve_host_path_toolchain(path: str, home: str, covered_roots: Sequence[str], denied_roots: Sequence[str]) -> HostPathToolchain:
    home = _normalize(home)

    def refused(p: str) -> bool:
        return _is_home_or_ancestor(p, home) or _is_within_any(p, denied_roots)

    entries = list(dict.fromkeys(_normalize(e) for e in path.split(":") if e.startswith("/")))
    accepted: List[Tuple[str, str]] = []
    for e in entries:
        if refused(e):
            continue
        r = _real(e)
        if r is None or refused(r):
            continue
        accepted.append((e, r))
    uncovered = [(e, r) for e, r in accepted if not _is_within_any(r, covered_roots)]
    roots: List[str] = []
    for d in dict.fromkeys(r for _, r in accepted):
        for target in _symlink_targets_in(d):
            if _is_within(target, d) or _is_within_any(target, covered_roots):
                continue
            root = _install_root_of(target, home)
            if refused(root) or os.path.exists(posixpath.join(root, ".git")):
                continue
            roots.append(root)
    return HostPathToolchain(
        path_entries=[e for e, _ in accepted],
        readable_dirs=_outermost([r for _, r in uncovered] + roots),
        symlinks=[(e, r) for e, r in uncovered if r != e and not _is_within_any(e, covered_roots)],
    )


def host_path_toolchain_from_env(covered_roots: Sequence[str], denied_roots: Sequence[str]) -> HostPathToolchain:
    return resolve_host_path_toolchain(os.environ.get("PATH", ""), os.path.expanduser("~"), covered_roots, denied_roots)


def copy_creating_parents(src: str, dst: str) -> None:
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
    except OSError as e:
        raise CopyError(f"Couldn't copy {src} to {dst}: {e}") from e


def register_native_sandbox_shutdown(kill_all: Callable[[], None], root: str) -> Callable[[], None]:
    def teardown() -> None:
        kill_all()
        shutil.rmtree(root, ignore_errors=True)

    return register_shutdown(teardown)

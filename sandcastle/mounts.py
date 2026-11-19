"""Mount utilities. Port of ``mountUtils.ts``, ``mountPaths.ts``,
``gitMountOverride.ts``, ``identityMounts.ts`` and ``protectGitMounts.ts``.
"""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, replace
from typing import Callable, Dict, Iterable, List, Literal, Optional, Sequence, Tuple, Union

from .errors import WorktreeError
from .sandbox_provider import Mount, MountConfig

#: The mount point inside the sandbox where the project worktree is bound.
SANDBOX_REPO_DIR = "/home/agent/workspace"

#: Mount point of the parent repo's .git dir on Windows hosts (ADR-0006).
PARENT_GIT_SANDBOX_DIR = "/.sandcastle-parent-git"

SelinuxLabel = Union[Literal["z", "Z"], bool]


@dataclass
class MountEntry:
    """A git mount for a bind-mount sandbox."""

    host_path: str
    sandbox_path: str
    readonly: bool = False
    #: A temp dir holding ``host_path``, removed when the sandbox closes.
    temp_dir: Optional[str] = None


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def expand_tilde(p: str, home: Optional[str] = None) -> str:
    h = home if home is not None else os.path.expanduser("~")
    if p == "~":
        return h
    if p.startswith("~/") or p.startswith("~\\"):
        return f"{h}/{p[2:]}"
    return p


def resolve_host_path(host_path: str) -> str:
    expanded = expand_tilde(host_path)
    return expanded if os.path.isabs(expanded) else os.path.abspath(os.path.join(os.getcwd(), expanded))


def resolve_sandbox_path(sandbox_path: str, sandbox_homedir: Optional[str] = None) -> str:
    has_tilde = sandbox_path == "~" or sandbox_path.startswith("~/") or sandbox_path.startswith("~\\")
    if has_tilde and sandbox_homedir is None:
        raise ValueError(f'sandboxPath "{sandbox_path}" contains a tilde but the provider has no sandboxHomedir set')
    expanded = expand_tilde(sandbox_path, sandbox_homedir) if has_tilde else sandbox_path
    if os.path.isabs(expanded) or expanded.startswith("/"):
        return posixpath.normpath(expanded) if expanded != "/" else "/"
    return posixpath.normpath(posixpath.join(SANDBOX_REPO_DIR, expanded))


def default_image_name(repo_dir: str) -> str:
    """``sandcastle:<dir-name>``, lowercased and sanitized for tag rules."""
    stripped = re.sub(r"[\\/]+$", "", repo_dir)
    dir_name = re.split(r"[\\/]", stripped)[-1] if stripped else "local"
    out = []
    for ch in dir_name.lower():
        if re.match(r"[a-z0-9_.-]", ch):
            out.append(ch)
        else:
            out.append("-" * (2 if ord(ch) > 0xFFFF else 1))
    return f"sandcastle:{''.join(out) or 'local'}"


def _as_mount_config(m: Union[MountConfig, Dict]) -> MountConfig:
    if isinstance(m, MountConfig):
        return m
    return MountConfig(
        host_path=m.get("host_path", m.get("hostPath")),
        sandbox_path=m.get("sandbox_path", m.get("sandboxPath")),
        readonly=bool(m.get("readonly", False)),
    )


def resolve_user_mounts(mounts: Sequence[Union[MountConfig, Dict]], sandbox_homedir: Optional[str] = None) -> List[Mount]:
    out: List[Mount] = []
    for raw in mounts:
        m = _as_mount_config(raw)
        resolved = resolve_host_path(m.host_path)
        if not os.path.exists(resolved):
            extra = "" if m.host_path == resolved else f" (resolved to {resolved})"
            raise ValueError(f"Mount hostPath does not exist: {m.host_path}{extra}")
        out.append(Mount(resolved, resolve_sandbox_path(m.sandbox_path, sandbox_homedir), m.readonly is True))
    return out


@dataclass(frozen=True)
class IdentityMount:
    host_path: str
    readonly: bool


def resolve_identity_mounts(provider: str, mounts: Sequence[Union[MountConfig, Dict]], sandbox_homedir: str) -> List[IdentityMount]:
    """Native providers: each mount appears at its own host path."""
    resolved = resolve_user_mounts(mounts, sandbox_homedir)
    out = []
    for i, r in enumerate(resolved):
        if r.sandbox_path != r.host_path:
            given = _as_mount_config(mounts[i])
            raise ValueError(
                f"{provider}() mounts appear at their host path, so sandboxPath must equal hostPath: "
                f"got hostPath {given.host_path} (resolved to {r.host_path}) and sandboxPath "
                f"{given.sandbox_path} (resolved to {r.sandbox_path})."
            )
        out.append(IdentityMount(r.host_path, r.readonly))
    return out


def normalize_mounts(mounts: List, worktree_host_path: str, sandbox_repo_dir: str, platform: str = sys.platform) -> List:
    """On Windows, use forward slashes and remap Windows-style sandbox paths
    under the worktree to ``sandbox_repo_dir``. Elsewhere, unchanged."""
    if platform != "win32":
        return mounts
    nw = worktree_host_path.replace("\\", "/")
    out = []
    for m in mounts:
        host = m.host_path.replace("\\", "/")
        sp = m.sandbox_path
        if re.match(r"^[A-Za-z]:[/\\]", sp) or "\\" in sp:
            ns = sp.replace("\\", "/")
            if ns.startswith(nw + "/"):
                sp = sandbox_repo_dir + ns[len(nw) :]
            elif ns == nw:
                sp = sandbox_repo_dir
            else:
                sp = ns
        out.append(replace(m, host_path=host, sandbox_path=sp))
    return out


def format_volume_mount(mount, selinux_label: Optional[SelinuxLabel]) -> str:
    """``host:sandbox[:ro][,z|Z]`` for ``-v``."""
    base = f"{mount.host_path}:{mount.sandbox_path}"
    opts = []
    if getattr(mount, "readonly", False):
        opts.append("ro")
    if selinux_label not in (False, None):
        opts.append(str(selinux_label))
    return f"{base}:{','.join(opts)}" if opts else base


def process_file_mount_parents(mounts: Sequence, sandbox_homedir: str, stat_fn: Callable[[str], bool] = os.path.isfile) -> List[str]:
    """Sandbox parent dirs of file mounts that must be created at container start.
    A file mount whose parent lies outside the sandbox home is a config error."""
    parents: List[str] = []
    for m in mounts:
        try:
            is_file = stat_fn(m.host_path)
        except OSError:
            continue
        if not is_file:
            continue
        parent = posixpath.dirname(m.sandbox_path)
        if parent == sandbox_homedir:
            continue
        if not parent.startswith(sandbox_homedir + "/"):
            raise ValueError(
                f"Cannot mount file to '{m.sandbox_path}': parent directory '{parent}' is outside the sandbox home "
                f"directory ('{sandbox_homedir}'). Mount the parent directory instead, or rebuild the image with "
                f"'{parent}' pre-created."
            )
        if parent not in parents:
            parents.append(parent)
    return parents


# ---------------------------------------------------------------------------
# Git mounts
# ---------------------------------------------------------------------------


def resolve_git_mounts(git_path: str) -> List[MountEntry]:
    """``.git`` dir: mount it. Worktree ``.git`` file: mount it and the parent ``.git``."""
    if os.path.isdir(git_path):
        return [MountEntry(git_path, git_path)]
    with open(git_path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    m = re.match(r"^gitdir:\s*(?P<gitdir>.+)$", content)
    if not m:
        return [MountEntry(git_path, git_path)]
    parent = os.path.normpath(os.path.join(m.group("gitdir"), "..", ".."))
    return [MountEntry(git_path, git_path), MountEntry(parent, parent)]


def parse_gitdir_path(gitdir_path: str) -> Tuple[str, str]:
    """``(parent_git_dir, worktree_name)`` of ``<repo>/.git/worktrees/<name>``."""
    normalized = re.sub(r"/+$", "", gitdir_path.replace("\\", "/"))
    segments = normalized.split("/")
    worktree_name = segments.pop() if segments else ""
    if segments:
        segments.pop()
    return "/".join(segments), worktree_name


def read_worktree_gitdir(git_entry_path: str) -> Optional[str]:
    if not os.path.exists(git_entry_path) or os.path.isdir(git_entry_path):
        return None
    try:
        with open(git_entry_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except OSError:
        return None
    m = re.match(r"^gitdir:\s*(?P<gitdir>.+)$", content)
    return m.group("gitdir") if m else None


def write_git_override(corrected_gitdir: str) -> MountEntry:
    try:
        temp_dir = tempfile.mkdtemp(prefix="sandcastle-git-")
    except OSError as e:
        raise WorktreeError(f"Failed to create temp dir for git override: {e}") from e
    path = os.path.join(temp_dir, "git-override")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"gitdir: {corrected_gitdir}\n")
    except OSError as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise WorktreeError(f"Failed to write git override file: {e}") from e
    return MountEntry(path, "", temp_dir=temp_dir)


def overlay_git_mounts(git_mounts: List[MountEntry], parent_git_dir: str, git_entry_path: str, override: MountEntry) -> List[MountEntry]:
    npg = parent_git_dir.replace("\\", "/")
    gfh = git_entry_path.replace("\\", "/")
    out: List[MountEntry] = []
    replaced = False
    for m in git_mounts:
        nh = m.host_path.replace("\\", "/")
        if nh == npg:
            out.append(replace(m, sandbox_path=PARENT_GIT_SANDBOX_DIR))
        elif nh == gfh:
            out.append(replace(override))
            replaced = True
        else:
            out.append(m)
    if not replaced:
        out.append(override)
    return out


def patch_git_mounts_for_windows(
    git_mounts: List[MountEntry], worktree_host_path: str, sandbox_repo_dir: str, platform: str = sys.platform
) -> List[MountEntry]:
    """On Windows, remap the parent ``.git`` and overlay a corrected ``.git``
    file so worktree pointers resolve in the Linux sandbox (ADR-0006)."""
    if platform != "win32":
        return git_mounts
    git_entry = os.path.join(worktree_host_path, ".git")
    gitdir = read_worktree_gitdir(git_entry)
    if gitdir is None:
        return git_mounts
    parent, name = parse_gitdir_path(gitdir)
    override = write_git_override(f"{PARENT_GIT_SANDBOX_DIR}/worktrees/{name}")
    override = replace(override, sandbox_path=f"{sandbox_repo_dir}/.git", readonly=True)
    return overlay_git_mounts(git_mounts, parent, git_entry, override)


def remove_mount_temp_dirs(mounts: Iterable[MountEntry]) -> None:
    for m in mounts:
        if getattr(m, "temp_dir", None):
            shutil.rmtree(m.temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Protect git control files (protectGitMounts.ts)
# ---------------------------------------------------------------------------

_GITDIR_LINE = re.compile(r"^gitdir: (?P<path>.+)$")


def _to_mount_path(p: str) -> str:
    return p.replace("\\", "/") if sys.platform == "win32" else p


def _real(p: str) -> Optional[str]:
    return os.path.realpath(p) if os.path.exists(p) else None


def _read_text(p: str) -> Optional[str]:
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _relative_within(child: str, parent: str) -> Optional[str]:
    try:
        rel = os.path.relpath(child, parent)
    except ValueError:
        return None
    if rel == ".":
        return ""
    if rel.startswith("..") or os.path.isabs(rel):
        return None
    return rel


def _common_dir_control_paths(common_dir: str) -> List[str]:
    for d in ("hooks", "info"):
        try:
            os.makedirs(os.path.join(common_dir, d), exist_ok=True)
        except OSError:
            pass
    return [os.path.join(common_dir, n) for n in ("config", "hooks", "info")]


def _linked_worktree_control_paths(d: str, gitdir: str) -> List[str]:
    dot_git = _real(os.path.join(d, ".git"))
    admin = _real(os.path.normpath(os.path.join(d, gitdir)))
    if dot_git is None or admin is None:
        return []
    commondir_file = os.path.join(admin, "commondir")
    commondir = _read_text(commondir_file)
    if commondir is None:
        return [dot_git]
    common = _real(os.path.normpath(os.path.join(admin, commondir.strip())))
    return [dot_git, commondir_file] + (_common_dir_control_paths(common) if common else [])


def _control_paths_of(d: str) -> List[str]:
    dot_git = os.path.join(d, ".git")
    if not os.path.lexists(dot_git):
        return []
    if os.path.isdir(dot_git) and not os.path.islink(dot_git):
        real = _real(dot_git)
        return _common_dir_control_paths(real) if real else []
    pointer = _read_text(dot_git)
    if pointer is None:
        return []
    m = _GITDIR_LINE.match(pointer.strip())
    return _linked_worktree_control_paths(d, m.group("path")) if m else []


def _repos_of(mounts: Sequence, worktree_or_repo_path: str) -> List[str]:
    out = [worktree_or_repo_path]
    for m in mounts:
        if posixpath.basename(_to_mount_path(m.host_path)) == ".git":
            parent = os.path.dirname(m.host_path)
            if parent not in out:
                out.append(parent)
    return out


def protect_git_control_files(mounts: Sequence, worktree_or_repo_path: str) -> List[Mount]:
    """Mount ``config``/``hooks``/``info``, the worktree ``.git`` pointer and
    its ``commondir`` read-only at every sandbox path they are visible at, and
    bind the dirs between each and its writable mount onto themselves."""
    control: List[str] = []
    for repo in _repos_of(mounts, worktree_or_repo_path):
        for p in _control_paths_of(repo):
            if p not in control:
                control.append(p)
    with_real = [(m, _real(m.host_path)) for m in mounts]
    result: List[Mount] = [Mount(m.host_path, m.sandbox_path, bool(getattr(m, "readonly", False))) for m in mounts]
    anchors: Dict[str, Mount] = {}
    for cp in control:
        for m, real in with_real:
            if real is None:
                continue
            rel = _relative_within(cp, real)
            if rel is None:
                continue
            if not getattr(m, "readonly", False):
                parts = rel.split(os.sep) if rel else []
                for i in range(len(parts) - 1):
                    sub = os.sep.join(parts[: i + 1])
                    a = Mount(_to_mount_path(os.path.join(real, sub)), posixpath.join(m.sandbox_path, _to_mount_path(sub)))
                    anchors[a.sandbox_path] = a
            sp = m.sandbox_path if rel == "" else posixpath.join(m.sandbox_path, _to_mount_path(rel))
            if any(r.sandbox_path == sp for r in result):
                result = [Mount(r.host_path, r.sandbox_path, True) if r.sandbox_path == sp else r for r in result]
            else:
                result.append(Mount(_to_mount_path(cp), sp, True))
    taken = {r.sandbox_path for r in result}
    n = len(mounts)
    return result[:n] + [a for a in anchors.values() if a.sandbox_path not in taken] + result[n:]

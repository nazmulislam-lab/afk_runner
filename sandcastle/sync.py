"""Git sync between the host and an isolated sandbox.

Port of ``syncIn.ts``, ``syncOut.ts``, ``syncOutExec.ts``,
``syncOutInspect.ts``, ``syncOutSave.ts``, ``syncOutPaths.ts``,
``syncOutPatchSymlinks.ts``, ``syncOutRefusal.ts``, ``patchFileChanges.ts``
and ``RecoveryMessage.ts``.

Sync-in: ``git bundle create --all`` on the host, ``copy_in`` the bundle,
``git clone`` it in the sandbox, verify HEAD.

Sync-out is two-phase. Phase 1 saves every artifact (patches, diff,
untracked files) to ``.sandcastle/patches/<timestamp>/``; phase 2 applies
from there (``git am --3way``, ``git apply``, copy). On failure the patch
dir is kept and recovery commands are printed. Everything the sandbox
reports is untrusted: every host destination must stay inside the worktree
and out of its ``.git`` (:class:`SyncOutPathError` otherwise).
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence

from .errors import HostGitError, SyncError, SyncOutPathError
from .host_git import host_git
from .session_store import json_dumps

#: Sandbox-owned ref tracking the last-synced commit (ADR 0017).
SYNC_BASE_REF = "refs/sandcastle/sync-base"

FailedStep = Literal["commits", "diff", "untracked"]

# ---------------------------------------------------------------------------
# exec helpers
# ---------------------------------------------------------------------------


async def exec_host_git(args: Sequence[str], cwd: str) -> str:
    try:
        return await host_git(list(args), cwd)
    except HostGitError as e:
        raise SyncError(f"Host command failed: git {' '.join(args)}\n{e.message}") from e


async def exec_sandbox(handle: Any, command: str, cwd: Optional[str] = None):
    try:
        return await handle.exec(command, cwd=cwd)
    except Exception as e:  # noqa: BLE001
        raise SyncError(f"Sandbox exec failed: {command}\n{e}") from e


async def exec_ok(handle: Any, command: str, cwd: Optional[str] = None):
    result = await exec_sandbox(handle, command, cwd)
    if result.exit_code != 0:
        raise SyncError(f"Sandbox command failed (exit {result.exit_code}): {command}\n{result.stderr}")
    return result


# ---------------------------------------------------------------------------
# Sync-in
# ---------------------------------------------------------------------------


async def sync_in(host_repo_dir: str, handle: Any) -> str:
    """Clone the host repo into the isolated sandbox. Returns the branch."""
    branch = (await exec_host_git(["rev-parse", "--abbrev-ref", "HEAD"], host_repo_dir)).strip()
    try:
        bundle_dir = tempfile.mkdtemp(prefix="sandcastle-bundle-")
    except OSError as e:
        raise SyncError(f"Failed to create temp dir: {e}") from e
    try:
        bundle_host = os.path.join(bundle_dir, "repo.bundle")
        await exec_host_git(["bundle", "create", bundle_host, "--all"], host_repo_dir)
        tmp = (await exec_ok(handle, "mktemp -d -t sandcastle-XXXXXX")).stdout.strip()
        bundle_sandbox = f"{tmp}/repo.bundle"
        try:
            await handle.copy_in(bundle_host, bundle_sandbox)
        except Exception as e:  # noqa: BLE001
            raise SyncError(f"Failed to copy bundle into sandbox: {e}") from e
        wt = handle.worktree_path
        await exec_ok(handle, f'git clone "{bundle_sandbox}" "{wt}_clone"')
        await exec_ok(handle, f'rm -rf "{wt}" && mv "{wt}_clone" "{wt}"')
        await exec_ok(handle, f'git checkout "{branch}"', cwd=wt)
        try:
            await handle.exec(f'rm -rf "{tmp}"')
        except Exception as e:  # noqa: BLE001
            raise SyncError("Failed to clean up sandbox temp dir") from e
        host_head = (await exec_host_git(["rev-parse", "HEAD"], host_repo_dir)).strip()
        sandbox_head = (await exec_ok(handle, "git rev-parse HEAD", cwd=wt)).stdout.strip()
        if host_head != sandbox_head:
            raise SyncError(f"HEAD mismatch after sync-in: host={host_head} sandbox={sandbox_head}")
    finally:
        shutil.rmtree(bundle_dir, ignore_errors=True)
    return branch


# ---------------------------------------------------------------------------
# Inspect
# ---------------------------------------------------------------------------


async def count_commits_to_sync(sandbox: Any, cwd: str, host_head: str) -> int:
    """Commits sync-out would emit; degrades to 0 on any failure."""
    try:
        base_res = await sandbox.exec(f"git rev-parse --verify --quiet {SYNC_BASE_REF}", cwd=cwd)
        base = base_res.stdout.strip() if base_res.exit_code == 0 and base_res.stdout.strip() else host_head
    except Exception:  # noqa: BLE001
        base = host_head
    try:
        res = await sandbox.exec(f'git rev-list "{base}..HEAD" --count', cwd=cwd)
    except Exception:  # noqa: BLE001
        return 0
    if res.exit_code != 0:
        return 0
    try:
        return int(res.stdout.strip())
    except ValueError:
        return 0


@dataclass
class SandboxChanges:
    base: str
    sandbox_head: str
    has_commits: bool
    has_diff: bool
    diff: str
    has_untracked: bool
    untracked_files: List[str]


async def inspect_sandbox(host_repo_dir: str, handle: Any) -> SandboxChanges:
    wt = handle.worktree_path
    host_head = (await exec_host_git(["rev-parse", "HEAD"], host_repo_dir)).strip()
    sandbox_head = (await exec_ok(handle, "git rev-parse HEAD", cwd=wt)).stdout.strip()
    base_res = await exec_sandbox(handle, f"git rev-parse --verify --quiet {SYNC_BASE_REF}", cwd=wt)
    base = base_res.stdout.strip() if base_res.exit_code == 0 and base_res.stdout.strip() else host_head
    diff_res = await exec_sandbox(handle, "git diff HEAD", cwd=wt)
    ut = await exec_sandbox(handle, "git ls-files --others --exclude-standard", cwd=wt)
    untracked = (
        [f for f in ut.stdout.strip().split("\n") if f] if ut.exit_code == 0 and ut.stdout.strip() else []
    )
    return SandboxChanges(
        base=base,
        sandbox_head=sandbox_head,
        has_commits=base != sandbox_head,
        has_diff=diff_res.exit_code == 0 and bool(diff_res.stdout.strip()),
        diff=diff_res.stdout,
        has_untracked=bool(untracked),
        untracked_files=untracked,
    )


# ---------------------------------------------------------------------------
# Path refusal (syncOutRefusal.ts / syncOutPaths.ts)
# ---------------------------------------------------------------------------


def refusal(path: str, reason: str) -> SyncOutPathError:
    return SyncOutPathError(f"Refusing to sync {json_dumps(path)} out of the sandbox: {reason}", path=path)


def is_git_segment(segment: str) -> bool:
    """``.git``, including the spellings Windows treats as the same name."""
    return re.sub(r"[. ]+$", "", segment.lower()) == ".git"


def is_absolute_anywhere(p: str) -> bool:
    return posixpath.isabs(p) or ntpath.isabs(p) or re.match(r"^[a-z]:", p, re.I) is not None or p.startswith("\\")


def _split_segments(path: str) -> List[str]:
    return [s for s in re.split(r"[\\/]", path) if s not in ("", ".")]


def check_relative_path(path: str) -> List[str]:
    segments = _split_segments(path)
    problem = None
    if "\0" in path:
        problem = "it contains a NUL byte"
    elif is_absolute_anywhere(path):
        problem = "it is an absolute path"
    elif ".." in segments:
        problem = "it has a '..' segment"
    elif any(is_git_segment(s) for s in segments):
        problem = "it is inside .git"
    elif not segments:
        problem = "it is empty"
    if problem:
        raise refusal(path, problem)
    return segments


def check_patch_name(name: str) -> str:
    if re.search(r"[\\/\0]", name) or name in (".", "..", ""):
        raise refusal(name, "a patch name must be a single file name")
    return name


def real_root_of(d: str) -> str:
    if not os.path.exists(d):
        raise refusal(d, "the host worktree can't be resolved")
    return os.path.realpath(d)


def _deepest_existing(root: str, segments: Sequence[str]) -> Optional[str]:
    for depth in range(len(segments), 0, -1):
        candidate = os.path.join(root, *segments[:depth])
        if os.path.lexists(candidate):
            return candidate
    return None


def _is_outside(rel: str) -> bool:
    return rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel)


def resolve_host_destination(real_root: str, rel_path: str) -> str:
    """The host path to write ``rel_path`` to; refuses symlinks that lead out
    of the worktree, nowhere, or into ``.git``."""
    segments = check_relative_path(rel_path)
    existing = _deepest_existing(real_root, segments)
    if existing is not None:
        if not os.path.exists(existing):
            raise refusal(rel_path, f"{existing} is a symlink to nowhere")
        real = os.path.realpath(existing)
        rel = os.path.relpath(real, real_root)
        if _is_outside(rel):
            raise refusal(rel_path, f"{existing} leads outside the worktree")
        if any(is_git_segment(s) for s in _split_segments(rel)):
            raise refusal(rel_path, f"{existing} leads inside .git")
    return os.path.join(real_root, *segments)


def check_staged_file(staged: str, rel_path: str) -> None:
    if not (os.path.lexists(staged) and os.path.isfile(staged) and not os.path.islink(staged)):
        raise refusal(rel_path, "it is not a regular file")


def host_symlink_at(real_root: str) -> Callable[[str], Optional[str]]:
    def lookup(rel_path: str) -> Optional[str]:
        p = os.path.join(real_root, *_split_segments(rel_path))
        if os.path.islink(p):
            try:
                return os.readlink(p)
            except OSError:
                return None
        return None

    return lookup


# ---------------------------------------------------------------------------
# Patch parsing (patchFileChanges.ts)
# ---------------------------------------------------------------------------


@dataclass
class FileChange:
    symlink: bool = False
    mode_only: bool = False
    binary: bool = False
    deleted: bool = False
    path: Optional[str] = None
    old_path: Optional[str] = None
    from_: Optional[str] = None
    in_hunk: bool = False
    new_lines: List[str] = field(default_factory=list)


_C_ESCAPES = {"a": "\x07", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}


def _unquote(raw: str) -> str:
    if len(raw) < 2 or not raw.startswith('"') or not raw.endswith('"'):
        return raw

    def repl(m: "re.Match[str]") -> str:
        e = m.group(1)
        if re.fullmatch(r"[0-7]{3}", e):
            return chr(int(e, 8))
        return _C_ESCAPES.get(e, e)

    latin1 = re.sub(r"\\([0-7]{3}|.)", repl, raw[1:-1])
    try:
        return latin1.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return latin1


def _strip_prefix(raw: str) -> str:
    path = _unquote(raw.strip())
    slash = path.find("/")
    return path if slash == -1 else path[slash + 1 :]


def _apply_header_line(c: FileChange, line: str) -> None:
    if re.match(r"^new file mode 120000$", line):
        c.symlink = True
    elif re.match(r"^new mode 120000$", line):
        c.symlink = True
        c.mode_only = True
    elif re.match(r"^index [0-9a-f]+\.\.[0-9a-f]+ 120000$", line):
        c.symlink = True
    elif line.startswith("deleted file mode "):
        c.deleted = True
    elif (m := re.match(r"^\+\+\+ (?P<p>.*)$", line)) is not None:
        raw = m.group("p")
        if raw.strip() == "/dev/null":
            c.deleted = True
        else:
            c.path = _strip_prefix(raw)
    elif (m := re.match(r"^--- (?P<p>.*)$", line)) is not None:
        raw = m.group("p")
        c.old_path = None if raw.strip() == "/dev/null" else _strip_prefix(raw)
    elif (m := re.match(r"^(?:rename|copy) to (?P<p>.*)$", line)) is not None:
        c.path = _unquote(m.group("p"))
    elif (m := re.match(r"^(?:rename|copy) from (?P<p>.*)$", line)) is not None:
        c.from_ = _unquote(m.group("p"))
    elif re.match(r"^(?:GIT binary patch$|Binary files )", line):
        c.binary = True
    elif line.startswith("@@"):
        c.in_hunk = True
        c.mode_only = False


def _apply_hunk_line(c: FileChange, line: str) -> bool:
    if re.match(r"^[+ ]", line):
        c.new_lines.append(line[1:])
    return re.match(r"^(?:[+\- \\]|@@)", line) is not None


def parse_patch(patch: str) -> List[FileChange]:
    changes: List[FileChange] = []
    current: Optional[FileChange] = None
    for raw in patch.split("\n"):
        line = raw[:-1] if raw.endswith("\r") else raw
        if line.startswith("diff --git "):
            current = FileChange()
            changes.append(current)
        elif current is not None:
            still = current.in_hunk and _apply_hunk_line(current, line)
            if not still:
                current.in_hunk = False
                _apply_header_line(current, line)
    return changes


# ---------------------------------------------------------------------------
# Patch symlink checks (syncOutPatchSymlinks.ts)
# ---------------------------------------------------------------------------

_UNSET = object()


def _check_symlink_target(link_path: str, target: str) -> None:
    if target == "" or "\0" in target:
        raise refusal(link_path, "it is a symlink with no target")
    if is_absolute_anywhere(target):
        raise refusal(link_path, f"it is a symlink to {target}")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(link_path), target.replace("\\", "/")))
    if resolved == ".." or resolved.startswith("../"):
        raise refusal(link_path, f"it is a symlink to {target}, outside the worktree")
    if any(is_git_segment(s) for s in resolved.split("/")):
        raise refusal(link_path, f"it is a symlink to {target}, in .git")


def check_patch_symlinks(patches: Sequence[str], host_lookup: Callable[[str], Optional[str]]) -> None:
    made: Dict[str, Optional[str]] = {}

    def symlink_at(rel: str) -> Optional[str]:
        return made[rel] if rel in made else host_lookup(rel)

    for patch in patches:
        for change in parse_patch(patch):
            path = (change.path or change.old_path) if change.deleted else change.path
            if path is None:
                if change.symlink:
                    raise refusal("(unknown)", "a symlink with no path")
                continue
            if change.deleted:
                made[path] = None
                continue
            keeps_type = not change.symlink and not change.new_lines
            if not change.symlink and not (keeps_type and change.from_ is not None):
                made[path] = None
                continue
            if change.binary or change.mode_only:
                raise refusal(path, "a symlink whose target can't be checked")
            if change.new_lines:
                target = change.new_lines[0] if len(change.new_lines) == 1 else None
            else:
                target = None if change.from_ is None else symlink_at(change.from_)
            if target is None:
                if change.symlink:
                    raise refusal(path, "a symlink whose target can't be checked")
                made[path] = None
                continue
            _check_symlink_target(path, target)
            made[path] = target


def check_patch_file_symlinks(paths: Sequence[str], host_lookup: Callable[[str], Optional[str]]) -> None:
    patches = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="surrogateescape") as f:
                patches.append(f.read())
        except OSError:
            raise refusal(p, "the patch can't be read") from None
    check_patch_symlinks(patches, host_lookup)


# ---------------------------------------------------------------------------
# Recovery message (RecoveryMessage.ts)
# ---------------------------------------------------------------------------


def _format_command_block(commands: Sequence[str]) -> str:
    if len(commands) == 1:
        return f"  {commands[0]}"
    return "\n".join(f"  {c} && \\" if i < len(commands) - 1 else f"  {c}" for i, c in enumerate(commands))


def _remaining_commands(patch_dir: str, steps: Sequence[Dict[str, Any]]) -> List[str]:
    out = []
    for s in steps:
        if not s["has"]:
            continue
        if s["key"] == "commits":
            out.append(f"git am --3way {patch_dir}/*.patch")
        elif s["key"] == "diff":
            out.append(f"git apply {patch_dir}/changes.patch")
        elif s["key"] == "untracked":
            out.append(f"cp -r {patch_dir}/untracked/* .")
    return out


def build_recovery_message(
    *,
    patch_dir: str,
    failed_step: str,
    has_commits: bool,
    has_diff: bool,
    has_untracked: bool,
    branch: Optional[str] = None,
) -> str:
    """Copy-pastable recovery commands for a failed sync-out."""
    has_branch = bool(branch)
    cmd_dir = f"../../{patch_dir}" if has_branch else patch_dir
    steps = [
        s
        for s in (
            {"key": "commits", "label": "committed changes", "has": has_commits},
            {"key": "diff", "label": "uncommitted changes", "has": has_diff},
            {"key": "untracked", "label": "untracked files", "has": has_untracked},
        )
        if s["has"]
    ]
    idx = next((i for i, s in enumerate(steps) if s["key"] == failed_step), -1)
    if idx == -1:
        raise ValueError(f'Recovery step "{failed_step}" failed, but it had no changes to apply.')
    preamble = (
        [
            "Set up worktree, then resolve:",
            _format_command_block([f"git worktree add .sandcastle/worktree {branch}", "cd .sandcastle/worktree"]),
            "",
        ]
        if has_branch
        else []
    )
    if failed_step == "commits":
        remaining = _remaining_commands(cmd_dir, steps[idx + 1 :])
        resolution = ["Resolve conflicts, then continue with:", "  git am --continue"]
        if remaining:
            resolution += ["", "After all commits are applied, run the remaining steps:", _format_command_block(remaining)]
    else:
        remaining = _remaining_commands(cmd_dir, steps[idx:])
        resolution = ["Run the remaining steps:", _format_command_block(remaining)] if remaining else []
    return "\n".join([f"Patch application failed at step {idx + 1} ({steps[idx]['label']}).", "", *preamble, *resolution])


# ---------------------------------------------------------------------------
# Sync-out
# ---------------------------------------------------------------------------


@dataclass
class SyncRun:
    host_repo_dir: str
    handle: Any
    real_root: str
    changes: SandboxChanges
    untracked_segments: List[List[str]]
    patch_dir: str
    untracked_dir: str

    @property
    def diff_path(self) -> str:
        return os.path.join(self.patch_dir, "changes.patch")

    def staged_untracked_path(self, i: int) -> str:
        return os.path.join(self.untracked_dir, *self.untracked_segments[i])


def create_patch_dir(host_repo_dir: str) -> str:
    try:
        base = datetime.now().strftime("%Y%m%d-%H%M%S")
        root = os.path.join(host_repo_dir, ".sandcastle", "patches")
        os.makedirs(root, exist_ok=True)
        taken = set(os.listdir(root))
        name, counter = base, 0
        while name in taken:
            counter += 1
            name = f"{base}-{counter}"
        d = os.path.join(root, name)
        os.makedirs(d, exist_ok=True)
        return d
    except OSError as e:
        raise SyncError(f"Failed to create patch directory: {e}") from e


def _is_empty_patch(path: str) -> bool:
    try:
        if os.path.getsize(path) == 0:
            return True
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "diff --git" not in f.read()
    except OSError as e:
        raise SyncError(f"Failed to check patch {path}: {e}") from e


async def _save_commit_patches(run: SyncRun) -> List[str]:
    h = run.handle
    sandbox_patch_dir = (await exec_ok(h, "mktemp -d -t sandcastle-patches-XXXXXX")).stdout.strip()
    non_empty: List[str] = []
    try:
        await exec_ok(h, f'git format-patch "{run.changes.base}..HEAD" -o "{sandbox_patch_dir}"', cwd=h.worktree_path)
        ls = await exec_ok(h, f'ls -1 "{sandbox_patch_dir}"')
        for name in [n for n in ls.stdout.strip().split("\n") if n]:
            check_patch_name(name)
            host_patch = os.path.join(run.patch_dir, name)
            try:
                await h.copy_file_out(f"{sandbox_patch_dir}/{name}", host_patch)
            except Exception as e:  # noqa: BLE001
                raise SyncError(f"Failed to copy patch {name}: {e}") from e
            if not _is_empty_patch(host_patch):
                non_empty.append(host_patch)
    finally:
        try:
            await exec_sandbox(h, f'rm -rf "{sandbox_patch_dir}"')
        except SyncError:
            pass
    return non_empty


def _check_untracked_destinations(run: SyncRun) -> List[str]:
    out = []
    for i, rel in enumerate(run.changes.untracked_files):
        check_staged_file(run.staged_untracked_path(i), rel)
        out.append(resolve_host_destination(run.real_root, rel))
    return out


async def _save_and_check(run: SyncRun) -> List[str]:
    non_empty = await _save_commit_patches(run) if run.changes.has_commits else []
    if run.changes.has_diff:
        try:
            with open(run.diff_path, "w", encoding="utf-8") as f:
                f.write(run.changes.diff)
        except OSError as e:
            raise SyncError(f"Failed to write diff patch: {e}") from e
    if run.changes.has_untracked:
        for i, rel in enumerate(run.changes.untracked_files):
            dest = run.staged_untracked_path(i)
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                await run.handle.copy_file_out(f"{run.handle.worktree_path}/{rel}", dest)
            except Exception as e:  # noqa: BLE001
                raise SyncError(f"Failed to save untracked file {rel}: {e}") from e
    _check_untracked_destinations(run)
    check_patch_file_symlinks(
        [*non_empty, *([run.diff_path] if run.changes.has_diff else [])], host_symlink_at(run.real_root)
    )
    return non_empty


async def _failed(coro) -> bool:
    try:
        await coro
        return False
    except SyncError:
        return True


async def _apply_saved(run: SyncRun, non_empty: List[str]) -> Optional[str]:
    hr = run.host_repo_dir
    if non_empty:
        await _failed(exec_host_git(["am", "--abort"], hr))
        if await _failed(exec_host_git(["am", "--3way", *non_empty], hr)):
            return "commits"
    if run.changes.has_diff and await _failed(exec_host_git(["apply", run.diff_path], hr)):
        return "diff"
    if run.changes.has_untracked:
        destinations = _check_untracked_destinations(run)
        try:
            for i, dest in enumerate(destinations):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copyfile(run.staged_untracked_path(i), dest)
        except OSError:
            return "untracked"
    return None


def _remove_patch_dir(host_repo_dir: str, patch_dir: str) -> None:
    try:
        shutil.rmtree(patch_dir, ignore_errors=True)
        root = os.path.join(host_repo_dir, ".sandcastle", "patches")
        try:
            if not os.listdir(root):
                shutil.rmtree(root, ignore_errors=True)
        except OSError:
            pass
    except OSError as e:
        raise SyncError("Failed to clean up patch directory") from e


async def sync_out(host_repo_dir: str, handle: Any) -> None:
    """Sync changes from an isolated sandbox back to the host worktree."""
    created: List[str] = []
    try:
        real_root = real_root_of(host_repo_dir)
        changes = await inspect_sandbox(host_repo_dir, handle)
        if not changes.has_commits and not changes.has_diff and not changes.has_untracked:
            return
        segments = [check_relative_path(p) for p in changes.untracked_files]
        patch_dir = create_patch_dir(host_repo_dir)
        created.append(patch_dir)
        resolve_host_destination(real_root, os.path.relpath(patch_dir, host_repo_dir))
        run = SyncRun(
            host_repo_dir, handle, real_root, changes, segments, patch_dir, os.path.join(patch_dir, "untracked")
        )
        non_empty = await _save_and_check(run)
        failed_step = await _apply_saved(run, non_empty)
        if changes.has_commits and failed_step != "commits":
            await exec_ok(handle, f"git update-ref {SYNC_BASE_REF} {changes.sandbox_head}", cwd=handle.worktree_path)
        if failed_step:
            msg = build_recovery_message(
                patch_dir=os.path.join(".sandcastle", "patches", os.path.basename(patch_dir)),
                failed_step=failed_step,
                has_commits=bool(non_empty),
                has_diff=changes.has_diff,
                has_untracked=changes.has_untracked,
            )
            print(f"\n{msg}", file=sys.stderr)
        else:
            _remove_patch_dir(host_repo_dir, patch_dir)
    except SyncOutPathError:
        if created:
            try:
                _remove_patch_dir(host_repo_dir, created[0])
            except SyncError:
                pass
        raise

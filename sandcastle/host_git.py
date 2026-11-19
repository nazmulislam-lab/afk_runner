"""The single choke point for running ``git`` on the host.

Port of ``hostGit.ts``, ``hostGitConfig.ts``, ``hostGitDir.ts`` and
``hostGitPath.ts``.

Bind-mount sandbox providers share the host repo's ``.git`` with the sandbox,
so a sandboxed agent can write repository config, hooks and the worktree's
``.git`` pointer. Several config keys name a program for git to run, so an
unguarded host-side ``git status`` would run whatever the agent put there,
outside the sandbox. Every host-side git call goes through :func:`host_git`,
which:

1. Checks the working directory's ``.git`` before running anything (a linked
   worktree must have git's layout; a worktree under
   ``.sandcastle/worktrees/`` must belong to the repo it sits in) and pins
   ``GIT_DIR`` / ``GIT_COMMON_DIR`` / ``GIT_WORK_TREE``.
2. Overrides every config key that runs a program through ``GIT_CONFIG_*``
   env vars, keeping only system/global values.
3. Drops relative ``PATH`` entries and entries inside the repo, spawns git
   by absolute path, and restricts ``GIT_ALLOW_PROTOCOL``.
4. Spawns git with an argv (no shell) in the C locale, prompts off.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import HostGitError

Env = Dict[str, str]

# ---------------------------------------------------------------------------
# Config hardening (hostGitConfig.ts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitConfigEntry:
    scope: str
    key: str
    #: ``None`` for a bare boolean key (``[core] bare``).
    value: Optional[str]


TRUSTED_SCOPES = {"system", "global", "command"}

PINNED_GROUPS: Sequence[Tuple[Sequence[str], str]] = (
    (("core.fsmonitor",), "false"),
    (("core.hookspath",), os.devnull),
    (("core.pager",), "cat"),
    (("core.editor",), "true"),
    (("sequence.editor",), "true"),
    (("diff.external",), ""),
    (("gpg.program", "gpg.openpgp.program"), "gpg"),
    (("gpg.x509.program",), "gpgsm"),
    (("gpg.ssh.program",), "ssh-keygen"),
    (("protocol.ext.allow",), "never"),
    (("submodule.recurse",), "false"),
    (("fetch.recursesubmodules",), "false"),
)

REPO_SET_KEYS: Mapping[str, str] = {
    "core.sshcommand": "ssh",
    "core.askpass": "",
    "core.alternaterefscommand": "",
    "gpg.ssh.defaultkeycommand": "",
}

NAMED_KEYS: Mapping[str, str] = {
    "filter.clean": "",
    "filter.smudge": "",
    "filter.process": "",
    "filter.required": "false",
    "diff.command": "",
    "diff.textconv": "",
    "merge.driver": "",
    "remote.uploadpack": "git-upload-pack",
    "remote.receivepack": "git-receive-pack",
}


def _is_trusted(e: GitConfigEntry) -> bool:
    return e.scope in TRUSTED_SCOPES


def _named_key_class(key: str) -> Optional[str]:
    first, last = key.find("."), key.rfind(".")
    if first == -1 or first == last:
        return None
    return f"{key[:first]}.{key[last + 1:]}"


def _is_credential_helper_key(key: str) -> bool:
    return key.startswith("credential.") and key.endswith(".helper")


def _last_trusted_value(entries: Sequence[GitConfigEntry], keys: Sequence[str]) -> Optional[str]:
    trusted = [e for e in entries if _is_trusted(e) and e.key in keys]
    if not trusted:
        return None
    last = trusted[-1]
    return "true" if last.value is None else last.value


def _pinned_overrides(entries: Sequence[GitConfigEntry]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for keys, neutral in PINNED_GROUPS:
        value = _last_trusted_value(entries, keys)
        value = neutral if value is None else value
        out.extend((k, value) for k in keys)
    return out


def _neutral_for_repo_set_key(key: str) -> Optional[str]:
    if key in REPO_SET_KEYS:
        return REPO_SET_KEYS[key]
    named = _named_key_class(key)
    return None if named is None else NAMED_KEYS.get(named)


def _repo_set_overrides(entries: Sequence[GitConfigEntry]) -> List[Tuple[str, str]]:
    repo_keys: List[str] = []
    for e in entries:
        if not _is_trusted(e) and e.key not in repo_keys:
            repo_keys.append(e.key)
    out: List[Tuple[str, str]] = []
    for key in repo_keys:
        neutral = _neutral_for_repo_set_key(key)
        if neutral is not None:
            trusted = _last_trusted_value(entries, [key])
            out.append((key, neutral if trusted is None else trusted))
    if "core.gitproxy" in repo_keys:
        out.append(("protocol.git.allow", "never"))
    return out


def _credential_overrides(entries: Sequence[GitConfigEntry]) -> List[Tuple[str, str]]:
    keys: List[str] = ["credential.helper"]
    for e in entries:
        if _is_credential_helper_key(e.key) and e.key not in keys:
            keys.append(e.key)
    out = [(k, "") for k in keys]
    out += [(e.key, e.value or "") for e in entries if _is_trusted(e) and e.key in keys]
    return out


def hardening_overrides(entries: Sequence[GitConfigEntry]) -> List[Tuple[str, str]]:
    """``GIT_CONFIG_*`` overrides neutralising every program-running key."""
    return _pinned_overrides(entries) + _repo_set_overrides(entries) + _credential_overrides(entries)


def parse_config_list(output: str) -> List[GitConfigEntry]:
    """Parse ``git config --list --show-scope -z``."""
    fields = output.split("\0")
    entries: List[GitConfigEntry] = []
    i = 0
    while i + 1 < len(fields):
        scope, entry = fields[i], fields[i + 1]
        nl = entry.find("\n")
        if nl == -1:
            entries.append(GitConfigEntry(scope, entry, None))
        else:
            entries.append(GitConfigEntry(scope, entry[:nl], entry[nl + 1 :]))
        i += 2
    return entries


def with_config_overrides(env: Mapping[str, str], overrides: Sequence[Tuple[str, str]]) -> Env:
    try:
        existing = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        existing = 0
    start = existing if existing > 0 else 0
    result = dict(env)
    result["GIT_CONFIG_COUNT"] = str(start + len(overrides))
    for offset, (k, v) in enumerate(overrides):
        result[f"GIT_CONFIG_KEY_{start + offset}"] = k
        result[f"GIT_CONFIG_VALUE_{start + offset}"] = v
    return result


# ---------------------------------------------------------------------------
# .git validation (hostGitDir.ts)
# ---------------------------------------------------------------------------


def _refusal(args: Sequence[str], message: str) -> HostGitError:
    return HostGitError(f"Refusing to run git on the host: {message}", args=args)


def _real(args: Sequence[str], p: str) -> str:
    try:
        if not os.path.exists(p):
            raise OSError(f"ENOENT: no such file or directory, realpath '{p}'")
        return os.path.realpath(p)
    except OSError as e:
        raise _refusal(args, f"cannot resolve {p}: {e}") from None


def _read(args: Sequence[str], p: str) -> str:
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise _refusal(args, f"cannot read {p}: {e}") from None


def _is_managed_worktree(d: str) -> bool:
    parent = os.path.dirname(os.path.abspath(d))
    return os.path.basename(parent) == "worktrees" and os.path.basename(os.path.dirname(parent)) == ".sandcastle"


_GITDIR_LINE = re.compile(r"^gitdir: (?P<path>.+)$")


def _read_linked_worktree(args: Sequence[str], d: str) -> Tuple[str, str]:
    dot_git = os.path.join(d, ".git")
    pointer = _read(args, dot_git)
    m = _GITDIR_LINE.match(pointer.strip())
    if not m:
        raise _refusal(args, f"{dot_git} is not a gitdir pointer")
    git_dir = _real(args, os.path.normpath(os.path.join(d, m.group("path"))))
    commondir = _read(args, os.path.join(git_dir, "commondir"))
    common_dir = _real(args, os.path.normpath(os.path.join(git_dir, commondir.strip())))
    layout_parent = _real(args, os.path.dirname(os.path.dirname(git_dir)))
    if os.path.basename(os.path.dirname(git_dir)) != "worktrees" or layout_parent != common_dir:
        raise _refusal(args, f"{dot_git} points at {git_dir}, which is not a worktree of {common_dir}")
    return git_dir, common_dir


def _common_dir_of(args: Sequence[str], repo_dir: str) -> str:
    dot_git = os.path.join(repo_dir, ".git")
    try:
        is_dir = os.path.isdir(dot_git) and not os.path.islink(dot_git)
        os.lstat(dot_git)
    except OSError as e:
        raise _refusal(args, f"cannot stat {dot_git}: {e}") from None
    if is_dir:
        return _real(args, dot_git)
    return _read_linked_worktree(args, repo_dir)[1]


def resolve_git_dir_pins(cwd: str, args: Sequence[str]) -> Dict[str, str]:
    """Validate ``cwd``'s ``.git`` and return the env that pins git to it."""
    dot_git = os.path.join(cwd, ".git")
    managed = _is_managed_worktree(cwd)
    try:
        st = os.lstat(dot_git)
    except OSError:
        st = None
    import stat as _stat

    if st is None or _stat.S_ISDIR(st.st_mode):
        if managed:
            raise _refusal(args, f"{dot_git} is not a worktree's .git file")
        return {}
    if not _stat.S_ISREG(st.st_mode):
        raise _refusal(args, f"{dot_git} is not a regular file")
    git_dir, common_dir = _read_linked_worktree(args, cwd)
    if managed:
        repo_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(cwd))))
        expected = _common_dir_of(args, repo_dir)
        if common_dir != expected:
            raise _refusal(args, f"{dot_git} leads to {common_dir}, not to {expected}")
    return {"GIT_DIR": git_dir, "GIT_COMMON_DIR": common_dir, "GIT_WORK_TREE": cwd}


# ---------------------------------------------------------------------------
# Trusted programs (hostGitPath.ts)
# ---------------------------------------------------------------------------

SAFE_PROTOCOLS = "file:git:http:https:ssh"


def _path_key(env: Mapping[str, str]) -> str:
    return next((k for k in env if k.upper() == "PATH"), "PATH")


def _forms_of(p: str) -> List[str]:
    resolved = os.path.abspath(p)
    try:
        if os.path.exists(p):
            return [resolved, os.path.realpath(p)]
    except OSError:
        pass
    return [resolved]


def _is_within(child: str, parent: str) -> bool:
    try:
        rel = os.path.relpath(child, parent)
    except ValueError:
        return False
    return rel == "." or (not rel.startswith("..") and not os.path.isabs(rel))


def repo_roots(cwd: str, common_dir: Optional[str]) -> List[str]:
    if common_dir is None:
        return [cwd]
    if os.path.basename(common_dir) == ".git":
        return [cwd, common_dir, os.path.dirname(common_dir)]
    return [cwd, common_dir]


def trusted_path(path_value: str, roots: Sequence[str]) -> str:
    root_forms = [f for r in roots for f in _forms_of(r)]
    kept = []
    for entry in path_value.split(os.pathsep):
        if entry == "" or not os.path.isabs(entry):
            continue
        forms = _forms_of(entry)
        if any(_is_within(f, r) for f in forms for r in root_forms):
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


def _is_executable_file(p: str) -> bool:
    return os.path.isfile(p) and os.access(p, os.X_OK)


def find_git(path_value: str, env: Mapping[str, str]) -> Optional[str]:
    exts = (
        [e for e in env.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
        if sys.platform == "win32"
        else [""]
    )
    for d in path_value.split(os.pathsep):
        for ext in exts:
            candidate = os.path.join(d, f"git{ext}")
            if _is_executable_file(candidate):
                return candidate
    return None


def with_trusted_programs(env: Mapping[str, str], roots: Sequence[str]) -> Env:
    key = _path_key(env)
    out = dict(env)
    out[key] = trusted_path(env.get(key, ""), roots)
    out["GIT_ALLOW_PROTOCOL"] = env.get("GIT_ALLOW_PROTOCOL", SAFE_PROTOCOLS)
    return out


def path_of(env: Mapping[str, str]) -> str:
    return env.get(_path_key(env), "")


# ---------------------------------------------------------------------------
# host_git
# ---------------------------------------------------------------------------

MAX_BUFFER = 10 * 1024 * 1024


@dataclass(frozen=True)
class HostGitOutput:
    stdout: str
    stderr: str


async def _spawn_git(args: Sequence[str], git: str, cwd: str, env: Mapping[str, str], input: str = "") -> HostGitOutput:
    proc = await asyncio.create_subprocess_exec(
        git,
        *args,
        cwd=cwd,
        env=dict(env),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # Not killed on cancellation: a git command stopped half-way (a merge,
    # `git am`) could leave the host repo locked or half-updated.
    out_b, err_b = await asyncio.shield(proc.communicate(input.encode("utf-8")))
    stdout = out_b.decode("utf-8", "replace")
    stderr = err_b.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise HostGitError(
            stderr.strip() or f"Command failed: git {' '.join(args)}", args=args, stdout=stdout, stderr=stderr
        )
    return HostGitOutput(stdout, stderr)


async def host_git_output(
    args: Sequence[str], cwd: str, env: Optional[Mapping[str, str]] = None, input: Optional[str] = None
) -> HostGitOutput:
    """Run ``git <args>`` on the host in ``cwd``, hardened (see module doc).
    Raises :class:`HostGitError` on refusal or non-zero exit."""
    pins = resolve_git_dir_pins(cwd, args)
    merged: Env = {**os.environ, **(env or {}), **pins, "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}
    trusted_env = with_trusted_programs(merged, repo_roots(cwd, pins.get("GIT_COMMON_DIR")))
    git = find_git(path_of(trusted_env), trusted_env)
    if git is None:
        raise HostGitError("git was not found on PATH (outside the repository)", args=args)
    try:
        listed = await _spawn_git(["config", "--list", "--show-scope", "-z"], git, cwd, trusted_env)
        listed_out = listed.stdout
    except HostGitError:
        listed_out = ""
    overrides = hardening_overrides(parse_config_list(listed_out))
    return await _spawn_git(args, git, cwd, with_config_overrides(trusted_env, overrides), input or "")


async def host_git(
    args: Sequence[str], cwd: str, env: Optional[Mapping[str, str]] = None, input: Optional[str] = None
) -> str:
    """:func:`host_git_output`, returning stdout only."""
    return (await host_git_output(args, cwd, env, input)).stdout

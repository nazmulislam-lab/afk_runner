"""Bubblewrap sandbox provider (Linux, WSL2). Port of ``sandboxes/bwrap.ts``,
``bwrapSandbox.ts``, ``bwrapPreflight.ts`` and ``bwrapProcess.ts``.

Each ``exec`` runs in a fresh Bubblewrap sandbox: host system dirs
read-only, the worktree and git dirs at their host paths, a private home at
``/home/agent`` and a private ``/tmp``, its own PID namespace, the host
network, and only the env vars sandcastle passes in. No image, no daemon.

Constructing ``bwrap()`` checks (once per process) that Bubblewrap works and
raises :class:`NativeSandboxUnavailableError` (with ``fix_command`` when there
is one) if not. It never falls back to running unsandboxed.
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Union

from ..display import MAX_TAIL_CHARS
from ..errors import BwrapError, NativeSandboxUnavailableError
from ..mounts import IdentityMount, resolve_identity_mounts
from ..sandbox_provider import BindMountCreateOptions, BindMountSandboxProvider, ExecResult, MountConfig
from ._native import copy_creating_parents, host_path_toolchain_from_env, register_native_sandbox_shutdown
from ._proc import run_process

SANDBOX_HOME = "/home/agent"
SANDBOX_TMP = "/tmp"
SYSTEM_DIRS = ["/usr", "/etc", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/opt"]
DENIED_ROOTS = ["/mnt", "/run/WSL"]
FALLBACK_PATH = "/usr/local/bin:/usr/bin:/bin"
INFO_FD = 3
WATCH_FD = 4

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

INSTALL_COMMANDS = [
    (("debian", "ubuntu"), "sudo apt install bubblewrap"),
    (("fedora", "rhel", "centos", "rocky", "almalinux", "amzn"), "sudo dnf install bubblewrap"),
    (("arch", "manjaro", "endeavouros"), "sudo pacman -S bubblewrap"),
    (("alpine",), "sudo apk add bubblewrap"),
    (("opensuse", "suse", "sles", "opensuse-leap", "opensuse-tumbleweed"), "sudo zypper install bubblewrap"),
]


def _distro_ids(os_release: str) -> List[str]:
    def field(name: str) -> str:
        m = re.search(rf"^{name}=(.*)$", os_release, re.M)
        return re.sub(r"[\"']", "", m.group(1) if m else "").strip()

    return [i.lower() for i in [field("ID"), *field("ID_LIKE").split()] if i]


def _read_os_release() -> str:
    for f in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            continue
    return ""


def _install_command(os_release: str) -> Optional[str]:
    for i in _distro_ids(os_release):
        for ids, cmd in INSTALL_COMMANDS:
            if i in ids:
                return cmd
    return None


def _not_installed() -> NativeSandboxUnavailableError:
    cmd = _install_command(_read_os_release())
    if cmd:
        return NativeSandboxUnavailableError(f"bwrap() needs Bubblewrap, and `bwrap` isn't on PATH. Install it with: {cmd}", fix_command=cmd)
    listing = "\n".join(f"  {c}" for _, c in INSTALL_COMMANDS)
    return NativeSandboxUnavailableError(
        "bwrap() needs Bubblewrap, and `bwrap` isn't on PATH. Install the `bubblewrap` package with your distro's "
        f"package manager:\n{listing}"
    )


def _userns_blocked(stderr: str) -> NativeSandboxUnavailableError:
    profile = "\\n".join(["abi <abi/4.0>,", "include <tunables/global>", "profile bwrap %s flags=(unconfined) {", "  userns,", "}"])
    fix = f"printf '{profile}\\n' \"$(command -v bwrap)\" | sudo tee /etc/apparmor.d/bwrap >/dev/null && sudo apparmor_parser -r /etc/apparmor.d/bwrap"
    return NativeSandboxUnavailableError(
        "bwrap() can't start a sandbox: this kernel blocks unprivileged user namespaces "
        "(Ubuntu 24.04+ restricts them with AppArmor). "
        f"Bubblewrap said: {stderr.strip()}\n"
        f"Fix it by giving bwrap an AppArmor profile that allows user namespaces:\n  {fix}\n"
        "Or, more bluntly, allow them for every program: sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0",
        fix_command=fix,
    )


def _probe() -> Optional[NativeSandboxUnavailableError]:
    try:
        r = subprocess.run(["bwrap", "--ro-bind", "/", "/", "true"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=30)
    except FileNotFoundError:
        return _not_installed()
    except subprocess.TimeoutExpired:
        return NativeSandboxUnavailableError("bwrap() can't start a sandbox: `bwrap --ro-bind / / true` timed out")
    if r.returncode == 0:
        return None
    stderr = r.stderr or ""
    if re.search(r"namespace|uid map", stderr, re.I):
        return _userns_blocked(stderr)
    reason = stderr.strip() or f"exit code {r.returncode}"
    return NativeSandboxUnavailableError(f"bwrap() can't start a sandbox: `bwrap --ro-bind / / true` failed: {reason}")


_outcome: List[Optional[NativeSandboxUnavailableError]] = []


def assert_bwrap_usable() -> None:
    if not _outcome:
        _outcome.append(_probe() if sys.platform.startswith("linux") else NativeSandboxUnavailableError("bwrap() needs Linux or WSL2; on macOS use seatbelt()"))
    if _outcome[0] is not None:
        raise _outcome[0]


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def _system_dir_args() -> List[str]:
    out: List[str] = []
    for d in SYSTEM_DIRS:
        if os.path.islink(d):
            out += ["--symlink", os.readlink(d), d]
        elif os.path.exists(d):
            out += ["--ro-bind", d, d]
    return out


def _resolv_conf_args() -> List[str]:
    try:
        target = os.path.realpath("/etc/resolv.conf") if os.path.exists("/etc/resolv.conf") else None
    except OSError:
        target = None
    if target is None or any(target == d or target.startswith(d + "/") for d in SYSTEM_DIRS):
        return []
    return ["--ro-bind", target, target]


def _mount_args(mounts: Sequence[Any]) -> List[str]:
    out: List[str] = []
    for m in mounts:
        out += ["--ro-bind" if getattr(m, "readonly", False) else "--bind", m.host_path, m.host_path]
    return out


def _is_within(p: str, root: str) -> bool:
    return p == root or p.startswith(root + "/")


def _anchor_args(mounts: Sequence[Any]) -> List[str]:
    writable = [m for m in mounts if not getattr(m, "readonly", False)]
    anchors: List[str] = []
    for overlay in [m for m in mounts if getattr(m, "readonly", False)]:
        roots = [m for m in writable if _is_within(overlay.host_path, m.host_path)]
        chain: List[str] = []
        d = posixpath.dirname(overlay.host_path)
        while any(d != m.host_path and _is_within(d, m.host_path) for m in roots):
            chain.insert(0, d)
            d = posixpath.dirname(d)
        for a in chain:
            if a not in anchors:
                anchors.append(a)
    out: List[str] = []
    for a in anchors:
        out += ["--bind", a, a]
    return out


def bwrap_mounts(mounts: Sequence[Union[MountConfig, Dict]]) -> List[IdentityMount]:
    return resolve_identity_mounts("bwrap", mounts, SANDBOX_HOME)


@dataclass
class PrivateDirs:
    root: str
    home: str
    tmp: str


def make_private_dirs() -> PrivateDirs:
    try:
        root = tempfile.mkdtemp(prefix="sandcastle-bwrap-")
        dirs = PrivateDirs(root, os.path.join(root, "home"), os.path.join(root, "tmp"))
        os.mkdir(dirs.home)
        os.mkdir(dirs.tmp)
        return dirs
    except OSError as e:
        raise BwrapError(f"Couldn't create the bwrap() sandbox's private dirs: {e}") from e


def sandbox_args(options: BindMountCreateOptions, dirs: PrivateDirs, user_mounts: Sequence[IdentityMount]) -> List[str]:
    tc = host_path_toolchain_from_env(SYSTEM_DIRS, DENIED_ROOTS)
    toolchain: List[str] = []
    for d in tc.readable_dirs:
        toolchain += ["--ro-bind", d, d]
    for path, target in tc.symlinks:
        toolchain += ["--symlink", target, path]
    env = {
        **options.env,
        "PATH": ":".join(tc.path_entries) if tc.path_entries else FALLBACK_PATH,
        "HOME": SANDBOX_HOME,
        "TMPDIR": SANDBOX_TMP,
    }
    env_args: List[str] = []
    for k, v in env.items():
        env_args += ["--setenv", k, v]
    return [
        *_system_dir_args(),
        *_resolv_conf_args(),
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/home",
        "--bind", dirs.home, SANDBOX_HOME,
        "--bind", dirs.tmp, SANDBOX_TMP,
        *toolchain,
        *_mount_args(options.mounts),
        *_mount_args(user_mounts),
        *_anchor_args(options.mounts),
        *_mount_args([m for m in options.mounts if m.readonly]),
        "--clearenv",
        *env_args,
        "--unshare-all",
        "--share-net",
        "--die-with-parent",
        "--new-session",
    ]


def to_host_path(path: str, dirs: PrivateDirs, mounts: Sequence[Any]) -> str:
    p = posixpath.normpath(path)
    if any(_is_within(p, m.host_path) for m in mounts):
        return p
    if _is_within(p, SANDBOX_HOME):
        return os.path.join(dirs.home, posixpath.relpath(p, SANDBOX_HOME))
    if _is_within(p, SANDBOX_TMP):
        return os.path.join(dirs.tmp, posixpath.relpath(p, SANDBOX_TMP))
    return p


def watched_command(command: str) -> List[str]:
    """Run ``command`` under a watcher that SIGKILLs the whole sandbox once
    this process dies (it reads EOF on ``WATCH_FD``)."""
    return [
        "sh",
        "-c",
        f'{{ read -r _ || :; kill -9 -1; }} <&{WATCH_FD} >/dev/null 2>&1 &\nexec {WATCH_FD}<&-\nexec sh -c "$1"',
        "sh",
        command,
    ]


_CHILD_PID = re.compile(r'"child-pid"\s*:\s*(\d+)')


def parse_child_pid(info: str) -> Optional[int]:
    m = _CHILD_PID.search(info)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Handle
# ---------------------------------------------------------------------------


class _SandboxProcess:
    def __init__(self) -> None:
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.sandbox_pid: Optional[int] = None
        self.reported: Optional[asyncio.Future] = None

    def kill(self) -> None:
        if self.sandbox_pid is not None:
            try:
                os.kill(self.sandbox_pid, signal.SIGKILL)
            except OSError:
                pass
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass


class BwrapHandle:
    def __init__(self, options: BindMountCreateOptions, user_mounts: Sequence[IdentityMount], max_tail: int) -> None:
        self.worktree_path = options.worktree_path
        self.sandbox_homedir = SANDBOX_HOME
        self.sandbox_tmpdir = SANDBOX_TMP
        self._options = options
        self._max_tail = max_tail
        self._dirs = make_private_dirs()
        self._base = sandbox_args(options, self._dirs, user_mounts)
        self._live: List[_SandboxProcess] = []
        self._closed = False
        self._unregister = register_native_sandbox_shutdown(self._kill_all, self._dirs.root)

    def _kill_all(self) -> None:
        for sp in list(self._live):
            sp.kill()

    async def exec(self, command: str, *, on_line=None, cwd: Optional[str] = None, sudo: bool = False, stdin: Optional[str] = None) -> ExecResult:
        if sudo:
            raise RuntimeError(
                "bwrap() has no root: exec with sudo: true isn't supported in a native sandbox. Use docker() for setup that needs root."
            )
        if self._closed:
            raise RuntimeError("bwrap() sandbox is closed")
        args = ["bwrap", "--info-fd", str(INFO_FD), *self._base, "--chdir", cwd or self.worktree_path, *watched_command(command)]
        info_r, info_w = os.pipe()
        watch_r, watch_w = os.pipe()

        def preexec() -> None:
            os.dup2(info_w, INFO_FD)
            os.dup2(watch_r, WATCH_FD)

        sp = _SandboxProcess()
        loop = asyncio.get_running_loop()

        def read_info() -> str:
            chunks = []
            with os.fdopen(info_r, "rb", closefd=True) as f:
                while True:
                    b = f.read(4096)
                    if not b:
                        break
                    chunks.append(b)
            return b"".join(chunks).decode("utf-8", "replace")

        def on_spawn(proc: "asyncio.subprocess.Process") -> None:
            sp.proc = proc
            os.close(info_w)
            os.close(watch_r)

            async def record() -> None:
                text = await loop.run_in_executor(None, read_info)
                sp.sandbox_pid = parse_child_pid(text)

            sp.reported = asyncio.ensure_future(record())

        self._live.append(sp)
        try:
            return await run_process(
                args,
                stdin=stdin if stdin is not None else "",
                on_line=on_line,
                max_tail_chars=self._max_tail,
                preexec_fn=preexec,
                pass_fds=(INFO_FD, WATCH_FD),
                on_spawn=on_spawn,
            )
        except asyncio.CancelledError:
            if sp.reported is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(sp.reported), 2)
                except Exception:  # noqa: BLE001
                    pass
            sp.kill()
            raise
        finally:
            if sp in self._live:
                self._live.remove(sp)
            try:
                os.close(watch_w)
            except OSError:
                pass
            if sp.proc is None:
                for fd in (info_r, info_w, watch_r):
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def _host_path_of(self, p: str) -> str:
        return to_host_path(p, self._dirs, self._options.mounts)

    async def copy_file_in(self, host_path: str, sandbox_file: str) -> None:
        copy_creating_parents(host_path, self._host_path_of(sandbox_file))

    async def copy_file_out(self, sandbox_file: str, host_path: str) -> None:
        copy_creating_parents(self._host_path_of(sandbox_file), host_path)

    async def close(self) -> None:
        self._closed = True
        running = list(self._live)
        for sp in running:
            if sp.reported is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(sp.reported), 5)
                except Exception:  # noqa: BLE001
                    pass
        self._kill_all()
        for sp in running:
            if sp.proc is not None:
                try:
                    await asyncio.wait_for(sp.proc.wait(), 5)
                except Exception:  # noqa: BLE001
                    pass
        self._unregister()
        shutil.rmtree(self._dirs.root, ignore_errors=True)


def bwrap(
    *,
    env: Optional[Dict[str, str]] = None,
    max_output_tail_chars: int = MAX_TAIL_CHARS,
    mounts: Optional[Sequence[Union[MountConfig, Dict]]] = None,
) -> BindMountSandboxProvider:
    """A Bubblewrap native sandbox (Linux and WSL2)."""
    user_mounts = bwrap_mounts(mounts or [])
    assert_bwrap_usable()

    async def create(opts: BindMountCreateOptions) -> BwrapHandle:
        return BwrapHandle(opts, user_mounts, max_output_tail_chars)

    return BindMountSandboxProvider(name="bwrap", create=create, env=dict(env or {}), sandbox_homedir=SANDBOX_HOME, native=True)

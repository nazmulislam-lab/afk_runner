"""Seatbelt sandbox provider (macOS 14+). Port of ``sandboxes/seatbelt.ts``,
``seatbeltSandbox.ts``, ``seatbeltPreflight.ts`` and ``seatbeltProfile.ts``.

Each ``exec`` runs under the system ``/usr/bin/sandbox-exec`` with a
deny-by-default profile: the system toolchain read-only, the worktree and git
dirs read/write at their host paths, a private home and temp dir, the host
network, and only the env vars sandcastle passes in. No paths are written
into the profile text: each is a ``(param ...)`` passed with ``-D``.

The profile's rule shapes follow OpenAI Codex (Apache-2.0), Google Gemini CLI
(Apache-2.0) and Microsoft MXC (MIT), as the original credits.
"""

from __future__ import annotations

import asyncio
import os
import platform
import posixpath
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ..display import MAX_TAIL_CHARS
from ..errors import NativeSandboxUnavailableError, SeatbeltError
from ..mounts import IdentityMount, resolve_identity_mounts
from ..sandbox_provider import BindMountCreateOptions, BindMountSandboxProvider, ExecResult, MountConfig
from ._native import copy_creating_parents, host_path_toolchain_from_env, register_native_sandbox_shutdown
from ._proc import kill_process, run_process

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SANDBOX_HOME = "/home/agent"
FALLBACK_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
XCODE_SELECT_LINK = "/var/db/xcode_select_link"
MIN_DARWIN_MAJOR = 23
USE_DOCKER = "Use docker() instead."

SYSTEM_READ_ROOTS = ["/bin", "/sbin", "/usr", "/System", "/Library", "/opt", "/private/etc", "/private/var/db", "/private/var/select"]
CONTAINER_ENGINE_HOME_DIRS = [".docker", ".colima", ".orbstack", ".lima", ".rd"]
CONTAINER_ENGINE_SOCKETS = ["/private/var/run/docker.sock", "/private/var/run/com.docker.vmnetd.sock"]

BASE = r'''(version 1)
(deny default)

(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))

(allow sysctl-read
  (sysctl-name "hw.activecpu")
  (sysctl-name "hw.busfrequency_compat")
  (sysctl-name "hw.byteorder")
  (sysctl-name "hw.cacheconfig")
  (sysctl-name "hw.cachelinesize_compat")
  (sysctl-name "hw.cpufamily")
  (sysctl-name "hw.cpufrequency_compat")
  (sysctl-name "hw.cputype")
  (sysctl-name "hw.l1dcachesize_compat")
  (sysctl-name "hw.l1icachesize_compat")
  (sysctl-name "hw.l2cachesize_compat")
  (sysctl-name "hw.l3cachesize_compat")
  (sysctl-name "hw.logicalcpu_max")
  (sysctl-name "hw.machine")
  (sysctl-name "hw.model")
  (sysctl-name "hw.memsize")
  (sysctl-name "hw.ncpu")
  (sysctl-name "hw.nperflevels")
  (sysctl-name-prefix "hw.optional.arm.")
  (sysctl-name-prefix "hw.optional.armv8_")
  (sysctl-name "hw.packages")
  (sysctl-name "hw.pagesize_compat")
  (sysctl-name "hw.pagesize")
  (sysctl-name "hw.physicalcpu")
  (sysctl-name "hw.physicalcpu_max")
  (sysctl-name "hw.logicalcpu")
  (sysctl-name "hw.cpufrequency")
  (sysctl-name "hw.tbfrequency_compat")
  (sysctl-name "hw.vectorunit")
  (sysctl-name "machdep.cpu.brand_string")
  (sysctl-name "kern.argmax")
  (sysctl-name "kern.hostname")
  (sysctl-name "kern.maxfilesperproc")
  (sysctl-name "kern.maxproc")
  (sysctl-name "kern.osproductversion")
  (sysctl-name "kern.osrelease")
  (sysctl-name "kern.ostype")
  (sysctl-name "kern.osvariant_status")
  (sysctl-name "kern.osversion")
  (sysctl-name "kern.secure_kernel")
  (sysctl-name "kern.sysv.semmns")
  (sysctl-name "kern.usrstack64")
  (sysctl-name "kern.version")
  (sysctl-name "sysctl.proc_cputype")
  (sysctl-name "vm.loadavg")
  (sysctl-name-prefix "hw.perflevel")
  (sysctl-name-prefix "kern.proc.pgrp.")
  (sysctl-name-prefix "kern.proc.pid.")
  (sysctl-name-prefix "net.routetable."))
(allow sysctl-write (sysctl-name "kern.grade_cputype"))

(allow iokit-open (iokit-registry-entry-class "RootDomainUserClient"))
(allow ipc-posix-sem)
(allow ipc-posix-shm-read* (ipc-posix-name "apple.shm.notification_center"))
(allow system-mac-syscall (mac-policy-name "vnguard"))
(allow system-mac-syscall
  (require-all (mac-policy-name "Sandbox") (mac-syscall-number 67)))
(allow system-fsctl (fsctl-command FSIOC_CAS_BSDFLAGS))

(allow mach-lookup
  (global-name "com.apple.system.opendirectoryd.libinfo")
  (global-name "com.apple.system.opendirectoryd.membership")
  (global-name "com.apple.system.DirectoryService.libinfo_v1")
  (global-name "com.apple.system.logger")
  (global-name "com.apple.system.notification_center")
  (global-name "com.apple.bsd.dirhelper")
  (global-name "com.apple.logd")
  (global-name "com.apple.diagnosticd")
  (global-name "com.apple.secinitd")
  (global-name "com.apple.trustd")
  (global-name "com.apple.trustd.agent")
  (global-name "com.apple.SecurityServer")
  (global-name "com.apple.networkd")
  (global-name "com.apple.ocspd")
  (global-name "com.apple.SystemConfiguration.DNSConfiguration")
  (global-name "com.apple.SystemConfiguration.configd")
  (global-name "com.apple.PowerManagement.control"))

; Path lookups: any path's metadata, never its contents.
(allow file-read-metadata)
(allow file-read* (literal "/"))

; Devices and terminals.
(allow pseudo-tty)
(allow file-read* file-write* file-ioctl
  (literal "/dev/null")
  (literal "/dev/zero")
  (literal "/dev/tty")
  (literal "/dev/ptmx")
  (literal "/dev/dtracehelper")
  (regex #"^/dev/ttys[0-9]+$"))
(allow file-read* (literal "/dev/random") (literal "/dev/urandom"))
(allow file-read-data file-write-data (subpath "/dev/fd"))

; Open egress (and host localhost, which the docs say).
(allow network*)
(allow system-socket
  (require-all (socket-domain AF_SYSTEM) (socket-protocol 2)))'''

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def _probe() -> Optional[str]:
    if not os.path.exists(SANDBOX_EXEC):
        return f"seatbelt() needs macOS's {SANDBOX_EXEC}, and it's missing. {USE_DOCKER}"
    try:
        r = subprocess.run([SANDBOX_EXEC, "-p", "(version 1)(allow default)", "--", "/usr/bin/true"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"seatbelt() can't start a sandbox: `{SANDBOX_EXEC}` failed: {e}. {USE_DOCKER}"
    if r.returncode == 0:
        return None
    stderr = (r.stderr or "").strip()
    reason = stderr or f"exit code {r.returncode}"
    nested = (
        " This process may already be inside a Seatbelt sandbox, and macOS refuses to nest them."
        if "operation not permitted" in stderr.lower()
        else ""
    )
    return f"seatbelt() can't start a sandbox: `{SANDBOX_EXEC}` failed: {reason}.{nested} {USE_DOCKER}"


def _check() -> Optional[str]:
    if sys.platform != "darwin":
        return "seatbelt() needs macOS; on Linux or WSL2 use bwrap()"
    try:
        major = int(platform.release().split(".")[0])
    except ValueError:
        major = MIN_DARWIN_MAJOR
    if major < MIN_DARWIN_MAJOR:
        return f"seatbelt() needs macOS 14 or later. {USE_DOCKER}"
    return _probe()


_outcome: List[Optional[str]] = []


def assert_seatbelt_usable() -> None:
    if not _outcome:
        _outcome.append(_check())
    if _outcome[0] is not None:
        raise NativeSandboxUnavailableError(_outcome[0])


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@dataclass
class SeatbeltPolicy:
    profile: str
    params: List[Tuple[str, str]] = field(default_factory=list)


def _is_within(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(root + "/")


def _anchored_dirs(writable_roots: Sequence[str], protected_paths: Sequence[str]) -> List[str]:
    dirs = list(dict.fromkeys(writable_roots))
    for p in protected_paths:
        roots = [r for r in writable_roots if _is_within(p, r)]
        d = posixpath.dirname(p)
        while any(_is_within(d, r) for r in roots):
            if d not in dirs:
                dirs.append(d)
            parent = posixpath.dirname(d)
            if parent == d:
                break
            d = parent
    return dirs


def build_seatbelt_policy(*, readable_roots: Sequence[str], writable_roots: Sequence[str], protected_paths: Sequence[str], host_home: str) -> SeatbeltPolicy:
    params: List[Tuple[str, str]] = []

    def ref(prefix: str, value: str) -> str:
        key = f"{prefix}_{len(params)}"
        params.append((key, value))
        return f'(param "{key}")'

    readable = [ref("READ", p) for p in dict.fromkeys(readable_roots)]
    writable = [ref("WRITE", p) for p in dict.fromkeys(writable_roots)]
    sockets = [ref("ENGINE", s) for s in CONTAINER_ENGINE_SOCKETS]
    engine_dirs = [ref("ENGINE", posixpath.join(host_home, n)) for n in CONTAINER_ENGINE_HOME_DIRS]
    protected = [ref("PROTECTED", p) for p in dict.fromkeys(protected_paths)]
    anchors = [ref("ANCHOR", p) for p in _anchored_dirs(writable_roots, protected_paths)]

    def allow_subpaths(ops: str, refs: Sequence[str]) -> List[str]:
        if not refs:
            return []
        return [f"(allow {ops}\n" + "\n".join(f"  (subpath {r})" for r in refs) + ")"]

    engine_filters = "\n".join([*(f"  (literal {r})" for r in sockets), *(f"  (subpath {r})" for r in engine_dirs)])
    lines = [
        BASE,
        "; Read-only: the system toolchain and the paths given to read.",
        *allow_subpaths("file-read* file-map-executable", readable),
        "; Read/write: the worktree, the writable git dirs, the private home and temp dir.",
        *allow_subpaths("file-read* file-write* file-map-executable", writable),
        "; No way into a container engine, which would be a way out.",
        f"(deny file-read* file-write* network-outbound process-exec\n{engine_filters})",
        '(deny mach-lookup (xpc-service-name-prefix ""))',
        "; Last, so nothing above reopens them: the read-only git control files,",
        "; and the dirs that hold them, which can't be moved aside.",
        *(f"(deny file-write* (subpath {r}))" for r in protected),
        *(f"(deny file-write-unlink (require-all (vnode-type DIRECTORY) (literal {r})))" for r in anchors),
        "; Writes through read-only descriptors (F_MAKECOMPRESSED, F_TRANSFEREXTENTS).",
        "(deny system-fcntl (fcntl-command 80 110))",
        "",
    ]
    return SeatbeltPolicy("\n".join(lines), params)


def sandbox_exec_args(policy: SeatbeltPolicy, command: str) -> List[str]:
    return ["-p", policy.profile, *(f"-D{k}={v}" for k, v in policy.params), "--", "/bin/sh", "-c", command]


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------


@dataclass
class PrivateDirs:
    root: str
    home: str
    tmp: str


def make_private_dirs() -> PrivateDirs:
    try:
        root = os.path.realpath(tempfile.mkdtemp(prefix="sandcastle-seatbelt-"))
        dirs = PrivateDirs(root, os.path.join(root, "home"), os.path.join(root, "tmp"))
        os.mkdir(dirs.home)
        os.mkdir(dirs.tmp)
        return dirs
    except OSError as e:
        raise SeatbeltError(f"Couldn't create the seatbelt() sandbox's private dirs: {e}") from e


def seatbelt_mounts(mounts: Sequence[Union[MountConfig, Dict]]) -> List[IdentityMount]:
    return resolve_identity_mounts("seatbelt", mounts, SANDBOX_HOME)


def sandbox_setup(options: BindMountCreateOptions, dirs: PrivateDirs, user_mounts: Sequence[IdentityMount]) -> Tuple[SeatbeltPolicy, Dict[str, str]]:
    tc = host_path_toolchain_from_env(SYSTEM_READ_ROOTS, [])
    dev_dir = os.path.realpath(XCODE_SELECT_LINK) if os.path.exists(XCODE_SELECT_LINK) else None
    mounts = []
    for m in [*options.mounts, *user_mounts]:
        if os.path.exists(m.host_path):
            mounts.append((os.path.realpath(m.host_path), bool(m.readonly)))
    policy = build_seatbelt_policy(
        readable_roots=[*SYSTEM_READ_ROOTS, *([dev_dir] if dev_dir else []), *tc.readable_dirs, *(p for p, ro in mounts if ro)],
        writable_roots=[*(p for p, ro in mounts if not ro), dirs.home, dirs.tmp],
        protected_paths=[p for p, ro in mounts if ro],
        host_home=os.path.expanduser("~"),
    )
    env = {**options.env, "PATH": ":".join(tc.path_entries) if tc.path_entries else FALLBACK_PATH, "HOME": dirs.home, "TMPDIR": dirs.tmp}
    return policy, env


def to_host_path(path: str, dirs: PrivateDirs, mounts: Sequence[Any]) -> str:
    p = posixpath.normpath(path)
    if any(_is_within(p, m.host_path) and m.host_path != "/" for m in mounts):
        return p
    if p == SANDBOX_HOME or p.startswith(SANDBOX_HOME + "/"):
        return os.path.join(dirs.home, posixpath.relpath(p, SANDBOX_HOME))
    return p


WATCHDOG = 'read -r reply || kill -9 -- "-$1" 2>/dev/null'


def _kill_group(pid: Optional[int]) -> None:
    if pid is None:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


class SeatbeltHandle:
    def __init__(self, options: BindMountCreateOptions, user_mounts: Sequence[IdentityMount], max_tail: int) -> None:
        self.worktree_path = options.worktree_path
        self._options = options
        self._max_tail = max_tail
        self._dirs = make_private_dirs()
        self.sandbox_homedir = self._dirs.home
        self.sandbox_tmpdir = self._dirs.tmp
        self._policy, self._env = sandbox_setup(options, self._dirs, user_mounts)
        self._live: List[int] = []
        self._closed = False
        self._unregister = register_native_sandbox_shutdown(self._kill_all, self._dirs.root)

    def _kill_all(self) -> None:
        for pid in list(self._live):
            _kill_group(pid)

    async def exec(self, command: str, *, on_line=None, cwd: Optional[str] = None, sudo: bool = False, stdin: Optional[str] = None) -> ExecResult:
        if sudo:
            raise RuntimeError(
                "seatbelt() has no root: exec with sudo: true isn't supported in a native sandbox. Use docker() for setup that needs root."
            )
        if self._closed:
            raise RuntimeError("seatbelt() sandbox is closed")
        pid_box: List[int] = []
        watchdogs: List[subprocess.Popen] = []

        def on_spawn(proc: "asyncio.subprocess.Process") -> None:
            pid_box.append(proc.pid)
            self._live.append(proc.pid)
            # A detached host watchdog SIGKILLs the group if this process dies.
            try:
                wd = subprocess.Popen(["/bin/sh", "-c", WATCHDOG, "sh", str(proc.pid)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                watchdogs.append(wd)
            except OSError:
                pass

        try:
            return await run_process(
                [SANDBOX_EXEC, *sandbox_exec_args(self._policy, command)],
                cwd=cwd or self.worktree_path,
                env=self._env,
                stdin=stdin if stdin is not None else "",
                on_line=on_line,
                max_tail_chars=self._max_tail,
                new_session=True,
                on_spawn=on_spawn,
            )
        finally:
            # Like a fresh bwrap per exec: nothing it started outlives it.
            for pid in pid_box:
                _kill_group(pid)
                if pid in self._live:
                    self._live.remove(pid)
            for wd in watchdogs:
                try:
                    if wd.stdin:
                        wd.stdin.write(b"exit\n")
                        wd.stdin.close()
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
        self._kill_all()
        self._unregister()
        shutil.rmtree(self._dirs.root, ignore_errors=True)


def seatbelt(
    *,
    env: Optional[Dict[str, str]] = None,
    max_output_tail_chars: int = MAX_TAIL_CHARS,
    mounts: Optional[Sequence[Union[MountConfig, Dict]]] = None,
) -> BindMountSandboxProvider:
    """A Seatbelt native sandbox (macOS 14 or later)."""
    user_mounts = seatbelt_mounts(mounts or [])
    assert_seatbelt_usable()

    async def create(opts: BindMountCreateOptions) -> SeatbeltHandle:
        return SeatbeltHandle(opts, user_mounts, max_output_tail_chars)

    return BindMountSandboxProvider(name="seatbelt", create=create, env=dict(env or {}), sandbox_homedir=SANDBOX_HOME, native=True)

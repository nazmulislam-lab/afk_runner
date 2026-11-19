"""Docker sandbox provider. Port of ``sandboxes/docker.ts`` and
``DockerLifecycle.ts``.

```python
from sandcastle.sandboxes.docker import docker
await run(agent=claude_code("claude-opus-4-8"), sandbox=docker(), prompt="...")
```
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Optional, Sequence, Union

from ..errors import DockerError, ImageNotFoundError
from ..mounts import SelinuxLabel, default_image_name, format_volume_mount, process_file_mount_parents, resolve_user_mounts
from ..sandbox_provider import BindMountCreateOptions, BindMountSandboxProvider, MountConfig
from ..display import MAX_TAIL_CHARS
from ._engine import EngineHandle, create_file_mount_parents, networks_of, repeat_flag
from ._proc import run_process

#: Capability names accepted by ``cap_drop`` (no ``CAP_`` prefix), or "ALL".
LINUX_CAPABILITIES = frozenset(
    "ALL AUDIT_CONTROL AUDIT_READ AUDIT_WRITE BLOCK_SUSPEND BPF CHECKPOINT_RESTORE CHOWN DAC_OVERRIDE "
    "DAC_READ_SEARCH FOWNER FSETID IPC_LOCK IPC_OWNER KILL LEASE LINUX_IMMUTABLE MAC_ADMIN MAC_OVERRIDE MKNOD "
    "NET_ADMIN NET_BIND_SERVICE NET_BROADCAST NET_RAW PERFMON SETFCAP SETGID SETPCAP SETUID SYS_ADMIN SYS_BOOT "
    "SYS_CHROOT SYS_MODULE SYS_NICE SYS_PACCT SYS_PTRACE SYS_RAWIO SYS_RESOURCE SYS_TIME SYS_TTY_CONFIG SYSLOG "
    "WAKE_ALARM".split()
)


def validate_cap_drop(cap_drop: Optional[Sequence[str]]) -> None:
    for cap in cap_drop or ():
        if cap not in LINUX_CAPABILITIES:
            raise ValueError(f"Unknown Linux capability {cap!r} in cap_drop (use names without the CAP_ prefix, or 'ALL').")


# ---------------------------------------------------------------------------
# DockerLifecycle
# ---------------------------------------------------------------------------


async def _docker(args: Sequence[str]) -> str:
    r = await run_process(["docker", *args])
    if r.exit_code != 0:
        raise DockerError(f"docker {args[0]} failed: {r.stderr or f'exit {r.exit_code}'}")
    return r.stdout


async def build_image(image_name: str, dockerfile_dir: str, *, dockerfile: Optional[str] = None, build_args: Optional[Dict[str, str]] = None) -> None:
    """``docker build``; with ``dockerfile`` the build context is the cwd."""
    flags: List[str] = []
    for k, v in (build_args or {}).items():
        flags += ["--build-arg", f"{k}={v}"]
    ctx = ["-f", os.path.abspath(dockerfile), os.getcwd()] if dockerfile else [os.path.abspath(dockerfile_dir)]
    await _docker(["build", "-t", image_name, *flags, *ctx])


def run_flags(env: Dict[str, str], *, volume_mounts=(), workdir=None, user=None, network=None, groups=None, cap_drop=None, devices=None, cpus=None, memory=None, pids_limit=None, selinux_label: Any = "z") -> List[str]:
    flags = repeat_flag("-e", [f"{k}={v}" for k, v in env.items()])
    flags += repeat_flag("-v", [format_volume_mount(m, selinux_label) for m in volume_mounts])
    if workdir:
        flags += ["-w", workdir]
    if user:
        flags += ["--user", user]
    flags += repeat_flag("--network", networks_of(network))
    flags += repeat_flag("--group-add", groups)
    flags += repeat_flag("--cap-drop", cap_drop)
    flags += repeat_flag("--device", devices)
    if cpus is not None:
        flags += ["--cpus", str(cpus)]
    if memory is not None:
        flags += ["--memory", memory, "--memory-swap", memory]
    if pids_limit is not None:
        flags += ["--pids-limit", str(pids_limit)]
    return flags


async def start_container(container_name: str, image_name: str, env: Dict[str, str], **options: Any) -> None:
    existing = await _docker(["ps", "-a", "--filter", f"name=^{container_name}$", "--format", "{{.Names}}"])
    if existing.strip() == container_name:
        raise DockerError(f"Container '{container_name}' already exists. Run cleanup first.")
    await _docker(["run", "-d", "--name", container_name, *run_flags(env, **options), image_name])


async def remove_container(container_name: str) -> None:
    for args in (["stop", container_name], ["rm", container_name]):
        try:
            await _docker(args)
        except DockerError:
            pass


async def remove_image(image_name: str) -> None:
    try:
        await _docker(["rmi", image_name])
    except DockerError as e:
        if "No such image" in e.message:
            raise ImageNotFoundError(f"Docker image '{image_name}' does not exist, so there is nothing to remove.") from e
        raise


async def check_image_uid(image_name: str, expected_uid: int) -> None:
    r = await run_process(["docker", "image", "inspect", image_name, "--format", "{{.Config.User}}"])
    if r.exit_code != 0:
        raise RuntimeError(f"Image '{image_name}' not found locally. Build it first with 'sandcastle docker build-image'.")
    user = r.stdout.strip()
    if not user:
        return
    try:
        image_uid = int(user.split(":")[0])
    except ValueError:
        return
    if image_uid != expected_uid:
        raise RuntimeError(
            f"UID mismatch: image '{image_name}' was built with UID {image_uid}, but the expected UID is {expected_uid}. "
            f"Rebuild the image with 'sandcastle docker build-image', or pass container_uid={image_uid} to docker() to match the image."
        )


class _DockerHandle(EngineHandle):
    async def _remove(self) -> None:
        await remove_container(self.sandbox_id)


def _host_id(fn: str, default: int) -> int:
    getter = getattr(os, fn, None)
    return getter() if getter is not None else default


def docker(
    *,
    image_name: Optional[str] = None,
    container_uid: Optional[int] = None,
    container_gid: Optional[int] = None,
    selinux_label: SelinuxLabel = "z",
    mounts: Optional[Sequence[Union[MountConfig, Dict]]] = None,
    env: Optional[Dict[str, str]] = None,
    network: Union[None, str, Sequence[str]] = None,
    groups: Optional[Sequence[Union[str, int]]] = None,
    cap_drop: Optional[Sequence[str]] = None,
    devices: Optional[Sequence[str]] = None,
    max_output_tail_chars: int = MAX_TAIL_CHARS,
    cpus: Optional[float] = None,
    memory: Optional[str] = None,
    pids_limit: Optional[int] = None,
) -> BindMountSandboxProvider:
    """Run the agent in a Docker container built from ``.sandcastle/Dockerfile``."""
    validate_cap_drop(cap_drop)
    home = "/home/agent"
    user_mounts = resolve_user_mounts(mounts, home) if mounts else []
    parent_dirs = process_file_mount_parents(user_mounts, home)

    async def create(opts: BindMountCreateOptions) -> _DockerHandle:
        name = f"sandcastle-{uuid.uuid4()}"
        worktree_path = next((m.sandbox_path for m in opts.mounts if m.host_path == opts.worktree_path), "/home/agent/workspace")
        image = image_name or default_image_name(opts.host_repo_path)
        uid = container_uid if container_uid is not None else _host_id("getuid", 1000)
        gid = container_gid if container_gid is not None else _host_id("getgid", 1000)
        await check_image_uid(image, uid)
        await start_container(
            name,
            image,
            {**opts.env, "HOME": home},
            volume_mounts=[*opts.mounts, *user_mounts],
            workdir=worktree_path,
            user=f"{uid}:{gid}",
            network=network,
            groups=groups,
            cap_drop=cap_drop,
            devices=devices,
            cpus=cpus,
            memory=memory,
            pids_limit=pids_limit,
            selinux_label=selinux_label,
        )
        handle = _DockerHandle("docker", name, worktree_path, max_output_tail_chars)
        try:
            await create_file_mount_parents("docker", name, parent_dirs, uid, gid)
        except BaseException:
            await handle.close()
            raise
        return handle

    return BindMountSandboxProvider(
        name="docker", create=create, env=dict(env or {}), sandbox_homedir=home, meta={"image_name": image_name, "engine": "docker"}
    )

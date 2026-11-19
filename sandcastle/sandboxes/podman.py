"""Podman sandbox provider. Port of ``sandboxes/podman.ts`` and
``PodmanLifecycle.ts``."""

from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any, Dict, List, Optional, Sequence, Union

from ..display import MAX_TAIL_CHARS
from ..errors import ImageNotFoundError, PodmanError
from ..mounts import SelinuxLabel, default_image_name, format_volume_mount, process_file_mount_parents, resolve_user_mounts
from ..sandbox_provider import BindMountCreateOptions, BindMountSandboxProvider, MountConfig
from ._engine import EngineHandle, create_file_mount_parents, networks_of, repeat_flag
from ._proc import run_process
from .docker import validate_cap_drop


async def _podman(args: Sequence[str]) -> str:
    r = await run_process(["podman", *args])
    if r.exit_code != 0:
        raise PodmanError(f"podman {args[0]} failed: {r.stderr or f'exit {r.exit_code}'}")
    return r.stdout


async def build_image(image_name: str, containerfile_dir: str, *, containerfile: Optional[str] = None) -> None:
    ctx = ["-f", os.path.abspath(containerfile), os.getcwd()] if containerfile else [os.path.abspath(containerfile_dir)]
    await _podman(["build", "-t", image_name, *ctx])


async def remove_image(image_name: str) -> None:
    try:
        await _podman(["rmi", image_name])
    except PodmanError as e:
        if "image not known" in e.message:
            raise ImageNotFoundError(f"Podman image '{image_name}' does not exist, so there is nothing to remove.") from e
        raise


async def _check_image_exists(image_name: str) -> None:
    r = await run_process(["podman", "image", "inspect", image_name])
    if r.exit_code != 0:
        raise RuntimeError(f"Image '{image_name}' not found locally. Build it first with 'podman build -t {image_name} .'")


async def _check_podman_machine() -> None:
    err = RuntimeError("Podman Machine is not running. Run 'podman machine init && podman machine start' first.")
    try:
        r = await run_process(["podman", "machine", "list", "--format", "json"])
    except FileNotFoundError:
        raise err from None
    if r.exit_code != 0:
        raise err
    try:
        machines = json.loads(r.stdout)
    except ValueError:
        raise err from None
    if not (isinstance(machines, list) and any(isinstance(m, dict) and m.get("Running") is True for m in machines)):
        raise err


def podman(
    *,
    image_name: Optional[str] = None,
    selinux_label: SelinuxLabel = "z",
    userns: Union[str, bool] = "keep-id",
    container_uid: int = 1000,
    container_gid: int = 1000,
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
    """Run the agent in a Podman container built from ``.sandcastle/Containerfile``."""
    validate_cap_drop(cap_drop)
    home = "/home/agent"
    user_mounts = resolve_user_mounts(mounts, home) if mounts else []
    parent_dirs = process_file_mount_parents(user_mounts, home)

    async def create(opts: BindMountCreateOptions) -> EngineHandle:
        name = f"sandcastle-{uuid.uuid4()}"
        worktree_path = next((m.sandbox_path for m in opts.mounts if m.host_path == opts.worktree_path), "/home/agent/workspace")
        volumes = [format_volume_mount(m, selinux_label) for m in [*opts.mounts, *user_mounts]]
        image = image_name or default_image_name(opts.host_repo_path)
        if sys.platform in ("darwin", "win32"):
            await _check_podman_machine()
        await _check_image_exists(image)
        e = {**opts.env, "HOME": home}
        args = ["run", "-d", "--name", name, "--user", f"{container_uid}:{container_gid}"]
        if userns is not False:
            args.append(f"--userns=keep-id:uid={container_uid},gid={container_gid}")
        args += repeat_flag("--network", networks_of(network))
        args += repeat_flag("--group-add", groups)
        args += repeat_flag("--cap-drop", cap_drop)
        args += repeat_flag("--device", devices)
        if cpus is not None:
            args += ["--cpus", str(cpus)]
        if memory is not None:
            args += ["--memory", memory, "--memory-swap", memory]
        if pids_limit is not None:
            args += ["--pids-limit", str(pids_limit)]
        args += ["-w", worktree_path]
        args += repeat_flag("-e", [f"{k}={v}" for k, v in e.items()])
        args += repeat_flag("-v", volumes)
        args += ["--entrypoint", "sleep", image, "infinity"]
        r = await run_process(["podman", *args])
        if r.exit_code != 0:
            raise RuntimeError(f"podman run failed: {r.stderr}")
        # `--time 0`: PID 1 is `sleep infinity`, which ignores SIGTERM.
        handle = EngineHandle("podman", name, worktree_path, max_output_tail_chars, rm_args=("rm", "-f", "--time", "0"))
        try:
            await create_file_mount_parents("podman", name, parent_dirs, container_uid, container_gid)
        except BaseException:
            await handle.close()
            raise
        return handle

    return BindMountSandboxProvider(
        name="podman", create=create, env=dict(env or {}), sandbox_homedir=home, meta={"image_name": image_name, "engine": "podman"}
    )

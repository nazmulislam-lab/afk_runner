"""Vercel Sandbox provider (Firecracker microVM). Port of
``sandboxes/vercel.ts``, ``vercelExec.ts`` and ``vercelAuth.ts``.

Needs ``pip install vercel-sandbox``. Auth: Vercel OIDC in a Vercel
environment, or ``VERCEL_TOKEN`` with ``VERCEL_TEAM_ID`` and
``VERCEL_PROJECT_ID`` (or the matching options). The repo lands in
``/vercel/sandbox/workspace``.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Optional

from ..display import MAX_TAIL_CHARS
from ..sandbox_provider import ExecResult, IsolatedCreateOptions, IsolatedSandboxProvider
from ._cloud import LineSink, make_tarball, require, result

VERCEL_REPO_PATH = "/vercel/sandbox/workspace"


def resolve_vercel_auth(token: Optional[str], team_id: Optional[str], project_id: Optional[str]) -> Dict[str, str]:
    tok = token or os.environ.get("VERCEL_TOKEN") or None
    if tok is None:
        out = {}
        if team_id:
            out["team_id"] = team_id
        if project_id:
            out["project_id"] = project_id
        return out
    team = team_id or os.environ.get("VERCEL_TEAM_ID") or None
    project = project_id or os.environ.get("VERCEL_PROJECT_ID") or None
    missing = [n for n, v in (("VERCEL_TEAM_ID (or the teamId option)", team), ("VERCEL_PROJECT_ID (or the projectId option)", project)) if v is None]
    if missing:
        source = "the token option" if token else "VERCEL_TOKEN"
        raise ValueError(f"vercel(): {source} is set, but a Vercel token also needs a team and project id. Missing: {', '.join(missing)}.")
    return {"token": tok, "team_id": team, "project_id": project}  # type: ignore[dict-item]


class VercelHandle:
    def __init__(self, sandbox: Any, max_tail: int) -> None:
        self._sb = sandbox
        self.worktree_path = VERCEL_REPO_PATH
        self.sandbox_id = getattr(sandbox, "name", None) or getattr(sandbox, "id", None)
        self._max_tail = max_tail

    async def exec(self, command: str, *, on_line=None, cwd: Optional[str] = None, sudo: bool = False, stdin: Optional[str] = None) -> ExecResult:
        kwargs: Dict[str, Any] = {"cwd": cwd or VERCEL_REPO_PATH}
        if sudo:
            kwargs["sudo"] = True
        if stdin is not None:
            kwargs["input"] = stdin.encode("utf-8")
        if on_line is None:
            r = await self._sb.run_command("sh", ["-c", command], capture_output=True, **kwargs)
            return result((r.stdout or b"").decode("utf-8", "replace"), (r.stderr or b"").decode("utf-8", "replace"), r.returncode)
        out = LineSink(on_line, self._max_tail)
        err = LineSink(None, self._max_tail, "")
        r = await self._sb.run_command("sh", ["-c", command], stdout=out, stderr=err, **kwargs)
        out.finish()
        return result(out.text(), err.text(), r.returncode)

    async def copy_in(self, host_path: str, sandbox_path: str) -> None:
        if os.path.isdir(host_path):
            tar = f"/tmp/sandcastle-copyin-{uuid.uuid4()}.tar.gz"
            await self._sb.fs.write_bytes(tar, make_tarball(host_path))
            await self._sb.run_command(
                "sh", ["-c", f'mkdir -p "{sandbox_path}" && tar -xzf "{tar}" -C "{sandbox_path}" && rm -f "{tar}"'], capture_output=True
            )
            return
        with open(host_path, "rb") as f:
            await self._sb.fs.write_bytes(sandbox_path, f.read())

    async def copy_file_out(self, sandbox_path: str, host_path: str) -> None:
        try:
            data = await self._sb.fs.read_bytes(sandbox_path)
        except Exception as e:  # noqa: BLE001
            raise FileNotFoundError(f"File not found in Vercel sandbox: {sandbox_path}") from e
        os.makedirs(os.path.dirname(host_path) or ".", exist_ok=True)
        with open(host_path, "wb") as f:
            f.write(data)

    async def close(self) -> None:
        await self._sb.stop()


def vercel(
    *,
    token: Optional[str] = None,
    team_id: Optional[str] = None,
    project_id: Optional[str] = None,
    source: Optional[Dict[str, Any]] = None,
    ports: Optional[List[int]] = None,
    timeout_ms: Optional[int] = None,
    resources: Optional[Dict[str, Any]] = None,
    runtime: Optional[str] = None,
    network_policy: Optional[Dict[str, Any]] = None,
    persistent: bool = False,
    env: Optional[Dict[str, str]] = None,
    max_output_tail_chars: int = MAX_TAIL_CHARS,
) -> IsolatedSandboxProvider:
    """Run the agent in a Vercel Sandbox."""

    async def create(opts: IsolatedCreateOptions) -> VercelHandle:
        auth = resolve_vercel_auth(token, team_id, project_id)
        mod = require("vercel.sandbox", "vercel-sandbox")
        for key, var in (("token", "VERCEL_TOKEN"), ("team_id", "VERCEL_TEAM_ID"), ("project_id", "VERCEL_PROJECT_ID")):
            if key in auth:
                os.environ[var] = auth[key]
        params: Dict[str, Any] = {"env": dict(opts.env), "persistent": persistent}
        if source is not None:
            params["source"] = source
        if ports:
            params["ports"] = ports
        if timeout_ms:
            params["execution_time_limit"] = timeout_ms / 1000
        if resources:
            params["resources"] = resources
        if runtime:
            params["runtime"] = runtime
        if network_policy:
            params["network_policy"] = network_policy
        sandbox = await mod.create_sandbox(**params)
        try:
            r = await sandbox.run_command("mkdir", ["-p", VERCEL_REPO_PATH], capture_output=True)
            if r.returncode != 0:
                raise RuntimeError(f"vercel(): could not create {VERCEL_REPO_PATH}: {(r.stderr or b'').decode().strip()}")
        except BaseException:
            try:
                await sandbox.stop()
            except Exception:  # noqa: BLE001
                pass
            raise
        return VercelHandle(sandbox, max_output_tail_chars)

    return IsolatedSandboxProvider(name="vercel", create=create, env=dict(env or {}))

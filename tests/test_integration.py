"""End-to-end runs against real git repos, with a fake `claude` CLI."""

import asyncio
import os
import shutil

import pytest

from conftest import git
from sandcastle import (
    AbortController,
    AgentError,
    AgentIdleTimeoutError,
    Output,
    StructuredOutputError,
    UsageLimitConfig,
    UsageLimitWaitError,
    branch,
    claude_code,
    create_sandbox,
    create_worktree,
    merge_to_head,
    run,
)
from sandcastle.cli import main as cli_main
from sandcastle.sandbox_provider import BindMountCreateOptions, ExecResult, create_bind_mount_sandbox_provider
from sandcastle.sandboxes._proc import run_process
from sandcastle.sandboxes.no_sandbox import no_sandbox
from sandcastle.sandboxes.test_providers import test_isolated

STDOUT = {"type": "stdout"}
FAST = UsageLimitConfig(buffer_seconds=0)


def agent(**kw):
    return claude_code("fake-model", capture_sessions=False, **kw)


# ---------------------------------------------------------------------------
# Branch strategies
# ---------------------------------------------------------------------------


async def test_head_mode_commits_on_current_branch(repo, fake_claude):
    fake_claude.scenario([{"commit": "a.txt", "signal": True}])
    r = await run(agent=agent(), sandbox=no_sandbox(), prompt="do it", cwd=str(repo), logging=STDOUT)
    assert r.branch == "main"
    assert len(r.commits) == 1 and r.completion_signal == "<promise>COMPLETE</promise>"
    assert git(repo, "log", "-1", "--format=%s") == "add a.txt"
    assert fake_claude.calls()[0]["prompt"] == "do it"


async def test_merge_to_head_merges_and_deletes_temp_branch(repo, fake_claude):
    fake_claude.scenario([{"commit": "b.txt"}])
    r = await run(agent=agent(), sandbox=no_sandbox(), prompt="x", cwd=str(repo), branch_strategy=merge_to_head(), logging=STDOUT)
    assert r.branch == "main" and len(r.commits) == 1
    assert (repo / "b.txt").exists()
    assert "sandcastle/" not in git(repo, "branch", "--list")
    assert os.listdir(repo / ".sandcastle" / "worktrees") == []
    assert ".sandcastle/worktrees/" in fake_claude.calls()[0]["cwd"]


async def test_named_branch_leaves_main_untouched(repo, fake_claude):
    fake_claude.scenario([{"commit": "c.txt"}])
    r = await run(agent=agent(), sandbox=no_sandbox(), prompt="x", cwd=str(repo), branch_strategy=branch("agent/fix-1"), logging=STDOUT)
    assert r.branch == "agent/fix-1" and len(r.commits) == 1
    assert not (repo / "c.txt").exists()
    assert git(repo, "log", "-1", "--format=%s", "agent/fix-1") == "add c.txt"


async def test_isolated_provider_syncs_commits_back(repo, fake_claude):
    fake_claude.scenario([{"commit": "iso.txt", "signal": True}])
    r = await run(agent=agent(), sandbox=test_isolated(), prompt="x", cwd=str(repo), logging=STDOUT)
    assert r.branch == "main" and len(r.commits) == 1
    assert (repo / "iso.txt").read_text() == "call 0\n"
    assert not (repo / ".sandcastle" / "patches").exists()


async def test_isolated_untracked_files_preserve_worktree(repo, fake_claude):
    fake_claude.scenario([{"commit": "one.txt", "untracked": "loose.txt"}])
    r = await run(agent=agent(), sandbox=test_isolated(), prompt="x", cwd=str(repo), branch_strategy=branch("iso-b"), logging=STDOUT)
    assert len(r.commits) == 1
    assert r.preserved_worktree_path and os.path.exists(os.path.join(r.preserved_worktree_path, "loose.txt"))


async def test_iterations_until_signal(repo, fake_claude):
    fake_claude.scenario([{"commit": "1.txt"}, {"commit": "2.txt"}, {"signal": True}, {"commit": "never.txt"}])
    r = await run(agent=agent(), sandbox=no_sandbox(), prompt="x", cwd=str(repo), max_iterations=5, logging=STDOUT)
    assert len(r.iterations) == 3 and len(r.commits) == 2
    assert not (repo / "never.txt").exists()


# ---------------------------------------------------------------------------
# Usage limits (the Python-port feature)
# ---------------------------------------------------------------------------


async def test_usage_limit_waits_and_resumes_same_session(repo, fake_claude):
    fake_claude.scenario([{"limit": 1}, {"commit": "after.txt", "signal": True}])
    r = await run(agent=agent(), sandbox=no_sandbox(), prompt="the task", cwd=str(repo), logging=STDOUT, usage_limit=FAST)
    calls = fake_claude.calls()
    assert len(calls) == 2
    assert "--resume" in calls[1]["argv"] and calls[1]["argv"][calls[1]["argv"].index("--resume") + 1] == "sess-0"
    assert "limit has now reset" in calls[1]["prompt"]
    assert r.iterations[0].usage_limit_waits == 1 and r.iterations[0].session_id == "sess-0"
    assert len(r.commits) == 1 and r.completion_signal
    assert r.stdout.startswith("partial work")


async def test_usage_limit_disabled_fails(repo, fake_claude):
    fake_claude.scenario([{"limit": 1}])
    with pytest.raises(AgentError) as e:
        await run(agent=agent(), sandbox=no_sandbox(), prompt="x", cwd=str(repo), logging=STDOUT, wait_for_usage_reset=False)
    assert "usage limit" in e.value.message


async def test_usage_limit_max_waits(repo, fake_claude):
    fake_claude.scenario([{"limit": 1}])
    with pytest.raises(UsageLimitWaitError):
        await run(agent=agent(), sandbox=no_sandbox(), prompt="x", cwd=str(repo), logging=STDOUT, usage_limit=UsageLimitConfig(buffer_seconds=0, max_waits=2))
    assert len(fake_claude.calls()) == 3


# ---------------------------------------------------------------------------
# Timeouts, abort, errors
# ---------------------------------------------------------------------------


async def test_idle_timeout(repo, fake_claude):
    fake_claude.scenario([{"sleep": 30}])
    with pytest.raises(AgentIdleTimeoutError):
        await run(agent=agent(), sandbox=no_sandbox(), prompt="x", cwd=str(repo), logging=STDOUT, idle_timeout_seconds=1)


async def test_hanging_process_force_completes(repo, fake_claude):
    fake_claude.scenario([{"hang": True}])
    r = await run(agent=agent(), sandbox=no_sandbox(), prompt="x", cwd=str(repo), logging=STDOUT, completion_timeout_seconds=1)
    assert r.completion_signal == "<promise>COMPLETE</promise>"


async def test_abort_signal_raises_reason(repo, fake_claude):
    fake_claude.scenario([{"sleep": 30}])
    ctrl = AbortController()

    class Stop(Exception):
        pass

    asyncio.get_running_loop().call_later(0.5, ctrl.abort, Stop("user stop"))
    with pytest.raises(Stop):
        await run(agent=agent(), sandbox=no_sandbox(), prompt="x", cwd=str(repo), logging=STDOUT, signal=ctrl.signal)


async def test_agent_error_surfaces_stderr(repo, fake_claude):
    fake_claude.scenario([{"error": "invalid api key"}])
    with pytest.raises(AgentError) as e:
        await run(agent=agent(), sandbox=no_sandbox(), prompt="x", cwd=str(repo), logging=STDOUT)
    assert "invalid api key" in e.value.message and "exited with code 1" in e.value.message


async def test_validation_errors(repo, fake_claude):
    with pytest.raises(ValueError):
        await run(agent=agent(), sandbox=test_isolated(), prompt="x", cwd=str(repo), branch_strategy={"type": "head"})
    with pytest.raises(ValueError):
        await run(agent=agent(), sandbox=no_sandbox(), prompt="x", cwd=str(repo), copy_to_worktree=["a"])
    with pytest.raises(ValueError):
        await run(agent=agent(), sandbox=no_sandbox(), prompt="x", cwd=str(repo), resume_session="s", max_iterations=2)
    with pytest.raises(ValueError):
        await run(agent=agent(), sandbox=no_sandbox(), prompt="no tag", cwd=str(repo), output=Output.string("plan"))


# ---------------------------------------------------------------------------
# Prompts, hooks, logging
# ---------------------------------------------------------------------------


async def test_prompt_file_args_expansion_and_hooks(repo, fake_claude, tmp_path):
    pf = tmp_path / "prompt.md"
    pf.write_text("Issue {{N}} on {{SOURCE_BRANCH}} -> {{TARGET_BRANCH}}\n!`cat marker.txt`\n<!-- !`exit 1` -->")
    hooks = {
        "host": {"onWorktreeReady": [{"command": "echo host > host.txt"}]},
        "sandbox": {"onSandboxReady": [{"command": "echo from-hook > marker.txt"}]},
    }
    log = tmp_path / "run.log"
    r = await run(
        agent=agent(), sandbox=no_sandbox(), prompt_file=str(pf), prompt_args={"N": 7}, cwd=str(repo),
        hooks=hooks, logging={"type": "file", "path": str(log), "verbose": True},
    )
    prompt = fake_claude.calls()[0]["prompt"]
    assert prompt.startswith("Issue 7 on main -> main\nfrom-hook")
    assert "<!-- !`exit 1` -->" in prompt
    assert (repo / "host.txt").exists()
    text = log.read_text()
    assert "Agent started" in text and '"type": "system"' in text and r.log_file_path == str(log)


async def test_agent_stream_events(repo, fake_claude, tmp_path):
    events = []
    fake_claude.scenario([{"commit": "e.txt", "text": "hello there."}])
    await run(
        agent=agent(), sandbox=no_sandbox(), prompt="x", cwd=str(repo),
        logging={"type": "file", "path": str(tmp_path / "l.log"), "on_agent_stream_event": events.append},
    )
    kinds = {e.type for e in events}
    assert {"raw", "text", "toolCall"} <= kinds
    assert any(e.type == "toolCall" and e.name == "Bash" for e in events)


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


async def test_structured_output_with_retry(repo, fake_claude):
    from pydantic import BaseModel

    class Plan(BaseModel):
        n: int

    # A real `claude` writes its own session file on the host (no sandbox).
    fake_claude.scenario([{"output": " <plan>not json</plan>", "session": True}, {"output": ' <plan>{"n": 5}</plan>', "session": True}])
    r = await run(
        agent=claude_code("m"), sandbox=no_sandbox(), prompt="emit <plan>", cwd=str(repo), logging=STDOUT,
        output=Output.object("plan", Plan, max_retries=1),
    )
    assert r.output.n == 5
    second = fake_claude.calls()[1]
    assert "--resume" in second["argv"] and "did not produce valid structured output" in second["prompt"]


async def test_structured_output_error_carries_session(repo, fake_claude):
    fake_claude.scenario([{"output": " nothing"}])
    with pytest.raises(StructuredOutputError) as e:
        await run(agent=agent(), sandbox=no_sandbox(), prompt="emit <plan>", cwd=str(repo), logging=STDOUT, output=Output.string("plan"))
    assert e.value.session_id == "sess-0"


# ---------------------------------------------------------------------------
# Session capture / resume / fork through a bind-mount provider
# ---------------------------------------------------------------------------


def local_bind(sandbox_home):
    """A bind-mount provider whose 'sandbox' is the host itself (paths are
    identical), with its own agent home, to exercise session capture."""

    class H:
        def __init__(self, opts: BindMountCreateOptions):
            self.worktree_path = opts.worktree_path
            self.sandbox_homedir = str(sandbox_home)
            self._env = {**os.environ, **opts.env, "HOME": str(sandbox_home)}

        async def exec(self, command, *, on_line=None, cwd=None, sudo=False, stdin=None):
            return await run_process(["sh", "-c", command], cwd=cwd or self.worktree_path, env=self._env, stdin=stdin, on_line=on_line)

        async def copy_file_in(self, src, dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)

        async def copy_file_out(self, src, dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)

        async def close(self):
            pass

    async def create(opts):
        return H(opts)

    return create_bind_mount_sandbox_provider("local-bind", create)


async def test_session_capture_resume_and_fork(repo, fake_claude, tmp_path):
    sandbox_home = tmp_path / "sandbox-home"
    sandbox_home.mkdir()
    (sandbox_home / ".gitconfig").write_text("[user]\n\tname = T\n\temail = t@t\n")
    fake_claude.scenario([{"session": True, "signal": True}])
    a = claude_code("m")
    first = await run(agent=a, sandbox=local_bind(sandbox_home), prompt="plan", cwd=str(repo), logging=STDOUT)
    it = first.iterations[0]
    assert it.session_id == "sess-0" and it.session_file_path and os.path.exists(it.session_file_path)
    assert str(fake_claude.home) in it.session_file_path
    assert it.usage.input_tokens == 7
    second = await first.resume("now implement")
    assert second.iterations[0].session_id == "sess-0"
    third = await first.fork("branch off")
    assert third.iterations[0].session_id == "fork-of-sess-0"
    argv = fake_claude.calls()[2]["argv"]
    assert "--fork-session" in argv


# ---------------------------------------------------------------------------
# create_sandbox / create_worktree
# ---------------------------------------------------------------------------


async def test_create_sandbox_runs_twice_on_one_branch(repo, fake_claude):
    fake_claude.scenario([{"commit": "impl.txt"}, {"commit": "review.txt"}])
    async with await create_sandbox(branch="agent/sb", sandbox=no_sandbox(), cwd=str(repo), hooks={"sandbox": {"onSandboxReady": [{"command": "true"}]}}) as sb:
        a = await sb.run(agent=agent(env={"AGENT_MARKER": "yes"}), prompt="impl", logging=STDOUT)
        b = await sb.run(agent=agent(), prompt="review", logging=STDOUT)
        assert len(a.commits) == 1 and len(b.commits) == 1
        res = await sb.exec("git log --format=%s")
        assert res.exit_code == 0 and "add impl.txt" in res.stdout
        assert (await sb.exec("exit 3")).exit_code == 3
        wt = sb.worktree_path
    assert fake_claude.calls()[0]["env_marker"] == "yes"
    assert not os.path.exists(wt)
    assert git(repo, "log", "-1", "--format=%s", "agent/sb") == "add review.txt"


async def test_create_worktree_merge_to_head(repo, fake_claude):
    fake_claude.scenario([{"commit": "w1.txt"}, {"commit": "w2.txt"}])
    wt = await create_worktree(branch_strategy=merge_to_head(), cwd=str(repo))
    r1 = await wt.run(agent=agent(), sandbox=no_sandbox(), prompt="one", logging=STDOUT)
    r2 = await wt.run(agent=agent(), sandbox=no_sandbox(), prompt="two", logging=STDOUT)
    assert r1.branch == "main" and len(r1.commits) == 1 and len(r2.commits) == 1
    assert (repo / "w1.txt").exists() and (repo / "w2.txt").exists()
    closed = await wt.close()
    assert closed.preserved_worktree_path is None and not os.path.exists(wt.worktree_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_init_non_interactive(repo, monkeypatch):
    monkeypatch.chdir(repo)
    code = cli_main([
        "init", "--agent", "codex", "--model", "gpt-x", "--sandbox", "docker", "--issue-tracker", "beads",
        "--template", "simple-loop", "--build-image", "false",
    ])
    assert code == 0
    cfg = repo / ".sandcastle"
    main_py = (cfg / "main.py").read_text()
    assert 'codex("gpt-x")' in main_py and "docker(**sandbox_options)" in main_py and "/home/agent/host-beads" in main_py
    assert "bd ready --json" in (cfg / "prompt.md").read_text()
    assert "RUN npm install -g @openai/codex" in (cfg / "Dockerfile").read_text()
    assert "CODEX_API_KEY=" in (cfg / ".env.example").read_text()
    assert cli_main(["init", "--agent", "codex", "--sandbox", "docker", "--issue-tracker", "beads", "--template", "blank", "--build-image", "false"]) == 1


def test_cli_init_requires_flags_without_tty(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    assert cli_main(["init", "--agent", "claude-code"]) == 1
    out = capsys.readouterr().out
    assert "--sandbox, --issue-tracker, --template" in out


def test_cli_run_with_usage_limit(repo, fake_claude, monkeypatch):
    monkeypatch.chdir(repo)
    fake_claude.scenario([{"limit": 1}, {"commit": "cli.txt", "signal": True}])
    assert cli_main(["run", "--sandbox", "none", "-p", "go", "--reset-buffer", "0"]) == 0
    assert (repo / "cli.txt").exists()


# ---------------------------------------------------------------------------
# Real native / container sandboxes (skipped when unavailable)
# ---------------------------------------------------------------------------


def _bwrap_or_skip():
    from sandcastle.errors import NativeSandboxUnavailableError
    from sandcastle.sandboxes.bwrap import bwrap

    try:
        return bwrap()
    except NativeSandboxUnavailableError as e:
        pytest.skip(f"bwrap unavailable: {e.message.splitlines()[0]}")


async def test_bwrap_run_end_to_end(repo, fake_claude, tmp_path):
    _bwrap_or_skip()
    import json as _json

    from sandcastle.sandboxes.bwrap import bwrap

    # The sandbox hides $HOME (where this checkout lives) and the host /tmp,
    # and passes only the env it is given: give the fake agent its own copy,
    # a writable state dir mounted at its host path, and its config as env.
    here = os.path.dirname(os.path.abspath(__file__))
    shutil.copy(os.path.join(here, "fake_claude.py"), tmp_path / "bin" / "fake_claude.py")
    (tmp_path / "bin" / "claude").write_text(f"#!/bin/sh\nexec python3 {tmp_path / 'bin' / 'fake_claude.py'} \"$@\"\n")
    fx = tmp_path / "fx"
    fx.mkdir()
    scenario = [{"limit": 1}, {"commit": "bw.txt", "signal": True, "session": True}]
    sb = bwrap(
        mounts=[{"host_path": str(fx), "sandbox_path": str(fx)}],
        env={"FAKE_STATE": str(fx / "state"), "FAKE_LOG": str(fx / "calls.jsonl"), "FAKE_SCENARIO": _json.dumps(scenario)},
    )
    r = await run(
        agent=claude_code("m"), sandbox=sb, prompt="x", cwd=str(repo), logging=STDOUT, usage_limit=FAST,
        branch_strategy=merge_to_head(),
    )
    assert len(r.commits) == 1 and (repo / "bw.txt").exists()
    it = r.iterations[0]
    assert it.usage_limit_waits == 1 and it.session_id == "sess-0"
    # Captured out of the sandbox's private home into the host's ~/.claude.
    assert it.session_file_path and os.path.exists(it.session_file_path)
    assert it.session_file_path.startswith(str(fake_claude.home))
    calls = [_json.loads(l) for l in (fx / "calls.jsonl").read_text().splitlines()]
    assert len(calls) == 2 and "--resume" in calls[1]["argv"]


async def test_docker_provider_handle_mechanics(tmp_path):
    image = os.environ.get("SANDCASTLE_DOCKER_TEST_IMAGE")
    if not image or not shutil.which("docker"):
        pytest.skip("set SANDCASTLE_DOCKER_TEST_IMAGE to an image with sh to run")
    from sandcastle.sandbox_provider import BindMountCreateOptions, Mount
    from sandcastle.sandboxes.docker import docker

    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "in.txt").write_text("host file\n")
    h = await docker(image_name=image).create(
        BindMountCreateOptions(worktree_path=str(wt), host_repo_path=str(wt), mounts=[Mount(str(wt), "/home/agent/workspace")], env={"K": "v"})
    )
    try:
        lines = []
        r = await h.exec("echo $K; cat in.txt; echo err >&2; exit 4", on_line=lines.append, cwd="/home/agent/workspace")
        assert (r.exit_code, lines, r.stderr.strip()) == (4, ["v", "host file"], "err")
        assert (await h.exec("cat", stdin="piped")).stdout == "piped"
        assert (await h.exec("id -u", sudo=True)).stdout.strip() == "0"
        await h.copy_file_out("/home/agent/workspace/in.txt", str(tmp_path / "out.txt"))
        assert (tmp_path / "out.txt").read_text() == "host file\n"
    finally:
        await h.close()

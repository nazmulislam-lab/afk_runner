# afk_runner: sandcastle for Python

A full Python port of [mattpocock/sandcastle](https://github.com/mattpocock/sandcastle): run AI
coding agents (Claude Code, Codex, pi, Cursor, OpenCode, Copilot) unattended, inside a sandbox,
on their own git branch. Their commits come back to you.

It also adds one feature the original doesn't have:

> **When the agent hits its usage limit (for example Claude's 5-hour window), sandcastle waits
> for the window to reset and resumes the same agent session as soon as it does.** No restart from
> scratch and no babysitting. This is on by default.

Pure Python 3.9+, no required dependencies. You need `git` and the agent's CLI (for example
`claude`) on your machine.

---

## Install

```bash
git clone https://github.com/nazmulislam-lab/afk_runner
cd afk_runner
pip install -e .              # adds the `sandcastle` command (also `afk-runner`)
pip install -e '.[planner]'   # + pydantic, for the parallel-planner templates
```

On Ubuntu 24.04 and later pip may refuse system-wide installs. Use `pipx install -e .`, a
virtualenv, or skip installing and run `python -m sandcastle ...` from the checkout.

## The fastest way: `sandcastle run`

```bash
# Run Claude directly on your machine, in this repo, until it prints <promise>COMPLETE</promise>
# or reaches 20 iterations. If the 5-hour limit hits, it waits and resumes automatically.
sandcastle run --sandbox none -n 20 -f task.md

# The same, but on a throwaway branch that is merged back into your current branch at the end
sandcastle run --sandbox none --merge-to-head -n 20 -p "Fix every failing test, then print <promise>COMPLETE</promise>"

# Contained in Bubblewrap (Linux / WSL2): the agent can't see your home dir or other repos
sandcastle run --sandbox bwrap -n 20 -f task.md

# In Docker (run `sandcastle init` first to get a Dockerfile, then `sandcastle docker build-image`)
sandcastle run --sandbox docker -n 20 -f task.md
```

Useful flags: `--agent codex|pi|cursor|opencode|copilot`, `-m MODEL`, `--effort high`,
`--branch agent/fix-42`, `--setup "npm install"`, `--log-file run.log`, `--max-waits 3`,
`--reset-buffer 60`, `--no-wait-for-reset`. Run `sandcastle run --help` for all of them.

> ⚠️ AFK agents run with their own permission prompts **off** (Claude gets
> `--dangerously-skip-permissions`), exactly like the original. With `--sandbox none` that means
> directly on your machine. Prefer `bwrap`, `seatbelt` or `docker` for anything you don't fully trust.
> Use `--permission-mode` to give Claude a permission mode instead.

To keep it running after you close the terminal, use `tmux`, `screen` or
`nohup sandcastle run ... > afk.out 2>&1 &`.

## How the usage-limit auto-resume works

1. Sandcastle reads the agent's own output. When it sees a limit message such as
   `Claude AI usage limit reached|<epoch>`, `5-hour limit reached ∙ resets 3pm`,
   `You've hit your limit · resets 11:30pm (Asia/Dhaka)`, `try again in 2h 15m` or
   `resets Nov 20, 3pm`, it works out the exact reset time, time zone included.
2. It keeps the sandbox, prints `Usage limit reached. Waiting until 2026-11-18 23:31 (4h 12m)…`, and
   posts a status line every 15 minutes while it waits. The wait can be aborted with Ctrl-C or an
   `AbortSignal`.
3. Once the reset time plus `buffer_seconds` (default 60) has passed, it **resumes the same session**
   (`claude --resume <id>`, `codex exec resume <id>`, …) with a short "the limit has reset, carry on"
   prompt, so the agent keeps all its context.
4. If the reset time can't be read, it retries every `fallback_wait_minutes` (default 15).
   `max_waits` (default unlimited) caps how many times it will wait. The idle timeout doesn't run
   while it waits.

From Python:

```python
from sandcastle import UsageLimitConfig

await run(..., usage_limit=UsageLimitConfig(buffer_seconds=120, fallback_wait_minutes=10, max_waits=5))
await run(..., wait_for_usage_reset=False)   # original behaviour: fail with AgentError
```

Each `IterationResult` reports `usage_limit_waits`. Cloud sandboxes have their own lifetimes (E2B
defaults to 30 minutes, Modal to 1 hour). For multi-hour waits, raise their `timeout_ms` or use a
local sandbox (`none`, `bwrap`, `seatbelt`, `docker`, `podman`).

---

## Python API

Everything from the TypeScript library is here, in `snake_case`. The API is `async`, and `run_sync()`
is a blocking wrapper around `run()`.

```python
import asyncio
from sandcastle import run, claude_code, merge_to_head
from sandcastle.sandboxes.docker import docker

async def main():
    result = await run(
        agent=claude_code("claude-opus-4-8", effort="high"),
        sandbox=docker(),
        prompt_file=".sandcastle/prompt.md",
        prompt_args={"ISSUE_NUMBER": 42},
        max_iterations=5,
        branch_strategy=merge_to_head(),
        hooks={"sandbox": {"onSandboxReady": [{"command": "npm install", "timeoutMs": 300_000}]}},
        copy_to_worktree=["node_modules"],
    )
    print(len(result.iterations), result.completion_signal, result.commits, result.branch)
    follow_up = await result.resume("Now write the docs")   # same session, one iteration
    fork = await result.fork("Try another approach", branch_strategy={"type": "branch", "branch": "alt"})

asyncio.run(main())
```

### `run()` options

| Option | Default | Meaning |
| --- | --- | --- |
| `agent` | required | `claude_code()`, `codex()`, `pi()`, `cursor()`, `opencode()`, `copilot()` or your own `AgentProvider` |
| `sandbox` | required | a sandbox provider (see below) |
| `cwd` | current dir | host repo (anchor for `.sandcastle/` and git) |
| `prompt` / `prompt_file` | one required | inline prompts are passed literally; prompt files get `{{KEY}}` and `` !`cmd` `` expansion |
| `prompt_args` | — | values for `{{KEY}}`; `SOURCE_BRANCH` / `TARGET_BRANCH` are built in |
| `max_iterations` | 1 | the agent loop; it stops early on the completion signal |
| `completion_signal` | `<promise>COMPLETE</promise>` | string or list of strings |
| `branch_strategy` | `head` (bind-mount / none), `merge-to-head` (isolated) | `head()`, `merge_to_head()`, `branch("name", base_branch=None)` or a TS-style dict |
| `hooks` | — | `{"host": {"onWorktreeReady": [...], "onSandboxReady": [...]}, "sandbox": {"onSandboxReady": [{"command", "sudo", "timeoutMs"}]}}` (or a `SandboxHooks`) |
| `copy_to_worktree` | — | host paths copied into the worktree (not with `head`) |
| `logging` | log file under `.sandcastle/logs/` | `{"type": "stdout"}` for the terminal UI, or `{"type": "file", "path": ..., "verbose": True, "on_agent_stream_event": fn}` |
| `idle_timeout_seconds` | 600 | no output for this long → `AgentIdleTimeoutError` |
| `completion_timeout_seconds` | 60 | grace period for an agent that printed the signal but hasn't exited |
| `resume_session` | — | resume a captured session id (one iteration only) |
| `signal` | — | an `AbortSignal` (`AbortController().signal`); aborting kills the agent and raises the abort reason |
| `timeouts` | — | `{"copyToWorktreeMs", "gitSetupMs", "commitCollectionMs", "mergeToHostMs"}` |
| `output` | — | `Output.object(tag, schema, max_retries=0)` / `Output.string(tag)`; the result goes to `result.output` |
| `wait_for_usage_reset` | `True` | **port extra:** wait out usage limits and resume |
| `usage_limit` | `UsageLimitConfig()` | **port extra:** tuning for the above |

`RunResult` has `iterations` (each with `session_id`, `session_file_path`, `usage`, `context_window`,
`usage_limit_waits`), `completion_signal`, `stdout`, `commits`, `branch`, `log_file_path`,
`preserved_worktree_path`, `output`, `resume()` and `fork()`.

### Structured output

```python
from pydantic import BaseModel
from sandcastle import Output

class Review(BaseModel):
    summary: str
    score: int

result = await run(..., prompt="Review the code. Reply with JSON inside <result> tags.",
                   output=Output.object("result", Review, max_retries=2))
print(result.output.score)
```

The schema can be a pydantic model, a `TypeAdapter`, any object with `.validate()`, a callable, a
JSON-schema `dict` (if `jsonschema` is installed), or `None`. If parsing fails, `run()` raises
`StructuredOutputError`, which carries `tag`, `raw_matched`, `cause`, `commits`, `branch` and
`session_id`. With `max_retries > 0`, sandcastle resumes the session and asks the agent to fix its
output.

### `create_sandbox()`: one warm sandbox, many runs

```python
from sandcastle import create_sandbox, claude_code

async with await create_sandbox(branch="agent/fix-42", sandbox=docker(),
                                hooks={"sandbox": {"onSandboxReady": [{"command": "npm install"}]}}) as sb:
    await sb.run(agent=claude_code("claude-opus-4-8"), prompt_file=".sandcastle/implement.md", max_iterations=5)
    tests = await sb.exec("npm test")              # a non-zero exit code is returned, not raised
    if tests.exit_code == 0:
        await sb.run(agent=claude_code("claude-sonnet-4-6"), prompt="Review the changes and fix any issues.")
# close(): removes the container, then removes the worktree unless it has uncommitted changes
```

### `create_worktree()`: a worktree as an object

```python
from sandcastle import create_worktree, branch

async with await create_worktree(branch_strategy=branch("agent/fix-42"), copy_to_worktree=["node_modules"]) as wt:
    await wt.interactive(agent=claude_code("claude-opus-4-8"), prompt="Explore the bug with me.")
    await wt.run(agent=claude_code("claude-opus-4-8"), sandbox=docker(), prompt="Fix issue #42.", max_iterations=3)
    sb = await wt.create_sandbox(sandbox=docker())   # sb.close() leaves the worktree to wt.close()
```

### `interactive()`

`await interactive(agent=claude_code("claude-opus-4-8"), sandbox=docker(), prompt="...")` opens the
agent's own TUI in the sandbox (the default sandbox is `no_sandbox()`), then collects commits and
merges like `run()`. Any `{{KEY}}` without a value is asked for on the terminal. Native sandboxes
don't support it and raise `NativeInteractiveNotSupportedError`.

## Agent providers

| Factory | CLI | Resume | Fork | Options |
| --- | --- | --- | --- | --- |
| `claude_code(model)` | `claude` | `--resume` | `--fork-session` | `effort`, `permission_mode`, `env`, `capture_sessions`, `session_storage` |
| `codex(model)` | `codex exec` | `exec resume` | `exec fork` | `effort`, `approvals_reviewer="auto_review"`, `env`, … |
| `pi(model)` | `pi` | `--session` | `--fork` | `thinking`, `env`, … |
| `opencode(model)` | `opencode run` | `--session` | `--session --fork` | `variant`, `agent`, `env`, … |
| `copilot(model)` | `copilot` | `--resume=` | ✗ (`ForkNotSupportedError`) | `effort`, `env`, … |
| `cursor(model)` | `agent` | `--resume=` | ✗ (`ForkNotSupportedError`) | `env`, … |

After each iteration, sessions are captured from the sandbox into your real
`~/.claude/projects/...`, `~/.codex/sessions/...` and so on, with `cwd` fields rewritten. That means
`claude --resume <id>` works on the host afterwards. To write your own agent provider, subclass
`sandcastle.AgentProvider`.

## Sandbox providers

| Import | Kind | Notes |
| --- | --- | --- |
| `sandcastle.sandboxes.no_sandbox.no_sandbox()` | none | runs on the host |
| `sandcastle.sandboxes.bwrap.bwrap()` | native (Linux/WSL2) | Bubblewrap: private home and `/tmp`, own PID namespace, host toolchain read-only, git control files read-only; gives the fix command if bwrap is missing or blocked by AppArmor |
| `sandcastle.sandboxes.seatbelt.seatbelt()` | native (macOS 14+) | `sandbox-exec` deny-by-default profile, same guarantees |
| `sandcastle.sandboxes.docker.docker()` | bind-mount | `image_name`, `mounts`, `env`, `network`, `groups`, `cap_drop`, `devices`, `cpus`, `memory`, `pids_limit`, `selinux_label`, `container_uid/gid` |
| `sandcastle.sandboxes.podman.podman()` | bind-mount | same options plus `userns` |
| `sandcastle.sandboxes.e2b.e2b()` | isolated | `pip install e2b`; `template="claude"` |
| `sandcastle.sandboxes.daytona.daytona()` | isolated | `pip install daytona` |
| `sandcastle.sandboxes.modal.modal()` | isolated | `pip install modal`; builds `node:24-bookworm` + Claude Code by default |
| `sandcastle.sandboxes.sprites.sprites()` | isolated | `pip install sprites-py`; `SPRITES_TOKEN` |
| `sandcastle.sandboxes.vercel.vercel()` | isolated | `pip install vercel-sandbox`; `VERCEL_TOKEN` + team/project or OIDC |

Native sandboxes hide your home directory, so the agent can't use the logins stored there. Put
`CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) or `ANTHROPIC_API_KEY`, plus `GH_TOKEN` if
needed, in `.sandcastle/.env`. Only keys declared there are forwarded. An empty value is taken from
your shell environment.

For your own providers, use `create_bind_mount_sandbox_provider(name, create)` or
`create_isolated_sandbox_provider(name, create)`. The handle needs async `exec(command, on_line=,
cwd=, sudo=, stdin=)`, which must stream lines, plus `copy_file_in`/`copy_file_out` (bind-mount) or
`copy_in`/`copy_file_out` (isolated), and `close`.

Security behaviours carried over from the original:

- Host-side git runs through a hardened wrapper. It neutralises repo-controlled `core.fsmonitor`,
  hooks, filters, credential helpers and similar settings, pins `GIT_DIR`, drops repo-local `PATH`
  entries, and restricts transports.
- Git control files are mounted read-only into bind-mount sandboxes.
- Sync-out from isolated sandboxes refuses any path or symlink that escapes the worktree or points
  into `.git`.

## `sandcastle init` and templates

```bash
sandcastle init                                   # interactive
sandcastle init --agent claude-code --sandbox docker --issue-tracker github-issues \
  --template simple-loop --create-label true --build-image true
sandcastle docker build-image  [--image-name N] [--dockerfile PATH]
sandcastle docker remove-image [--image-name N]
sandcastle podman build-image  [--image-name N] [--containerfile PATH]
sandcastle podman remove-image [--image-name N]
```

`init` scaffolds `.sandcastle/`, which contains `main.py` (run it with `python .sandcastle/main.py`),
the prompts, a `Dockerfile`/`Containerfile` (none for bwrap/seatbelt), `.env.example` and
`.gitignore`. Templates: `blank`, `simple-loop`, `sequential-reviewer`, `parallel-planner` and
`parallel-planner-with-review`. Issue trackers: GitHub Issues, Beads, or Custom (which writes a
`SETUP_ISSUE_TRACKER.md` for your agent to follow). The Dockerfiles, prompts and tracker commands are
the original's, verbatim.

## Differences from the TypeScript original

- The API is `async`/`await`. Options are `snake_case`, and TS-style dicts (`{"type": "branch", ...}`,
  `onSandboxReady`, `timeoutMs`) are also accepted.
- Added: usage-limit wait-and-resume (`wait_for_usage_reset`, `UsageLimitConfig`,
  `UsageLimitWaitError`) and `sandcastle run`.
- The scaffolded main file is Python (`main.py`). The planners validate `<plan>` with pydantic instead
  of Zod. Template-dependency installs use pip.
- `Output.object()` takes pydantic, callables or JSON schema in place of Standard Schema validators.
- The terminal UI is a dependency-free, clack-style renderer.
- Cloud providers use each vendor's official Python SDK. Prompts that go over stdin are delivered
  through a temp file where an SDK has no stdin streaming (E2B, Daytona; the original does the same
  for Daytona and Vercel).
- Native Windows (outside WSL2) is best effort, as in the original. WSL2 + `bwrap()` is the
  recommended setup.

## Development

```bash
pip install -e '.[dev]'
pytest            # unit tests + end-to-end runs with a scriptable fake `claude`
SANDCASTLE_DOCKER_TEST_IMAGE=redis:7.4 pytest -k docker   # opt-in real Docker test
```

The bwrap end-to-end test runs automatically when Bubblewrap is usable.

MIT licensed: see `LICENSE` (original © Matt Pocock).

# afk_runner

**Give Claude a task, walk away, and come back to finished commits, even if you hit your usage limit halfway through.**

afk_runner is a complete Python port of Matt Pocock's [sandcastle](https://github.com/mattpocock/sandcastle).
It runs AI coding agents (Claude Code, Codex, pi, Cursor, OpenCode, Copilot) **AFK**, "away from keyboard":
unattended, in a loop, optionally inside a sandbox and on their own git branch.

It adds one thing the original doesn't have:

> 🔁 **Auto-resume after usage limits.** When Claude hits its 5-hour limit, afk_runner reads the reset
> time, waits, and continues **the same conversation** right after the limit resets. You don't have to
> babysit it or start over.

An example of what you'll see while it waits (shortened):

```text
$ sandcastle run --sandbox none -n 20 -f task.md
...
▲  Usage limit reached. Waiting until 2026-11-19 03:01 +06 (4h 12m) for the window to reset, then resuming.
●  Still waiting for the usage limit to reset: 3h 57m left.
...
✔  Usage window reset. Resuming the agent session.
```

---

## Contents

- [Quick start (5 minutes)](#quick-start-5-minutes)
- [Writing a good task file](#writing-a-good-task-file)
- [Everyday recipes](#everyday-recipes)
- [Which sandbox should I use?](#which-sandbox-should-i-use)
- [How auto-resume works](#how-auto-resume-works)
- [Project setup with `sandcastle init`](#project-setup-with-sandcastle-init)
- [Using it from Python](#using-it-from-python)
- [Troubleshooting](#troubleshooting)
- [Full reference](#full-reference)

---

## Quick start (5 minutes)

### 1. Make sure you have the basics

| You need | Check with | Get it |
| --- | --- | --- |
| Python 3.9+ | `python3 --version` | [python.org](https://www.python.org/downloads/) |
| git | `git --version` | [git-scm.com](https://git-scm.com/) |
| Claude Code, logged in | `claude --version` | `npm install -g @anthropic-ai/claude-code`, then run `claude` once to log in |

### 2. Get afk_runner

```bash
git clone https://github.com/nazmulislam-lab/afk_runner
cd afk_runner
```

Nothing to install: you can now run it from this folder with `python3 -m sandcastle`.

Do you want a `sandcastle` command that works from any folder? Pick one:

```bash
pipx install -e .          # easiest (get pipx with: sudo apt install pipx)
# or, in a virtualenv:
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

> On Ubuntu, a plain `pip install` may fail with "externally-managed-environment". That's normal.
> Use one of the options above instead.

The examples below use `sandcastle`. If you didn't install it, type `python3 -m sandcastle` instead,
run from the afk_runner folder, and point it at your project with `-C /path/to/your/project`.

### 3. Where does my login / API key go?

**With `--sandbox none`, nowhere.** afk_runner just runs your normal `claude` command, so it uses
whatever login Claude Code already has: your Pro/Max subscription from running `claude` once, or an
`ANTHROPIC_API_KEY` in your shell. There's no extra setup.

**With a sandbox** (`bwrap`, `seatbelt`, `docker`, …), your home folder is hidden, so put a token in
`.sandcastle/.env` **inside the project you're working on** (not inside afk_runner):

```bash
claude setup-token                        # prints a long-lived token for your subscription
cd ~/my-project
mkdir -p .sandcastle
echo "CLAUDE_CODE_OAUTH_TOKEN=paste-token-here" >> .sandcastle/.env
# or, to pay per use instead:  echo "ANTHROPIC_API_KEY=sk-ant-..." >> .sandcastle/.env
echo ".sandcastle/.env" >> .gitignore     # never commit it
```

`sandcastle init` creates this folder for you, with a `.env.example` to copy.

### 4. Go to your project and write a task

afk_runner works on **any git repository**. Commit or stash your own work first, so the agent's changes
are easy to review.

```bash
cd ~/my-project
```

Create `task.md`:

```markdown
Add input validation to the signup form in src/signup.py.

- Reject empty emails and passwords shorter than 8 characters.
- Add tests for each rule and make sure `pytest` passes.
- Commit your work with a clear message.

When everything is done and committed, print <promise>COMPLETE</promise>
```

### 5. Run it

```bash
sandcastle run --sandbox none -n 20 -f task.md
```

What this does:

- `--sandbox none` runs Claude directly on your machine. See [Which sandbox should I use?](#which-sandbox-should-i-use).
- `-n 20` allows up to 20 rounds. Each round is a fresh Claude run on the same task.
- It stops early once Claude prints `<promise>COMPLETE</promise>`.
- If the 5-hour limit hits, it waits and resumes on its own.

### 6. Review the result

```bash
git log --oneline     # see what Claude committed
git show              # inspect the latest change
```

That's it. 🎉

> ⚠️ **Read this once.** To work unattended, Claude runs with its permission prompts turned off
> (`--dangerously-skip-permissions`), just like the original sandcastle. With `--sandbox none` it can
> run any command on your computer. For code or tasks you don't fully trust, use
> `--sandbox bwrap` (Linux) or `--sandbox docker`.

---

## Writing a good task file

Each round starts fresh: Claude sees your task file, plus whatever it finds in the repo. Good task files:

1. **Say what "done" looks like.** For example: "tests pass", "the endpoint returns 200", "the README
   documents X".
2. **Ask it to commit.** Commits are how work survives between rounds and comes back to you.
3. **End with the completion signal.** Use "When finished, print `<promise>COMPLETE</promise>`", so
   the loop knows when to stop.
4. **For big jobs, ask it to keep notes.** For example: "Keep a checklist in `PROGRESS.md`. Read it at
   the start, update it as you go." Each round then picks up where the last one stopped.

Task files can also pull in live information:

```markdown
Fix issue #{{ISSUE}}. Here it is:

!`gh issue view {{ISSUE}}`
```

`{{ISSUE}}` is filled in from Python (`prompt_args={"ISSUE": 42}`), and `` !`command` `` is replaced
with that command's output before Claude sees it.

---

## Everyday recipes

```bash
# Simplest: work directly on your current branch
sandcastle run --sandbox none -n 20 -f task.md

# Quick one-line task, no file needed
sandcastle run --sandbox none -p "Fix all the failing tests and commit. Then print <promise>COMPLETE</promise>"

# Work on a temporary branch, then merge it back into your branch when finished
sandcastle run --sandbox none --merge-to-head -n 20 -f task.md

# Leave the work on its own branch for you to review later
sandcastle run --sandbox none --branch agent/signup-validation -n 20 -f task.md

# Pick a model and effort level
sandcastle run --sandbox none -m claude-opus-4-8 --effort high -n 20 -f task.md

# Use a different agent
sandcastle run --sandbox none --agent codex -n 20 -f task.md

# Safer: run inside Bubblewrap (Linux / WSL2)
sandcastle run --sandbox bwrap -n 20 -f task.md

# Save the log to a file instead of the terminal
sandcastle run --sandbox none -n 20 -f task.md --log-file afk.log
```

### Keep it running after you close the terminal

```bash
tmux new -s afk                                   # start a session
sandcastle run --sandbox none -n 50 -f task.md
# press Ctrl-B then D to detach. Later: tmux attach -t afk
```

Without tmux: `nohup sandcastle run --sandbox none -n 50 -f task.md > afk.out 2>&1 &`

### Useful flags

| Flag | What it does |
| --- | --- |
| `-f FILE` / `-p TEXT` | Task from a file, or inline text |
| `-n N` | Maximum rounds (default 1) |
| `--sandbox` | `none`, `bwrap`, `seatbelt`, `docker` or `podman` |
| `--agent` | `claude-code` (default), `codex`, `pi`, `cursor`, `opencode`, `copilot` |
| `-m`, `--effort` | Model and reasoning effort |
| `--branch NAME` | Commit on this branch, in a separate worktree |
| `--merge-to-head` | Use a temporary branch and merge back at the end |
| `--setup "CMD"` | Run a command first, e.g. `--setup "pip install -r requirements.txt"` |
| `-C DIR` | Run against a project in another folder |
| `--log-file FILE` | Write the log to a file |
| `--idle-timeout S` | Give up if the agent prints nothing for S seconds (default 600) |
| `--no-wait-for-reset` | Stop instead of waiting when the usage limit is hit |
| `--max-waits N` | Wait for at most N limit resets |
| `--reset-buffer S` | Extra seconds to wait after the reset time (default 60) |
| `--permission-mode MODE` | Use a Claude permission mode instead of skipping prompts |

`sandcastle run --help` lists everything.

---

## Which sandbox should I use?

| Sandbox | Best for | Setup |
| --- | --- | --- |
| `none` | Your own trusted tasks; the fastest start | Nothing |
| `bwrap` | **Recommended on Linux / WSL2.** The agent can't see your home folder or other projects | `sudo apt install bubblewrap`, plus a token (below) |
| `seatbelt` | The same idea on macOS 14+ | A token (below) |
| `docker` / `podman` | Full container isolation, with its own tools installed | `sandcastle init`, then `sandcastle docker build-image` |
| E2B, Daytona, Modal, Sprites, Vercel | Running in the cloud | From Python only; see [Sandbox providers](#sandbox-providers) |

**Logging in inside a sandbox.** Sandboxes hide your home folder, so Claude can't use your normal
login. Run `claude setup-token` once, then put the token in `.sandcastle/.env` inside your project:

```bash
mkdir -p .sandcastle
echo "CLAUDE_CODE_OAUTH_TOKEN=paste-your-token-here" >> .sandcastle/.env
echo ".sandcastle/.env" >> .gitignore          # never commit tokens
```

`ANTHROPIC_API_KEY=...` works too. Add `GH_TOKEN=...` if your task uses the GitHub CLI. Only the keys
listed in `.sandcastle/.env` are passed in. A key with an empty value is taken from your shell
environment.

---

## How auto-resume works

1. **Detect.** afk_runner watches Claude's output for limit messages, such as `5-hour limit reached ∙
   resets 3pm`, `You've hit your limit · resets 11:30pm (Asia/Dhaka)`, `try again in 2h 15m` or
   `Claude AI usage limit reached|<timestamp>`.
2. **Work out when.** It turns that message into an exact reset time, including the time zone.
3. **Wait.** It prints when it will resume, then a status line every 15 minutes. Your branch and
   sandbox stay as they are. Press Ctrl-C at any time to stop.
4. **Resume.** A minute after the reset (`--reset-buffer`), it continues **the same Claude session**
   (`claude --resume <id>`) with a short "the limit has reset, carry on" message. Claude keeps all its
   context, so it doesn't redo work.

If it can't read the reset time, it tries again every 15 minutes (`--fallback-wait`). It never gives
up unless you set `--max-waits`.

It works the same way for Codex, pi, OpenCode, Copilot and Cursor.

---

## Project setup with `sandcastle init`

Running something regularly? Scaffold a reusable setup in your project:

```bash
cd ~/my-project
sandcastle init
```

It asks a few questions (which agent, which sandbox, which issue tracker, which template) and creates a
`.sandcastle/` folder with:

- `main.py`, your runner script. Run it with `python3 .sandcastle/main.py`.
- The prompt files used by the template.
- A `Dockerfile` or `Containerfile` (for the docker and podman sandboxes).
- `.env.example`. Copy it to `.env` and add your tokens.

| Template | What it does |
| --- | --- |
| `blank` | A bare starting point |
| `simple-loop` | Works through your open issues one by one |
| `sequential-reviewer` | Implements an issue, then a second pass reviews it |
| `parallel-planner` | Plans which issues can be done in parallel, then runs one agent per branch |
| `parallel-planner-with-review` | The same, plus a review step per branch |

Issue trackers: GitHub Issues, Beads, or Custom (it writes setup notes for your agent to follow).

For non-interactive use:

```bash
sandcastle init --agent claude-code --sandbox docker --issue-tracker github-issues --template simple-loop
sandcastle docker build-image
python3 .sandcastle/main.py
```

The planner templates need pydantic: `pip install -e '.[planner]'`.

---

## Using it from Python

Everything the CLI does, and much more, is available as a library:

```python
import asyncio
from sandcastle import run, claude_code, merge_to_head
from sandcastle.sandboxes.no_sandbox import no_sandbox

async def main():
    result = await run(
        agent=claude_code("claude-opus-4-8"),
        sandbox=no_sandbox(),
        prompt_file="task.md",
        max_iterations=20,
        branch_strategy=merge_to_head(),
    )
    print(f"{len(result.iterations)} rounds, {len(result.commits)} commits")

asyncio.run(main())
```

Don't want `async`? Use `run_sync(...)` with the same arguments.

The [Full reference](#full-reference) has more examples:

- structured output
- reusing a warm sandbox for several runs
- worktrees and interactive mode
- resuming or forking a session

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `externally-managed-environment` when installing | Use `pipx install -e .`, a virtualenv, or run `python3 -m sandcastle` from the checkout |
| `claude: command not found` | Install Claude Code (`npm install -g @anthropic-ai/claude-code`) and make sure it's on your `PATH` |
| Claude says it isn't logged in (bwrap, seatbelt, docker) | Add `CLAUDE_CODE_OAUTH_TOKEN` to `.sandcastle/.env` ([see above](#which-sandbox-should-i-use)) |
| bwrap fails with "permission denied" or an AppArmor error | afk_runner prints the exact command that fixes it. Ubuntu 24.04 blocks this by default |
| It stopped after one round | The default is `-n 1`. Pass `-n 20` or more |
| It never stops | Make sure your task tells Claude to print `<promise>COMPLETE</promise>` when done |
| "Agent idle timeout" | Claude printed nothing for 10 minutes. Raise the limit with `--idle-timeout 1800` |
| Where are the logs? | In the terminal by default, or in the file `--log-file` names |
| I want to continue the session myself | Sessions are copied to your normal `~/.claude/projects/`, so `claude --resume <id>` works |

---

## Full reference

<details>
<summary><b><code>run()</code> options</b></summary>

| Option | Default | Meaning |
| --- | --- | --- |
| `agent` | required | `claude_code()`, `codex()`, `pi()`, `cursor()`, `opencode()`, `copilot()`, or your own `AgentProvider` |
| `sandbox` | required | A sandbox provider (see below) |
| `cwd` | current dir | The host repo (anchor for `.sandcastle/` and git) |
| `prompt` / `prompt_file` | one required | Inline prompts are passed literally. Prompt files get `{{KEY}}` and `` !`cmd` `` expansion |
| `prompt_args` | — | Values for `{{KEY}}`. `SOURCE_BRANCH` and `TARGET_BRANCH` are built in |
| `max_iterations` | 1 | Number of rounds. Stops early on the completion signal |
| `completion_signal` | `<promise>COMPLETE</promise>` | A string or a list of strings |
| `branch_strategy` | `head` (bind-mount / none), `merge-to-head` (isolated) | `head()`, `merge_to_head()`, `branch("name", base_branch=None)`, or a TS-style dict |
| `hooks` | — | `{"host": {"onWorktreeReady": [...], "onSandboxReady": [...]}, "sandbox": {"onSandboxReady": [{"command", "sudo", "timeoutMs"}]}}` |
| `copy_to_worktree` | — | Host paths copied into the worktree (not with `head`) |
| `logging` | log file under `.sandcastle/logs/` | `{"type": "stdout"}` for the terminal UI, or `{"type": "file", "path": ..., "verbose": True, "on_agent_stream_event": fn}` |
| `idle_timeout_seconds` | 600 | No output for this long raises `AgentIdleTimeoutError` |
| `completion_timeout_seconds` | 60 | Grace period for an agent that printed the signal but hasn't exited |
| `resume_session` | — | Resume a captured session id (one iteration only) |
| `signal` | — | An `AbortSignal` (`AbortController().signal`). Aborting kills the agent |
| `timeouts` | — | `{"copyToWorktreeMs", "gitSetupMs", "commitCollectionMs", "mergeToHostMs"}` |
| `output` | — | `Output.object(tag, schema, max_retries=0)` or `Output.string(tag)`. The result goes to `result.output` |
| `wait_for_usage_reset` | `True` | **afk_runner extra:** wait out usage limits and resume |
| `usage_limit` | `UsageLimitConfig()` | **afk_runner extra:** `buffer_seconds=60`, `fallback_wait_minutes=15`, `max_waits=None`, `max_wait_hours`, `continue_prompt` |

`RunResult` has:

- `iterations`, each with `session_id`, `session_file_path`, `usage`, `context_window` and
  `usage_limit_waits`
- `completion_signal`, `stdout`, `commits` and `branch`
- `log_file_path`, `preserved_worktree_path` and `output`
- `resume(prompt)` and `fork(prompt)`

</details>

<details>
<summary><b>A bigger example: Docker, hooks, resume and fork</b></summary>

```python
from sandcastle import run, claude_code, merge_to_head
from sandcastle.sandboxes.docker import docker

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
follow_up = await result.resume("Now write the docs")          # same session, one more round
alt = await result.fork("Try another approach", branch_strategy={"type": "branch", "branch": "alt"})
```

</details>

<details>
<summary><b>Structured output</b></summary>

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

The schema can be any of these:

- a pydantic model or a `TypeAdapter`
- an object with `.validate()`, or a plain callable
- a JSON-schema `dict` (needs `jsonschema` installed)
- `None`

If parsing fails, `run()` raises `StructuredOutputError`. With `max_retries > 0`, the agent is asked to
fix its output first.

</details>

<details>
<summary><b><code>create_sandbox()</code>: one warm sandbox, many runs</b></summary>

```python
from sandcastle import create_sandbox, claude_code
from sandcastle.sandboxes.docker import docker

async with await create_sandbox(branch="agent/fix-42", sandbox=docker(),
                                hooks={"sandbox": {"onSandboxReady": [{"command": "npm install"}]}}) as sb:
    await sb.run(agent=claude_code("claude-opus-4-8"), prompt_file=".sandcastle/implement.md", max_iterations=5)
    tests = await sb.exec("npm test")              # a non-zero exit code is returned, not raised
    if tests.exit_code == 0:
        await sb.run(agent=claude_code("claude-sonnet-4-6"), prompt="Review the changes and fix any issues.")
```

</details>

<details>
<summary><b><code>create_worktree()</code> and <code>interactive()</code></b></summary>

```python
from sandcastle import create_worktree, interactive, branch, claude_code
from sandcastle.sandboxes.docker import docker

async with await create_worktree(branch_strategy=branch("agent/fix-42")) as wt:
    await wt.interactive(agent=claude_code("claude-opus-4-8"), prompt="Explore the bug with me.")
    await wt.run(agent=claude_code("claude-opus-4-8"), sandbox=docker(), prompt="Fix issue #42.", max_iterations=3)

# Opens the agent's own UI; afterwards commits are collected and merged like run()
await interactive(agent=claude_code("claude-opus-4-8"), sandbox=docker(), prompt="...")
```

</details>

<details>
<summary><b>Agent providers</b></summary>

| Factory | CLI | Resume | Fork | Options |
| --- | --- | --- | --- | --- |
| `claude_code(model)` | `claude` | ✓ | ✓ | `effort`, `permission_mode`, `env`, `capture_sessions` |
| `codex(model)` | `codex exec` | ✓ | ✓ | `effort`, `approvals_reviewer`, `env` |
| `pi(model)` | `pi` | ✓ | ✓ | `thinking`, `env` |
| `opencode(model)` | `opencode run` | ✓ | ✓ | `variant`, `agent`, `env` |
| `copilot(model)` | `copilot` | ✓ | ✗ | `effort`, `env` |
| `cursor(model)` | `agent` | ✓ | ✗ | `env` |

Sessions are copied back to your real `~/.claude/projects/` (or `~/.codex/sessions/`, and so on), so you
can resume them on the host. To write your own provider, subclass `sandcastle.AgentProvider`.

</details>

<details id="sandbox-providers">
<summary><b>Sandbox providers</b></summary>

| Import | Kind | Notes |
| --- | --- | --- |
| `sandcastle.sandboxes.no_sandbox.no_sandbox()` | none | Runs on the host |
| `sandcastle.sandboxes.bwrap.bwrap()` | native (Linux/WSL2) | Private home and `/tmp`, own PID namespace, toolchain and git control files read-only |
| `sandcastle.sandboxes.seatbelt.seatbelt()` | native (macOS 14+) | `sandbox-exec` deny-by-default profile |
| `sandcastle.sandboxes.docker.docker()` | bind-mount | `image_name`, `mounts`, `env`, `network`, `cpus`, `memory`, … |
| `sandcastle.sandboxes.podman.podman()` | bind-mount | Same as Docker, plus `userns` |
| `sandcastle.sandboxes.e2b.e2b()` | cloud | `pip install e2b` |
| `sandcastle.sandboxes.daytona.daytona()` | cloud | `pip install daytona` |
| `sandcastle.sandboxes.modal.modal()` | cloud | `pip install modal` |
| `sandcastle.sandboxes.sprites.sprites()` | cloud | `pip install sprites-py`, plus `SPRITES_TOKEN` |
| `sandcastle.sandboxes.vercel.vercel()` | cloud | `pip install vercel-sandbox`, plus `VERCEL_TOKEN` |

Cloud sandboxes have their own time limits (E2B defaults to 30 minutes, Modal to 1 hour). For
multi-hour usage-limit waits, raise their `timeout_ms` or use a local sandbox.

Protections carried over from the original:

- Host-side git runs hardened. Repo-controlled hooks, filters, `fsmonitor` and credential helpers are
  neutralised.
- Git control files are read-only inside sandboxes.
- Syncing changes back refuses paths that escape the worktree or point into `.git`.

To build your own provider, use `create_bind_mount_sandbox_provider()` or
`create_isolated_sandbox_provider()`.

</details>

<details>
<summary><b>Docker and Podman image commands</b></summary>

```bash
sandcastle docker build-image  [--image-name N] [--dockerfile PATH]
sandcastle docker remove-image [--image-name N]
sandcastle podman build-image  [--image-name N] [--containerfile PATH]
sandcastle podman remove-image [--image-name N]
```

</details>

<details>
<summary><b>Differences from the TypeScript original</b></summary>

- The API is `async`/`await` and uses `snake_case`. TS-style dicts (`{"type": "branch", ...}`,
  `onSandboxReady`, `timeoutMs`) are also accepted.
- **Added:** usage-limit wait-and-resume, and the `sandcastle run` command.
- The scaffolded runner is `main.py`, and the planner templates use pydantic instead of Zod.
- `Output.object()` takes pydantic, callables or JSON schema.
- Cloud providers use each vendor's official Python SDK.
- Native Windows (outside WSL2) is best effort, as in the original. WSL2 with `bwrap` is recommended.

</details>

---

## Development

```bash
pip install -e '.[dev]'
pytest                                                    # unit + end-to-end tests with a fake `claude`
SANDCASTLE_DOCKER_TEST_IMAGE=redis:7.4 pytest -k docker   # opt-in real Docker test
```

## Credits

Based on [sandcastle](https://github.com/mattpocock/sandcastle) by Matt Pocock. MIT licensed; see `LICENSE`.

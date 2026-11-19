# Simple loop: an agent that picks open issues one by one and closes them.
# Run this with: python .sandcastle/main.py

import asyncio

from sandcastle import claude_code, merge_to_head, run
from sandcastle.sandboxes.docker import docker


async def main() -> None:
    await run(
        # A name for this run, shown as a prefix in log output.
        name="worker",
        # Sandbox provider — runs the agent inside an isolated container.
        sandbox=docker(),
        # The agent provider. Pass a model string to claude_code() — sonnet balances
        # capability and speed for most tasks. Switch to claude-opus-4-8 for harder
        # problems, or claude-haiku-4-5-20251001 for speed.
        agent=claude_code("claude-sonnet-4-6"),
        # Path to the prompt file. Shell expressions inside are evaluated inside the
        # sandbox at the start of each iteration, so the agent always sees fresh data.
        prompt_file="./.sandcastle/prompt.md",
        # Maximum number of iterations (agent invocations) to run in a session.
        # Each iteration works on a single issue. Increase this to process more issues
        # per run, or set it to 1 for a single-shot mode.
        max_iterations=3,
        # Branch strategy — merge-to-head creates a temporary branch for the agent
        # to work on, then merges the result back to HEAD when the run completes.
        # This is required when using copy_to_worktree, since head mode bind-mounts
        # the host directory directly (no worktree to copy into).
        branch_strategy=merge_to_head(),
        # Copy node_modules from the host into the worktree before the sandbox
        # starts. This avoids a full npm install from scratch on every iteration.
        # The onSandboxReady hook still runs npm install as a safety net to handle
        # platform-specific binaries and any packages added since the last copy.
        copy_to_worktree=["node_modules"],
        # Lifecycle hooks — commands grouped by where they run (host or sandbox).
        hooks={
            "sandbox": {
                # onSandboxReady runs once after the sandbox is initialised and the repo is
                # synced in, before the agent starts. Use it to install dependencies or run
                # any other setup steps your project needs.
                "onSandboxReady": [{"command": "npm install"}],
            },
        },
        # If the agent hits its usage limit (e.g. Claude's 5-hour window), wait for
        # the reset and resume the same session instead of failing. On by default.
        wait_for_usage_reset=True,
    )


asyncio.run(main())

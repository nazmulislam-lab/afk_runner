# Sequential Reviewer — implement-then-review loop
#
# This template drives a two-phase workflow per issue:
#   Phase 1 (Implement): A sonnet agent picks an open issue, works on it
#                        on a dedicated branch, commits the changes, and signals
#                        completion.
#   Phase 2 (Review):    A second sonnet agent reviews the branch diff and either
#                        approves it or makes corrections directly on the branch.
#
# Both phases share a single sandbox created via create_sandbox(), so the
# implementer and reviewer work on the same explicit branch.
#
# The outer loop repeats up to MAX_ITERATIONS times, processing one issue per
# iteration and stopping early once the backlog is exhausted (an implement
# phase that produces no commits).
#
# Usage:
#   python .sandcastle/main.py

import asyncio
import time

import sandcastle
from sandcastle.sandboxes.docker import docker

# Maximum number of implement→review cycles to run before stopping.
MAX_ITERATIONS = 10

# Hooks run inside the sandbox before the agent starts each iteration.
hooks = {"sandbox": {"onSandboxReady": [{"command": "npm install"}]}}

# Copy node_modules from the host into the worktree before each sandbox
# starts. Avoids a full npm install from scratch; the hook above handles
# platform-specific binaries and any packages added since the last copy.
copy_to_worktree = ["node_modules"]


async def main() -> None:
    for iteration in range(1, MAX_ITERATIONS + 1):
        print(f"\n=== Iteration {iteration}/{MAX_ITERATIONS} ===\n")

        # Generate a unique branch name for this iteration.
        branch = f"sandcastle/sequential-reviewer/{int(time.time() * 1000)}"

        # Create a single sandbox that both the implementer and reviewer share.
        sandbox = await sandcastle.create_sandbox(
            branch=branch,
            sandbox=docker(),
            hooks=hooks,
            copy_to_worktree=copy_to_worktree,
        )
        try:
            # Phase 1: Implement. One iteration so each outer pass implements a
            # single issue on its own branch, then hands it to the reviewer.
            implement = await sandbox.run(
                name="implementer",
                max_iterations=1,
                agent=sandcastle.claude_code("claude-sonnet-4-6"),
                prompt_file="./.sandcastle/implement-prompt.md",
            )

            if not implement.commits:
                # No commits means the backlog is empty or every remaining issue
                # is blocked — nothing left to implement or review, so stop.
                print("Implementation agent made no commits. Stopping.")
                break

            print(f"\nImplementation complete on branch: {branch}")
            print(f"Commits: {len(implement.commits)}")

            # Phase 2: Review the branch produced by Phase 1.
            await sandbox.run(
                name="reviewer",
                max_iterations=1,
                agent=sandcastle.claude_code("claude-sonnet-4-6"),
                prompt_file="./.sandcastle/review-prompt.md",
                prompt_args={"BRANCH": branch},
            )
            print("\nReview complete.")
        finally:
            await sandbox.close()

    print("\nAll done.")


asyncio.run(main())

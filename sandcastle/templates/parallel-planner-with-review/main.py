# Parallel Planner with Review — four-phase orchestration loop
#
#   Phase 1 (Plan):             An opus agent analyzes open issues and outputs a
#                               <plan> JSON listing unblocked issues with branches.
#   Phase 2 (Execute + Review): Per issue, a sandbox via create_sandbox(): the
#                               implementer runs, then (if the branch is ahead)
#                               a reviewer on the same branch. All concurrent.
#   Phase 3 (Merge):            One agent merges all completed branches.
#
# Usage: python .sandcastle/main.py   (needs: pip install pydantic)

import asyncio
import subprocess
from typing import List

from pydantic import BaseModel

import sandcastle
from sandcastle.sandboxes.docker import docker


# The planner emits its plan as JSON inside <plan> tags; Output.object extracts
# and validates it against this schema. We use pydantic here, but any validator
# works: a callable, an object with .validate(), or a JSON-schema dict.
class PlanIssue(BaseModel):
    id: str
    title: str
    branch: str


class Plan(BaseModel):
    issues: List[PlanIssue]


async def is_ahead_of_head(branch: str) -> bool:
    """True when `branch` has commits the current branch doesn't. The merge
    phase asks git rather than trusting this cycle's `commits`: a branch an
    earlier cycle advanced but never merged gets no new commits when its issue
    comes round again, so gating on `len(commits)` would strand it forever."""
    proc = await asyncio.create_subprocess_exec(
        "git", "rev-list", "--count", f"HEAD..{branch}", stdout=subprocess.PIPE
    )
    out, _ = await proc.communicate()
    try:
        return proc.returncode == 0 and int(out.decode().strip()) > 0
    except ValueError:
        return False


# Maximum number of plan→execute→merge cycles before stopping.
MAX_ITERATIONS = 10

# Install hooks only for worktree phases. Planner and merger bind-mount the
# real checkout (no branch), so an `npm install` there would rewrite the
# host's node_modules.
install_hooks = {"sandbox": {"onSandboxReady": [{"command": "npm install"}]}}

# Copy node_modules from the host into the worktree before each execute
# sandbox starts. Safe only with a worktree — never pair this with head mode.
copy_to_worktree = ["node_modules"]

async def main() -> None:
    for iteration in range(1, MAX_ITERATIONS + 1):
        print(f"\n=== Iteration {iteration}/{MAX_ITERATIONS} ===\n")

        # Phase 1: Plan. The planning agent (opus, for deeper reasoning) reads
        # the open issue list, builds a dependency graph, and selects the issues
        # that can be worked in parallel right now. It outputs a <plan> JSON
        # block that Output.object parses and validates.
        plan = await sandcastle.run(
            sandbox=docker(),
            name="planner",
            # Structured output requires max_iterations=1.
            max_iterations=1,
            agent=sandcastle.claude_code("claude-opus-4-8"),
            prompt_file="./.sandcastle/plan-prompt.md",
            output=sandcastle.Output.object(tag="plan", schema=Plan),
        )
        issues = plan.output.issues
        if not issues:
            print("No unblocked issues to work on. Exiting.")
            break
        print(f"Planning complete. {len(issues)} issue(s) to work in parallel:")
        for issue in issues:
            print(f"  {issue.id}: {issue.title} → {issue.branch}")

        # Phase 2: Execute + Review. Each issue gets a sandbox via
        # create_sandbox() so the implementer and reviewer share it. The
        # reviewer runs whenever the branch is ahead of the current branch.
        async def pipeline(issue: PlanIssue):
            sandbox = await sandcastle.create_sandbox(
                branch=issue.branch, sandbox=docker(), hooks=install_hooks, copy_to_worktree=copy_to_worktree
            )
            try:
                implement = await sandbox.run(
                    name="implementer",
                    max_iterations=100,
                    agent=sandcastle.claude_code("claude-sonnet-4-6"),
                    prompt_file="./.sandcastle/implement-prompt.md",
                    prompt_args={"TASK_ID": issue.id, "ISSUE_TITLE": issue.title, "BRANCH": issue.branch},
                )
                if await is_ahead_of_head(issue.branch):
                    review = await sandbox.run(
                        name="reviewer",
                        max_iterations=1,
                        agent=sandcastle.claude_code("claude-sonnet-4-6"),
                        prompt_file="./.sandcastle/review-prompt.md",
                        prompt_args={"BRANCH": issue.branch},
                    )
                    review.commits = [*implement.commits, *review.commits]
                    return review
                return implement
            finally:
                await sandbox.close()

        settled = await asyncio.gather(*(pipeline(i) for i in issues), return_exceptions=True)

        # Log any agents that failed, and pass only branches that are ahead of
        # the current branch to the merge phase.
        finished = []
        for issue, outcome in zip(issues, settled):
            if isinstance(outcome, BaseException):
                print(f"  ✗ {issue.id} ({issue.branch}) failed: {outcome}")
            else:
                finished.append(issue)
        ahead = await asyncio.gather(*(is_ahead_of_head(i.branch) for i in finished))
        completed = [i for i, a in zip(finished, ahead) if a]
        completed_branches = [i.branch for i in completed]

        print(f"\nExecution complete. {len(completed_branches)} branch(es) ahead of HEAD:")
        for b in completed_branches:
            print(f"  {b}")
        if not completed_branches:
            print("No branches ahead of HEAD. Nothing to merge.")
            continue

        # Phase 3: Merge. One agent merges all completed branches into the
        # current branch, resolving conflicts and running tests.
        await sandcastle.run(
            sandbox=docker(),
            name="merger",
            max_iterations=1,
            agent=sandcastle.claude_code("claude-sonnet-4-6"),
            prompt_file="./.sandcastle/merge-prompt.md",
            prompt_args={
                # A markdown list of branch names, one per line.
                "BRANCHES": "\n".join(f"- {b}" for b in completed_branches),
                # A markdown list of issue IDs and titles, one per line.
                "ISSUES": "\n".join(f"- {i.id}: {i.title}" for i in completed),
            },
        )
        print("\nBranches merged.")

    print("\nAll done.")


asyncio.run(main())

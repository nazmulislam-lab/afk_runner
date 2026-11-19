"""sandcastle: orchestrate AI coding agents in isolated sandboxes.

A Python port of https://github.com/mattpocock/sandcastle (MIT), with one
addition: when an agent hits its usage limit (e.g. Claude's 5-hour window),
the run waits for the reset and resumes the same session automatically.

```python
import asyncio
from sandcastle import run, claude_code
from sandcastle.sandboxes.docker import docker

result = asyncio.run(run(agent=claude_code("claude-opus-4-8"), sandbox=docker(),
                         prompt_file=".sandcastle/prompt.md", max_iterations=5))
```
"""

from .abort import AbortController, AbortError, AbortSignal
from .agent_provider import (
    DEFAULT_MODEL,
    AgentCommandOptions,
    AgentProvider,
    IterationUsage,
    PrintCommand,
    claude_code,
    claudeCode,
    codex,
    copilot,
    cursor,
    opencode,
    pi,
)
from .context_window import ContextWindow
from .create_sandbox import CloseResult, Sandbox, SandboxInteractiveResult, SandboxRunResult, create_sandbox
from .create_worktree import Worktree, WorktreeRunResult, create_worktree
from .display import AgentStreamEvent
from .errors import (
    AgentError,
    AgentIdleTimeoutError,
    CwdError,
    ForkNotSupportedError,
    NativeInteractiveNotSupportedError,
    NativeSandboxUnavailableError,
    SandcastleError,
    SyncOutPathError,
    UsageLimitWaitError,
)
from .interactive import InteractiveResult, interactive
from .lifecycle import HostHook, SandboxHook, SandboxHooks, Timeouts
from .orchestrator import IterationResult
from .output import Output, StructuredOutputError
from .run import FileLogging, RunResult, StdoutLogging, run, run_sync
from .sandbox_provider import (
    BindMountCreateOptions,
    BindMountSandboxProvider,
    ExecResult,
    HeadBranchStrategy,
    IsolatedCreateOptions,
    IsolatedSandboxProvider,
    MergeToHeadBranchStrategy,
    MountConfig,
    NamedBranchStrategy,
    NoSandboxProvider,
    branch,
    create_bind_mount_sandbox_provider,
    create_isolated_sandbox_provider,
    createBindMountSandboxProvider,
    createIsolatedSandboxProvider,
    head,
    merge_to_head,
)
from .session_store import (
    claude_host_session_path,
    claude_sandbox_session_path,
    encode_project_path,
    find_claude_session_on_host,
    find_codex_session_on_host,
    transfer_claude_session,
    transfer_codex_session,
)
from .usage_limit import UsageLimitConfig, is_usage_limit_message, parse_reset_time
from .version import VERSION, __version__

__all__ = [n for n in dir() if not n.startswith("_")]

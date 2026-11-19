"""Prompt resolution, argument substitution and shell-expression expansion.

Port of ``PromptResolver.ts``, ``PromptArgumentSubstitution.ts``,
``PromptPreprocessor.ts`` and ``resolveSandboxPrompt.ts``.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Literal, Optional, Set, Union

from .display import Display
from .errors import PromptError, PromptExpansionTimeoutError

PromptArgs = Dict[str, Union[str, int, float, bool]]

PROMPT_EXPANSION_TIMEOUT_MS = 30_000

#: Marker inserted between ``!`` and the opening backtick for shell blocks
#: written in the raw template. Only marked blocks run, so ``!`...``` arriving
#: through argument substitution is treated as data.
SHELL_BLOCK_MARKER = "\u0001"

_MARKED_SHELL_BLOCK_RE = re.compile("!" + SHELL_BLOCK_MARKER + r"`([^`]+)`")
_SHELL_BLOCK_RE = re.compile(r"!`(?P<command>[^`]+)`")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?(?:-->|$)")

BUILT_IN_PROMPT_ARG_KEYS = ("SOURCE_BRANCH", "TARGET_BRANCH")


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedPrompt:
    text: str
    #: ``inline`` prompts reach the agent verbatim; ``template`` prompts get
    #: ``{{KEY}}`` substitution and ``!`command``` expansion.
    source: Literal["inline", "template"]


def resolve_prompt(prompt: Optional[str] = None, prompt_file: Optional[str] = None) -> ResolvedPrompt:
    if prompt is not None and prompt_file is not None:
        raise PromptError("Cannot provide both prompt and promptFile")
    if prompt is not None:
        return ResolvedPrompt(prompt, "inline")
    if prompt_file is None:
        raise PromptError(
            "Must provide either prompt or promptFile. Pass prompt='...' or "
            "prompt_file='./.sandcastle/prompt.md' to run()."
        )
    try:
        with open(prompt_file, "r", encoding="utf-8") as f:
            return ResolvedPrompt(f.read(), "template")
    except OSError as e:
        raise PromptError(f"Failed to read prompt from {prompt_file}: {e}") from e


# ---------------------------------------------------------------------------
# Argument substitution
# ---------------------------------------------------------------------------


def _placeholder_keys(text: str) -> List[str]:
    return [m.group("key") for m in _PLACEHOLDER_RE.finditer(text)]


def validate_no_args_with_inline_prompt(args: Optional[PromptArgs]) -> None:
    if args:
        raise PromptError(
            'promptArgs is only supported with promptFile. Inline prompts (prompt="...") are passed '
            "to the agent as-is: interpolate values directly in Python, or switch to prompt_file to "
            "use {{KEY}} substitution."
        )


def validate_no_built_in_arg_override(args: PromptArgs) -> None:
    for key in BUILT_IN_PROMPT_ARG_KEYS:
        if key in args:
            raise PromptError(f'"{key}" is a built-in prompt argument and cannot be overridden via promptArgs')


def find_missing_prompt_arg_keys(prompt: str, provided: PromptArgs) -> List[str]:
    seen: Set[str] = set()
    missing: List[str] = []
    for key in _placeholder_keys(prompt):
        if key in seen:
            continue
        seen.add(key)
        if key in BUILT_IN_PROMPT_ARG_KEYS:
            continue
        if key in provided and provided[key] is not None:
            continue
        missing.append(key)
    return missing


def _mark_shell_blocks(prompt: str) -> str:
    ranges = [(m.start(), m.end()) for m in _HTML_COMMENT_RE.finditer(prompt)]

    def repl(m: "re.Match[str]") -> str:
        offset = m.start()
        if any(start <= offset < end for start, end in ranges):
            return m.group(0)
        return f"!{SHELL_BLOCK_MARKER}`{m.group('command')}`"

    return _SHELL_BLOCK_RE.sub(repl, prompt)


def _format_arg(value: Any) -> str:
    """``String(value)`` semantics: booleans as ``true``/``false``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def substitute_prompt_args(
    prompt: str,
    args: PromptArgs,
    display: Optional[Display] = None,
    silent_keys: Optional[Iterable[str]] = None,
) -> str:
    """Replace each ``{{KEY}}`` with its value. A placeholder with no value is
    an error; an unused argument is a warning. Shell blocks in the raw
    template are marked so only they run; markers in argument values are
    stripped so they can't be forged."""
    marked = _mark_shell_blocks(prompt.replace(SHELL_BLOCK_MARKER, ""))
    sanitized = {k: (v.replace(SHELL_BLOCK_MARKER, "") if isinstance(v, str) else v) for k, v in args.items()}
    keys = _placeholder_keys(marked)
    if not keys and not sanitized:
        return marked
    referenced = set(keys)
    for key in keys:
        if key not in sanitized:
            raise PromptError(f'Prompt argument "{{{{{key}}}}}" has no matching value in promptArgs')
        if sanitized[key] is None:
            raise PromptError(f'Prompt argument "{{{{{key}}}}}" has value null in promptArgs')
    silent = set(silent_keys or ())
    if display is not None:
        for key in sanitized:
            if key not in referenced and key not in silent:
                display.status(f'Prompt argument "{key}" was provided but not referenced in the prompt', "warn")
    return _PLACEHOLDER_RE.sub(lambda m: _format_arg(sanitized[m.group("key")]), marked)


# ---------------------------------------------------------------------------
# Shell expression expansion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ShellBlock:
    text: str
    command: str
    index: int


async def _expand_block(block: _ShellBlock, sandbox: Any, cwd: str) -> str:
    start = time.monotonic()
    try:
        result = await asyncio.wait_for(sandbox.exec(block.command, cwd=cwd), PROMPT_EXPANSION_TIMEOUT_MS / 1000)
    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - start) * 1000)
        raise PromptExpansionTimeoutError(
            f"Shell expression `{block.command}` timed out after {elapsed}ms",
            timeout_ms=PROMPT_EXPANSION_TIMEOUT_MS,
            expression=block.command,
            elapsed_ms=elapsed,
        ) from None
    if result.exit_code != 0:
        raise PromptError(
            f"Command `{block.command}` exited with code {result.exit_code}: {result.stderr}",
            exit_code=result.exit_code,
        )
    return result.stdout.rstrip()


async def preprocess_prompt(prompt: str, sandbox: Any, cwd: str, display: Display) -> str:
    """Run every marked ``!`command``` in the sandbox (in parallel) and splice
    its trimmed stdout in place. A non-zero exit fails the run."""
    blocks = [_ShellBlock(m.group(0), m.group(1), m.start()) for m in _MARKED_SHELL_BLOCK_RE.finditer(prompt)]
    if not blocks:
        return prompt.replace(SHELL_BLOCK_MARKER, "")

    async def work(message: Any) -> str:
        tasks = [asyncio.ensure_future(_expand_block(b, sandbox, cwd)) for b in blocks]
        try:
            outputs = await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            raise
        for b, out in zip(blocks, outputs):
            message(f"{b.command} → ~{math.ceil(len(out) / 4)} tokens")
        result, cursor = "", 0
        for b, out in zip(blocks, outputs):
            result += prompt[cursor : b.index] + out
            cursor = b.index + len(b.text)
        result += prompt[cursor:]
        return result.replace(SHELL_BLOCK_MARKER, "")

    return await display.task_log("Expanding shell expressions", work)


# ---------------------------------------------------------------------------
# Sandbox prompt (createSandbox().run / interactive)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedSandboxPrompt:
    text: str
    is_inline: bool


async def resolve_sandbox_prompt(
    *,
    prompt: Optional[str],
    prompt_file: Optional[str],
    prompt_args: Optional[PromptArgs],
    branch: str,
    host_repo_dir: str,
    display: Display,
) -> ResolvedSandboxPrompt:
    from .worktree_manager import get_current_branch

    resolved = resolve_prompt(prompt, prompt_file)
    is_inline = resolved.source == "inline"
    user_args = dict(prompt_args or {})
    current = await get_current_branch(host_repo_dir)
    if is_inline:
        validate_no_args_with_inline_prompt(user_args)
        return ResolvedSandboxPrompt(resolved.text, True)
    validate_no_built_in_arg_override(user_args)
    text = substitute_prompt_args(
        resolved.text,
        {"SOURCE_BRANCH": branch, "TARGET_BRANCH": current, **user_args},
        display,
        BUILT_IN_PROMPT_ARG_KEYS,
    )
    return ResolvedSandboxPrompt(text, False)

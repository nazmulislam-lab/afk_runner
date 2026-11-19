#!/usr/bin/env python3
"""
afk_runner - run Claude Code unattended and auto-resume after usage-limit resets.

Runs `claude -p <prompt>` in headless mode. If the 5-hour usage window runs out,
it parses the reset time from Claude's message, sleeps until then, and resumes
the *same* session immediately after the reset.

Usage:
    python afk_runner.py "Build the feature described in TODO.md"
    python afk_runner.py --prompt-file task.md --cwd ~/myproject --yolo
    python afk_runner.py --prompt-file task.md --until-done AFK_DONE
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None  # type: ignore

CONTINUE_PROMPT = (
    "You were interrupted by a usage limit. The limit has now reset. "
    "Continue exactly where you left off and finish the task."
)

# Phrases Claude Code uses when the usage window is exhausted.
LIMIT_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"usage limit reached",
        r"5-hour limit reached",
        r"hit your (usage )?limit",
        r"limit reached.{0,40}resets",
        r"out of (extra )?usage",
        r"rate_limit_error",
    )
]

# Transient errors worth retrying with backoff (not limit-related).
TRANSIENT_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (r"overloaded", r"\b529\b", r"\b50[0234]\b", r"ECONNRESET", r"ETIMEDOUT",
              r"network error", r"connection (error|refused|reset)", r"fetch failed")
]

# Old format: "Claude AI usage limit reached|1731960000"
EPOCH_RE = re.compile(r"limit reached\|(\d{10})", re.IGNORECASE)
# New format: "resets 3pm", "resets 3:30pm (Asia/Dhaka)", "resets at 15:00"
CLOCK_RE = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([^)]+)\))?",
    re.IGNORECASE,
)
# Relative format: "resets in 2h 15m" / "try again in 37 minutes"
REL_RE = re.compile(
    r"(?:resets?|try again)\s+in\s+(?:(\d+)\s*h(?:ours?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?)?)?)?",
    re.IGNORECASE,
)


def log(msg: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\033[36m[afk {stamp}]\033[0m {msg}", flush=True)


@dataclass
class RunResult:
    exit_code: int
    text: str = ""                 # everything seen (stdout + stderr) for pattern matching
    final_result: str = ""         # the final "result" message text
    is_error: bool = False
    session_id: str | None = None
    started: bool = False          # claude actually created/loaded the session
    lines: list[str] = field(default_factory=list)


def is_limit_hit(text: str) -> bool:
    return any(p.search(text) for p in LIMIT_PATTERNS)


def is_transient(text: str) -> bool:
    return any(p.search(text) for p in TRANSIENT_PATTERNS)


def parse_reset_time(text: str) -> datetime | None:
    """Return the reset moment as an aware datetime, or None if not found."""
    now = datetime.now(timezone.utc)

    m = EPOCH_RE.search(text)
    if m:
        return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)

    m = REL_RE.search(text)
    if m and (m.group(1) or m.group(2)):
        return now + timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))

    for m in CLOCK_RE.finditer(text):
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        ampm, tzname = m.group(3), m.group(4)
        if ampm:
            if hour == 12:
                hour = 0
            if ampm.lower() == "pm":
                hour += 12
        if not (0 <= hour < 24 and 0 <= minute < 60):
            continue
        tz = None
        if tzname and ZoneInfo is not None:
            try:
                tz = ZoneInfo(tzname.strip())
            except Exception:
                tz = None
        local_now = datetime.now(tz) if tz else datetime.now().astimezone()
        reset = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset <= local_now:
            reset += timedelta(days=1)
        return reset.astimezone(timezone.utc)

    return None


def sleep_until(when: datetime, buffer_s: int) -> None:
    target = when + timedelta(seconds=buffer_s)
    log(f"Usage limit hit. Sleeping until {target.astimezone():%Y-%m-%d %H:%M:%S %Z} "
        f"(reset + {buffer_s}s buffer).")
    while True:
        remaining = (target - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            break
        h, rem = divmod(int(remaining), 3600)
        m, s = divmod(rem, 60)
        print(f"\r  ⏳ resuming in {h:02d}:{m:02d}:{s:02d} ", end="", flush=True)
        # Short sleeps so laptop suspend / clock jumps are handled correctly.
        time.sleep(min(30, max(1, remaining)))
    print()
    log("Reset time reached — resuming now.")


def summarize_event(ev: dict) -> str | None:
    """Turn a stream-json event into a short human-readable line."""
    t = ev.get("type")
    if t == "assistant":
        out = []
        for block in ev.get("message", {}).get("content", []) or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                out.append(block["text"].strip())
            elif block.get("type") == "tool_use":
                inp = block.get("input", {})
                hint = inp.get("command") or inp.get("file_path") or inp.get("pattern") \
                    or inp.get("description") or ""
                hint = str(hint).replace("\n", " ")[:120]
                out.append(f"\033[33m→ {block.get('name')}\033[0m {hint}")
        return "\n".join(out) or None
    if t == "result":
        return f"\033[32m✔ result ({ev.get('subtype')})\033[0m: {str(ev.get('result', ''))[:500]}"
    return None


def run_claude(args: argparse.Namespace, prompt: str, session_id: str, resume: bool) -> RunResult:
    cmd = [args.claude_bin, "-p", prompt, "--output-format", "stream-json", "--verbose"]
    cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
    if args.model:
        cmd += ["--model", args.model]
    if args.yolo:
        cmd.append("--dangerously-skip-permissions")
    elif args.permission_mode:
        cmd += ["--permission-mode", args.permission_mode]
    cmd += args.extra

    log(f"{'Resuming' if resume else 'Starting'} session {session_id}")
    res = RunResult(exit_code=-1, session_id=session_id)
    proc = subprocess.Popen(
        cmd, cwd=args.cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    assert proc.stdout is not None
    logf = open(args.log_file, "a", encoding="utf-8") if args.log_file else None
    try:
        for line in proc.stdout:
            res.lines.append(line)
            if logf:
                logf.write(line)
                logf.flush()
            line_s = line.strip()
            if not line_s:
                continue
            try:
                ev = json.loads(line_s)
            except json.JSONDecodeError:
                print(line_s, flush=True)  # plain stderr / non-JSON output
                continue
            if ev.get("session_id"):
                res.session_id = ev["session_id"]
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                res.started = True
            if ev.get("type") == "result":
                res.final_result = str(ev.get("result", ""))
                res.is_error = bool(ev.get("is_error"))
            msg = summarize_event(ev)
            if msg:
                print(msg, flush=True)
    except KeyboardInterrupt:
        proc.terminate()
        raise
    finally:
        if logf:
            logf.close()
    res.exit_code = proc.wait()
    res.text = "".join(res.lines)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="Run Claude Code AFK; auto-resume after usage-limit resets.")
    ap.add_argument("prompt", nargs="?", help="Task prompt (or use --prompt-file)")
    ap.add_argument("-f", "--prompt-file", help="Read the task prompt from a file")
    ap.add_argument("-C", "--cwd", default=".", help="Project directory Claude works in (default: .)")
    ap.add_argument("-m", "--model", help="Model to use (e.g. opus, sonnet)")
    ap.add_argument("--yolo", action="store_true",
                    help="Pass --dangerously-skip-permissions (needed for true AFK; use in a safe dir)")
    ap.add_argument("--permission-mode", default="acceptEdits",
                    help="Permission mode when not using --yolo (default: acceptEdits)")
    ap.add_argument("--resume", metavar="SESSION_ID", help="Resume an existing session instead of starting new")
    ap.add_argument("--until-done", metavar="TOKEN",
                    help="Keep continuing until Claude's final reply contains TOKEN "
                         "(the token instruction is appended to your prompt)")
    ap.add_argument("--max-runs", type=int, default=50, help="Safety cap on claude invocations (default 50)")
    ap.add_argument("--buffer", type=int, default=60, help="Seconds to wait after reset time (default 60)")
    ap.add_argument("--fallback-wait", type=int, default=15,
                    help="Minutes between retries if reset time can't be parsed (default 15)")
    ap.add_argument("--log-file", default="afk_runner.log", help="Raw stream-json log (default afk_runner.log, '' to disable)")
    ap.add_argument("--claude-bin", default=shutil.which("claude") or "claude", help="Path to claude CLI")
    argv = sys.argv[1:]
    extra: list[str] = []
    if "--" in argv:  # everything after `--` is passed straight to claude
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    args = ap.parse_args(argv)
    args.extra = extra
    args.cwd = str(Path(args.cwd).expanduser().resolve())

    if args.prompt_file:
        prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
    elif args.prompt:
        prompt = args.prompt
    elif args.resume:
        prompt = CONTINUE_PROMPT
    else:
        ap.error("give a prompt, --prompt-file, or --resume")

    if args.until_done:
        prompt += (f"\n\nWhen the ENTIRE task is fully complete, end your final message with the "
                   f"exact token {args.until_done} on its own line. Do not output it before then.")

    session_id = args.resume or str(uuid.uuid4())
    resume = bool(args.resume)
    transient_failures = 0

    log(f"Working dir: {args.cwd}")
    for run in range(1, args.max_runs + 1):
        log(f"Run {run}/{args.max_runs}")
        res = run_claude(args, prompt, session_id, resume)
        session_id = res.session_id or session_id
        if res.started:
            resume = True  # every later run continues the same conversation

        # Only look at error-ish output for limit detection, so Claude merely
        # *talking* about limits in a successful answer doesn't trigger a wait.
        failed = res.is_error or res.exit_code != 0
        error_text = res.text if failed else res.final_result
        limit_hit = is_limit_hit(error_text) if failed else \
            (is_limit_hit(res.final_result) and len(res.final_result) < 300)
        if limit_hit:
            reset = parse_reset_time(error_text)
            if reset is None:
                log(f"Couldn't parse reset time; retrying in {args.fallback_wait} min.")
                reset = datetime.now(timezone.utc) + timedelta(minutes=args.fallback_wait)
                sleep_until(reset, 0)
            else:
                sleep_until(reset, args.buffer)
            if resume:
                prompt = CONTINUE_PROMPT
            transient_failures = 0
            continue

        if res.is_error or res.exit_code != 0:
            transient_failures += 1
            if transient_failures > 8:
                log("Too many consecutive errors — giving up.")
                return 1
            wait = min(600, 15 * 2 ** (transient_failures - 1))
            kind = "Transient" if is_transient(res.text) else "Unknown"
            log(f"{kind} error (exit {res.exit_code}). Retrying in {wait}s.")
            time.sleep(wait)
            if resume:
                prompt = CONTINUE_PROMPT
            continue

        transient_failures = 0
        if args.until_done and args.until_done not in res.final_result:
            log(f"Done-token '{args.until_done}' not seen yet — telling Claude to keep going.")
            prompt = (f"Continue working on the task. When it is fully complete, end with "
                      f"{args.until_done} on its own line.")
            continue

        log(f"✅ Task finished. Session: {session_id}")
        log(f"   Reopen interactively with: claude --resume {session_id}")
        return 0

    log("Hit --max-runs cap; stopping.")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        log("Interrupted by user.")
        sys.exit(130)

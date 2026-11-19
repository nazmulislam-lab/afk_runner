#!/usr/bin/env python3
"""A scriptable stand-in for the `claude` CLI used by the integration tests.

It speaks Claude Code's `--output-format stream-json`. Behaviour per call is
read from the JSON list in $FAKE_SCENARIO (call N uses entry N, the last
entry repeats). Each entry may set:

  text       assistant text to print
  commit     a file name to create and `git commit`
  untracked  a file name to create without committing
  signal     print <promise>COMPLETE</promise>
  limit      fail with a Claude usage-limit message (reset in `limit` seconds)
  error      fail with this message (exit 1)
  sleep      seconds to sleep before exiting
  hang       print the signal, then hang without exiting
  output     raw text to append to the final result (e.g. a <plan> tag)
  session    write a session JSONL under $HOME/.claude/projects

Every call is logged to $FAKE_LOG as a JSON line (argv, prompt, cwd).
"""

import json
import os
import re
import subprocess
import sys
import time

args = sys.argv[1:]
prompt = sys.stdin.read()
state = os.environ["FAKE_STATE"]
n = int(open(state).read()) if os.path.exists(state) else 0
open(state, "w").write(str(n + 1))
scenario = json.loads(os.environ.get("FAKE_SCENARIO", "[{}]"))
step = scenario[min(n, len(scenario) - 1)]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"argv": args, "prompt": prompt, "cwd": os.getcwd(), "env_marker": os.environ.get("AGENT_MARKER")}) + "\n")

sid = args[args.index("--resume") + 1] if "--resume" in args else f"sess-{n}"
if "--fork-session" in args:
    sid = f"fork-of-{sid}"


def out(o):
    print(json.dumps(o), flush=True)


out({"type": "system", "subtype": "init", "session_id": sid})
if step.get("session"):
    cwd = os.getcwd()
    enc = re.sub(r"[^A-Za-z0-9]", "-", cwd.rstrip("/"))
    d = os.path.join(os.environ["HOME"], ".claude", "projects", enc)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{sid}.jsonl"), "a") as f:
        f.write(json.dumps({"type": "user", "cwd": cwd, "sessionId": sid}) + "\n")
        f.write(json.dumps({"type": "assistant", "cwd": cwd, "message": {"usage": {"input_tokens": 7, "cache_creation_input_tokens": 1, "cache_read_input_tokens": 2, "output_tokens": 3}}}) + "\n")

if step.get("limit") is not None:
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": "partial work\n"}]}})
    reset = int(time.time()) + int(step["limit"])
    out({"type": "result", "subtype": "success", "is_error": True, "result": f"Claude AI usage limit reached|{reset}", "session_id": sid})
    sys.exit(1)
if step.get("error"):
    print(step["error"], file=sys.stderr)
    sys.exit(1)
if step.get("commit"):
    with open(step["commit"], "w") as f:
        f.write(f"call {n}\n")
    subprocess.run(["git", "add", step["commit"]], check=True)
    subprocess.run(["git", "commit", "-qm", f"add {step['commit']}"], check=True)
    out({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "git commit"}}]}})
if step.get("untracked"):
    with open(step["untracked"], "w") as f:
        f.write("untracked\n")
text = step.get("text", f"call {n} done.")
if step.get("signal") or step.get("hang"):
    text += " <promise>COMPLETE</promise>"
text += step.get("output", "")
out({"type": "assistant", "message": {"content": [{"type": "text", "text": text}], "usage": {"input_tokens": 100, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 900, "output_tokens": 5}}})
if step.get("hang"):
    time.sleep(3600)
if step.get("sleep"):
    time.sleep(step["sleep"])
out({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": sid})

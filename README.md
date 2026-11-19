# afk_runner

Run [Claude Code](https://claude.com/claude-code) unattended. If the **5-hour usage window runs out**,
the runner reads the reset time from Claude's message, sleeps until then, and **resumes the same
session as soon as the limit resets**. You don't have to do anything.

Pure Python 3.9+, no dependencies. You need the `claude` CLI installed and logged in.

## Quick start

```bash
# simple task
python3 afk_runner.py "Implement everything in TODO.md and make the tests pass"

# long task from a file, in another project, fully hands-off
python3 afk_runner.py -f task.md -C ~/my_project --yolo

# keep pushing Claude until it says it's really done
python3 afk_runner.py -f task.md -C ~/my_project --yolo --until-done AFK_DONE
```

To leave it running after you close the terminal:

```bash
nohup python3 afk_runner.py -f task.md -C ~/my_project --yolo > afk.out 2>&1 &
# or inside tmux / screen
```

## How it works

1. Starts `claude -p <prompt> --output-format stream-json` with a fixed session ID, and prints
   Claude's text and tool calls live.
2. If the run ends with a usage-limit message (`usage limit reached|<epoch>`, `resets 3pm (Asia/Dhaka)`,
   `resets in 2h 15m`, …), it works out the reset time and waits until then plus `--buffer` seconds
   (60 by default).
   - If it can't read the reset time, it retries every `--fallback-wait` minutes.
3. After the reset it runs `claude --resume <session>` with "continue where you left off", so the
   context is kept.
4. Network or overload errors are retried with exponential backoff.
5. It stops when Claude finishes (or when the `--until-done` token shows up), or when it reaches `--max-runs`.

## Options

| Flag | Meaning |
|---|---|
| `prompt` / `-f FILE` | Task prompt, inline or from a file |
| `-C DIR` | Project directory Claude works in (default `.`) |
| `-m MODEL` | Model, e.g. `opus`, `sonnet` |
| `--yolo` | Pass `--dangerously-skip-permissions` (true AFK; use only in a safe directory) |
| `--permission-mode` | Used when not using `--yolo` (default `acceptEdits`) |
| `--resume ID` | Continue an existing session |
| `--until-done TOKEN` | Keep going until Claude's final reply contains TOKEN |
| `--buffer N` | Seconds to wait after the reset time (default 60) |
| `--fallback-wait N` | Minutes between retries if the reset time can't be parsed (default 15) |
| `--max-runs N` | Safety cap on how many times claude is called (default 50) |
| `--log-file PATH` | Raw stream-json log (default `afk_runner.log`; `''` turns it off) |
| `-- ...` | Anything after `--` is passed straight to `claude` |

When it finishes, it prints the session ID so you can open it with `claude --resume <id>`.

> ⚠️ Without `--yolo`, Claude can't run commands that need approval (headless mode can't ask you).
> With `--yolo`, Claude can do anything your user can do, so use it in a repo or container you trust.

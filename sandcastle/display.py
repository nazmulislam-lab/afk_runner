"""Progress display. Port of ``Display.ts``, ``DisplayService.ts``,
``clackDisplayService.ts``, ``AgentStreamEmitter.ts``, ``TextDeltaBuffer.ts``,
``boundedTail.ts`` and ``terminalCleanup.ts``.

Three displays:

* :class:`TerminalDisplay` - terminal mode, a clack-style log with spinners.
* :class:`FileDisplay` - log-to-file mode (the default for ``run()``).
* :class:`SilentDisplay` - records entries in memory (tests, embedding).
"""

from __future__ import annotations

import asyncio
import atexit
import itertools
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, TypeVar, Union

T = TypeVar("T")
Severity = Literal["info", "success", "warn", "error"]

# ---------------------------------------------------------------------------
# Agent stream events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentTextStreamEvent:
    message: str
    iteration: int
    timestamp: datetime
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class AgentToolCallStreamEvent:
    name: str
    formatted_args: str
    iteration: int
    timestamp: datetime
    type: Literal["toolCall"] = "toolCall"


@dataclass(frozen=True)
class AgentRawStreamEvent:
    line: str
    iteration: int
    timestamp: datetime
    type: Literal["raw"] = "raw"


AgentStreamEvent = Union[AgentTextStreamEvent, AgentToolCallStreamEvent, AgentRawStreamEvent]


class AgentStreamEmitter:
    """Forwards agent stream events to a callback; callback errors are
    swallowed so a broken forwarder cannot kill the run."""

    def __init__(self, on_event: Optional[Callable[[AgentStreamEvent], Any]] = None) -> None:
        self._on_event = on_event

    def emit(self, event: AgentStreamEvent) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001
            pass


def now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# TextDeltaBuffer
# ---------------------------------------------------------------------------

LENGTH_THRESHOLD = 80
DEBOUNCE_S = 0.05
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?] $")


class TextDeltaBuffer:
    """Buffers streaming text deltas and flushes them as readable chunks.

    Flushes on a newline, a sentence boundary, ~80 chars, or 50 ms of quiet.
    """

    def __init__(self, on_flush: Callable[[str], None]) -> None:
        self._buffer = ""
        self._timer: Optional[asyncio.TimerHandle] = None
        self._on_flush = on_flush

    def write(self, text: str) -> None:
        if not text:
            return
        self._buffer += text
        self._clear_timer()
        if self._should_flush():
            self._do_flush()
            return
        try:
            loop = asyncio.get_running_loop()
            self._timer = loop.call_later(DEBOUNCE_S, self._do_flush)
        except RuntimeError:
            self._do_flush()

    def flush(self) -> None:
        self._clear_timer()
        self._do_flush()

    def dispose(self) -> None:
        self.flush()

    def _should_flush(self) -> bool:
        return (
            "\n" in self._buffer
            or _SENTENCE_BOUNDARY_RE.search(self._buffer) is not None
            or len(self._buffer) >= LENGTH_THRESHOLD
        )

    def _do_flush(self) -> None:
        if not self._buffer:
            return
        text, self._buffer = self._buffer, ""
        self._on_flush(text)

    def _clear_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


# ---------------------------------------------------------------------------
# BoundedTail
# ---------------------------------------------------------------------------

MAX_TAIL_CHARS = 64 * 1024


class BoundedTail:
    """A fixed-size rolling tail of strings, bounded by total length.

    Providers keep only the tail of streamed output to build
    ``ExecResult.stdout``: consumers only read the end (error tails, final
    result fallback), and retaining a whole long agent run is wasteful.
    """

    def __init__(self, max_chars: int = MAX_TAIL_CHARS, separator: str = "") -> None:
        self._items: List[str] = []
        self._total = 0
        self._max = max_chars
        self._sep = separator

    def push(self, item: str) -> None:
        bounded = item[len(item) - self._max :] if len(item) > self._max else item
        self._total += len(bounded) + (len(self._sep) if self._items else 0)
        self._items.append(bounded)
        while self._total > self._max and len(self._items) > 1:
            dropped = self._items.pop(0)
            self._total -= len(dropped) + len(self._sep)

    def __str__(self) -> str:
        return self._sep.join(self._items)

    to_string = __str__


# ---------------------------------------------------------------------------
# Display entries / base
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DisplayEntry:
    tag: str
    message: str = ""
    severity: Optional[str] = None
    title: str = ""
    rows: Dict[str, str] = field(default_factory=dict)
    messages: List[str] = field(default_factory=list)
    name: str = ""
    formatted_args: str = ""


class Display:
    """The display service interface. All methods are synchronous except
    ``spinner`` and ``task_log``, which wrap an awaitable."""

    def intro(self, title: str) -> None:
        pass

    def status(self, message: str, severity: Severity = "info") -> None:
        pass

    async def spinner(self, message: str, awaitable: Awaitable[T]) -> T:
        return await awaitable

    def summary(self, title: str, rows: Dict[str, str]) -> None:
        pass

    async def task_log(self, title: str, fn: Callable[[Callable[[str], None]], Awaitable[T]]) -> T:
        return await fn(lambda _m: None)

    def text(self, message: str) -> None:
        pass

    def text_chunk(self, chunk: str) -> None:
        pass

    def tool_call(self, name: str, formatted_args: str) -> None:
        pass

    def close(self) -> None:
        pass


class SilentDisplay(Display):
    """Records every entry in ``self.entries``."""

    def __init__(self) -> None:
        self.entries: List[DisplayEntry] = []

    def intro(self, title: str) -> None:
        self.entries.append(DisplayEntry("intro", title=title))

    def status(self, message: str, severity: Severity = "info") -> None:
        self.entries.append(DisplayEntry("status", message=message, severity=severity))

    async def spinner(self, message: str, awaitable: Awaitable[T]) -> T:
        self.entries.append(DisplayEntry("spinner", message=message))
        return await awaitable

    def summary(self, title: str, rows: Dict[str, str]) -> None:
        self.entries.append(DisplayEntry("summary", title=title, rows=dict(rows)))

    async def task_log(self, title: str, fn: Callable[[Callable[[str], None]], Awaitable[T]]) -> T:
        messages: List[str] = []
        result = await fn(messages.append)
        self.entries.append(DisplayEntry("taskLog", title=title, messages=list(messages)))
        return result

    def text(self, message: str) -> None:
        self.entries.append(DisplayEntry("text", message=message))

    def text_chunk(self, chunk: str) -> None:
        self.entries.append(DisplayEntry("textChunk", message=chunk))

    def tool_call(self, name: str, formatted_args: str) -> None:
        self.entries.append(DisplayEntry("toolCall", name=name, formatted_args=formatted_args))


# ---------------------------------------------------------------------------
# File display
# ---------------------------------------------------------------------------


class _LogWriter:
    def __init__(self, path: str) -> None:
        self.path = path
        self._mid_line = False
        self._lock = threading.Lock()

    def _write(self, data: str) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(data)
        except OSError:
            pass

    def append_line(self, line: str) -> None:
        with self._lock:
            prefix = "\n" if self._mid_line else ""
            self._mid_line = False
            self._write(f"{prefix}{line}\n")

    def append_raw(self, chunk: str) -> None:
        if not chunk:
            return
        with self._lock:
            self._mid_line = not chunk.endswith("\n")
            self._write(chunk)


_NAME_PREFIX_RE = re.compile(r"^\[[^\]]+\] ")


class FileDisplay(Display):
    """Log-to-file mode: progress and agent output go to ``path``."""

    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n--- Run started: {now().isoformat().replace('+00:00', 'Z')} ---\n")
        self._log = _LogWriter(path)

    def status(self, message: str, severity: Severity = "info") -> None:
        self._log.append_line(_NAME_PREFIX_RE.sub("", message, count=1))

    async def spinner(self, message: str, awaitable: Awaitable[T]) -> T:
        self._log.append_line(f"{message}...")
        start = time.monotonic()
        result = await awaitable
        self._log.append_line(f"{message} done ({time.monotonic() - start:.1f}s)")
        return result

    def summary(self, title: str, rows: Dict[str, str]) -> None:
        lines = "\n".join(f"  {k}: {v}" for k, v in rows.items())
        self._log.append_line(f"{title}\n{lines}")

    async def task_log(self, title: str, fn: Callable[[Callable[[str], None]], Awaitable[T]]) -> T:
        self._log.append_line(title)
        start = time.monotonic()
        messages: List[str] = []
        result = await fn(messages.append)
        elapsed = time.monotonic() - start
        for m in messages:
            self._log.append_line(f"  {m}")
        self._log.append_line(f"{title} done ({elapsed:.1f}s)")
        return result

    def text(self, message: str) -> None:
        self._log.append_line(message)

    def text_chunk(self, chunk: str) -> None:
        self._log.append_raw(chunk)

    def tool_call(self, name: str, formatted_args: str) -> None:
        self._log.append_line(f"{name}({formatted_args})")


# ---------------------------------------------------------------------------
# Terminal display (clack-style)
# ---------------------------------------------------------------------------


def _use_color(stream: Any) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty()


class _Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, s: str, end: str = "0") -> str:
        return f"\x1b[{code}m{s}\x1b[{end}m" if self.enabled else s

    def bold(self, s: str) -> str:
        return self._wrap("1", s, "22")

    def dim(self, s: str) -> str:
        return self._wrap("2", s, "22")

    def inverse(self, s: str) -> str:
        return self._wrap("7", s, "27")

    def color(self, name: str, s: str) -> str:
        codes = {"gray": "90", "green": "32", "yellow": "33", "red": "31", "blue": "34", "cyan": "36", "magenta": "35"}
        return self._wrap(codes[name], s, "39")


SHOW_CURSOR = "\x1b[?25h"
HIDE_CURSOR = "\x1b[?25l"
_SPINNER_FRAMES = ["◒", "◐", "◓", "◑"]


class TerminalDisplay(Display):
    """Terminal mode: a clack-style log with spinners and status lines."""

    def __init__(self, stream: Any = None) -> None:
        self._out = stream or sys.stdout
        self._tty = hasattr(self._out, "isatty") and self._out.isatty()
        self._s = _Style(_use_color(self._out))
        self._pending_text = ""
        self._lock = threading.RLock()
        atexit.register(self._restore_cursor)

    # -- helpers --------------------------------------------------------
    def _bar(self) -> str:
        return self._s.color("gray", "│")

    def _print(self, text: str) -> None:
        with self._lock:
            self._out.write(text + "\n")
            self._out.flush()

    def _restore_cursor(self) -> None:
        if self._tty:
            try:
                self._out.write(SHOW_CURSOR)
                self._out.flush()
            except Exception:  # noqa: BLE001
                pass

    def _log(self, symbol: str, message: str) -> None:
        lines = message.split("\n")
        out = [self._bar(), f"{symbol}  {lines[0]}"]
        out += [f"{self._bar()}  {line}" for line in lines[1:]]
        self._print("\n".join(out))

    def _print_text(self, text: str) -> None:
        trimmed = re.sub(r"^\n+|\n+$", "", text)
        if trimmed.strip():
            self._log(self._bar(), trimmed)

    def _flush_text(self) -> None:
        with self._lock:
            text, self._pending_text = self._pending_text, ""
        self._print_text(text)

    # -- Display --------------------------------------------------------
    def intro(self, title: str) -> None:
        self._flush_text()
        self._print(f"{self._s.color('gray', '┌')}  {self._s.inverse(f' {title} ')}")

    def status(self, message: str, severity: Severity = "info") -> None:
        self._flush_text()
        symbol = {
            "info": self._s.color("blue", "●"),
            "success": self._s.color("green", "◆"),
            "warn": self._s.color("yellow", "▲"),
            "error": self._s.color("red", "■"),
        }[severity]
        self._log(symbol, self._s.bold(message))

    async def spinner(self, message: str, awaitable: Awaitable[T]) -> T:
        self._flush_text()
        if not self._tty:
            self._print(f"{self._bar()}\n{self._s.color('magenta', '◒')}  {message}")
            try:
                result = await awaitable
            except BaseException:
                self._print(f"{self._s.color('red', '■')}  {message} (failed)")
                raise
            self._print(f"{self._s.color('green', '◇')}  {message}")
            return result

        stop = asyncio.Event()

        async def animate() -> None:
            self._out.write(HIDE_CURSOR)
            for frame in itertools.cycle(_SPINNER_FRAMES):
                with self._lock:
                    self._out.write(f"\r\x1b[2K{self._s.color('magenta', frame)}  {message}")
                    self._out.flush()
                try:
                    await asyncio.wait_for(stop.wait(), 0.08)
                    return
                except asyncio.TimeoutError:
                    continue

        with self._lock:
            self._out.write(self._bar() + "\n")
        anim = asyncio.ensure_future(animate())
        ok = False
        try:
            result = await awaitable
            ok = True
            return result
        finally:
            stop.set()
            try:
                await anim
            except BaseException:  # noqa: BLE001
                pass
            with self._lock:
                symbol = self._s.color("green", "◇") if ok else self._s.color("red", "■")
                suffix = "" if ok else " (failed)"
                self._out.write(f"\r\x1b[2K{symbol}  {message}{suffix}\n{SHOW_CURSOR}")
                self._out.flush()

    def summary(self, title: str, rows: Dict[str, str]) -> None:
        self._flush_text()
        lines = [f"{self._s.bold(k)}: {self._s.dim(v)}" for k, v in rows.items()]
        width = max([len(title)] + [len(f"{k}: {v}") for k, v in rows.items()]) + 2
        out = [self._bar(), f"{self._s.color('green', '◇')}  {self._s.bold(title)} " + self._s.color("gray", "─" * max(1, width - len(title)) + "╮")]
        out.append(self._bar() + " " * (width + 3) + self._s.color("gray", "│"))
        for line, (k, v) in zip(lines, rows.items()):
            pad = width - len(f"{k}: {v}")
            out.append(f"{self._bar()}  {line}{' ' * pad} {self._s.color('gray', '│')}")
        out.append(self._bar() + " " * (width + 3) + self._s.color("gray", "│"))
        out.append(self._s.color("gray", "├" + "─" * (width + 3) + "╯"))
        self._print("\n".join(out))

    async def task_log(self, title: str, fn: Callable[[Callable[[str], None]], Awaitable[T]]) -> T:
        self._flush_text()
        messages: List[str] = []
        self._print(f"{self._bar()}\n{self._s.color('magenta', '◆')}  {title}")
        ok = False
        try:
            result = await fn(messages.append)
            ok = True
            return result
        finally:
            for m in messages:
                self._print(f"{self._bar()}  {self._s.dim(m)}")
            symbol = self._s.color("green", "◇") if ok else self._s.color("red", "■")
            self._print(f"{symbol}  {title}")

    def text(self, message: str) -> None:
        self._flush_text()
        self._log(self._bar(), message)

    def text_chunk(self, chunk: str) -> None:
        with self._lock:
            self._pending_text += chunk
            idx = self._pending_text.rfind("\n")
            if idx == -1:
                return
            complete = self._pending_text[:idx]
            self._pending_text = self._pending_text[idx + 1 :]
        self._print_text(complete)

    def tool_call(self, name: str, formatted_args: str) -> None:
        self._flush_text()
        self._log(self._s.color("green", "◇"), self._s.dim(f"{name}({formatted_args})"))

    def close(self) -> None:
        self._flush_text()


# TS-compatible name
ClackDisplay = TerminalDisplay


def make_terminal_cleanup_handler(stdout: Any = None) -> Callable[[], None]:
    """Restores the cursor on exit (terminal raw mode is not used in Python)."""
    out = stdout or sys.stdout

    def handler() -> None:
        try:
            if not (hasattr(out, "isatty") and out.isatty()):
                return
            out.write(SHOW_CURSOR)
            out.flush()
        except Exception:  # noqa: BLE001
            pass

    return handler


def setup_terminal_cleanup() -> None:
    atexit.register(make_terminal_cleanup_handler())

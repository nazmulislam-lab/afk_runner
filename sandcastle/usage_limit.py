"""Usage-limit detection and waiting (new in the Python port).

When an agent hits its subscription usage window (Claude's 5-hour limit,
Codex's usage limit, ...) the iteration would normally just fail. With
``wait_for_usage_reset=True`` (the default) the orchestrator instead:

1. detects the limit message in the agent's output,
2. works out when the window resets from that message,
3. sleeps until then (plus ``usage_reset_buffer_seconds``), and
4. resumes the same agent session in the same sandbox, so the agent picks up
   exactly where it stopped.

Recognised reset formats::

    Claude AI usage limit reached|1731960000          (epoch seconds)
    5-hour limit reached ∙ resets 3pm
    You've hit your limit · resets 11:30pm (Asia/Dhaka)
    ... resets at 15:00 / resets Nov 19, 3pm
    try again in 2h 15m / resets in 37 minutes
    Try again at 3:15 PM

When no time can be read, it re-checks every ``usage_fallback_wait_minutes``.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from .abort import AbortSignal

LIMIT_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"usage limit reached",
        r"\b\d+-hour limit reached",
        r"(?:weekly|session|opus|sonnet)\s+limit reached",
        r"hit your (?:usage |session |weekly )?limit",
        r"reached your (?:usage |session |weekly )?limit",
        r"limit reached.{0,60}resets",
        r"out of (?:extra )?usage",
        r"usage_limit_reached",
        r"\brate_limit_error\b.{0,200}usage",
        r"you've reached the limit",
    )
]

_EPOCH_RE = re.compile(r"limit reached\|(\d{10,13})", re.IGNORECASE)
_REL_RE = re.compile(
    r"(?:resets?|try again|available again)\s+in\s+"
    r"(?:(?P<d>\d+)\s*d(?:ays?)?\s*)?(?:(?P<h>\d+)\s*h(?:ours?|rs?)?\s*)?(?:(?P<m>\d+)\s*m(?:in(?:ute)?s?)?)?",
    re.IGNORECASE,
)
_CLOCK_RE = re.compile(
    r"(?:resets?|try again|available again)\s+(?:at\s+|on\s+)?"
    r"(?:(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2}),?\s*(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>am|pm)?"
    r"(?:\s*\((?P<tz>[^)]+)\))?",
    re.IGNORECASE,
)
_MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


def is_usage_limit_message(text: str) -> bool:
    return bool(text) and any(p.search(text) for p in LIMIT_PATTERNS)


def parse_reset_time(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """The reset moment (aware UTC datetime) named in ``text``, or ``None``."""
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    m = _EPOCH_RE.search(text)
    if m:
        value = int(m.group(1))
        if value > 10**11:  # milliseconds
            value //= 1000
        return datetime.fromtimestamp(value, tz=timezone.utc)

    for m in _REL_RE.finditer(text):
        d, h, mi = m.group("d"), m.group("h"), m.group("m")
        if d or h or mi:
            return now_utc + timedelta(days=int(d or 0), hours=int(h or 0), minutes=int(mi or 0))

    for m in _CLOCK_RE.finditer(text):
        hour = int(m.group("hour"))
        minute = int(m.group("min") or 0)
        ampm = m.group("ampm")
        if not ampm and not m.group("min"):
            # A bare number ("resets 3") is too ambiguous to trust.
            continue
        if ampm:
            if not 1 <= hour <= 12:
                continue
            hour = (0 if hour == 12 else hour) + (12 if ampm.lower() == "pm" else 0)
        if not (0 <= hour < 24 and 0 <= minute < 60):
            continue
        tz = None
        tzname = m.group("tz")
        if tzname and ZoneInfo is not None:
            try:
                tz = ZoneInfo(tzname.strip())
            except Exception:  # noqa: BLE001
                tz = None
        local_now = now_utc.astimezone(tz) if tz else now_utc.astimezone()
        reset = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if m.group("mon") and m.group("day"):
            try:
                reset = reset.replace(month=_MONTHS[m.group("mon")[:3].lower()], day=int(m.group("day")))
            except ValueError:
                pass
            if reset < local_now - timedelta(days=1):
                reset = reset.replace(year=reset.year + 1)
        elif reset <= local_now:
            reset += timedelta(days=1)
        return reset.astimezone(timezone.utc)
    return None


@dataclass
class UsageLimitConfig:
    #: Wait for the usage window to reset and resume, instead of failing.
    enabled: bool = True
    #: Seconds to wait past the reset time before resuming.
    buffer_seconds: float = 60
    #: When the reset time can't be read, re-check after this many minutes.
    fallback_wait_minutes: float = 15
    #: Maximum number of waits per run (``None`` = unlimited).
    max_waits: Optional[int] = None
    #: Never wait longer than this in one go (hours), guarding against a bad parse.
    max_wait_hours: float = 7 * 24
    #: The prompt sent when resuming the session after the reset.
    continue_prompt: str = (
        "You were interrupted because the usage limit was reached. The limit has now reset. "
        "Continue exactly where you left off and finish the task."
    )


class UsageLimitReached(Exception):
    """Raised by the agent invocation when the agent hit its usage limit."""

    _tag = "UsageLimitReached"

    def __init__(self, message: str, session_id: Optional[str], resets_at: Optional[datetime], partial_output: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.session_id = session_id
        self.resets_at = resets_at
        self.partial_output = partial_output


def _fmt_remaining(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


async def wait_for_reset(
    resets_at: Optional[datetime],
    config: UsageLimitConfig,
    status: Callable[[str, str], None],
    signal: Optional[AbortSignal] = None,
    sleep: Callable[[float], "asyncio.Future"] = asyncio.sleep,  # type: ignore[assignment]
) -> None:
    """Sleep until ``resets_at`` + buffer (or the fallback interval), posting
    a status line every 15 minutes. Abortable through ``signal``."""
    now = datetime.now(timezone.utc)
    if resets_at is None:
        target = now + timedelta(minutes=config.fallback_wait_minutes)
        status(
            f"Usage limit reached, but the reset time couldn't be read. Retrying in {config.fallback_wait_minutes:g} min.",
            "warn",
        )
    else:
        target = resets_at + timedelta(seconds=config.buffer_seconds)
        cap = now + timedelta(hours=config.max_wait_hours)
        if target > cap:
            target = cap
        local = target.astimezone()
        status(
            f"Usage limit reached. Waiting until {local:%Y-%m-%d %H:%M %Z} "
            f"({_fmt_remaining((target - now).total_seconds())}) for the window to reset, then resuming.",
            "warn",
        )
    last_report = time.monotonic()
    while True:
        if signal is not None:
            signal.throw_if_aborted()
        remaining = (target - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            break
        # Short sleeps keep laptop suspend / clock jumps honest.
        chunk = min(30.0, remaining)
        if signal is not None:
            waiter = asyncio.ensure_future(signal.wait())
            try:
                await asyncio.wait({waiter}, timeout=chunk)
            finally:
                waiter.cancel()
        else:
            await sleep(chunk)
        if time.monotonic() - last_report >= 15 * 60:
            last_report = time.monotonic()
            left = (target - datetime.now(timezone.utc)).total_seconds()
            if left > 0:
                status(f"Still waiting for the usage limit to reset: {_fmt_remaining(left)} left.", "info")
    if signal is not None:
        signal.throw_if_aborted()
    status("Usage window reset. Resuming the agent session.", "success")

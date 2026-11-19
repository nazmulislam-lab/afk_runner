"""Context window figures. Port of ``ContextWindow.ts``.

How full an iteration's context window was: the input side of its LAST model
call, cache reads and cache writes included (ADR 0023). Never sum calls.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, List, Optional


@dataclass(frozen=True)
class ContextWindow:
    #: Input tokens of the last model call, cached and uncached together.
    tokens: int
    #: The model's context window size, only when the agent reports it.
    limit: Optional[int] = None


def format_context_window(cw: ContextWindow) -> str:
    """``47k``, or ``32k / 258k`` when the limit is known. Tokens round up,
    the limit rounds down."""
    used = f"{math.ceil(cw.tokens / 1000)}k"
    return used if cw.limit is None else f"{used} / {math.floor(cw.limit / 1000)}k"


def build_context_window_lines(iterations: Iterable[object]) -> List[str]:
    """``Context window: NNNk`` per iteration that has a context window."""
    lines: List[str] = []
    for it in iterations:
        cw = getattr(it, "context_window", None)
        if cw is not None:
            lines.append(f"Context window: {format_context_window(cw)}")
    return lines

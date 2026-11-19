"""Process-wide shutdown registry. Port of ``shutdownRegistry.ts``.

Sandboxes need synchronous cleanup (remove a container, print worktree
recovery guidance) when the host process is interrupted. One handler per
signal fans out to every registered teardown. On SIGINT/SIGTERM every
teardown runs and the process exits once with code 1; on a normal exit every
teardown runs without forcing the exit code.

A teardown must be synchronous and must not exit the process itself.
"""

from __future__ import annotations

import atexit
import os
import signal
import sys
import threading
from typing import Any, Callable, Dict, List

ShutdownCallback = Callable[[], None]

_teardowns: List[ShutdownCallback] = []
_lock = threading.RLock()
_installed = False
_previous: Dict[int, Any] = {}
_atexit_registered = False


def _run_teardowns() -> None:
    with _lock:
        callbacks = list(_teardowns)
    for teardown in callbacks:
        try:
            teardown()
        except Exception:  # noqa: BLE001 - best effort
            pass


def _handle_exit() -> None:
    _run_teardowns()


def _handle_signal(signum: int, frame: Any) -> None:  # noqa: ARG001
    _detach()
    _run_teardowns()
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    os._exit(1)


def _attach() -> None:
    global _installed, _atexit_registered
    if _installed:
        return
    _installed = True
    if not _atexit_registered:
        atexit.register(_handle_exit)
        _atexit_registered = True
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                _previous[sig] = signal.signal(sig, _handle_signal)
            except (ValueError, OSError):
                pass


def _detach() -> None:
    global _installed
    if not _installed:
        return
    _installed = False
    if threading.current_thread() is threading.main_thread():
        for sig, prev in list(_previous.items()):
            try:
                signal.signal(sig, prev if prev is not None else signal.SIG_DFL)
            except (ValueError, OSError):
                pass
    _previous.clear()


def register_shutdown(teardown: ShutdownCallback) -> Callable[[], None]:
    """Register ``teardown`` to run on SIGINT/SIGTERM or exit. Returns an
    idempotent unregister function; the last unregister restores the
    default signal behaviour."""
    with _lock:
        _teardowns.append(teardown)
        _attach()
    active = [True]

    def unregister() -> None:
        if not active[0]:
            return
        active[0] = False
        with _lock:
            if teardown in _teardowns:
                _teardowns.remove(teardown)
            if not _teardowns:
                _detach()

    return unregister

"""AbortController / AbortSignal for asyncio, and ``race_abort_signal``.

Python stand-in for the web ``AbortSignal`` the TypeScript API accepts as
``signal``. Aborting cancels the in-flight work and re-raises
``signal.reason`` verbatim (no sandcastle wrapping), like the original.

``abort()`` is thread-safe: you can abort from a signal handler or another
thread while the run awaits on its event loop.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Awaitable, Callable, List, Optional, TypeVar

T = TypeVar("T")


class AbortError(Exception):
    """Default abort reason, like the DOM's ``AbortError``."""

    _tag = "AbortError"

    def __init__(self, message: str = "This operation was aborted") -> None:
        super().__init__(message)
        self.message = message


class AbortSignal:
    def __init__(self) -> None:
        self._aborted = False
        self._reason: Optional[BaseException] = None
        self._listeners: List[Callable[[], None]] = []
        self._lock = threading.Lock()

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def reason(self) -> Optional[BaseException]:
        return self._reason

    def throw_if_aborted(self) -> None:
        if self._aborted:
            raise self._reason if self._reason is not None else AbortError()

    def add_listener(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Call ``fn`` once on abort (immediately if already aborted).
        Returns a function that removes the listener."""
        with self._lock:
            if not self._aborted:
                self._listeners.append(fn)

                def remove() -> None:
                    with self._lock:
                        if fn in self._listeners:
                            self._listeners.remove(fn)

                return remove
        fn()
        return lambda: None

    def _abort(self, reason: Optional[BaseException]) -> None:
        with self._lock:
            if self._aborted:
                return
            self._aborted = True
            self._reason = reason if reason is not None else AbortError()
            listeners, self._listeners = self._listeners, []
        for fn in listeners:
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    async def wait(self) -> None:
        """Resolve once the signal aborts."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        def on_abort() -> None:
            def settle() -> None:
                if not fut.done():
                    fut.set_result(None)

            try:
                loop.call_soon_threadsafe(settle)
            except RuntimeError:
                pass

        remove = self.add_listener(on_abort)
        try:
            await fut
        finally:
            remove()

    @staticmethod
    def timeout(seconds: float) -> "AbortSignal":
        """A signal that aborts after ``seconds`` (like ``AbortSignal.timeout``)."""
        controller = AbortController()
        timer = threading.Timer(seconds, lambda: controller.abort(TimeoutError("The operation timed out")))
        timer.daemon = True
        timer.start()
        return controller.signal


class AbortController:
    def __init__(self) -> None:
        self.signal = AbortSignal()

    def abort(self, reason: Optional[BaseException] = None) -> None:
        self.signal._abort(reason)


async def race_abort_signal(awaitable: Awaitable[T], signal: Optional[AbortSignal]) -> T:
    """Race ``awaitable`` against ``signal``.

    No signal: await as is. Already aborted: raise ``signal.reason``. Otherwise
    whichever settles first wins; on abort the work is cancelled and
    ``signal.reason`` is raised.
    """
    if signal is None:
        return await awaitable
    if signal.aborted:
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        signal.throw_if_aborted()
    task = asyncio.ensure_future(awaitable)
    abort_task = asyncio.ensure_future(signal.wait())
    try:
        done, _ = await asyncio.wait({task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        task.cancel()
        abort_task.cancel()
        raise
    if task in done:
        abort_task.cancel()
        return task.result()
    task.cancel()
    try:
        await task
    except BaseException:  # noqa: BLE001 - cancellation / downstream errors
        pass
    signal.throw_if_aborted()
    raise AbortError()  # pragma: no cover


def check_abort(signal: Optional[AbortSignal]) -> None:
    if signal is not None:
        signal.throw_if_aborted()

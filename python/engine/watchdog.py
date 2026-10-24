"""Detect an engine whose event loop no longer runs, and let the app's shell restart it.

The Rust shell only notices an engine process that *exited*. A deadlocked engine (a C library
that never returns while holding the GIL, a lock taken twice) stays alive but answers nothing,
and the app looks connected for ever. This watchdog closes that gap from the inside:

* a task on the event loop writes a time stamp every second (the loop's heartbeat);
* a daemon thread, which does not need the loop, looks at that time stamp; when it is older than
  ``XRADAR_WATCHDOG_S`` seconds (default 60; ``0`` switches the watchdog off), it writes the
  Python stack of every thread to the crash log (so the cause can be read afterwards), logs one
  sentence, and ends the process with exit code 75. The shell restarts an engine that exited, the
  app notices the new ``engine_id`` and says so (docs/STABILITY.md, "A hung engine").

A thread that holds the GIL for good also stops the watchdog thread; there only the app's own
ping (``src/lib/utils/heartbeat.ts``) can tell the user. Long but healthy work never trips it:
the slowest stall measured in an export is about 2 s, far below the default.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("engine.watchdog")

DEFAULT_STALL_S = 60.0
#: Exit code of an engine that stopped because its event loop was stuck (EX_TEMPFAIL).
EXIT_CODE_STALLED = 75
_TICK_S = 1.0


def stall_limit_s() -> float | None:
    """Seconds without a loop heartbeat after which the engine restarts (None = never)."""
    raw = os.environ.get("XRADAR_WATCHDOG_S", "").strip()
    if not raw:
        return DEFAULT_STALL_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_STALL_S
    if value != value:  # NaN
        return DEFAULT_STALL_S
    return value if 0 < value < float("inf") else None


def exit_stalled(stalled_s: float, crash_log: Any = None) -> None:  # pragma: no cover
    """End the process: stacks of all threads to *crash_log* (stderr when None), then exit 75."""
    with contextlib.suppress(Exception):
        faulthandler.dump_traceback(file=crash_log or sys.stderr, all_threads=True)
    logger.critical(
        "The engine's event loop did not run for %.0f s; exiting so the app can restart it",
        stalled_s,
    )
    for stream in (sys.stderr, sys.stdout):
        with contextlib.suppress(Exception):
            stream.flush()
    os._exit(EXIT_CODE_STALLED)


class LoopWatchdog:
    """Heartbeat of one event loop plus the thread that watches it."""

    def __init__(
        self,
        limit_s: float,
        on_stall: Callable[[float], None] = exit_stalled,
        *,
        tick_s: float = _TICK_S,
        check_s: float | None = None,
    ) -> None:
        self.limit_s = limit_s
        self.on_stall = on_stall
        self.tick_s = tick_s
        self._check_s = check_s if check_s is not None else max(0.05, min(5.0, limit_s / 4))
        self._last = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._task: asyncio.Task[Any] | None = None
        self.fired = False

    def beat(self) -> None:
        self._last = time.monotonic()

    def stalled_for(self) -> float:
        return time.monotonic() - self._last

    async def _ticker(self) -> None:
        while True:
            self.beat()
            await asyncio.sleep(self.tick_s)

    def _watch(self) -> None:
        while not self._stop.wait(self._check_s):
            stalled = self.stalled_for()
            if stalled > self.limit_s:
                self.fired = True
                self.on_stall(stalled)
                return

    def start(self) -> LoopWatchdog:
        """Start beating on the running loop and watching from a daemon thread."""
        self.beat()
        self._task = asyncio.get_running_loop().create_task(self._ticker())
        self._thread = threading.Thread(target=self._watch, name="engine-watchdog", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()

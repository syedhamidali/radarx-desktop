"""Cooperative cancellation of long jobs (modules, exports) and the guards around them.

A Python thread cannot be killed, so a job is stopped by asking it: the job checks a flag
whenever it reports progress and stops with :class:`JobCancelled`. Exports already report
progress between their stages (and a batch between its files), so wrapping the progress callback
with :func:`cancel_on_progress` is enough to give them a real cancel; modules check the flag
through ``ctx.progress`` / ``ctx.cancelled`` (see ``engine.plugins``).

Used by ``server.py`` (the ``cancel_module`` / ``cancel_export`` messages) and by
``engine.exporter`` (a cancelled batch must stop, not log the cancel as one more failed item).
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Callable, Iterator

ProgressCallback = Callable[[int, str], None]


class JobCancelled(Exception):  # noqa: N818 - control-flow signal, not an error condition
    """A job was stopped at the user's request; whatever it had written is cleaned up."""


def cancel_on_progress(progress: ProgressCallback, cancel: threading.Event) -> ProgressCallback:
    """Wrap *progress* so the next report after ``cancel`` is set raises :class:`JobCancelled`."""

    def wrapped(percent: int, message: str) -> None:
        if cancel.is_set():
            raise JobCancelled("cancelled")
        progress(percent, message)

    return wrapped


class JobRegistry:
    """The cancel flags of the jobs one connection is running, by kind (``module``, ``export``).

    Event-loop only (no locking): handlers register and cancel from the loop; the worker
    threads only read the ``threading.Event`` they were handed.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, list[tuple[str | None, threading.Event]]] = {}

    def start(self, kind: str, file_id: str | None = None) -> threading.Event:
        """Register a job and give its cancel flag; call :meth:`finish` when it ends."""
        event = threading.Event()
        self._jobs.setdefault(kind, []).append((file_id, event))
        return event

    def finish(self, kind: str, event: threading.Event) -> None:
        self._jobs[kind] = [job for job in self._jobs.get(kind, []) if job[1] is not event]

    @contextlib.contextmanager
    def running(self, kind: str, file_id: str | None = None) -> Iterator[threading.Event]:
        """Register a job for the duration of the ``with`` block and give its cancel flag."""
        event = self.start(kind, file_id)
        try:
            yield event
        finally:
            self.finish(kind, event)

    def cancel(self, kind: str, file_id: str | None = None) -> int:
        """Ask the running jobs of *kind* to stop (only those on *file_id* when one is given).

        Returns how many were asked.
        """
        asked = 0
        for owner, event in list(self._jobs.get(kind, [])):
            if file_id is None or owner is None or owner == file_id:
                if not event.is_set():
                    asked += 1
                event.set()
        return asked

    def running_count(self, kind: str) -> int:
        return len(self._jobs.get(kind, []))


# ---------------------------------------------------------------------------
# Memory guard for a running job
# ---------------------------------------------------------------------------

MB = 1024 * 1024


def _total_ram() -> int | None:
    # engine.reader knows Windows too (os.sysconf does not exist there); imported late because
    # it pulls in xarray
    from engine.reader import system_memory_bytes

    return system_memory_bytes()


def module_memory_limit_bytes() -> int | None:
    """Resident size of the whole engine above which a running module is stopped.

    ``XRADAR_MODULE_MAX_MB`` sets it in megabytes (``0`` switches the guard off). Default: 75 %
    of the machine's RAM, so a module that allocates without bound is stopped before the
    operating system starts swapping the whole desktop to a crawl. ``None``: guard off or the
    RAM size is unknown.
    """
    raw = os.environ.get("XRADAR_MODULE_MAX_MB", "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = -1.0
        if value == 0:
            return None
        if value > 0 and value != float("inf"):
            return int(value * MB)
    total = _total_ram()
    return int(total * 0.75) if total else None

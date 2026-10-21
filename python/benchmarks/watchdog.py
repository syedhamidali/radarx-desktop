"""A memory and disk watchdog for the long benchmark scripts (not part of the package).

A daemon thread checks every few seconds the resident memory of this process and its
children, the free space of the data volume and the swap in use; when a limit is crossed it
says so on stderr, stops the children and exits (code 3) before the machine starts swapping
to a full disk. It also keeps the peak of the process tree's RSS, printed at exit so that a
run can report what it needed.

    from watchdog import start_watchdog
    start_watchdog(max_rss_gb=6, min_free_disk_gb=20, max_swap_gb=8)
"""

from __future__ import annotations

import atexit
import contextlib
import os
import shutil
import sys
import threading
import time

import psutil

DISK_PATH = "/System/Volumes/Data" if sys.platform == "darwin" else "/"

_peak = {"rss_gb": 0.0}


def tree_rss_gb(proc: psutil.Process | None = None) -> float:
    """Resident memory (GB) of a process and all its descendants."""
    p = proc or psutil.Process()
    total = 0
    for q in [p, *p.children(recursive=True)]:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            total += q.memory_info().rss
    return total / 1e9


def free_disk_gb(path: str = DISK_PATH) -> float:
    return shutil.disk_usage(path if os.path.exists(path) else "/").free / 1e9


def swap_used_gb() -> float:
    return psutil.swap_memory().used / 1e9


def peak_rss_gb() -> float:
    return _peak["rss_gb"]


def start_watchdog(
    *,
    max_rss_gb: float = 6.0,
    min_free_disk_gb: float = 20.0,
    max_swap_gb: float = 8.0,
    interval_s: float = 3.0,
    disk_path: str = DISK_PATH,
    log=None,
) -> threading.Thread:
    """Start the watchdog thread; returns it. ``log`` is a ``str -> None`` sink (stderr)."""
    say = log or (lambda m: print(m, file=sys.stderr, flush=True))
    me = psutil.Process()

    def abort(reason: str) -> None:
        say(f"WATCHDOG ABORT: {reason} (peak tree RSS {_peak['rss_gb']:.2f} GB)")
        for c in me.children(recursive=True):
            with contextlib.suppress(psutil.Error):
                c.terminate()
        time.sleep(0.5)
        for c in me.children(recursive=True):
            with contextlib.suppress(psutil.Error):
                c.kill()
        os._exit(3)

    def loop() -> None:
        while True:
            try:
                rss = tree_rss_gb(me)
                _peak["rss_gb"] = max(_peak["rss_gb"], rss)
                if rss > max_rss_gb:
                    abort(f"process tree RSS {rss:.2f} GB > {max_rss_gb:g} GB")
                free = free_disk_gb(disk_path)
                if free < min_free_disk_gb:
                    abort(f"free disk {free:.1f} GB < {min_free_disk_gb:g} GB on {disk_path}")
                swap = swap_used_gb()
                if swap > max_swap_gb:
                    abort(f"swap in use {swap:.1f} GB > {max_swap_gb:g} GB")
            except psutil.Error:
                pass
            time.sleep(interval_s)

    t = threading.Thread(target=loop, name="watchdog", daemon=True)
    t.start()
    atexit.register(lambda: say(f"watchdog: peak process-tree RSS {_peak['rss_gb']:.2f} GB"))
    return t

"""Give freed memory back to the operating system.

Opening and gridding a radar volume allocates a few hundred MB of short-lived arrays. When
they are freed, the C allocator keeps most of those pages for itself (glibc on Linux, the
malloc zones on macOS), so the process's resident size stays at its high-water mark and
looks like a leak although the next volume reuses the same pages. A process that does this
for many volumes in a row (a nowcast frame worker, a batch) is better off returning the
pages after each one; that is what :func:`trim` does. It is a hint, never an error: where the
platform has no such call (Windows, musl) it does nothing.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import gc
import sys
from collections.abc import Callable

_trim_fn: Callable[[], None] | None = None
_resolved = False


def _resolve() -> Callable[[], None] | None:
    try:
        if sys.platform == "darwin":
            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libSystem.B.dylib")
            relief = libc.malloc_zone_pressure_relief
            relief.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            relief.restype = ctypes.c_size_t
            return lambda: relief(None, 0)  # every zone, as much as possible
        if sys.platform.startswith("linux"):
            libc = ctypes.CDLL("libc.so.6")
            malloc_trim = libc.malloc_trim  # glibc only; musl raises AttributeError
            malloc_trim.argtypes = [ctypes.c_size_t]
            malloc_trim.restype = ctypes.c_int
            return lambda: malloc_trim(0)
    except (OSError, AttributeError):
        pass
    return None


def trim(collect: bool = True) -> bool:
    """Collect garbage (``collect``) and ask the allocator to return its free pages.

    Returns whether the allocator was asked (False where the platform has no such call).
    """
    global _trim_fn, _resolved
    if collect:
        gc.collect()
    if not _resolved:
        _trim_fn, _resolved = _resolve(), True
    if _trim_fn is None:
        return False
    try:
        _trim_fn()
    except Exception:  # a hint only
        return False
    return True

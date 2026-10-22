"""Make the netCDF-C / HDF5 libraries safe to use from this engine.

Why this exists (see docs/STABILITY.md, "Native crash in NetCDF files"): the engine died with
SIGSEGV / "Bus error" when it opened a NetCDF file that holds a variable-length string (every file
that xarray wrote with a ``str`` variable, e.g. ``sweep_mode``). The reproduction needs no
threads at all, only *two handles on the same file at the same time*:

1. handle A on the file stays open,
2. handle B on the same file reads the string variable and is closed,
3. handle C is opened on the file -> use-after-free inside HDF5 (``H5T__conv_vlen``).

xarray gives every ``CachingFileManager`` its own handle, so the engine produced that pattern
all by itself: a failed ``cfradial1`` attempt leaves a handle behind, xradar's ``cfradial2`` reader
opens the file, closes it again and the first data read re-opens it (step 3). Two readers of the
same file, or a reader plus an export, do the same.

Three rules remove it, and they also make the libraries safe for the engine's worker threads
(the netCDF-C and HDF5 builds in the wheels are not thread-safe):

* **One handle per file.** Read-only managers of the same file share one cached handle (the key
  no longer contains a per-manager id). Closing it - explicitly or by cache eviction - makes every
  user re-open it later, one after the other, never two at once.
* **One lock.** Opening, reading, closing and evicting a handle all take :data:`LOCK`
  (a re-entrant lock; xarray's own netCDF-C and HDF5 locks are pointed at the same object, so a
  read cannot overlap an open or a close).
* :func:`native_section` is the same lock for engine code that talks to the libraries outside
  xarray's managers (opening a tree, writing a NetCDF file).

``XRADAR_HDF5_GUARD=0`` switches the patches off (for comparing behaviour; not for use).
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import threading
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

#: Held for every call into netCDF-C / HDF5 made by the engine. Re-entrant on purpose: a read
#: may trigger an open (re-opening an evicted handle) and a garbage collection may close a file
#: from inside either.
LOCK = threading.RLock()

#: manager_id shared by all read-only managers of one file (see module docstring)
SHARED_ID = "xradar-shared-handle"

_installed = False
_install_lock = threading.Lock()


@contextlib.contextmanager
def native_section() -> Iterator[None]:
    """Run a block of native netCDF/HDF5 work (open a tree, write a file) one thread at a time."""
    with LOCK:
        yield


@contextlib.contextmanager
def new_file(path: str | os.PathLike[str]) -> Iterator[None]:
    """Write a NetCDF-4 file at ``path`` without HDF5 printing a harmless error.

    Creating a file, netCDF-C first asks HDF5 whether ``path`` is an HDF5 file. When it does not
    exist yet, HDF5 prints a ``H5F__is_hdf5 ... No such file or directory`` error stack from any
    thread where netCDF-C did not switch HDF5's error printing off (the engine's worker
    threads), which looks alarming in the engine log. So the probe only ever sees an existing
    file: an empty one is created first and removed again if the write fails.
    """
    p = os.fspath(path)
    created = False
    if not os.path.exists(p):
        try:
            with open(p, "xb"):
                created = True
        except OSError:
            created = False  # the writer reports the real problem (folder missing, read-only...)
    try:
        yield
    except BaseException:
        if created:
            with contextlib.suppress(OSError):
                if os.path.getsize(p) == 0:
                    os.remove(p)
        raise


def guarded[F](func: F) -> F:
    """Decorator: the whole function runs inside :func:`native_section`.

    For writers (``to_netcdf`` ...) and one-shot readers. Do not use it around code that waits
    for dask worker threads to read NetCDF data: they would wait for this lock (the engine
    does not open its files with dask chunks).
    """

    @functools.wraps(func)  # type: ignore[arg-type]
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with LOCK:
            return func(*args, **kwargs)  # type: ignore[operator]

    return wrapper  # type: ignore[return-value]


def enabled() -> bool:
    return os.environ.get("XRADAR_HDF5_GUARD", "1").strip().lower() not in ("0", "false", "no")


def _guarded_openers() -> tuple[Any, ...]:
    openers: list[Any] = []
    try:
        import netCDF4

        openers.append(netCDF4.Dataset)
    except ImportError:
        pass
    try:
        import h5netcdf

        openers.append(h5netcdf.File)
    except ImportError:
        pass
    return tuple(openers)


def install() -> bool:
    """Patch xarray's file handling (once). Returns True when the patches are active."""
    global _installed
    if not enabled():
        return False
    with _install_lock:
        if _installed:
            return True
        try:
            from xarray.backends import file_manager, locks
        except ImportError:  # no xarray: nothing to guard
            return False

        manager_cls = file_manager.CachingFileManager
        openers = _guarded_openers()
        if not openers or not hasattr(manager_cls, "_acquire_with_cache_info"):
            logger.warning("xarray file manager changed shape; HDF5 guard not installed")
            return False

        # 1. xarray's own netCDF-C and HDF5 locks become our re-entrant lock
        for name in ("NETCDFC_LOCK", "HDF5_LOCK"):
            lock = getattr(locks, name, None)
            if lock is not None and hasattr(lock, "lock"):
                lock.lock = LOCK

        # 2. read-only managers of one file share one handle, guarded by LOCK
        original_init = manager_cls.__init__

        def shared_init(self, opener, *args, **kwargs):  # type: ignore[no-untyped-def]
            mode = kwargs.get("mode")
            if (
                opener in openers
                and mode in ("r", None)
                and kwargs.get("manager_id") is None
                and kwargs.get("cache") is None
            ):
                kwargs["manager_id"] = SHARED_ID
                if kwargs.get("lock") in (None, False):
                    kwargs["lock"] = LOCK  # (xarray's own netCDF4 store passes its locks: ours)
            original_init(self, opener, *args, **kwargs)

        manager_cls.__init__ = shared_init  # type: ignore[method-assign]

        # 3. closing (explicit, by garbage collection, or by cache eviction) takes the lock too
        original_close = manager_cls.close

        def close(self, needs_lock: bool = True) -> None:  # type: ignore[no-untyped-def]
            with LOCK:
                original_close(self, needs_lock)

        manager_cls.close = close  # type: ignore[method-assign]

        cache = file_manager.FILE_CACHE
        previous = getattr(cache, "_on_evict", None)
        if previous is not None:

            def on_evict(key: Any, value: Any) -> None:
                with LOCK:
                    previous(key, value)

            cache._on_evict = on_evict

        _installed = True
        logger.debug("HDF5 guard installed")
        return True

"""WebSocket server entry point for radarx-desktop Python sidecar."""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextlib
import contextvars
import functools
import gc
import inspect
import json
import logging
import math
import os
import re
import shutil
import signal
import sys
import threading
import time
import traceback
import uuid
import weakref
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np

_server_dir = Path(__file__).resolve().parent
if str(_server_dir) not in sys.path:
    sys.path.insert(0, str(_server_dir))

if getattr(sys, "frozen", False):
    # The packaged engine: PyInstaller points matplotlib at a fresh temp dir on every launch, which
    # re-builds the font cache (~10 s) each time. Keep it in a per-user folder instead. This runs
    # before anything imports matplotlib.
    _mpl_dir = Path.home() / ".xradar-desktop" / "matplotlib"
    try:
        _mpl_dir.mkdir(parents=True, exist_ok=True)
        os.environ["MPLCONFIGDIR"] = str(_mpl_dir)
    except OSError:
        pass

import websockets
from websockets.asyncio.server import ServerConnection

from engine import (
    _radarx_import,
    derived,
    layer_provenance,
    mosaic,
    pipeline,
    plugins,
    quality,
    rhi,
    varmap,
    watchdog,
    web,
)
from engine import multidoppler as mdop
from engine.account import activity as account_activity
from engine.account import config as account_config
from engine.account import core as account_core
from engine.account.errors import AccountRequired
from engine.animate_service import AnimateMessages
from engine.cloud import (
    get_latest_nexrad_l2,
    latest_scan_info,
    list_arco_scans,
    list_nexrad_l2_files,
    list_nexrad_stations,
)
from engine.dualpol import histogram_data, region_stats, scatter_data
from engine.environment.messages import EnvironmentMessages
from engine.exporter import _FORMAT_EXT, RadarExporter
from engine.jobs import JobCancelled, JobRegistry, cancel_on_progress
from engine.processor import (
    MAX_SECTION_POINTS,
    RadarProcessor,
    extract_cross_section,
    extract_vertical_profile,
    rhi_line,
)
from engine.qc import QC_REGISTRY, QCPipeline
from engine.reader import (
    RadarReader,
    _serializable,
    max_open_bytes,
    preload_backends,
    sorted_sweep_names,
    system_memory_bytes,
)
from engine.renderer import RadarRenderer, _palettes, _resolve_range
from engine.retrievals import RETRIEVAL_REGISTRY, run_retrieval
from engine.statistics import get_statistics
from engine.temporal import (
    compute_accumulation,
    compute_difference,
    compute_time_series,
)
from engine.volume import extract_cross_section_3d, extract_volume

_radarx_import.install()  # radarx must not configure the root logger (issue #91)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("xradar-server")

# WebSocket configuration — override via environment variables
WS_MAX_SIZE_MB = int(os.environ.get("XRADAR_WS_MAX_SIZE_MB", "10"))
WS_MAX_SIZE = WS_MAX_SIZE_MB * 1024 * 1024
EXPECTED_SESSION_TOKEN = os.environ.get("XRADAR_SESSION_TOKEN")
ALLOWED_ORIGIN_PREFIXES = (
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "http://localhost:",
    "http://127.0.0.1:",
)

# Binary frames stay comfortably under WKWebView's 1 MB WebSocket message cap.
CHUNK_SIZE = 900 * 1024
# Sentinel the client interprets as "no data" in binary value arrays.
NODATA_SENTINEL = -9999.0
# Largest text file the UI may ask the engine to write (CSV / GeoJSON exports).
SAVE_TEXT_MAX_BYTES = 20 * 1024 * 1024
# ... and the (smaller) limit of the public web service.
WEB_SAVE_TEXT_MAX_BYTES = 5 * 1024 * 1024


#: Identifies this engine process; reported by ``ping`` (see ``_handle_ping``).
ENGINE_ID = uuid.uuid4().hex

#: The account gate (docs/ACCOUNTS.md): until a connection has presented a valid signed-in account
#: with ``auth_session``, every message except these is answered ``{type: "auth_required"}`` and no
#: radar processing happens. ``ping`` and ``get_web_info`` carry no data.
AUTH_MESSAGES = frozenset({"auth_session", "auth_signout", "auth_status"})
GATE_OPEN_MESSAGES = frozenset({"ping", "get_web_info"}) | AUTH_MESSAGES

#: One worker thread for the activity log: events keep their order (a sign-out is sent after what
#: came before it) and the log never competes with the work threads or blocks the event loop.
_activity_pool_lock = threading.Lock()
_activity_pool_obj: concurrent.futures.ThreadPoolExecutor | None = None


def _activity_pool() -> concurrent.futures.ThreadPoolExecutor:
    """Created on first use; :func:`shutdown_activity_pool` joins it (tests, forking)."""
    global _activity_pool_obj
    with _activity_pool_lock:
        if _activity_pool_obj is None:
            _activity_pool_obj = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="radarx-rec"
            )
        return _activity_pool_obj


def shutdown_activity_pool() -> None:
    global _activity_pool_obj
    with _activity_pool_lock:
        pool, _activity_pool_obj = _activity_pool_obj, None
    if pool is not None:
        pool.shutdown(wait=True)


# The request being handled by the current task (for the activity log's details).
_CURRENT_REQUEST: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "radarx_current_request", default=None
)

# True while the current message task has announced progress < 100 without closing it.
_PROGRESS_OPEN: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "xradar_progress_open", default=False
)

# Shared application state — survives across WebSocket reconnections
_shared_renderer = RadarRenderer()
_shared_processor = RadarProcessor()
_shared_exporter = RadarExporter()

# Multi-file support: dict of file_id -> RadarReader instances (insertion ordered).
_file_readers: dict[str, RadarReader] = {}
_last_file_id: str | None = None  # most recently opened file
# Placeholder reader used only when no file is open (never holds data).
_shared_reader = RadarReader()
# Bumped whenever a reader's datatree is replaced (e.g. by ``process``) so the
# renderer cache never serves stale pixels for a previous incarnation of the data.
_generations: dict[str, int] = {}
# User variable mappings ({role: variable}) per file_id; persisted per file path (engine.varmap).
_var_overrides = varmap.OverrideStore()
# Strong refs to fire-and-forget tasks so they are not garbage collected mid-run.
_background_tasks: set[asyncio.Task[Any]] = set()

_RENDER_ACCEPTS_TOKEN = "cache_token" in inspect.signature(RadarRenderer.render_sweep).parameters


class _LoopLocks:
    """Named asyncio locks that are (re)created per running event loop.

    Module-level ``asyncio.Lock`` objects can end up bound to a stale loop (tests,
    server restarts); resolving them lazily avoids "attached to a different loop".
    """

    def __init__(self) -> None:
        self._locks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}

    def get(self, name: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        entry = self._locks.get(name)
        if entry is None or entry[0] is not loop:
            entry = (loop, asyncio.Lock())
            self._locks[name] = entry
        return entry[1]


_locks = _LoopLocks()

# file_id -> cancel flag of the pipeline currently running on that file (see _handle_run_pipeline)
_PIPELINE_CANCELS: dict[str, threading.Event] = {}

# request_id -> cancel flag of a Command Studio run (see _handle_cli_run)
_CLI_CANCELS: dict[str, threading.Event] = {}


class _ConnState:
    """Engine state private to one connection (web mode).

    Desktop mode shares the module-level objects above across reconnections. A public service
    must not: every field here mirrors one of them, and the accessors below pick the calling
    connection's copy (via ``_SESSION``) or, with web mode off, the module-level one.
    """

    def __init__(self) -> None:
        self.renderer = RadarRenderer()
        self.processor = RadarProcessor()
        self.exporter = RadarExporter()
        self.file_readers: dict[str, RadarReader] = {}
        self.last_file_id: str | None = None
        self.placeholder = RadarReader()
        self.generations: dict[str, int] = {}
        self.var_overrides = varmap.OverrideStore(persist=False)
        self.pipeline_cancels: dict[str, threading.Event] = {}
        self.multidoppler_runs: dict[str, threading.Event] = {}
        self.locks = _LoopLocks()


#: The web session of the message being handled (None in desktop mode). Set per message task;
#: ``_ContextExecutor`` carries it into worker threads.
_SESSION: contextvars.ContextVar[web.SessionContext | None] = contextvars.ContextVar(
    "xradar_web_session", default=None
)


def _cs() -> _ConnState | None:
    """Per-connection state in web mode, else None (use the module-level desktop state)."""
    ctx = _SESSION.get()
    if ctx is None:
        if web.is_web_mode():
            # Failing closed beats silently serving another visitor's data.
            raise RuntimeError("web mode: request is not bound to a session")
        return None
    return ctx.state


def _readers() -> dict[str, RadarReader]:
    cs = _cs()
    return _file_readers if cs is None else cs.file_readers


def _generations_map() -> dict[str, int]:
    cs = _cs()
    return _generations if cs is None else cs.generations


def _get_last_file_id() -> str | None:
    cs = _cs()
    return _last_file_id if cs is None else cs.last_file_id


def _set_last_file_id(value: str | None) -> None:
    global _last_file_id
    cs = _cs()
    if cs is None:
        _last_file_id = value
    else:
        cs.last_file_id = value


def _placeholder_reader() -> RadarReader:
    cs = _cs()
    return _shared_reader if cs is None else cs.placeholder


def _overrides_store() -> varmap.OverrideStore:
    cs = _cs()
    return _var_overrides if cs is None else cs.var_overrides


def _renderer() -> RadarRenderer:
    cs = _cs()
    return _shared_renderer if cs is None else cs.renderer


def _processor() -> RadarProcessor:
    cs = _cs()
    return _shared_processor if cs is None else cs.processor


def _exporter() -> RadarExporter:
    cs = _cs()
    return _shared_exporter if cs is None else cs.exporter


def _pipeline_cancels() -> dict[str, threading.Event]:
    cs = _cs()
    return _PIPELINE_CANCELS if cs is None else cs.pipeline_cancels


def _mdop_runs() -> dict[str, threading.Event]:
    cs = _cs()
    return _multidoppler_runs if cs is None else cs.multidoppler_runs


class _ContextExecutor(concurrent.futures.ThreadPoolExecutor):
    """Thread pool whose tasks run in the submitter's context (so ``_SESSION`` follows them)."""

    def submit(self, fn, /, *args, **kwargs):  # type: ignore[override]
        ctx = contextvars.copy_context()
        future = super().submit(ctx.run, fn, *args, **kwargs)
        session = _SESSION.get()
        if session is not None:
            session.track_job(future)  # lets the session folder be cleaned up after late writes
        return future


_context_loops: weakref.WeakSet[asyncio.AbstractEventLoop] = weakref.WeakSet()


def _ensure_context_executor(loop: asyncio.AbstractEventLoop) -> None:
    """Make ``loop.run_in_executor(None, ...)`` carry the web session into worker threads."""
    if loop not in _context_loops:
        loop.set_default_executor(_ContextExecutor(thread_name_prefix="xradar-web"))
        _context_loops.add(loop)


def _render_lock() -> asyncio.Lock:
    """Serialises access to the (non thread-safe) datashader renderer."""
    cs = _cs()
    return (_locks if cs is None else cs.locks).get("render")


def _mutation_lock() -> asyncio.Lock:
    """Serialises operations that mutate/consume shared processor/exporter state."""
    cs = _cs()
    return (_locks if cs is None else cs.locks).get("mutation")


async def _run_holding(lock: asyncio.Lock, func: Callable[..., Any], *args: Any) -> Any:
    """Run ``func(*args)`` in the executor while holding *lock*, until the worker really ends.

    ``async with lock: await loop.run_in_executor(...)`` frees the lock the moment the awaiting
    task is cancelled (the client disconnected, a newer request replaced it) although the thread
    keeps running -- and the next request then starts alongside it. For work that is only safe
    (or only affordable: radarx uses every core) one at a time that stacks up. Here the lock is
    released by the worker thread itself when it returns, whatever happened to the caller.
    """
    loop = asyncio.get_running_loop()
    await lock.acquire()

    def job() -> Any:
        try:
            return func(*args)
        finally:
            with contextlib.suppress(RuntimeError):  # RuntimeError: the loop is already closed
                loop.call_soon_threadsafe(lock.release)

    try:
        future = loop.run_in_executor(None, job)
    except BaseException:
        lock.release()
        raise
    return await future


DEFAULT_CLOUD_TIMEOUT_S = 300.0


def cloud_timeout_s() -> float | None:
    """Overall limit of one cloud listing or download-and-open in seconds.

    ``XRADAR_CLOUD_TIMEOUT_S`` changes it (default 300; ``0`` or less switches it off; an
    unparsable value keeps the default). A connection that stalls half-way never fails by
    itself, so without a limit the request (and its progress bar) would hang for ever.
    """
    raw = os.environ.get("XRADAR_CLOUD_TIMEOUT_S", "").strip()
    if not raw:
        return DEFAULT_CLOUD_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CLOUD_TIMEOUT_S
    if value != value:  # NaN
        return DEFAULT_CLOUD_TIMEOUT_S
    return value if 0 < value < float("inf") else None


def _cloud_timeout_text() -> str:
    limit = cloud_timeout_s()
    return (
        f"timed out after {limit:g} s. The cloud service may be slow or unreachable: check the "
        "internet connection and try again."
    )


async def _cloud_call(func: Callable[..., Any], *args: Any) -> Any:
    """Run a blocking cloud call in the executor, at most :func:`cloud_timeout_s` seconds.

    Raises ``TimeoutError`` on expiry; the worker thread cannot be killed and ends on its own
    (its late result is dropped).
    """
    job = asyncio.get_running_loop().run_in_executor(None, func, *args)
    limit = cloud_timeout_s()
    return await (asyncio.wait_for(job, limit) if limit else job)


class UnknownFileError(KeyError):
    """Raised when a request references a file_id that is not (or no longer) open."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else "unknown file"


def _resolve_reader(file_id: str | None) -> tuple[str | None, RadarReader]:
    """Resolve a file_id to ``(file_id, reader)``.

    * An explicit ``file_id`` must refer to an open file — silently serving a
      different file's data would be far worse than an error.
    * No ``file_id`` (older clients) means "the most recently opened file".
    * With nothing open, the empty placeholder reader is returned so callers can
      report "no file is open".
    """
    readers = _readers()
    if file_id:
        reader = readers.get(file_id)
        if reader is None:
            raise UnknownFileError(f"Unknown or closed file_id '{file_id}'")
        return file_id, reader
    last = _get_last_file_id()
    if last and last in readers:
        return last, readers[last]
    return None, _placeholder_reader()


def _get_reader(file_id: str | None) -> RadarReader:
    return _resolve_reader(file_id)[1]


def _get_datatree(file_id: str | None):
    return _get_reader(file_id).datatree


def _lp_kw(reader: RadarReader) -> dict[str, str]:
    """``valid_time`` / ``source`` of an open file for the layer provenance builders."""
    ctx = layer_provenance.reader_context(reader)
    return {"valid_time": ctx["scan_time"], "source": ctx["source"]}


def _overrides_for(file_id: str | None) -> dict[str, str]:
    """The user's variable mapping for a file (``{role: variable}``; empty when none)."""
    resolved_id, reader = _resolve_reader(file_id)
    return _overrides_store().get(resolved_id, reader._path)


def _register_reader(reader: RadarReader) -> str:
    """Store a freshly opened reader under a new file_id and make it the active one."""
    file_id = str(uuid.uuid4())
    _readers()[file_id] = reader
    _generations_map()[file_id] = 0
    _set_last_file_id(file_id)
    if not _RENDER_ACCEPTS_TOKEN:  # legacy renderer cache has no data identity
        _renderer().invalidate_cache()
    return file_id


def _readers_snapshot() -> list[RadarReader]:
    return list(_readers().values())


def _open_usage_bytes(extra: RadarReader | None = None) -> int:
    """Decoded size of every open volume (plus *extra*, a reader not yet registered)."""
    readers = _readers_snapshot()
    if extra is not None and not any(r is extra for r in readers):
        readers.append(extra)
    total = 0
    for reader in readers:
        try:
            total += reader.memory_bytes()
            store = derived.store_for(reader, create=False)
            if store is not None:
                total += store.nbytes
        except Exception as exc:  # accounting must never break a request
            logger.debug("memory accounting failed for a reader: %s", exc)
    return total


def _release_caches() -> None:
    """Drop every re-computable cache: rendered tiles, index maps, lazily fetched sweeps."""
    _renderer().release_memory()
    for reader in _readers_snapshot():
        reader.release_caches()
    gc.collect()


def _evict_if_over_budget(incoming_bytes: int = 0, extra: RadarReader | None = None) -> bool:
    """Evict caches when open data (+ *incoming_bytes*) would exceed the budget.

    Returns True if the budget is still exceeded afterwards. Derived variables are results the
    user computed (already capped by their own LRU), so they are never evicted here.
    """
    budget = max_open_bytes()
    if budget is None:
        return False
    if _open_usage_bytes(extra) + incoming_bytes <= budget:
        return False
    logger.info("Memory budget %d MB exceeded: releasing caches", budget // (1024 * 1024))
    _release_caches()
    return _open_usage_bytes(extra) + incoming_bytes > budget


def memory_status() -> dict[str, Any]:
    """Tracked memory for health checks: ``{open_mb, max_mb, files}`` (``max_mb`` None = no cap)."""
    budget = max_open_bytes()
    return {
        "open_mb": round(_open_usage_bytes() / (1024 * 1024), 1),
        "max_mb": round(budget / (1024 * 1024)) if budget else None,
        "files": len(_readers()),
    }


def _describe_open_error(path: str, exc: BaseException) -> str:
    """Specific, actionable text for a failure to open *path* (no traceback, no jargon)."""
    name = Path(path).name or path
    if isinstance(exc, FileNotFoundError):
        return (
            f"File not found: {path}. It may have been moved, renamed or deleted, or its drive "
            "is not connected."
        )
    if isinstance(exc, PermissionError):
        text = str(exc)
        if "Permission denied reading" in text:
            return text
        return (
            f"Permission denied reading {name}. Check the file's permissions (on macOS, allow "
            "radarx-desktop to access this folder under System Settings > Privacy & Security), "
            "or copy the file to a folder you own."
        )
    if isinstance(exc, MemoryError):
        return (
            f"Not enough memory to open {name}. Close other open files (or other applications) "
            "and try again."
        )
    if isinstance(exc, IsADirectoryError):
        return f"{path} is a folder, not a radar file. Pick a file inside it."
    if isinstance(exc, ValueError):
        return str(exc)  # the reader's messages already name the file and the likely cause
    if isinstance(exc, OSError):
        return (
            f"Could not read {name}: {exc.strerror or exc}. Check that the file is still available."
        )
    return f"Could not open {name} ({type(exc).__name__}: {exc}). The file may be damaged."


#: Fragments of the messages HDF5 / NetCDF / zlib / xradar raise when the bytes of an already
#: opened file turn out to be damaged (opening is lazy, so this shows up on the first data read).
_DAMAGED_DATA_MARKERS = (
    "netcdf: hdf error",
    "netcdf: unknown file format",
    "filter returned failure",
    "can't synchronously read",
    "unable to synchronously",
    "file signature not found",
    "unexpected file end",
    "bad crc",
    "while decompressing",
    "compressed file ended",
    "incorrect header check",
    "invalid block type",
)

_MAX_ERROR_TEXT = 400


def _failure_text(msg_type: str, exc: BaseException) -> tuple[int, str]:
    """``(log level, user-facing text)`` for an exception raised while handling *msg_type*.

    The text names the handler (``... in <type>`` / ``... for <type>``: the UI uses that to decide
    which request a failure belongs to), says what is wrong in plain words and, where the user can
    do something about it, what to do next. It never contains a traceback.
    """
    import errno

    detail = " ".join(str(exc).split())
    if len(detail) > _MAX_ERROR_TEXT:
        detail = detail[:_MAX_ERROR_TEXT] + "..."
    low = detail.lower()
    if isinstance(exc, MemoryError):
        return logging.ERROR, (
            f"Not enough memory in {msg_type}. Close other open files or applications, or ask "
            "for a smaller area / lower resolution, and try again."
        )
    if isinstance(exc, (FileNotFoundError, PermissionError, KeyError, IndexError)):
        pass  # handled below; these are never "damaged data"
    elif any(marker in low for marker in _DAMAGED_DATA_MARKERS):
        return logging.WARNING, (
            f"Unreadable data in {msg_type}: the open file looks truncated or damaged ({detail}). "
            "Re-download or re-copy it, then open it again."
        )
    if isinstance(exc, (ValueError, TypeError, OverflowError)):
        return logging.WARNING, f"Invalid input for {msg_type}: {detail}"
    if isinstance(exc, FileNotFoundError):
        return logging.WARNING, f"File not found: {detail}. Check that the file or folder exists."
    if isinstance(exc, (KeyError, IndexError)):
        return logging.WARNING, f"Not found: {detail}"
    if isinstance(exc, OSError):
        where = (
            f" ({os.path.basename(str(exc.filename))})" if getattr(exc, "filename", None) else ""
        )
        if exc.errno == errno.ENOSPC:
            tail = f"the disk is full{where}. Free some space or choose another folder, then retry."
        elif exc.errno in (errno.EACCES, errno.EPERM):
            tail = f"permission denied{where}. Choose a folder you are allowed to write to."
        elif exc.errno == errno.EROFS:
            tail = f"the location is read-only{where}. Choose another folder."
        elif exc.errno == errno.ENAMETOOLONG:
            tail = f"the file name is too long{where}. Choose a shorter name."
        else:
            tail = f"{detail}. Check that the file or folder is still available."
        return logging.ERROR, f"I/O error in {msg_type}: {tail}"
    if isinstance(exc, ImportError):
        return logging.ERROR, f"Missing dependency: {detail}. Reinstall radarx-desktop's engine."
    return logging.ERROR, (
        f"Unexpected error in {msg_type}: {detail}. Try again; if it keeps happening, restart "
        "the app (details are in the engine log)."
    )


def _blank_to_none(value: Any) -> Any:
    """An empty path means "choose for me" -- not the engine's working directory."""
    return None if value == "" else value


def _cache_token(file_id: str | None) -> tuple[str, int] | None:
    """Identity of the data behind a reader: (file_id, generation)."""
    if file_id is None:
        return None
    return file_id, _generations_map().get(file_id, 0)


def _spawn(coro: Any) -> asyncio.Task[Any]:
    task = asyncio.get_running_loop().create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _origin_allowed(ws: ServerConnection) -> bool:
    request = getattr(ws, "request", None)
    headers = getattr(request, "headers", None)
    if web.is_web_mode():
        # Public service: an explicit allow-list (or same-origin); browsers always send Origin.
        return web.origin_allowed(
            headers.get("Origin") if headers is not None else None,
            headers.get("Host") if headers is not None else None,
            web.settings(),
            allow_missing=False,
        )
    if not EXPECTED_SESSION_TOKEN:
        return True
    origin = headers.get("Origin") if headers is not None else None
    if not origin:
        return True
    return any(origin.startswith(prefix) for prefix in ALLOWED_ORIGIN_PREFIXES)


# ---------------------------------------------------------------------------
# JSON + binary helpers
# ---------------------------------------------------------------------------


def _read_uncached(da: Any) -> np.ndarray:
    """The values of *da* without the open tree keeping them.

    ``.values`` of a lazily read variable stores the array in the variable, which the tree
    shares: one metadata request kept the whole file in memory (+530 MB for a 37 MB NetCDF
    file) and the memory budget did not see it. A full slice reads through a new wrapper.
    """
    if da.ndim == 0:
        return np.asarray(da.values)
    return np.asarray(da.variable[(slice(None),) * da.ndim].values)


def _natural_key(name: str) -> list[Any]:
    """Sort key so ``sweep_2`` comes before ``sweep_10``."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(name))]


def _json_default(obj: Any) -> Any:
    result = _serializable(obj)
    if result is not obj:
        return result
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _sanitize(obj: Any) -> Any:
    """Make *obj* strictly JSON-safe: numpy -> python, NaN/Inf -> None."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return _sanitize(obj.tolist())
    if isinstance(obj, np.generic):
        return _sanitize(obj.item())
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return obj


def _dumps(msg: dict[str, Any]) -> str:
    """Serialise *msg*; NaN/Inf become ``null`` instead of failing the request."""
    try:
        return json.dumps(msg, default=_json_default, allow_nan=False)
    except ValueError:
        # NaN/Inf somewhere in the payload: clean it and retry (slow path only).
        return json.dumps(_sanitize(msg), default=_json_default, allow_nan=False)


def _byte_views(arrays: Sequence[Any]) -> list[memoryview]:
    """Zero-copy flat byte views over C-contiguous arrays / bytes-like objects."""
    views: list[memoryview] = []
    for arr in arrays:
        if isinstance(arr, (bytes, bytearray, memoryview)):
            view = memoryview(arr).cast("B")
        elif getattr(arr, "size", 1) == 0:
            continue  # nothing to send (and memoryview cannot cast a zero-length shape)
        else:
            view = memoryview(np.ascontiguousarray(arr)).cast("B")
        views.append(view)
    return views


def _iter_chunks(views: Sequence[memoryview], chunk_size: int = CHUNK_SIZE) -> Iterator[Any]:
    """Yield the concatenation of *views* in ``chunk_size`` frames.

    A frame that lies within one view is a zero-copy slice; only frames that
    straddle two arrays are joined (at most ``chunk_size`` bytes each).
    """
    parts: list[memoryview] = []
    filled = 0
    for view in views:
        pos, size = 0, len(view)
        while pos < size:
            take = min(size - pos, chunk_size - filled)
            parts.append(view[pos : pos + take])
            filled += take
            pos += take
            if filled == chunk_size:
                yield parts[0] if len(parts) == 1 else b"".join(parts)
                parts, filled = [], 0
    if filled:
        yield parts[0] if len(parts) == 1 else b"".join(parts)


def _finite_range(values: np.ndarray) -> tuple[float, float] | None:
    """(min, max) of the finite values in one pass, ``None`` if there are none."""
    if values.size == 0:
        return None
    lo = float(np.fmin.reduce(values, axis=None))
    hi = float(np.fmax.reduce(values, axis=None))
    if math.isfinite(lo) and math.isfinite(hi):
        return lo, hi
    # NaN-only (fmin returns NaN) or Inf present: fall back to a masked scan.
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return None
    return float(finite.min()), float(finite.max())


def _pack_field(
    data: Any,
    variable: str,
    *,
    nodata: float = NODATA_SENTINEL,
    rays: rhi.RhiRays | None = None,
) -> tuple[dict[str, Any], list[np.ndarray]]:
    """Prepare a 2-D field for the ``[azimuth][range][values]`` wire format.

    Returns ``(header_fields, [azimuth_f32, range_f32, values_f32])``. Pure CPU
    work — call it from an executor thread.

    For an RHI sweep pass *rays* (:func:`engine.rhi.rhi_rays`): only those rays are sent, in
    ascending elevation, with a trailing ``[elevation f32]`` frame and the header fields
    ``sweep_kind="rhi"``, ``rhi_azimuth`` and ``elevation_bytes``. Without it the payload is
    exactly the PPI one.
    """
    azimuth = (
        np.ascontiguousarray(data.coords["azimuth"].values, dtype=np.float32)
        if "azimuth" in data.coords
        else np.arange(data.shape[0], dtype=np.float32)
    )
    range_m = (
        np.ascontiguousarray(data.coords["range"].values, dtype=np.float32)
        if "range" in data.coords
        else np.arange(data.shape[-1], dtype=np.float32)
    )
    raw = np.asarray(data.values)
    elevation: np.ndarray | None = None
    if rays is not None and raw.ndim == 2 and rays.order.size and rays.order.max() < raw.shape[0]:
        raw = raw[rays.order]
        azimuth = np.ascontiguousarray(rays.azimuth, dtype=np.float32)
        elevation = np.ascontiguousarray(rays.elevation, dtype=np.float32)
    # One copy (also detaches from any cached source array); sentinel fill is in-place.
    values = np.array(raw, dtype=np.float32, order="C", copy=True)
    observed = _finite_range(values)
    np.nan_to_num(values, copy=False, nan=nodata)

    vmin, vmax = _resolve_range(variable)
    if vmin is None:
        vmin = observed[0] if observed else 0.0
    if vmax is None:
        vmax = observed[1] if observed else 1.0

    fields = {
        "n_az": len(azimuth),
        "n_range": len(range_m),
        "azimuth_bytes": azimuth.nbytes,
        "range_bytes": range_m.nbytes,
        "values_bytes": values.nbytes,
        "vmin": float(vmin),
        "vmax": float(vmax),
        "units": str(data.attrs.get("units", data.attrs.get("unit", ""))),
        "max_range": float(range_m.max()) if range_m.size else 0.0,
    }
    arrays = [azimuth, range_m, values]
    if elevation is not None and rays is not None:
        fields.update(
            sweep_kind="rhi",
            rhi_azimuth=float(rays.plane_azimuth),
            elevation_bytes=elevation.nbytes,
        )
        arrays.append(elevation)
    return fields, arrays


def _export_inputs(
    reader: RadarReader,
    datatree: Any,
    *,
    variables: list[str],
    include_derived: Any,
    include_original: Any,
    options: dict[str, Any],
) -> tuple[Any, list[str], dict[str, Any], list[str]]:
    """The data an export should write, with derived (e.g. cleaned) variables added if needed.

    Returns ``(tree, variables, options, derived_names)``. ``variables`` may name derived
    variables (they are mapped to their NetCDF-safe names); ``include_derived`` is ``True`` (all),
    a list of names, or falsy; ``include_original=False`` drops the file's own 2-D moments.
    Raises ``derived.DerivedError`` (a ``ValueError``) with a user-facing message.
    """
    store = derived.store_for(reader, create=False)
    stored = {v["name"]: v for v in store.variables()} if store is not None else {}
    asked = [v for v in variables if v in stored]
    if include_derived is True:
        wanted = list(stored)
    elif isinstance(include_derived, list):
        wanted = [str(n) for n in include_derived]
    else:
        wanted = []
    if include_derived and not stored:
        raise derived.DerivedError(
            "This file has no derived variables yet: run a module with 'All sweeps' first"
        )
    if include_original is False and not (wanted or asked):
        raise derived.DerivedError("Nothing to export: no derived variables selected")
    names = sorted({*wanted, *asked})
    if not names:
        return datatree, variables, options, []
    tree = derived.tree_with_derived(
        datatree, store, names=names, include_original=include_original is not False
    )
    opts = dict(options)
    if asked:  # a derived variable keeps its own colour scale in figures
        meta = stored[asked[0]]
        for key in ("vmin", "vmax"):
            if opts.get(key) is None and isinstance(meta.get(key), (int, float)):
                opts[key] = meta[key]
    # value masks are keyed by the name the viewer uses; the exported tree uses the NetCDF-safe name
    if isinstance(opts.get("value_masks"), dict):
        opts["value_masks"] = {
            (derived.export_name(k) if k in stored else k): m
            for k, m in opts["value_masks"].items()
        }
    if opts.get("value_mask_variable") in stored:
        opts["value_mask_variable"] = derived.export_name(opts["value_mask_variable"])
    return tree, [derived.export_name(v) if v in stored else v for v in variables], opts, names


class ConnectionHandler(EnvironmentMessages, AnimateMessages):
    """Manages message routing for a single WebSocket connection, using shared state."""

    def __init__(
        self,
        ws: ServerConnection,
        session: web.SessionContext | None = None,
        *,
        remote_engine: bool = False,
    ) -> None:
        self.ws = ws
        #: Web mode: this connection's private session (None = desktop mode).
        self.session = session
        #: ``rxd serve`` (engine/serve.py): the desktop engine of a cluster account, used by an app
        #: on another computer through an SSH tunnel. The shared session file there is the cluster
        #: user's own ``rxd login``: the app's token is checked but never written over it.
        self.remote_engine = remote_engine
        # Header + binary chunks of one response must reach the client contiguously
        # (it has a single "pending binary header" slot), so all sends share a lock.
        self._send_lock = asyncio.Lock()
        # Storm-cell tracking state is per connection; the lock keeps
        # identify_cells -> track_cells in arrival order despite concurrent dispatch.
        self._previous_cells: list[Any] | None = None
        self._cells_lock = asyncio.Lock()
        #: The signed-in account this connection presented (None until ``auth_session`` succeeds).
        self.account: account_core.Account | None = None
        self._access_token: str | None = None
        self._why_refused = "no_session"
        self._app_started = False
        #: Cancel flags of the modules and exports this connection is running (``cancel_module``,
        #: ``cancel_export``; engine/jobs.py).
        self._jobs = JobRegistry()

    # ------------------------------------------------------------------
    # Accounts: the gate, auth_session / auth_signout, the activity log
    # ------------------------------------------------------------------

    def _gate_active(self) -> bool:
        """Is the account gate on for this connection? (Off only for the development bypass.)"""
        if account_config.auth_disabled():
            return False
        if self.session is not None:  # the public web service
            return account_config.web_gate_enabled()
        return True

    def _authorised(self) -> bool:
        """True while this connection holds an account that is good right now."""
        account = self.account
        if account is None:
            return False
        if account.bypassed:
            return True
        if (
            self.session is not None or self.remote_engine
        ) and account_core.now() > account.expires_at + account_config.LEEWAY_S:
            # the web service is always online: an access token past its expiry is not enough; the
            # browser renews it (Supabase) and presents the new one with auth_session. A remote
            # engine works the same way: the app that talks to it has its network
            self._why_refused = "expired"
            return False
        # The offline rule (docs/ACCOUNTS.md): 30 days from when the server last vouched.
        if (
            account_core.now() - account.issued_at
            > account_config.OFFLINE_GRACE_DAYS * account_config.DAY_S
        ):
            self._why_refused = "offline_expired"
            return False
        return True

    async def _refuse(self, msg_type: Any, msg: dict[str, Any]) -> None:
        text = AccountRequired(self._why_refused)
        await self.send(
            {
                "type": "auth_required",
                "reason": text.reason,
                "message": str(text),
                "request_type": str(msg_type)[:64],
                "request_id": msg.get("request_id")
                if isinstance(msg.get("request_id"), str)
                else None,
            }
        )

    async def _handle_account_message(self, msg_type: str, msg: dict[str, Any]) -> None:
        loop = asyncio.get_running_loop()
        if msg_type == "auth_status":
            if not self._gate_active():
                await self.send({"type": "auth_ok", "bypass": True})
            elif self._authorised():
                await self.send(self._auth_ok_message())
            else:
                await self._refuse(msg_type, msg)
            return
        if msg_type == "auth_signout":
            if self.account is not None and not self.account.bypassed:
                self._record("sign_out")
                await loop.run_in_executor(_activity_pool(), account_activity.flush, 2.0)
            if self.session is None and not self.remote_engine and self._gate_active():
                # the app signed out: the shared file goes whether or not this connection was ever
                # authorised (a sign-out must not depend on the engine having accepted the account)
                await loop.run_in_executor(None, account_core.end_presented_session)
            self.account = None
            self._access_token = None
            self._why_refused = "no_session"
            await self.send({"type": "auth_signed_out"})
            return
        # auth_session
        if not self._gate_active():
            self.account = account_core.bypass_account()  # nothing to verify: no gate here
            await self.send({"type": "auth_ok", "bypass": True})
            return
        token = msg.get("access_token")
        try:
            if not isinstance(token, str):
                raise AccountRequired("invalid")
            # the desktop engine has one user; the web service many; a remote engine's file is the
            # cluster account's own `rxd login`, which the app must not overwrite
            persist = self.session is None and not self.remote_engine
            account = await loop.run_in_executor(
                None,
                functools.partial(account_core.verify_presented_token, token, persist=persist),
            )
        except AccountRequired as exc:
            self.account = None
            self._access_token = None
            self._why_refused = exc.reason
            await self.send({"type": "auth_required", "reason": exc.reason, "message": str(exc)})
            return
        self.account = account
        self._access_token = token
        self._why_refused = "no_session"
        device = msg.get("device_id")
        if self.session is None and not self.remote_engine and isinstance(device, str):
            from engine.account import store as account_store

            await loop.run_in_executor(None, account_store.set_device_id, device)
        if not self._app_started:
            self._app_started = True
            self._record("app_start")
        if msg.get("reason") == "sign_in":
            self._record("sign_in")
        await self.send(self._auth_ok_message())

    def _auth_ok_message(self) -> dict[str, Any]:
        account = self.account
        if account is None:
            return {"type": "auth_ok"}
        return {
            "type": "auth_ok",
            "email": account.email,
            "stale": account.stale,
            "grace_days_left": account.grace_days_left,
            "bypass": account.bypassed,
        }

    def _record(self, event: str, **detail: Any) -> None:
        """Queue one activity event for this account (never blocks or raises)."""
        account = self.account
        if account is None or account.bypassed or not self._gate_active():
            return
        web_extra: dict[str, Any] = {}
        if (
            self.session is not None
        ):  # the web service: the visitor's own token, kept in memory only
            web_extra = {
                "token": self._access_token,
                "device": re.sub(r"[^A-Za-z0-9]", "", str(self.session.id))[:40] or None,
                "os_name": "Web",
            }
        else:  # desktop: only while the shared session file is still this connection's account
            web_extra = {"uid": account.user_id}
        surface = "engine" if event == "error" else "app"
        with contextlib.suppress(RuntimeError):  # no running loop (shutting down)
            asyncio.get_running_loop().run_in_executor(
                _activity_pool(),
                functools.partial(
                    account_activity.record, event, surface=surface, **web_extra, **detail
                ),
            )

    def _observe_reply(self, msg: dict[str, Any]) -> None:
        """Turn a successful reply into at most one activity event (feature level only)."""
        kind = msg.get("type")
        request = _CURRENT_REQUEST.get() or {}
        if kind == "file_opened":
            data = msg.get("data")
            label = data.get("format") if isinstance(data, dict) else None
            self._record("file_opened", format=account_activity.format_family(label))
        elif kind == "module_result" and isinstance(msg.get("module"), str):
            spec = plugins.MODULES.get(msg["module"])
            self._record(
                "module_run",
                module=account_activity.module_label(msg["module"], getattr(spec, "source", None)),
            )
        elif kind in ("export_complete", "batch_export_complete"):
            self._record("export", format=request.get("format"))
        elif kind == "animation_export_complete":
            self._record("animate_run", type=request.get("format"))
        elif kind == "mosaic_result":
            self._record("mosaic_run", type=request.get("product"))
        elif kind == "multidoppler_result":
            self._record("winds_run", type=request.get("method"))
        elif kind == "qpe_result" and request.get("type") == "qpe_run":
            self._record("qpe_run")
        elif kind == "nowcast_result":
            self._record("nowcast_run")

    async def _send_text(self, msg: dict[str, Any]) -> None:
        """Send one JSON message. Caller must hold ``_send_lock``."""
        try:
            payload = _dumps(msg)
        except (TypeError, ValueError) as exc:
            # Never leave the client hanging: report the failure for this request.
            logger.error("JSON serialization error for '%s': %s", msg.get("type"), exc)
            payload = json.dumps(
                {"type": "error", "message": f"Could not encode '{msg.get('type')}': {exc}"}
            )
        await self.ws.send(payload)

    async def send(self, msg: dict[str, Any]) -> None:
        if self.account is not None:
            self._observe_reply(msg)
        if self.session is not None and isinstance(msg.get("message"), str):
            # never show the server's folder layout to a visitor (progress texts name files too)
            msg = {**msg, "message": self._scrub(msg["message"])}
        try:
            async with self._send_lock:
                await self._send_text(msg)
        except websockets.exceptions.ConnectionClosed:
            logger.warning("Connection closed while sending message")

    async def _send_binary_chunks(
        self,
        arrays: Sequence[Any],
        header: dict[str, Any] | None = None,
        *,
        count_key: str | None = "chunks",
        length_key: str | None = None,
    ) -> None:
        """Send an optional JSON *header* followed by *arrays* as binary frames.

        The arrays are concatenated on the wire and split into ``CHUNK_SIZE``
        frames without copying the whole payload. The header (if any) receives
        the frame count under ``count_key`` and the total byte count under
        ``length_key``. Header and frames are sent atomically with respect to
        other messages on this connection.
        """
        views = _byte_views(arrays)
        total = sum(len(v) for v in views)
        n_chunks = -(-total // CHUNK_SIZE)
        if header is not None:
            header = dict(header)
            if count_key:
                header[count_key] = n_chunks
            if length_key:
                header[length_key] = total
        try:
            async with self._send_lock:
                if header is not None:
                    await self._send_text(header)
                for chunk in _iter_chunks(views):
                    await self.ws.send(chunk)
        except websockets.exceptions.ConnectionClosed:
            logger.warning("Connection closed while sending binary data")

    def _scrub(self, text: str) -> str:
        assert self.session is not None
        for folder in (str(self.session.dir), self.session.real_dir):
            text = text.replace(folder, "<session>")
        return text

    async def send_error(self, message: str) -> None:
        await self.send({"type": "error", "message": message})

    async def send_warning(self, message: str, **extra: Any) -> None:
        """Non-fatal notice (the request still succeeded); the UI shows it as a toast."""
        await self.send({"type": "warning", "message": message, **extra})

    async def _enforce_memory_budget(self, name: str, reader: RadarReader) -> None:
        """After *reader* opened: evict caches if the open data exceeds the budget, else warn.

        Never refuses or crashes: the file stays open, the user is told what to close.
        """
        loop = asyncio.get_running_loop()
        try:
            still_over = await loop.run_in_executor(None, _evict_if_over_budget, 0, reader)
            if not still_over:
                return
            budget = max_open_bytes() or 0
            used = await loop.run_in_executor(None, _open_usage_bytes, reader)
        except Exception as exc:  # accounting must never fail an open
            logger.debug("memory budget check failed: %s", exc)
            return
        mb = 1024 * 1024
        logger.warning(
            "Open data %d MB exceeds budget %d MB after opening %s", used // mb, budget // mb, name
        )
        await self.send_warning(
            f"Opening {name} brings the loaded radar data to {used // mb} MB, above the "
            f"{budget // mb} MB memory budget. Close files you no longer need to avoid "
            "slowdowns or running out of memory (the budget is XRADAR_MAX_OPEN_MB, "
            "default 60% of RAM).",
            code="memory_budget",
            file=name,
            open_mb=used // mb,
            max_mb=budget // mb,
        )

    async def send_progress(self, percent: int, message: str) -> None:
        # Remember (per message task) whether a progress run is still open, so handle_message can
        # close it: many handlers announce "10 %" and then simply finish.
        _PROGRESS_OPEN.set(percent < 100)
        await self.send({"type": "progress", "percent": percent, "message": message})

    def _progress_callback(self, loop: asyncio.AbstractEventLoop) -> Callable[[int, str], None]:
        """Thread-safe progress reporter for work running in an executor."""

        def progress_sync(percent: int, message: str) -> None:
            if loop.is_closed():
                return
            asyncio.run_coroutine_threadsafe(self.send_progress(percent, message), loop)

        return progress_sync

    async def handle_message(self, raw: str) -> None:
        if self.session is None:
            await self._handle_message(raw)
            return
        # Web mode: bind the session for this message (worker threads inherit it, see
        # ``_ContextExecutor``) and point the recipe store at the session folder.
        _ensure_context_executor(asyncio.get_running_loop())
        token = _SESSION.set(self.session)
        recipes_token = pipeline.SESSION_RECIPES_DIR.set(self.session.dir / "recipes")
        try:
            await self._handle_message(raw)
        finally:
            pipeline.SESSION_RECIPES_DIR.reset(recipes_token)
            _SESSION.reset(token)

    async def _send_ppi_only_error(self, msg_type: str, msg: dict[str, Any], text: str) -> None:
        """Refuse a PPI-only request, using the error message type its UI listens for."""
        if msg_type == "run_module":
            await self.send({"type": "module_error", "module": msg.get("module"), "message": text})
        elif msg_type == "create_mosaic":
            await self._mosaic_error(msg.get("request_id"), text)
        elif msg_type == "run_multidoppler":
            await self._multidoppler_error(msg.get("request_id"), text)
        else:
            await self.send_error(text)

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except RecursionError:
            await self.send_error("Invalid JSON: nested too deeply")
            return
        except ValueError as exc:  # JSONDecodeError, or e.g. an integer with thousands of digits
            await self.send_error(f"Invalid JSON: {str(exc)[:200]}")
            return

        if not isinstance(msg, dict):
            await self.send_error("Message must be a JSON object")
            return

        msg_type = msg.get("type")
        if not msg_type:
            await self.send_error("Message missing 'type' field")
            return

        # Web mode has no session token (a browser has none); the session itself is the boundary.
        if (
            EXPECTED_SESSION_TOKEN
            and self.session is None
            and msg.get("token") != EXPECTED_SESSION_TOKEN
        ):
            logger.warning("Unauthorized message type=%s (token mismatch)", msg_type)
            await self.send_error("Unauthorized WebSocket message")
            return

        # The account gate: nothing but ping / get_web_info / auth_* is answered without a signed-in
        # account on this connection (docs/ACCOUNTS.md).
        if msg_type in AUTH_MESSAGES:
            await self._handle_account_message(msg_type, msg)
            return
        if msg_type not in GATE_OPEN_MESSAGES and self._gate_active() and not self._authorised():
            await self._refuse(msg_type, msg)
            return

        table: dict[str, Any] = {
            "open_file": self._handle_open_file,
            "close_file": self._handle_close_file,
            "render": self._handle_render,
            "get_sweep_data": self._handle_get_sweep_data,
            "process": self._handle_process,
            "export": self._handle_export,
            "batch_export": self._handle_batch_export,
            "export_animation": self._handle_export_animation,
            "identify_cells": self._handle_identify_cells,
            "track_cells": self._handle_track_cells,
            "run_retrieval": self._handle_run_retrieval,
            "get_cross_section": self._handle_get_cross_section,
            "get_vertical_profile": self._handle_get_vertical_profile,
            "get_statistics": self._handle_get_statistics,
            "list_retrievals": self._handle_list_retrievals,
            "get_time_series": self._handle_get_time_series,
            "get_difference": self._handle_get_difference,
            "get_accumulation": self._handle_get_accumulation,
            "run_qc": self._handle_run_qc,
            "list_qc_algorithms": self._handle_list_qc_algorithms,
            "get_data_table": self._handle_get_data_table,
            "get_metadata_tree": self._handle_get_metadata_tree,
            "get_variable_mapping": self._handle_get_variable_mapping,
            "set_variable_mapping": self._handle_set_variable_mapping,
            "data_check": self._handle_data_check,
            "ping": self._handle_ping,
            "list_nexrad_stations": self._handle_list_nexrad_stations,
            "list_nexrad_l2_files": self._handle_list_nexrad_l2_files,
            "open_nexrad_l2": self._handle_open_nexrad_l2,
            "open_nexrad_realtime": self._handle_open_nexrad_realtime,
            "get_nexrad_latest_info": self._handle_nexrad_latest_info,
            "live_check": self._handle_live_check,
            "live_update": self._handle_live_update,
            "live_stop": self._handle_live_stop,
            "live_restyle": self._handle_live_restyle,
            "live_open_volumes": self._handle_live_open_volumes,
            "list_arco_scans": self._handle_list_arco_scans,
            "open_arco_scan": self._handle_open_arco_scan,
            "list_network_providers": self._handle_list_network_providers,
            "get_network_latest_info": self._handle_network_latest_info,
            "list_network_scans": self._handle_list_network_scans,
            "open_network_scan": self._handle_open_network_scan,
            "get_scatter_data": self._handle_get_scatter_data,
            "get_histogram": self._handle_get_histogram,
            "get_region_stats": self._handle_get_region_stats,
            "get_volume": self._handle_get_volume,
            "get_cross_section_3d": self._handle_get_cross_section_3d,
            "list_modules": self._handle_list_modules,
            "run_module": self._handle_run_module,
            "cancel_module": self._handle_cancel_module,
            "cancel_export": self._handle_cancel_export,
            "list_derived": self._handle_list_derived,
            "clear_derived": self._handle_clear_derived,
            "list_pipeline_steps": self._handle_list_pipeline_steps,
            "list_recipes": self._handle_list_recipes,
            "save_recipe": self._handle_save_recipe,
            "delete_recipe": self._handle_delete_recipe,
            "export_recipe_script": self._handle_export_recipe_script,
            "preview_pipeline": self._handle_preview_pipeline,
            "run_pipeline": self._handle_run_pipeline,
            "cancel_pipeline": self._handle_cancel_pipeline,
            "save_text_file": self._handle_save_text_file,
            "save_image_file": self._handle_save_image_file,
            "create_mosaic": self._handle_create_mosaic,
            "mosaic_probe": self._handle_mosaic_probe,
            "export_mosaic": self._handle_export_mosaic,
            "get_mosaic_volume": self._handle_get_mosaic_volume,
            "qpe_info": self._handle_qpe,
            "qpe_run": self._handle_qpe,
            "qpe_layer": self._handle_qpe,
            "qpe_adjust": self._handle_qpe,
            "qpe_series": self._handle_qpe,
            "qpe_export": self._handle_qpe,
            "nowcast_run": self._handle_nowcast,
            "nowcast_cancel": self._handle_nowcast,
            "nowcast_layers": self._handle_nowcast,
            "nowcast_export": self._handle_nowcast,
            "get_wind_volume": self._handle_get_wind_volume,
            "run_multidoppler": self._handle_run_multidoppler,
            "multidoppler_cancel": self._handle_multidoppler_cancel,
            "export_multidoppler": self._handle_export_multidoppler,
            "cli_describe": self._handle_cli_describe,
            "cli_run": self._handle_cli_run,
            "cli_cancel": self._handle_cli_cancel,
            "get_web_info": self._handle_get_web_info,
            "auto_inspect": self._handle_autopilot,
            "auto_plan": self._handle_autopilot,
            "auto_run": self._handle_autopilot,
            "auto_cancel": self._handle_autopilot,
            **self._env_handlers(),  # env_* and sounding_open (engine/environment/messages.py)
            **self._animate_handlers(),  # animate_* : the Movie Studio (engine/animate_service.py)
        }
        if self.session is not None:
            table.update(
                upload_start=self._handle_upload_start,
                upload_chunk=self._handle_upload_chunk,
                upload_end=self._handle_upload_end,
                upload_abort=self._handle_upload_abort,
            )
        handler = table.get(msg_type) if isinstance(msg_type, str) else None

        if handler is None:
            await self.send_error(f"Unknown message type: {msg_type}")
            return

        blocked = _ppi_only_block(msg_type, msg)
        if blocked is not None:
            await self._send_ppi_only_error(msg_type, msg, blocked)
            return

        _PROGRESS_OPEN.set(False)
        _CURRENT_REQUEST.set(msg)
        try:
            await handler(msg)
        except (SystemExit, KeyboardInterrupt, GeneratorExit) as exc:
            # Code that runs on a worker thread (a plug-in module, a library) must not be able to
            # stop the whole engine: asyncio re-raises these out of the event loop.
            logger.error("'%s' tried to stop the engine (%s)", msg_type, type(exc).__name__)
            await self.send_error(
                f"Unexpected error in {msg_type}: the operation tried to stop the program "
                f"({type(exc).__name__}). Nothing was changed; try again."
            )
        except JobCancelled:
            await self.send({"type": "export_cancelled", "request": msg_type})
        except Exception as exc:
            self._record("error", **{"class": type(exc).__name__})
            level, text = _failure_text(str(msg_type), exc)
            if level == logging.ERROR:
                logger.error("Error handling '%s': %s\n%s", msg_type, exc, traceback.format_exc())
            else:
                logger.warning("Error in '%s': %s", msg_type, exc)
            await self.send_error(text)
        finally:
            if _PROGRESS_OPEN.get():
                _PROGRESS_OPEN.set(False)
                await self.send_progress(100, "Done")

    # ------------------------------------------------------------------
    # Dual-pol analysis handlers
    # ------------------------------------------------------------------

    async def _handle_get_scatter_data(self, msg: dict[str, Any]) -> None:
        """Return paired arrays for a scatter plot of two variables."""
        var1 = msg.get("var1")
        var2 = msg.get("var2")
        sweep = msg.get("sweep", 0)
        subsample = msg.get("subsample", 5000)
        color_var = msg.get("color_var")

        if not var1 or not isinstance(var1, str):
            await self.send_error("get_scatter_data: missing or invalid 'var1'")
            return
        if not var2 or not isinstance(var2, str):
            await self.send_error("get_scatter_data: missing or invalid 'var2'")
            return
        if not isinstance(sweep, int) or sweep < 0:
            await self.send_error("get_scatter_data: 'sweep' must be a non-negative integer")
            return

        datatree = _get_datatree(msg.get("file_id"))
        if datatree is None:
            await self.send_error("get_scatter_data: no file is open")
            return
        overrides = _overrides_for(msg.get("file_id"))

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: scatter_data(
                datatree,
                var1,
                var2,
                sweep,
                subsample=int(subsample),
                color_var=color_var,
                overrides=overrides,
            ),
        )

        await self.send({"type": "scatter_data_result", "sweep": sweep, **result})

    async def _handle_get_histogram(self, msg: dict[str, Any]) -> None:
        """Compute histogram for a variable."""
        variable = msg.get("variable")
        sweep = msg.get("sweep", 0)
        bins = msg.get("bins", 50)

        if not variable or not isinstance(variable, str):
            await self.send_error("get_histogram: missing or invalid 'variable'")
            return
        if not isinstance(sweep, int) or sweep < 0:
            await self.send_error("get_histogram: 'sweep' must be a non-negative integer")
            return

        datatree = _get_datatree(msg.get("file_id"))
        if datatree is None:
            await self.send_error("get_histogram: no file is open")
            return
        overrides = _overrides_for(msg.get("file_id"))

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: histogram_data(datatree, variable, sweep, bins=bins, overrides=overrides),
        )

        await self.send({"type": "histogram_result", "sweep": sweep, **result})

    async def _handle_get_region_stats(self, msg: dict[str, Any]) -> None:
        """Compute statistics for a polar region."""
        variable = msg.get("variable")
        sweep = msg.get("sweep", 0)
        az_min = msg.get("az_min", 0.0)
        az_max = msg.get("az_max", 360.0)
        range_min = msg.get("range_min", 0.0)
        range_max = msg.get("range_max", 300000.0)

        if not variable or not isinstance(variable, str):
            await self.send_error("get_region_stats: missing or invalid 'variable'")
            return
        if not isinstance(sweep, int) or sweep < 0:
            await self.send_error("get_region_stats: 'sweep' must be a non-negative integer")
            return

        datatree = _get_datatree(msg.get("file_id"))
        if datatree is None:
            await self.send_error("get_region_stats: no file is open")
            return
        overrides = _overrides_for(msg.get("file_id"))

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: region_stats(
                datatree,
                variable,
                sweep,
                az_min=float(az_min),
                az_max=float(az_max),
                range_min=float(range_min),
                range_max=float(range_max),
                overrides=overrides,
            ),
        )

        await self.send({"type": "region_stats_result", "sweep": sweep, **result})

    # ------------------------------------------------------------------
    # 3D volume & cross-section handlers
    # ------------------------------------------------------------------

    async def _handle_get_volume(self, msg: dict[str, Any]) -> None:
        """Extract a 3D Cartesian volume grid from all sweeps.

        Expected message::

            {type: "get_volume", variable: "DBZH",
             box: {x_min: -50000, x_max: 50000, y_min: -50000, y_max: 50000},
             resolution: 100, z_max: 15000, file_id: "..."}

        Response: JSON header followed by chunked binary Float32 data.
        """
        variable = msg.get("variable")
        box = msg.get("box")
        resolution = msg.get("resolution", 100)
        z_max_val = msg.get("z_max", 15000.0)
        file_id = msg.get("file_id")

        if not variable or not isinstance(variable, str):
            await self.send_error("get_volume: missing or invalid 'variable'")
            return
        if not isinstance(box, dict):
            await self.send_error(
                "get_volume: 'box' must be an object with x_min, x_max, y_min, y_max"
            )
            return
        for key in ("x_min", "x_max", "y_min", "y_max"):
            if key not in box or not isinstance(box[key], (int, float)):
                await self.send_error(f"get_volume: 'box.{key}' must be a number")
                return
        if not isinstance(resolution, int) or resolution < 2 or resolution > 500:
            await self.send_error("get_volume: 'resolution' must be an integer between 2 and 500")
            return
        if not isinstance(z_max_val, (int, float)) or z_max_val <= 0:
            await self.send_error("get_volume: 'z_max' must be a positive number")
            return

        reader = _get_reader(file_id)
        if reader.datatree is None:
            await self.send_error("get_volume: no file is open")
            return

        await self.send_progress(10, f"Extracting 3D volume for {variable}...")

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: extract_volume(
                    reader.datatree,
                    variable,
                    box,
                    resolution=resolution,
                    z_max=float(z_max_val),
                    sweep_loader=_loader_for(reader, variable),
                ),
            )
        except ValueError as exc:
            await self.send_error(f"get_volume: {exc}")
            return

        flat_data: np.ndarray = result.pop("data")
        await self._send_binary_chunks(
            [flat_data],
            {
                "type": "volume_data",
                **result,
                "provenance": layer_provenance.volume_3d(**_lp_kw(reader)).to_dict(),
            },
            length_key="byte_length",
        )

    async def _handle_get_cross_section_3d(self, msg: dict[str, Any]) -> None:
        """Extract a vertical curtain cross-section along a Cartesian line.

        Expected message::

            {type: "get_cross_section_3d", variable: "DBZH",
             start: {x: 0, y: 0}, end: {x: 50000, y: 50000},
             width: 2000, file_id: "..."}

        Response: JSON header followed by chunked binary Float32 data.
        """
        variable = msg.get("variable")
        start = msg.get("start")
        end = msg.get("end")
        width = msg.get("width", 2000.0)
        file_id = msg.get("file_id")

        if not variable or not isinstance(variable, str):
            await self.send_error("get_cross_section_3d: missing or invalid 'variable'")
            return
        if not isinstance(start, dict) or "x" not in start or "y" not in start:
            await self.send_error("get_cross_section_3d: 'start' must be {x, y}")
            return
        if not isinstance(end, dict) or "x" not in end or "y" not in end:
            await self.send_error("get_cross_section_3d: 'end' must be {x, y}")
            return
        for pt_name, pt in [("start", start), ("end", end)]:
            for coord in ("x", "y"):
                if not isinstance(pt[coord], (int, float)):
                    await self.send_error(
                        f"get_cross_section_3d: '{pt_name}.{coord}' must be a number"
                    )
                    return
        if not isinstance(width, (int, float)) or width <= 0:
            await self.send_error("get_cross_section_3d: 'width' must be a positive number")
            return

        reader = _get_reader(file_id)
        if reader.datatree is None:
            await self.send_error("get_cross_section_3d: no file is open")
            return

        await self.send_progress(10, f"Extracting 3D cross-section for {variable}...")

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: extract_cross_section_3d(
                    reader.datatree,
                    variable,
                    start,
                    end,
                    width=float(width),
                    sweep_loader=_loader_for(reader, variable),
                ),
            )
        except ValueError as exc:
            await self.send_error(f"get_cross_section_3d: {exc}")
            return

        flat_data: np.ndarray = result.pop("data")
        await self._send_binary_chunks(
            [flat_data],
            {
                "type": "cross_section_3d_data",
                **result,
                "provenance": layer_provenance.cross_section(
                    variable=variable, **_lp_kw(reader)
                ).to_dict(),
            },
            length_key="byte_length",
        )

    # ── Web mode: info, uploads, path/limit helpers ───────────────────────────

    async def _handle_get_web_info(self, msg: dict[str, Any]) -> None:
        """``{type: "get_web_info"}`` -> ``web_info`` (``web_mode: false`` on the desktop)."""
        if self.session is None:
            await self.send({"type": "web_info", "web_mode": False})
        else:
            await self.send(web.web_info())

    async def _upload_error(self, upload_id: Any, exc: BaseException) -> None:
        await self.send(
            {
                "type": "upload_error",
                "upload_id": upload_id if isinstance(upload_id, str) else None,
                "message": str(exc),
            }
        )

    async def _handle_upload_start(self, msg: dict[str, Any]) -> None:
        """``upload_start {upload_id, filename, size}`` -> ``upload_ready`` | ``upload_error``."""
        assert self.session is not None
        upload_id = msg.get("upload_id")
        try:
            self.session.upload_start(upload_id, msg.get("filename"), msg.get("size"))
        except web.WebError as exc:
            await self._upload_error(upload_id, exc)
            return
        await self.send({"type": "upload_ready", "upload_id": upload_id})

    async def _handle_upload_chunk(self, msg: dict[str, Any]) -> None:
        """``upload_chunk {upload_id, seq, data}`` -> ``upload_progress {upload_id, received}``.

        No ``await`` happens before the chunk is stored, so chunks pipelined by the client are
        applied in arrival order even though every message runs as its own task.
        """
        assert self.session is not None
        upload_id = msg.get("upload_id")
        try:
            received = self.session.upload_chunk(upload_id, msg.get("seq"), msg.get("data"))
        except web.WebError as exc:
            await self._upload_error(upload_id, exc)
            return
        await self.send({"type": "upload_progress", "upload_id": upload_id, "received": received})

    async def _handle_upload_end(self, msg: dict[str, Any]) -> None:
        """``upload_end {upload_id}`` -> ``upload_complete {upload_id, path}`` or error"""
        assert self.session is not None
        upload_id = msg.get("upload_id")
        try:
            final = await asyncio.get_running_loop().run_in_executor(
                None, self.session.upload_end, upload_id
            )
        except web.WebError as exc:
            await self._upload_error(upload_id, exc)
            return
        await self.send(
            {
                "type": "upload_complete",
                "upload_id": upload_id,
                "path": str(final),
                "size": final.stat().st_size,
            }
        )

    async def _handle_upload_abort(self, msg: dict[str, Any]) -> None:
        """``upload_abort {upload_id}``: discard a partial upload (no reply)."""
        assert self.session is not None
        upload_id = msg.get("upload_id")
        if isinstance(upload_id, str):
            self.session.upload_abort(upload_id)

    def _web_prepare_reader(self, reader: RadarReader) -> None:
        """Web mode: keep a reader's scratch files in the session folder and cap inflation."""
        if self.session is None:
            return
        reader.tmp_dir = str(self.session.dir)
        reader.max_decompressed_bytes = self.session.settings.max_session_bytes

    def _web_export_target(
        self, requested: Any, fmt: Any, dpi: Any, *, ext: str | None = None
    ) -> Path:
        """Web mode: the session-folder file an export is written to (bare names only)."""
        assert self.session is not None
        fmt_l = str(fmt).lower().strip()
        if fmt_l == "zarr":
            raise web.WebError("Zarr (a folder) cannot be downloaded; choose NetCDF instead")
        if isinstance(dpi, int) and dpi > 300:
            raise web.WebError("'dpi' is limited to 300 in the web service")
        self.session.check_quota()
        ext = ext or _FORMAT_EXT.get(fmt_l, "bin")
        return self.session.export_target(requested, ext=ext)

    def _web_batch_dir(
        self, requested: Any, fmt: Any, dpi: Any, variables: list[Any], sweeps: list[Any]
    ) -> Path:
        """Web mode: a fresh folder under the session's exports for a batch export."""
        assert self.session is not None
        if str(fmt).lower().strip() == "zarr":
            raise web.WebError("Zarr (a folder) cannot be downloaded; choose NetCDF instead")
        if isinstance(dpi, int) and dpi > 300:
            raise web.WebError("'dpi' is limited to 300 in the web service")
        if len(variables) * len(sweeps) > 60:
            raise web.WebError("A batch export can have at most 60 files here")
        self.session.check_quota()
        return self.session.export_subdir(requested)

    def _web_discard(self, path: str | None) -> None:
        """Web mode: remove a half-written export after a failure."""
        if self.session is None or not path:
            return
        with contextlib.suppress(OSError):
            os.remove(path)

    def _web_check_can_open(self) -> None:
        """Web mode: refuse a new file when the per-session open-file limit is reached."""
        if self.session is None:
            return
        limit = self.session.settings.max_open_files
        if len(_readers()) >= limit:
            raise web.WebError(
                f"At most {limit} files can be open at once in the web service; close one first"
            )

    async def _web_enforce_budget(self, reader: RadarReader) -> None:
        """Web mode: refuse (instead of merely warning about) a file beyond the memory budget."""
        if self.session is None:
            return
        loop = asyncio.get_running_loop()
        over = await loop.run_in_executor(None, _evict_if_over_budget, 0, reader)
        if over:
            budget = (max_open_bytes() or 0) // (1024 * 1024)
            used = await loop.run_in_executor(None, _open_usage_bytes, reader) // (1024 * 1024)
            with contextlib.suppress(Exception):
                reader.close()
            raise web.WebError(
                f"This file needs about {used} MB in memory, over the {budget} MB per-session "
                "limit of this service; close other files or use a smaller file"
            )

    def _web_module_params(self, module_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Web mode: file-valued module parameters may only name files inside the session."""
        if self.session is None:
            return params
        checked = dict(params)
        for key, value in params.items():
            if not re.search(r"(?:^|_)(?:path|file|dir|folder)s?$", str(key)):
                continue
            if value in (None, ""):
                continue
            if key == "model_path":  # an ML model is code-like input; only the bundled one runs
                raise plugins.ModuleError("Custom model files are not available in the web service")
            try:
                checked[key] = self.session.resolve_input_path(value)
            except web.WebError as exc:
                raise plugins.ModuleError(f"{module_id}: parameter '{key}': {exc}") from exc
        return checked

    async def _handle_ping(self, msg: dict[str, Any]) -> None:
        """Health check handler — responds with pong and server status."""
        try:
            has_file = _get_datatree(msg.get("file_id")) is not None
        except UnknownFileError:
            has_file = False
        await self.send(
            {
                "type": "pong",
                "status": "ok",
                "file_open": has_file,
                "memory": memory_status(),
                # Differs after the engine process was restarted (crash, kill, update): the UI
                # then knows its file ids are gone instead of failing on each request.
                "engine_id": ENGINE_ID,
            }
        )

    # ── Cloud / AWS handlers ──────────────────────────────────────────────────

    async def _handle_list_nexrad_stations(self, msg: dict[str, Any]) -> None:
        loop = asyncio.get_running_loop()
        try:
            stations = await loop.run_in_executor(None, list_nexrad_stations)
            await self.send({"type": "nexrad_stations", "stations": stations})
        except Exception as exc:
            await self.send_error(f"list_nexrad_stations failed: {exc}")

    async def _handle_list_nexrad_l2_files(self, msg: dict[str, Any]) -> None:
        station = msg.get("station", "")
        date = msg.get("date", "")
        if not station or not date:
            await self.send_error("list_nexrad_l2_files: 'station' and 'date' required")
            return
        try:
            files = await _cloud_call(list_nexrad_l2_files, station, date)
            await self.send(
                {"type": "nexrad_l2_files", "station": station, "date": date, "files": files}
            )
        except TimeoutError:
            await self.send_error(f"list_nexrad_l2_files failed: {_cloud_timeout_text()}")
        except Exception as exc:
            await self.send_error(f"list_nexrad_l2_files failed: {exc}")

    async def _open_with_new_reader(
        self, opener: Callable[[RadarReader], dict[str, Any]], *, timeout: float | None = None
    ) -> None:
        """Open a source in a brand-new reader and announce it as a new file_id.

        Every open gets its own :class:`RadarReader`, so previously opened files
        keep their data and ``close_file`` on one never affects another.
        """
        loop = asyncio.get_running_loop()
        self._web_check_can_open()
        reader = RadarReader()
        self._web_prepare_reader(reader)
        job = loop.run_in_executor(None, opener, reader)
        limit = timeout or cloud_timeout_s()
        schema = await (asyncio.wait_for(job, limit) if limit else job)
        await self._web_enforce_budget(reader)
        await self._enforce_memory_budget(
            str(schema.get("source") or "cloud file").rsplit("/", 1)[-1], reader
        )
        file_id = _register_reader(reader)
        await self.send_progress(100, "Loaded")
        schema_serial = {k: _serializable(v) for k, v in schema.items()}
        prov = await loop.run_in_executor(None, layer_provenance.describe_reader, reader, schema)
        await self.send(
            {"type": "file_opened", "data": schema_serial, "file_id": file_id, "provenance": prov}
        )

    async def _handle_open_nexrad_l2(self, msg: dict[str, Any]) -> None:
        s3_path = msg.get("path", "")
        if not s3_path or not isinstance(s3_path, str):
            await self.send_error("open_nexrad_l2: 'path' required")
            return
        if self.session is not None:  # no user-supplied buckets or keys in a public service
            try:
                s3_path = web.check_s3_path(s3_path)
            except web.WebError as exc:
                await self.send_error(f"open_nexrad_l2 failed: {exc}")
                return
        await self.send_progress(5, "Connecting to S3...")
        try:
            await self.send_progress(15, f"Downloading {s3_path.split('/')[-1]}...")
            await self._open_with_new_reader(lambda r: r.open_s3_file(s3_path))
        except TimeoutError:
            await self.send_error(f"open_nexrad_l2 failed: {_cloud_timeout_text()}")
        except Exception as exc:
            await self.send_error(f"open_nexrad_l2 failed: {exc}")

    async def _handle_nexrad_latest_info(self, msg: dict[str, Any]) -> None:
        """Radar-sites card: age/size/name of a station's newest Level II volume (one S3 listing,
        metadata only, nothing is downloaded).  Always answers with ``nexrad_latest_info``;
        ``status`` says ok / not_found / offline / error."""
        station = msg.get("station", "")
        if not isinstance(station, str) or not station:
            await self.send_error("get_nexrad_latest_info: 'station' required")
            return
        try:
            info = await _cloud_call(latest_scan_info, station)
        except TimeoutError:
            info = {"station": station, "status": "offline", "message": _cloud_timeout_text()}
        await self.send({"type": "nexrad_latest_info", **info})

    # ── Live map (engine/live.py): newest volume per default radar, one composite ──────────

    async def _live_error(self, request_id: Any, message: str) -> None:
        await self.send({"type": "live_error", "request_id": request_id, "message": message})

    async def _handle_live_check(self, msg: dict[str, Any]) -> None:
        """``{type: "live_check", request_id, stations: [...]}`` -> ``live_check_result`` with one
        :func:`engine.cloud.latest_scan_info` entry per station (listing only, no download) and
        whether the live cache already holds that volume. A bad request is a ``live_error``."""
        from engine import live

        request_id = msg.get("request_id")
        if self.session is not None:
            await self._live_error(request_id, "The live map is a feature of the desktop app.")
            return
        try:
            stations = live.normalize_stations(msg.get("stations"))
        except (live.LiveError, ValueError) as exc:
            await self._live_error(request_id, f"live_check: {exc}")
            return
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, live.check, stations)
        await self.send(
            {"type": "live_check_result", "request_id": request_id, "stations": results}
        )

    async def _handle_live_update(self, msg: dict[str, Any]) -> None:
        """``{type: "live_update", request_id, files: [{station, path, name, size, scan_time}],
        variable?, product?, grid?, merge?}`` -> progress, then a ``live_frame`` header (a mosaic
        header plus ``stations`` and ``provenance``) followed by the PNG frames. Only the named
        files are downloaded; a station already held is not fetched again. Failure: ``live_error``;
        progress always closes at 100."""
        from engine import live, mosaic

        request_id = msg.get("request_id")
        if self.session is not None:
            await self._live_error(request_id, "The live map is a feature of the desktop app.")
            return
        for key in ("grid", "merge"):
            if msg.get(key) is not None and not isinstance(msg[key], dict):
                await self._live_error(request_id, f"live_update: '{key}' must be an object")
                return
        try:
            floor = (
                live.clamp_floor(msg["floor_dbz"]) if "floor_dbz" in msg else live.DEFAULT_FLOOR_DBZ
            )
        except live.LiveError as exc:
            await self._live_error(request_id, f"live_update: {exc}")
            return
        loop = asyncio.get_running_loop()
        progress_sync = self._progress_callback(loop)
        await self.send_progress(3, "Live map: starting update")
        try:
            mosaic._radarx()  # fail before any download when radarx is missing

            def _build():
                return live.update(
                    msg.get("files"),
                    variable=str(msg.get("variable") or "DBZH"),
                    product=str(msg.get("product") or "composite"),
                    grid=msg.get("grid"),
                    merge=msg.get("merge"),
                    floor_dbz=floor,
                    progress=progress_sync,
                )

            t0 = time.perf_counter()
            # Downloads and the merge share the mosaic lock: one heavy build at a time.
            record, radars = await _run_holding(_locks.get("mosaic"), _build)
            header = {
                "type": "live_frame",
                "request_id": request_id,
                **live.frame_header(record, radars),
                "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            }
            await self._send_binary_chunks([record.png], header, length_key="png_bytes")
        except (live.LiveError, mosaic.MosaicError, plugins.ModuleError) as exc:
            logger.warning("Live update failed: %s", exc)
            await self._live_error(request_id, str(exc))
        except ImportError as exc:
            await self._live_error(
                request_id, f"The live composite needs the Python package radarx: {exc}"
            )
        except Exception as exc:
            logger.error("Live update failed: %s\n%s", exc, traceback.format_exc())
            await self._live_error(request_id, f"{type(exc).__name__}: {exc}")
        finally:
            await self.send_progress(100, "Live map: update finished")

    async def _handle_live_restyle(self, msg: dict[str, Any]) -> None:
        """``{type: "live_restyle", request_id, mosaic_id, floor_dbz}`` -> a ``live_frame`` with the
        same composite drawn with another display floor (``null`` draws every echo). No download and
        no new merge; the composite must still be in the engine's cache, else ``live_error``."""
        from engine import live, mosaic

        request_id = msg.get("request_id")
        mosaic_id = msg.get("mosaic_id")
        if self.session is not None:
            await self._live_error(request_id, "The live map is a feature of the desktop app.")
            return
        if not isinstance(mosaic_id, str) or not mosaic_id:
            await self._live_error(request_id, "live_restyle: missing 'mosaic_id'")
            return
        try:
            floor = live.clamp_floor(msg.get("floor_dbz"))
            loop = asyncio.get_running_loop()
            record = await loop.run_in_executor(None, live.restyle, mosaic_id, floor)
            header = {
                "type": "live_frame",
                "request_id": request_id,
                "restyled": True,
                **record.header(),
                "display": record.display,
            }
            await self._send_binary_chunks([record.png], header, length_key="png_bytes")
        except (live.LiveError, mosaic.MosaicError) as exc:
            await self._live_error(request_id, str(exc))
        except Exception as exc:
            logger.error("Live restyle failed: %s\n%s", exc, traceback.format_exc())
            await self._live_error(request_id, f"{type(exc).__name__}: {exc}")

    async def _handle_live_open_volumes(self, msg: dict[str, Any]) -> None:
        """``{type: "live_open_volumes", request_id, stations: [...]}``: open the volumes the live
        map holds as normal files (each announced by a ``file_opened``), without a new download.

        Every volume is pinned first (its file is kept until the opened file is closed, so the next
        live update cannot take it away), then opened in its own reader. Answer:
        ``live_volumes_opened`` with the file ids, the sizes and the disk the pinned files use;
        a problem (nothing held, too many open) is a ``live_error``."""
        from engine import live

        request_id = msg.get("request_id")
        if self.session is not None:
            await self._live_error(request_id, "The live map is a feature of the desktop app.")
            return
        loop = asyncio.get_running_loop()
        try:
            stations = live.normalize_stations(msg.get("stations"))
            pins = await loop.run_in_executor(None, live.pin_volumes, stations)
        except (live.LiveError, ValueError) as exc:
            await self._live_error(request_id, f"live_open_volumes: {exc}")
            return
        opened: list[dict[str, Any]] = []
        failed: list[str] = []
        known_ids = msg.get("known_file_ids")
        client_known = (
            {k for k in known_ids if isinstance(k, str)} if isinstance(known_ids, list) else set()
        )
        for i, pin in enumerate(pins):
            existing = _readers().get(pin.file_id) if pin.file_id else None
            if existing is not None and pin.file_id in client_known:
                # open, and the app already has it as a tab: nothing to announce
                opened.append({**pin.as_dict(), "already_open": True})
                continue
            if existing is not None:
                # open in the engine but unknown to this page (reloaded): announce it again
                schema = await loop.run_in_executor(None, existing.get_schema)
                source_type = (
                    "nexrad_l2_s3" if pin.provider == "nexrad" else f"{pin.provider}_opendata"
                )
                schema = {**schema, "source": pin.source, "source_type": source_type}
                prov = await loop.run_in_executor(
                    None, layer_provenance.describe_reader, existing, schema
                )
                await self.send(
                    {
                        "type": "file_opened",
                        "data": {k: _serializable(v) for k, v in schema.items()},
                        "file_id": pin.file_id,
                        "provenance": prov,
                    }
                )
                opened.append({**pin.as_dict(), "already_open": True})
                continue
            await self.send_progress(
                int(100 * i / max(len(pins), 1)), f"Opening {pin.name} ({i + 1}/{len(pins)})"
            )
            try:
                reader, schema = await loop.run_in_executor(None, live.open_pinned, pin)
                await self._enforce_memory_budget(pin.name, reader)
                file_id = _register_reader(reader)
            except Exception as exc:
                logger.warning("live_open_volumes: %s failed: %s", pin.name, exc)
                live.PINS.release(pin.key)
                failed.append(f"{pin.station} ({exc})")
                continue
            live.PINS.bind(pin.key, file_id)
            schema_serial = {k: _serializable(v) for k, v in schema.items()}
            prov = await loop.run_in_executor(
                None, layer_provenance.describe_reader, reader, schema
            )
            await self.send(
                {
                    "type": "file_opened",
                    "data": schema_serial,
                    "file_id": file_id,
                    "provenance": prov,
                }
            )
            opened.append({**pin.as_dict(), "file_id": file_id, "already_open": False})
        await self.send_progress(100, "Opened")
        if not opened:
            await self._live_error(
                request_id,
                "live_open_volumes: no volume could be opened (" + "; ".join(failed) + ")",
            )
            return
        await self.send(
            {
                "type": "live_volumes_opened",
                "request_id": request_id,
                "files": opened,
                "failed": failed,
                "pinned": live.PINS.status(),
            }
        )

    async def _handle_live_stop(self, msg: dict[str, Any]) -> None:
        """Release every volume the live map holds (the app stopped the live map or is closing)."""
        from engine import live

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, live.LIVE.clear)
        await self.send({"type": "live_stopped", "request_id": msg.get("request_id")})

    async def _handle_open_nexrad_realtime(self, msg: dict[str, Any]) -> None:
        station = msg.get("station", "")
        if not station or not isinstance(station, str):
            await self.send_error("open_nexrad_realtime: 'station' required")
            return
        await self.send_progress(5, f"Finding latest scan for {station}...")
        try:
            latest = await _cloud_call(get_latest_nexrad_l2, station)
            await self.send_progress(15, f"Downloading {latest['name']}...")
            await self._open_with_new_reader(lambda r: r.open_s3_file(latest["path"]))
        except TimeoutError:
            await self.send_error(f"open_nexrad_realtime failed: {_cloud_timeout_text()}")
        except Exception as exc:
            await self.send_error(f"open_nexrad_realtime failed: {exc}")

    async def _handle_list_arco_scans(self, msg: dict[str, Any]) -> None:
        station = msg.get("station", "KLOT")
        limit = int(msg.get("limit", 50))
        loop = asyncio.get_running_loop()
        try:
            scans = await loop.run_in_executor(None, list_arco_scans, station, limit)
            await self.send({"type": "arco_scans", "station": station, "scans": scans})
        except Exception as exc:
            await self.send_error(f"list_arco_scans failed: {exc}")

    async def _handle_open_arco_scan(self, msg: dict[str, Any]) -> None:
        station = msg.get("station", "")
        scan_key = msg.get("scan_key", "")
        if not station or not scan_key:
            await self.send_error("open_arco_scan: 'station' and 'scan_key' required")
            return
        if self.session is not None and not (
            isinstance(station, str)
            and isinstance(scan_key, str)
            and re.fullmatch(r"[A-Za-z0-9]{4}", station)
            and re.fullmatch(r"[A-Za-z0-9_.\-]{1,40}", scan_key)
            and ".." not in scan_key
        ):
            await self.send_error("open_arco_scan failed: invalid station or scan key")
            return
        await self.send_progress(5, "Connecting to ARCO store (first open ~30s, cached after)…")
        try:
            await self._open_with_new_reader(
                lambda r: r.open_arco_scan(station, scan_key), timeout=120.0
            )
        except TimeoutError:
            await self.send_error(
                "open_arco_scan timed out after 120s — the ARCO store may be slow or unavailable"
            )
        except Exception as exc:
            await self.send_error(f"open_arco_scan failed: {exc}")

    # ── Open radar networks beyond NEXRAD (engine.radar_networks) ────────────

    async def _handle_list_network_providers(self, msg: dict[str, Any]) -> None:
        """The providers of other open radar networks: ids, sites, credit, licence, what a
        listing costs (so the UI can ask for consent with the real numbers)."""
        from engine import radar_networks

        await self.send(
            {"type": "network_providers", "providers": radar_networks.describe_providers()}
        )

    async def _handle_network_latest_info(self, msg: dict[str, Any]) -> None:
        """Newest volume of a site of a network provider: one listing, nothing downloaded.
        Always answers ``network_latest_info`` with a ``status``."""
        from engine import radar_networks

        provider, site = msg.get("provider", ""), msg.get("site", "")
        if not isinstance(provider, str) or not isinstance(site, str) or not provider or not site:
            await self.send_error("get_network_latest_info: 'provider' and 'site' required")
            return
        try:
            p = radar_networks.get_provider(provider)
        except ValueError as exc:
            await self.send(
                {
                    "type": "network_latest_info",
                    "status": "error",
                    "provider": provider,
                    "site": site,
                    "message": str(exc),
                }
            )
            return
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, p.latest_scan_info, site)
        await self.send({"type": "network_latest_info", **info})

    async def _handle_list_network_scans(self, msg: dict[str, Any]) -> None:
        from engine import radar_networks

        provider, site, date = msg.get("provider", ""), msg.get("site", ""), msg.get("date", "")
        if not all(isinstance(v, str) and v for v in (provider, site, date)):
            await self.send_error("list_network_scans: 'provider', 'site' and 'date' required")
            return
        loop = asyncio.get_running_loop()
        try:
            p = radar_networks.get_provider(provider)
            scans = await loop.run_in_executor(None, p.list_scans, site, date)
            await self.send(
                {
                    "type": "network_scans",
                    "provider": p.id,
                    "site": site,
                    "date": date,
                    "scans": scans,
                }
            )
        except Exception as exc:
            await self.send_error(f"list_network_scans failed: {exc}")

    async def _handle_open_network_scan(self, msg: dict[str, Any]) -> None:
        """Download one listed volume of a network provider and open it (the UI has shown the
        consent dialog with the provider's host and the volume's size before sending this)."""
        from engine import radar_networks

        provider, site, key = msg.get("provider", ""), msg.get("site", ""), msg.get("key", "")
        if not all(isinstance(v, str) and v for v in (provider, site, key)):
            await self.send_error("open_network_scan: 'provider', 'site' and 'key' required")
            return
        if len(key) > 200 or ".." in key:
            await self.send_error("open_network_scan failed: invalid key")
            return
        try:
            p = radar_networks.get_provider(provider)
        except ValueError as exc:
            await self.send_error(f"open_network_scan failed: {exc}")
            return
        await self.send_progress(5, f"Listing {p.network} {site}...")
        try:
            await self.send_progress(15, f"Downloading {p.network} {site} {key}...")
            await self._open_with_new_reader(lambda r: p.open(r, site, key), timeout=300.0)
        except TimeoutError:
            await self.send_error("open_network_scan failed: timed out after 300 s")
        except Exception as exc:
            await self.send_error(f"open_network_scan failed: {exc}")

    async def _handle_open_file(self, msg: dict[str, Any]) -> None:
        path = msg.get("path")
        if not path:
            await self.send_error("open_file: missing 'path'")
            return
        if not isinstance(path, str):
            await self.send_error("open_file: 'path' must be a string")
            return

        if self.session is not None:
            # Web mode: only files this session uploaded (or downloaded from the cloud tab).
            try:
                path = self.session.resolve_input_path(path)
                self._web_check_can_open()
            except web.WebError as exc:
                await self.send_error(f"open_file failed: {exc}")
                return

        filename = Path(path).name
        await self.send_progress(5, f"Detecting format for {filename}...")

        reader = RadarReader()
        self._web_prepare_reader(reader)
        loop = asyncio.get_running_loop()

        # Free re-computable caches before a big read if the budget is already tight.
        try:
            incoming_hint = os.path.getsize(path)
        except OSError:
            incoming_hint = 0
        await loop.run_in_executor(None, _evict_if_over_budget, incoming_hint)

        await self.send_progress(15, f"Opening {filename}...")
        try:
            schema = await loop.run_in_executor(None, reader.open_file, path)
        except Exception as exc:
            # Prefixed "open_file failed:" so the UI attributes it to this open (see
            # App.svelte isOpenFileError), and specific enough to act on.
            logger.warning("open_file %s failed: %s", path, exc)
            await self.send_error(f"open_file failed: {_describe_open_error(path, exc)}")
            return

        await self.send_progress(80, f"Building metadata for {filename}...")

        try:
            await self._web_enforce_budget(reader)
        except web.WebError as exc:
            await self.send_error(f"open_file failed: {exc}")
            return
        await self._enforce_memory_budget(filename, reader)

        # No renderer-cache flush needed: cache entries are keyed by (file_id, generation).
        file_id = _register_reader(reader)

        await self.send_progress(100, f"Loaded {filename}")
        prov = await loop.run_in_executor(None, layer_provenance.describe_reader, reader, schema)
        await self.send(
            {"type": "file_opened", "data": schema, "file_id": file_id, "provenance": prov}
        )

        # Warm the reader's lazy sweep data for the most likely first request.
        if schema.get("variables") and schema.get("sweeps"):
            _spawn(
                self._prefetch(reader, schema["sweeps"][0].get("index", 0), schema["variables"][0])
            )

    async def _handle_close_file(self, msg: dict[str, Any]) -> None:
        """Remove a file from the multi-file reader dict."""
        file_id = msg.get("file_id")
        if not file_id or not isinstance(file_id, str):
            await self.send_error("close_file: missing or invalid 'file_id'")
            return

        removed = _readers().pop(file_id, None)
        _generations_map().pop(file_id, None)
        _overrides_store().drop(file_id)
        if removed is None:
            logger.debug("close_file: file_id %s not found (already closed?)", file_id)
        else:
            # A volume opened from the live map owns a pinned file on disk: closing releases it.
            from engine import live

            if live.release_reader(removed):
                logger.info("Released the live volume file of %s", file_id)
            # Free the decompressed copy of a .gz now: waiting for the garbage collector to find
            # the reader leaves up to gigabytes on disk for as long as anything still points at it
            # (a finishing prefetch, a cache entry, a traceback).
            await asyncio.get_running_loop().run_in_executor(None, removed.close)
            # Entries are token-keyed so they could never be served again; free the memory.
            _renderer().invalidate_cache()
            store = derived.store_for(removed, create=False)
            if store is not None:
                store.drop()

        # If we closed the active file, fall back to the most recently opened remaining one.
        if _get_last_file_id() == file_id:
            _set_last_file_id(next(reversed(_readers()), None))

        await self.send({"type": "file_closed", "file_id": file_id})
        logger.info("Closed file %s (%d files still open)", file_id, len(_readers()))

    async def _prefetch(self, reader: RadarReader, sweep: int, variable: str) -> None:
        """Load a sweep's values in the background so the first get_sweep_data is instant.

        xarray's lazily-indexed arrays memoise on first ``.values``, and the reader
        hands out views of the same variables, so this warms the reader itself.
        """
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: reader.get_sweep_data(sweep, variable).values)
            logger.info("Prefetched %s sweep %d", variable, sweep)
        except Exception:
            logger.debug("Prefetch failed (non-fatal)", exc_info=True)

    async def _handle_render(self, msg: dict[str, Any]) -> None:
        variable = msg.get("variable")
        sweep = msg.get("sweep", 0)
        width = msg.get("width", 800)
        height = msg.get("height", 600)
        bbox = msg.get("bbox")
        file_id = msg.get("file_id")

        if not variable:
            await self.send_error("render: missing 'variable'")
            return
        if not isinstance(variable, str):
            await self.send_error("render: 'variable' must be a string")
            return
        if not isinstance(sweep, int) or sweep < 0:
            await self.send_error("render: 'sweep' must be a non-negative integer")
            return
        if not isinstance(width, int) or width <= 0 or width > 10000:
            await self.send_error("render: 'width' must be an integer between 1 and 10000")
            return
        if not isinstance(height, int) or height <= 0 or height > 10000:
            await self.send_error("render: 'height' must be an integer between 1 and 10000")
            return
        if self.session is not None and (width > 4096 or height > 4096):
            await self.send_error("render: 'width' and 'height' are limited to 4096 here")
            return
        if bbox is not None:
            if not isinstance(bbox, list) or len(bbox) != 4:
                await self.send_error("render: 'bbox' must be a list of 4 numbers")
                return
            if not all(isinstance(v, (int, float)) for v in bbox):
                await self.send_error("render: 'bbox' values must be numbers")
                return

        resolved_id, reader = _resolve_reader(file_id)
        # Snapshot the token *before* loading so a concurrent `process` can only
        # make us cache under the older generation, never under the newer one.
        token = _cache_token(resolved_id)

        await self.send_progress(10, f"Rendering {variable} sweep {sweep}...")

        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, reader.get_sweep_data, sweep, variable)

        def _render() -> dict[str, Any]:
            if _RENDER_ACCEPTS_TOKEN:
                return _renderer().render_sweep(
                    data, variable, sweep, width, height, bbox, cache_token=token
                )
            return _renderer().render_sweep(data, variable, sweep, width, height, bbox)

        async with _render_lock():
            result = await loop.run_in_executor(None, _render)

        await self.send({"type": "render_result", **result})

    async def _handle_get_sweep_data(self, msg: dict[str, Any]) -> None:
        """Send raw sweep data as binary for client-side WebGL rendering.

        The response is a JSON header followed by binary Float32Arrays:
        {type: "sweep_data", variable, sweep, n_az, n_range,
         azimuth_bytes, range_bytes, values_bytes, vmin, vmax, units,
         max_range, chunks}
        Then ``chunks`` binary messages whose concatenation is
        [azimuth f32] [range f32] [values f32].

        Optional ``file_id`` selects which open file to read from (defaults
        to the most recently opened file for backward compatibility).
        """

        variable = msg.get("variable")
        sweep = msg.get("sweep", 0)
        file_id = msg.get("file_id")

        if not variable or not isinstance(variable, str):
            await self.send_error("get_sweep_data: missing or invalid 'variable'")
            return
        if not isinstance(sweep, int) or sweep < 0:
            await self.send_error("get_sweep_data: 'sweep' must be a non-negative integer")
            return

        resolved_id, reader = _resolve_reader(file_id)
        logger.info("get_sweep_data: variable=%s sweep=%d file_id=%s", variable, sweep, file_id)

        loop = asyncio.get_running_loop()

        def _load_and_pack() -> tuple[dict[str, Any], list[np.ndarray]]:
            served = _derived_sweep_data(reader, sweep, variable)
            if served is not None:
                return served
            return _pack_field(
                reader.get_sweep_data(sweep, variable), variable, rays=_rhi_rays_of(reader, sweep)
            )

        fields, arrays = await loop.run_in_executor(None, _load_and_pack)
        prov = fields.pop("provenance", None) or getattr(reader, "_layer_prov", None)
        if prov:
            fields["provenance"] = prov
        # file_id: replies arrive in any order, and two open files can both be asked for the
        # same variable and sweep -- the UI needs to know whose data this is.
        await self._send_binary_chunks(
            arrays,
            {
                "type": "sweep_data",
                "variable": variable,
                "sweep": sweep,
                "file_id": resolved_id,
                **fields,
            },
        )

    async def _handle_process(self, msg: dict[str, Any]) -> None:
        pipeline = msg.get("pipeline", {})
        file_id = msg.get("file_id")
        if not isinstance(pipeline, dict):
            await self.send_error("process: 'pipeline' must be an object")
            return

        resolved_id, reader = _resolve_reader(file_id)
        datatree = reader.datatree
        if datatree is None:
            await self.send_error("process: no file is open")
            return

        loop = asyncio.get_running_loop()
        progress_sync = self._progress_callback(loop)

        result_tree = await _run_holding(
            _mutation_lock(), _processor().process, datatree, pipeline, progress_sync
        )

        # Swap the data and bump the generation together (no await in between) so
        # no render can pair the new generation with the old tree or vice versa.
        reader._dtree = result_tree
        generations = _generations_map()
        if resolved_id is not None and resolved_id in generations:
            generations[resolved_id] += 1
        # Derived variables were computed from the old tree: never serve them again.
        stale = derived.store_for(reader, create=False)
        dropped = stale.drop() if stale is not None else []
        if not _RENDER_ACCEPTS_TOKEN:
            _renderer().invalidate_cache()

        if dropped:
            await self._send_derived_list(resolved_id, reader, dropped=dropped)
        await self.send_progress(100, "Processing complete")

    async def _handle_export(self, msg: dict[str, Any]) -> None:
        fmt = msg.get("format", "png")
        dpi = msg.get("dpi", 300)
        output_path = _blank_to_none(msg.get("path"))
        variable = msg.get("variable")
        sweep = msg.get("sweep", 0)
        options = msg.get("options", {})
        file_id = msg.get("file_id")

        if not isinstance(fmt, str):
            await self.send_error("export: 'format' must be a string")
            return
        if not isinstance(dpi, int) or dpi <= 0 or dpi > 600:
            await self.send_error("export: 'dpi' must be an integer between 1 and 600")
            return
        if not isinstance(sweep, int) or sweep < 0:
            await self.send_error("export: 'sweep' must be a non-negative integer")
            return
        if variable is not None and not isinstance(variable, str):
            await self.send_error("export: 'variable' must be a string")
            return
        if output_path is not None and not isinstance(output_path, str):
            await self.send_error("export: 'path' must be a string")
            return
        if not isinstance(options, dict):
            options = {}

        datatree = _get_datatree(file_id)
        if datatree is None:
            await self.send_error("export: no file is open")
            return

        if self.session is not None:
            try:
                output_path = str(self._web_export_target(output_path, fmt, dpi))
            except web.WebError as exc:
                await self.send_error(f"export: {exc}")
                return

        await self.send_progress(10, f"Exporting as {fmt}...")

        loop = asyncio.get_running_loop()

        export_cancel = self._jobs.start("export", file_id if isinstance(file_id, str) else None)
        progress_sync = cancel_on_progress(self._progress_callback(loop), export_cancel)
        reader = _get_reader(file_id)
        include_derived = msg.get("include_derived")
        include_original = msg.get("include_original", True)

        def _do_export() -> tuple[str, list[str]]:
            tree, variables, opts, names = _export_inputs(
                reader,
                datatree,
                variables=[variable] if variable else [],
                include_derived=include_derived,
                include_original=include_original,
                options=options,
            )
            path = _exporter().export(
                tree,
                fmt=fmt,
                dpi=dpi,
                output_path=output_path,
                variable=variables[0] if variables else None,
                sweep=sweep,
                progress=progress_sync,
                options=opts,
            )
            return path, names

        try:
            saved_path, derived_names = await _run_holding(_mutation_lock(), _do_export)
        except BaseException:
            self._web_discard(output_path)
            raise
        finally:
            self._jobs.finish("export", export_cancel)

        result_path: dict[str, Any] = {"path": saved_path}
        if self.session is not None:
            try:
                self.session.check_export_quota([saved_path])
                result_path = self.session.download_info(saved_path)
            except web.WebError as exc:
                await self.send_error(f"export: {exc}")
                return
        await self.send(
            {"type": "export_complete", **result_path, "derived_exported": derived_names}
        )

    async def _handle_batch_export(self, msg: dict[str, Any]) -> None:
        variables = msg.get("variables", [])
        sweeps = msg.get("sweeps", [])
        fmt = msg.get("format", "png")
        dpi = msg.get("dpi", 300)
        output_dir = _blank_to_none(msg.get("output_dir"))
        options = msg.get("options", {})
        file_id = msg.get("file_id")

        if not isinstance(variables, list) or not all(isinstance(v, str) for v in variables):
            await self.send_error("batch_export: 'variables' must be a list of strings")
            return
        if not isinstance(sweeps, list) or not all(isinstance(s, int) for s in sweeps):
            await self.send_error("batch_export: 'sweeps' must be a list of integers")
            return
        if not isinstance(fmt, str):
            await self.send_error("batch_export: 'format' must be a string")
            return
        if not isinstance(dpi, int) or dpi <= 0 or dpi > 600:
            await self.send_error("batch_export: 'dpi' must be between 1 and 600")
            return
        if output_dir is not None and not isinstance(output_dir, str):
            await self.send_error("batch_export: 'output_dir' must be a string")
            return
        if not isinstance(options, dict):
            options = {}

        datatree = _get_datatree(file_id)
        if datatree is None:
            await self.send_error("batch_export: no file is open")
            return

        if self.session is not None:
            try:
                output_dir = str(self._web_batch_dir(output_dir, fmt, dpi, variables, sweeps))
            except web.WebError as exc:
                await self.send_error(f"batch_export: {exc}")
                return

        loop = asyncio.get_running_loop()

        export_cancel = self._jobs.start("export", file_id if isinstance(file_id, str) else None)
        progress_sync = cancel_on_progress(self._progress_callback(loop), export_cancel)

        batch_reader = _get_reader(file_id)

        def _do_batch() -> list[str]:
            tree, mapped, opts, _ = _export_inputs(
                batch_reader,
                datatree,
                variables=variables,
                include_derived=None,
                include_original=True,
                options=options,
            )
            return _exporter().batch_export(
                tree,
                variables=mapped,
                sweeps=sweeps,
                fmt=fmt,
                dpi=dpi,
                output_dir=output_dir,
                progress=progress_sync,
                options=opts,
            )

        try:
            saved_paths = await _run_holding(_mutation_lock(), _do_batch)
        except BaseException:
            if self.session is not None and output_dir:
                shutil.rmtree(output_dir, ignore_errors=True)
            raise
        finally:
            self._jobs.finish("export", export_cancel)

        if self.session is not None:
            try:
                self.session.check_export_quota([output_dir])
                downloads = [self.session.download_info(p) for p in saved_paths]
            except web.WebError as exc:
                await self.send_error(f"batch_export: {exc}")
                return
            await self.send(
                {
                    "type": "batch_export_complete",
                    "paths": [d["path"] for d in downloads],
                    "download_urls": [d["download_url"] for d in downloads],
                    "count": len(saved_paths),
                    "output_dir": os.path.basename(output_dir),
                }
            )
            return
        await self.send(
            {
                "type": "batch_export_complete",
                "paths": saved_paths,
                "count": len(saved_paths),
                "output_dir": output_dir
                or (str(Path(saved_paths[0]).parent) if saved_paths else None),
            }
        )

    async def _handle_export_animation(self, msg: dict[str, Any]) -> None:
        variable = msg.get("variable")
        sweeps = msg.get("sweeps", [])
        output_path = _blank_to_none(msg.get("path"))
        frame_duration_ms = msg.get("frame_duration_ms", 500)
        dpi = msg.get("dpi", 150)
        options = msg.get("options", {})
        file_id = msg.get("file_id")

        if not variable or not isinstance(variable, str):
            await self.send_error("export_animation: 'variable' must be a non-empty string")
            return
        if not isinstance(sweeps, list) or not all(isinstance(s, int) for s in sweeps):
            await self.send_error("export_animation: 'sweeps' must be a list of integers")
            return
        if not sweeps:
            await self.send_error("export_animation: 'sweeps' must not be empty")
            return
        if not isinstance(frame_duration_ms, int) or not 50 <= frame_duration_ms <= 60_000:
            await self.send_error(
                "export_animation: 'frame_duration_ms' must be between 50 and 60000"
            )
            return
        if not isinstance(dpi, int) or not 72 <= dpi <= 600:  # below 72 matplotlib cannot set fonts
            await self.send_error("export_animation: 'dpi' must be between 72 and 600")
            return
        if output_path is not None and not isinstance(output_path, str):
            await self.send_error("export_animation: 'path' must be a string")
            return
        if not isinstance(options, dict):
            options = {}

        datatree = _get_datatree(file_id)
        if datatree is None:
            await self.send_error("export_animation: no file is open")
            return

        if self.session is not None:
            try:
                if len(sweeps) > 60:
                    raise web.WebError("An animation can have at most 60 frames here")
                output_path = str(self._web_export_target(output_path, "gif", dpi, ext="gif"))
            except web.WebError as exc:
                await self.send_error(f"export_animation: {exc}")
                return

        loop = asyncio.get_running_loop()

        export_cancel = self._jobs.start("export", file_id if isinstance(file_id, str) else None)
        progress_sync = cancel_on_progress(self._progress_callback(loop), export_cancel)

        anim_reader = _get_reader(file_id)

        def _do_animation() -> str:
            tree, mapped, opts, _ = _export_inputs(
                anim_reader,
                datatree,
                variables=[variable],
                include_derived=None,
                include_original=True,
                options=options,
            )
            return _exporter().export_animation(
                tree,
                variable=mapped[0],
                sweeps=sweeps,
                output_path=output_path,
                frame_duration_ms=frame_duration_ms,
                dpi=dpi,
                progress=progress_sync,
                options=opts,
            )

        try:
            saved_path = await _run_holding(_mutation_lock(), _do_animation)
        except BaseException:
            self._web_discard(output_path)
            raise
        finally:
            self._jobs.finish("export", export_cancel)

        result_path: dict[str, Any] = {"path": saved_path}
        if self.session is not None:
            try:
                self.session.check_export_quota([saved_path])
                result_path = self.session.download_info(saved_path)
            except web.WebError as exc:
                await self.send_error(f"export_animation: {exc}")
                return
        await self.send({"type": "animation_export_complete", **result_path})

    # ------------------------------------------------------------------
    # Retrieval handlers
    # ------------------------------------------------------------------

    async def _handle_run_retrieval(self, msg: dict[str, Any]) -> None:
        """Run a meteorological retrieval algorithm on the current data."""
        name = msg.get("name")
        if not name or not isinstance(name, str):
            await self.send_error("run_retrieval: missing or invalid 'name'")
            return

        sweep = msg.get("sweep", 0)
        params = msg.get("params", {})
        if not isinstance(sweep, int) or sweep < 0:
            await self.send_error("run_retrieval: 'sweep' must be a non-negative integer")
            return
        if not isinstance(params, dict):
            await self.send_error("run_retrieval: 'params' must be an object")
            return

        datatree = _get_datatree(msg.get("file_id"))
        if datatree is None:
            await self.send_error("run_retrieval: no file is open")
            return
        var_overrides = _overrides_for(msg.get("file_id"))

        await self.send_progress(10, f"Running retrieval: {name}...")

        loop = asyncio.get_running_loop()

        def _compute() -> tuple[dict[str, Any], np.ndarray]:
            result_da = run_retrieval(datatree, name, sweep, params, var_overrides=var_overrides)
            values = np.array(result_da.values, dtype=np.float32, copy=True)
            observed = _finite_range(values)
            stats: dict[str, Any] = {}
            if observed is not None:
                valid = values[np.isfinite(values)]
                stats = {
                    "min": observed[0],
                    "max": observed[1],
                    "mean": float(np.mean(valid)),
                    "std": float(np.std(valid)),
                }
            info = {
                "type": "retrieval_result",
                "name": name,
                "sweep": sweep,
                "shape": list(values.shape),
                "units": str(result_da.attrs.get("units", "")),
                "long_name": str(result_da.attrs.get("long_name", name)),
                "stats": stats,
            }
            np.nan_to_num(values, copy=False, nan=NODATA_SENTINEL)
            return info, values

        info, values = await loop.run_in_executor(None, _compute)

        await self.send(info)

        # Send the data as binary (chunked like sweep_data)
        await self._send_binary_chunks(
            [values],
            {
                "type": "retrieval_data",
                "name": name,
                "sweep": sweep,
                "n_az": values.shape[0] if values.ndim >= 1 else 1,
                "n_range": values.shape[1] if values.ndim >= 2 else values.shape[0],
            },
            length_key="byte_length",
        )

    async def _handle_list_retrievals(self, msg: dict[str, Any]) -> None:
        """List available retrievals with their metadata."""
        retrievals = []
        for name, entry in RETRIEVAL_REGISTRY.items():
            retrievals.append(
                {
                    "name": name,
                    "description": entry["description"],
                    "required_variables": entry["required_variables"],
                    "params": {
                        k: {"type": v["type"], "default": v["default"], "label": v["label"]}
                        for k, v in entry["params"].items()
                    },
                }
            )
        await self.send({"type": "retrievals_list", "retrievals": retrievals})

    # ------------------------------------------------------------------
    # Cross-section and vertical profile handlers
    # ------------------------------------------------------------------

    async def _handle_get_cross_section(self, msg: dict[str, Any]) -> None:
        """Extract an RHI-like cross-section between two geographic points.

        ``{type: "get_cross_section", variable, start: [lat, lon], end: [lat, lon], n_points?}``,
        or an RHI-style cut from the radar: ``{variable, azimuth, max_range_km, min_range_km?}``
        (the geodesic leaving the radar on ``azimuth``).  The reply echoes the line it used.
        """
        start = msg.get("start")
        end = msg.get("end")
        variable = msg.get("variable")
        n_points = msg.get("n_points", 200)
        rhi_az = msg.get("azimuth")
        rhi_range = msg.get("max_range_km")
        rhi_min = msg.get("min_range_km", 0)

        def number(v: Any) -> bool:
            return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

        if not variable or not isinstance(variable, str):
            await self.send_error("get_cross_section: missing or invalid 'variable'")
            return
        if not number(n_points) or n_points < 2:
            await self.send_error("get_cross_section: 'n_points' must be a number of at least 2")
            return
        n_points = min(int(n_points), MAX_SECTION_POINTS)
        rhi = rhi_az is not None or rhi_range is not None
        if rhi:
            if not number(rhi_az) or not number(rhi_range) or not number(rhi_min):
                await self.send_error(
                    "get_cross_section: 'azimuth' and 'max_range_km' must be numbers"
                )
                return
            if rhi_range <= max(rhi_min, 0):
                await self.send_error("get_cross_section: 'max_range_km' must be above the start")
                return
        else:
            if not isinstance(start, list) or len(start) != 2:
                await self.send_error("get_cross_section: 'start' must be [lat, lon]")
                return
            if not isinstance(end, list) or len(end) != 2:
                await self.send_error("get_cross_section: 'end' must be [lat, lon]")
                return
            if not all(number(v) for v in start + end):
                await self.send_error("get_cross_section: coordinates must be numbers")
                return

        datatree = _get_datatree(msg.get("file_id"))
        if datatree is None:
            await self.send_error("get_cross_section: no file is open")
            return

        await self.send_progress(10, "Extracting cross-section...")

        loop = asyncio.get_running_loop()
        loader = _loader_for(_get_reader(msg.get("file_id")), variable)

        def run() -> dict[str, Any]:
            if rhi:
                a, b = rhi_line(datatree, float(rhi_az), float(rhi_range), float(rhi_min))
            else:
                a, b = (float(start[0]), float(start[1])), (float(end[0]), float(end[1]))
            out = extract_cross_section(
                datatree, a, b, variable, n_points=n_points, sweep_loader=loader
            )
            if rhi:
                out["rhi_azimuth"] = float(rhi_az) % 360.0
            return out

        result = await loop.run_in_executor(None, run)

        await self.send(
            {
                "type": "cross_section_result",
                **result,
                "provenance": layer_provenance.cross_section(
                    variable=variable, **_lp_kw(_get_reader(msg.get("file_id")))
                ).to_dict(),
            }
        )

    async def _handle_get_vertical_profile(self, msg: dict[str, Any]) -> None:
        """Extract a vertical profile at a geographic point."""
        lat = msg.get("lat")
        lon = msg.get("lon")
        variable = msg.get("variable")

        if not variable or not isinstance(variable, str):
            await self.send_error("get_vertical_profile: missing or invalid 'variable'")
            return
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            await self.send_error("get_vertical_profile: 'lat' and 'lon' must be numbers")
            return

        datatree = _get_datatree(msg.get("file_id"))
        if datatree is None:
            await self.send_error("get_vertical_profile: no file is open")
            return

        await self.send_progress(10, "Extracting vertical profile...")

        loop = asyncio.get_running_loop()
        loader = _loader_for(_get_reader(msg.get("file_id")), variable)
        result = await loop.run_in_executor(
            None,
            lambda: extract_vertical_profile(
                datatree,
                float(lat),
                float(lon),
                variable,
                sweep_loader=loader,
            ),
        )

        await self.send(
            {"type": "vertical_profile_result", "lat": float(lat), "lon": float(lon), **result}
        )

    # ------------------------------------------------------------------
    # Statistics handler
    # ------------------------------------------------------------------

    async def _handle_get_statistics(self, msg: dict[str, Any]) -> None:
        """Compute statistics for a variable/sweep."""
        variable = msg.get("variable")
        sweep = msg.get("sweep", 0)
        stat_type = msg.get("stat_type", "histogram")
        params = msg.get("params", {})

        if not variable or not isinstance(variable, str):
            await self.send_error("get_statistics: missing or invalid 'variable'")
            return
        if not isinstance(sweep, int) or sweep < 0:
            await self.send_error("get_statistics: 'sweep' must be a non-negative integer")
            return
        if not isinstance(stat_type, str):
            await self.send_error("get_statistics: 'stat_type' must be a string")
            return

        datatree = _get_datatree(msg.get("file_id"))
        if datatree is None:
            await self.send_error("get_statistics: no file is open")
            return

        await self.send_progress(10, f"Computing {stat_type}...")

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            get_statistics,
            datatree,
            variable,
            sweep,
            stat_type,
            params,
        )

        await self.send({"type": "statistics_result", "stat_type": stat_type, **result})

    # ------------------------------------------------------------------
    # QC pipeline handlers
    # ------------------------------------------------------------------

    async def _handle_run_qc(self, msg: dict[str, Any]) -> None:
        """Run a QC pipeline on a variable/sweep and return cleaned data.

        Expected message::

            {type: "run_qc",
             steps: [{name: "despeckle", params: {window_size: 3}}, ...],
             variable: "DBZH", sweep: 0}

        Response: same chunked binary protocol as ``get_sweep_data``.
        """

        steps = msg.get("steps", [])
        variable = msg.get("variable")
        sweep = msg.get("sweep", 0)

        if not variable or not isinstance(variable, str):
            await self.send_error("run_qc: missing or invalid 'variable'")
            return
        if not isinstance(sweep, int) or sweep < 0:
            await self.send_error("run_qc: 'sweep' must be a non-negative integer")
            return
        if not isinstance(steps, list) or len(steps) == 0:
            await self.send_error("run_qc: 'steps' must be a non-empty list")
            return

        reader = _get_reader(msg.get("file_id"))
        datatree = reader.datatree
        if datatree is None:
            await self.send_error("run_qc: no file is open")
            return

        await self.send_progress(5, "Building QC pipeline...")

        # Build pipeline
        try:
            pipeline = QCPipeline.from_dict(steps)
        except ValueError as exc:
            await self.send_error(f"run_qc: {exc}")
            return

        loop = asyncio.get_running_loop()

        await self.send_progress(15, f"Reading {variable} sweep {sweep}...")
        data = await loop.run_in_executor(
            None,
            reader.get_sweep_data,
            sweep,
            variable,
        )

        await self.send_progress(30, f"Running {len(pipeline)} QC steps...")

        # remove_clutter also thresholds RhoHV when the sweep has it (split-cut Doppler
        # sweeps do not).
        rhohv = None
        wants_rhohv = any(s["name"] == "remove_clutter" for s in pipeline.steps)
        if wants_rhohv and variable.upper() != "RHOHV":
            try:
                rhohv = await loop.run_in_executor(None, reader.get_sweep_data, sweep, "RHOHV")
            except (KeyError, ValueError, IndexError):
                rhohv = None
            if rhohv is not None and rhohv.shape != data.shape:
                rhohv = None

        def _run_and_pack() -> tuple[dict[str, Any], list[np.ndarray]]:
            return _pack_field(
                pipeline.run(data, rhohv=rhohv), variable, rays=_rhi_rays_of(reader, sweep)
            )

        fields, arrays = await loop.run_in_executor(None, _run_and_pack)

        await self.send_progress(80, "Sending QC results...")

        await self._send_binary_chunks(
            arrays,
            {
                "type": "qc_result",
                "variable": variable,
                "sweep": sweep,
                **fields,
                "steps_applied": pipeline.to_dict(),
            },
        )

        await self.send_progress(100, "QC pipeline complete")

    async def _handle_list_qc_algorithms(self, msg: dict[str, Any]) -> None:
        """Return the QC algorithm registry with descriptions and parameter metadata."""
        algorithms = []
        for name, entry in QC_REGISTRY.items():
            algorithms.append(
                {
                    "name": name,
                    "description": entry["description"],
                    "params": {
                        k: {
                            "type": v["type"],
                            "default": v["default"],
                            "label": v["label"],
                            **({"min": v["min"]} if "min" in v else {}),
                            **({"max": v["max"]} if "max" in v else {}),
                            **({"options": v["options"]} if "options" in v else {}),
                        }
                        for k, v in entry["params"].items()
                    },
                    "applicable_variables": entry["applicable_variables"],
                }
            )
        await self.send({"type": "qc_algorithms_list", "algorithms": algorithms})

    # ------------------------------------------------------------------
    # Temporal analysis handlers
    # ------------------------------------------------------------------

    async def _handle_get_time_series(self, msg: dict[str, Any]) -> None:
        """Extract value at a point across multiple files."""
        variable = msg.get("variable")
        sweep = msg.get("sweep", 0)
        azimuth = msg.get("azimuth")
        range_m = msg.get("range_m")
        file_ids = msg.get("file_ids", [])

        if not variable or not isinstance(variable, str):
            await self.send_error("get_time_series: missing or invalid 'variable'")
            return
        if not isinstance(azimuth, (int, float)) or not isinstance(range_m, (int, float)):
            await self.send_error("get_time_series: 'azimuth' and 'range_m' must be numbers")
            return
        if not isinstance(file_ids, list) or len(file_ids) == 0:
            await self.send_error("get_time_series: 'file_ids' must be a non-empty list")
            return

        readers: dict[str, RadarReader] = {}
        for fid in file_ids:
            if fid in _readers():
                readers[fid] = _readers()[fid]

        if not readers:
            await self.send_error("get_time_series: none of the specified file_ids are open")
            return

        await self.send_progress(10, "Computing time series...")

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            compute_time_series,
            readers,
            variable,
            sweep,
            float(azimuth),
            float(range_m),
        )

        await self.send({"type": "time_series_result", **result})

    async def _handle_get_difference(self, msg: dict[str, Any]) -> None:
        """Compute element-wise difference between two files."""
        variable = msg.get("variable")
        sweep = msg.get("sweep", 0)
        fid1 = msg.get("file_id_1")
        fid2 = msg.get("file_id_2")

        if not variable or not isinstance(variable, str):
            await self.send_error("get_difference: missing or invalid 'variable'")
            return
        if not fid1 or not fid2:
            await self.send_error("get_difference: 'file_id_1' and 'file_id_2' are required")
            return

        reader1 = _readers().get(fid1)
        reader2 = _readers().get(fid2)
        if not reader1:
            await self.send_error(f"get_difference: file_id_1 '{fid1}' not found")
            return
        if not reader2:
            await self.send_error(f"get_difference: file_id_2 '{fid2}' not found")
            return

        await self.send_progress(10, "Computing difference...")

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            compute_difference,
            reader1,
            reader2,
            variable,
            sweep,
        )

        diff_bytes = result.pop("diff_values")
        az_bytes_raw = result.pop("azimuth")
        rng_bytes_raw = result.pop("range")

        # The header now also carries the frame count (the client defaults to a single
        # frame when it is missing, which truncated differences larger than 900 KB).
        await self._send_binary_chunks(
            [az_bytes_raw, rng_bytes_raw, diff_bytes],
            {"type": "difference_result", **result},
        )

    async def _handle_get_accumulation(self, msg: dict[str, Any]) -> None:
        """Accumulate a variable across multiple files."""
        variable = msg.get("variable")
        sweep = msg.get("sweep", 0)
        file_ids = msg.get("file_ids", [])
        method = msg.get("method", "sum")

        if not variable or not isinstance(variable, str):
            await self.send_error("get_accumulation: missing or invalid 'variable'")
            return
        if not isinstance(file_ids, list) or len(file_ids) == 0:
            await self.send_error("get_accumulation: 'file_ids' must be a non-empty list")
            return
        if method not in ("sum", "mean", "max"):
            await self.send_error("get_accumulation: 'method' must be sum, mean, or max")
            return

        readers: dict[str, RadarReader] = {}
        for fid in file_ids:
            if fid in _readers():
                readers[fid] = _readers()[fid]

        if not readers:
            await self.send_error("get_accumulation: none of the specified file_ids are open")
            return

        await self.send_progress(10, f"Computing {method} accumulation...")

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            compute_accumulation,
            readers,
            variable,
            sweep,
            method,
        )

        await self.send({"type": "accumulation_result", **result})

    # ------------------------------------------------------------------
    # Storm cell identification & tracking
    # ------------------------------------------------------------------

    async def _handle_identify_cells(self, msg: dict[str, Any]) -> None:
        async with self._cells_lock:
            await self._identify_cells_locked(msg)

    async def _handle_track_cells(self, msg: dict[str, Any]) -> None:
        async with self._cells_lock:
            await self._track_cells_locked(msg)

    async def _identify_cells_locked(self, msg: dict[str, Any]) -> None:
        """Identify storm cells in a reflectivity sweep.

        Expected message::

            {type: "identify_cells", variable: "DBZH", sweep: 0,
             params: {threshold_dbz: 35, min_size_km2: 10}}

        Response::

            {type: "cells_identified", variable, sweep, cells: [...]}
        """
        variable = msg.get("variable", "DBZH")
        sweep_idx = msg.get("sweep", 0)
        params = msg.get("params", {})

        if not isinstance(variable, str):
            await self.send_error("identify_cells: 'variable' must be a string")
            return
        if not isinstance(sweep_idx, int) or sweep_idx < 0:
            await self.send_error("identify_cells: 'sweep' must be a non-negative integer")
            return
        if not isinstance(params, dict):
            await self.send_error("identify_cells: 'params' must be an object")
            return

        reader = _get_reader(msg.get("file_id"))
        datatree = reader.datatree
        if datatree is None:
            await self.send_error("identify_cells: no file is open")
            return

        threshold_dbz = float(params.get("threshold_dbz", 35.0))
        min_size_km2 = float(params.get("min_size_km2", 10.0))
        from engine import cells as cell_engine

        try:
            opts = cell_engine.options_from_params(params)
        except ValueError as exc:
            await self.send_error(f"identify_cells: {exc}")
            return

        await self.send_progress(10, f"Identifying cells in {variable} sweep {sweep_idx}...")

        loop = asyncio.get_running_loop()

        data = await loop.run_in_executor(
            None,
            reader.get_sweep_data,
            sweep_idx,
            variable,
        )

        scan = await loop.run_in_executor(
            None,
            functools.partial(
                cell_engine.identify_sweep, datatree, data, threshold_dbz, min_size_km2, **opts
            ),
        )

        # Store for subsequent tracking
        self._previous_cells = scan

        await self.send_progress(100, f"Found {len(scan)} cells")
        await self.send(
            {
                "type": "cells_identified",
                "variable": variable,
                "sweep": sweep_idx,
                "threshold_dbz": threshold_dbz,
                "min_size_km2": min_size_km2,
                "cells": [c.to_dict() for c in scan],
                "backend": scan.backend,
                "backend_label": scan.backend_label,
                "note": scan.note,
                "provenance": layer_provenance.storm_cells(
                    valid_time=layer_provenance.reader_context(_get_reader(msg.get("file_id")))[
                        "scan_time"
                    ],
                    params={"threshold_dbz": threshold_dbz, "min_size_km2": min_size_km2},
                ).to_dict(),
            }
        )

    async def _track_cells_locked(self, msg: dict[str, Any]) -> None:
        """Track cells between the previous identification and a new sweep.

        Expected message::

            {type: "track_cells", variable: "DBZH", sweep: 1,
             params: {max_distance_km: 30, dt_seconds: 300,
                      threshold_dbz: 35, min_size_km2: 10}}

        Response::

            {type: "cells_tracked", tracks: [...], cells: [...]}
        """
        variable = msg.get("variable", "DBZH")
        sweep_idx = msg.get("sweep", 0)
        params = msg.get("params", {})

        if not isinstance(variable, str):
            await self.send_error("track_cells: 'variable' must be a string")
            return
        if not isinstance(sweep_idx, int) or sweep_idx < 0:
            await self.send_error("track_cells: 'sweep' must be a non-negative integer")
            return
        if not isinstance(params, dict):
            await self.send_error("track_cells: 'params' must be an object")
            return

        reader = _get_reader(msg.get("file_id"))
        datatree = reader.datatree
        if datatree is None:
            await self.send_error("track_cells: no file is open")
            return

        if not self._previous_cells:
            await self.send_error("track_cells: no previous cells — run identify_cells first")
            return

        threshold_dbz = float(params.get("threshold_dbz", 35.0))
        min_size_km2 = float(params.get("min_size_km2", 10.0))
        max_distance_km = float(params.get("max_distance_km", 30.0))
        from engine import cells as cell_engine

        try:
            opts = cell_engine.options_from_params(params)
        except ValueError as exc:
            await self.send_error(f"track_cells: {exc}")
            return

        await self.send_progress(10, f"Tracking cells in {variable} sweep {sweep_idx}...")

        loop = asyncio.get_running_loop()

        data = await loop.run_in_executor(
            None,
            reader.get_sweep_data,
            sweep_idx,
            variable,
        )

        new_scan = await loop.run_in_executor(
            None,
            functools.partial(
                cell_engine.identify_sweep, datatree, data, threshold_dbz, min_size_km2, **opts
            ),
        )

        # Time step: explicit dt_seconds wins, then the scan times, then 5 minutes.
        previous = self._previous_cells
        scan_dt = None
        t_prev, t_new = getattr(previous, "time_s", None), new_scan.time_s
        if t_prev is not None and t_new is not None and t_new > t_prev:
            scan_dt = float(t_new - t_prev)
        if "dt_seconds" in params:
            dt_seconds, dt_source = float(params["dt_seconds"]), "request"
        elif scan_dt is not None:
            dt_seconds, dt_source = scan_dt, "scan times"
        else:
            dt_seconds, dt_source = 300.0, "default"

        tracks = await loop.run_in_executor(
            None,
            functools.partial(
                cell_engine.track_pair,
                previous,
                new_scan,
                max_distance_km=max_distance_km,
                dt_seconds=dt_seconds,
                backend=opts["backend"],
                min_overlap=float(params.get("min_overlap", 0.3)),
            ),
        )

        self._previous_cells = new_scan

        await self.send_progress(100, f"Tracked {len(tracks)} cells")
        motion = None
        if tracks.motion is not None:
            speed, from_deg = cell_engine.uv_to_speed_dir(*tracks.motion)
            motion = {
                "u_mps": round(tracks.motion[0], 2),
                "v_mps": round(tracks.motion[1], 2),
                "speed_mps": round(speed, 2),
                "direction_deg": round(from_deg, 1),
            }
        await self.send(
            {
                "type": "cells_tracked",
                "variable": variable,
                "sweep": sweep_idx,
                "cells": [c.to_dict() for c in new_scan],
                "tracks": [t.to_dict() for t in tracks],
                "backend": tracks.backend,
                "backend_label": tracks.backend_label,
                "note": tracks.note or new_scan.note,
                "dt_seconds": dt_seconds,
                "dt_source": dt_source,
                "motion": motion,
            }
        )

    # ------------------------------------------------------------------
    # Data table handler — windowed azimuth x range grid
    # ------------------------------------------------------------------

    async def _handle_get_data_table(self, msg: dict[str, Any]) -> None:
        """Return a windowed 2D slice of radar data for the spreadsheet viewer.

        Expected message::

            {type: "get_data_table", variable: "DBZH", sweep: 0,
             az_start: 0, az_end: 100, range_start: 0, range_end: 100}

        Response::

            {type: "data_table_result", azimuths: [...], ranges: [...],
             values: [[...], ...], total_az: N, total_range: M}
        """
        variable = msg.get("variable")
        sweep = msg.get("sweep", 0)
        az_start = msg.get("az_start", 0)
        az_end = msg.get("az_end", 360)
        range_start = msg.get("range_start", 0)
        range_end = msg.get("range_end", 1000)

        if not variable or not isinstance(variable, str):
            await self.send_error("get_data_table: missing or invalid 'variable'")
            return
        if not isinstance(sweep, int) or sweep < 0:
            await self.send_error("get_data_table: 'sweep' must be a non-negative integer")
            return

        reader = _get_reader(msg.get("file_id"))
        if reader.datatree is None:
            await self.send_error("get_data_table: no file is open")
            return

        loop = asyncio.get_running_loop()

        def _build() -> dict[str, Any]:
            data = reader.get_sweep_data(sweep, variable)
            azimuth = (
                data.coords["azimuth"].values
                if "azimuth" in data.coords
                else np.arange(data.shape[0], dtype=np.float64)
            )
            range_m = (
                data.coords["range"].values
                if "range" in data.coords
                else np.arange(data.shape[-1], dtype=np.float64)
            )
            vals = data.values
            ray_axis = "azimuth"
            if "elevation" in data.coords and reader.sweep_kinds()[sweep : sweep + 1] == ["rhi"]:
                # A range-height scan's rays differ in elevation (azimuth is the same on every row).
                azimuth = data.coords["elevation"].values
                ray_axis = "elevation"

            total_az = len(azimuth)
            total_range = len(range_m)

            # Clamp window indices
            az_s = max(0, min(int(az_start), total_az))
            az_e = max(az_s, min(int(az_end), total_az))
            rng_s = max(0, min(int(range_start), total_range))
            rng_e = max(rng_s, min(int(range_end), total_range))

            window = np.round(np.asarray(vals[az_s:az_e, rng_s:rng_e], dtype=np.float64), 6)
            cells = window.astype(object)
            cells[~np.isfinite(window)] = None  # NaN/Inf -> null

            return {
                "type": "data_table_result",
                "azimuths": np.round(azimuth[az_s:az_e].astype(np.float64), 4).tolist(),
                "ranges": np.round(range_m[rng_s:rng_e].astype(np.float64), 4).tolist(),
                "values": cells.tolist(),
                "total_az": total_az,
                "total_range": total_range,
                "ray_axis": ray_axis,
            }

        await self.send(await loop.run_in_executor(None, _build))

    # ------------------------------------------------------------------
    # Metadata tree handler — full DataTree structure as nested dict
    # ------------------------------------------------------------------

    async def _handle_get_variable_mapping(self, msg: dict[str, Any]) -> None:
        """Resolved role -> variable map of a file, with how each role matched.

        Request ``{type: "get_variable_mapping", file_id?, sweep?}``; reply
        ``{type: "variable_mapping", file_id, sweep, overrides, roles: [{role, label, override,
        variable, kind, confidence, sweep, corrected}], variables: [...]}``. ``kind`` is one of
        override / cf / alias / pattern; ``sweep`` is set when the field lives on the paired sweep.
        """
        await self._send_variable_mapping(msg)

    async def _handle_set_variable_mapping(self, msg: dict[str, Any]) -> None:
        """Set (merge) user overrides ``{role: variable_name}`` for a file; a blank value clears.

        Stored per open file and persisted per file path (``~/.xradar-desktop/variable_maps.json``).
        ``replace: true`` replaces the whole mapping. Replies like ``get_variable_mapping``.
        """
        mapping = msg.get("mapping")
        if not isinstance(mapping, dict):
            await self.send_error("set_variable_mapping: 'mapping' must be an object")
            return
        resolved_id, reader = _resolve_reader(msg.get("file_id"))
        if reader.datatree is None:
            await self.send_error("set_variable_mapping: no file is open")
            return
        try:
            _overrides_store().update(
                resolved_id, reader._path, mapping, replace=bool(msg.get("replace", False))
            )
        except varmap.VarMapError as exc:
            await self.send_error(f"set_variable_mapping: {exc}")
            return
        await self._send_variable_mapping(msg)

    async def _send_variable_mapping(self, msg: dict[str, Any]) -> None:
        sweep = msg.get("sweep", 0)
        if isinstance(sweep, bool) or not isinstance(sweep, int) or sweep < 0:
            await self.send_error("variable mapping: 'sweep' must be a non-negative integer")
            return
        resolved_id, reader = _resolve_reader(msg.get("file_id"))
        tree = reader.datatree
        if tree is None:
            await self.send_error("variable mapping: no file is open")
            return
        overrides = _overrides_store().get(resolved_id, reader._path)
        loop = asyncio.get_running_loop()
        report = await loop.run_in_executor(
            None, lambda: varmap.mapping_report(tree, sweep, overrides)
        )
        await self.send(
            {
                "type": "variable_mapping",
                "file_id": resolved_id or msg.get("file_id"),
                "overrides": overrides,
                **report,
            }
        )

    async def _handle_data_check(self, msg: dict[str, Any]) -> None:
        """Data-quality report of an open file (see ``engine/quality.py``, docs/DATA_CHECK.md).

        Request ``{type: "data_check", file_id?}``; reply ``{type: "data_check", file_id, report}``.
        Read-only: it looks at the data already in memory (a lazily-loaded ARCO sweep is never
        fetched for it) and applies nothing. One check runs at a time per connection.
        """
        resolved_id, reader = _resolve_reader(msg.get("file_id"))
        tree = reader.datatree
        if tree is None:
            await self.send_error("data_check: no file is open")
            return
        overrides = _overrides_store().get(resolved_id, reader._path)
        cs = _cs()
        lock = (_locks if cs is None else cs.locks).get("data_check")
        report = await _run_holding(lock, lambda: quality.check_reader(reader, overrides=overrides))
        if resolved_id is not None and _readers().get(resolved_id) is not reader:
            return  # closed or replaced while it ran: nobody wants this report
        await self.send(
            {"type": "data_check", "file_id": resolved_id or msg.get("file_id"), "report": report}
        )

    async def _handle_get_metadata_tree(self, msg: dict[str, Any]) -> None:
        """Return the full DataTree structure as a nested metadata tree.

        Response::

            {type: "metadata_tree_result",
             tree: [{name, type, children, dtype, shape, ...}, ...]}
        """
        reader = _get_reader(msg.get("file_id"))
        if reader.datatree is None:
            await self.send_error("get_metadata_tree: no file is open")
            return

        dtree = reader.datatree

        def _build_node(name: str, node) -> dict[str, Any]:
            """Recursively build a metadata node from a DataTree node."""
            result: dict[str, Any] = {
                "name": name,
                "type": "group",
                "children": [],
            }

            try:
                ds = node.to_dataset()
            except (ValueError, KeyError):
                return result

            # Coordinates
            for coord_name in sorted(ds.coords):
                coord = ds.coords[coord_name]
                child: dict[str, Any] = {
                    "name": str(coord_name),
                    "type": "coordinate",
                    "dtype": str(coord.dtype),
                    "shape": list(coord.shape),
                    "dims": [str(d) for d in coord.dims],
                }
                if coord.attrs.get("units"):
                    child["units"] = str(coord.attrs["units"])
                if coord.attrs.get("long_name"):
                    child["long_name"] = str(coord.attrs["long_name"])
                # Compute stats for numeric coords
                if coord.dtype.kind in ("f", "i", "u") and coord.size > 0:
                    try:
                        vals = _read_uncached(coord)
                        valid = vals[np.isfinite(vals)] if vals.dtype.kind == "f" else vals
                        if len(valid) > 0:
                            child["min"] = round(float(np.min(valid)), 6)
                            child["max"] = round(float(np.max(valid)), 6)
                            child["mean"] = round(float(np.mean(valid)), 6)
                    except Exception:
                        pass
                result["children"].append(child)

            # Data variables
            for var_name in sorted(ds.data_vars):
                da = ds[var_name]
                child = {
                    "name": str(var_name),
                    "type": "variable",
                    "dtype": str(da.dtype),
                    "shape": list(da.shape),
                    "dims": [str(d) for d in da.dims],
                }
                if da.attrs.get("units"):
                    child["units"] = str(da.attrs["units"])
                if da.attrs.get("long_name"):
                    child["long_name"] = str(da.attrs["long_name"])
                # Stats for float variables
                if da.dtype.kind == "f" and da.ndim >= 1 and da.size > 0:
                    try:
                        vals = _read_uncached(da)
                        valid = vals[np.isfinite(vals)]
                        if len(valid) > 0:
                            child["min"] = round(float(np.min(valid)), 4)
                            child["max"] = round(float(np.max(valid)), 4)
                            child["mean"] = round(float(np.mean(valid)), 4)
                    except Exception:
                        pass
                # Variable-level attributes as children
                attr_children = []
                for ak, av in da.attrs.items():
                    attr_children.append(
                        {
                            "name": str(ak),
                            "type": "attribute",
                            "value": _serializable(av),
                        }
                    )
                if attr_children:
                    child["children"] = attr_children
                result["children"].append(child)

            # Dataset-level attributes
            for ak, av in ds.attrs.items():
                result["children"].append(
                    {
                        "name": str(ak),
                        "type": "attribute",
                        "value": _serializable(av),
                    }
                )

            # Recurse into children
            if hasattr(node, "children"):
                for child_name in sorted(node.children, key=_natural_key):
                    child_node = node[child_name]
                    result["children"].append(_build_node(child_name, child_node))

            return result

        loop = asyncio.get_running_loop()
        tree_data = await loop.run_in_executor(
            None,
            _build_node,
            "root",
            dtree,
        )

        await self.send(
            {
                "type": "metadata_tree_result",
                "tree": tree_data.get("children", []),
            }
        )

    # ------------------------------------------------------------------
    # Plugin modules
    # ------------------------------------------------------------------

    async def _handle_list_modules(self, msg: dict[str, Any]) -> None:
        """List discovered plugin modules (``reload: true`` re-scans the module dirs)."""
        loop = asyncio.get_running_loop()
        # Web mode: the module registry is shared by every visitor and never re-scanned.
        if msg.get("reload") and self.session is None:
            async with _locks.get("modules"):
                await loop.run_in_executor(None, plugins.discover_modules)
        listing = plugins.list_modules()
        errors, dirs = listing["errors"], listing["dirs"]
        if self.session is not None:  # no server paths for visitors
            errors = [{**e, "file": os.path.basename(str(e.get("file", "")))} for e in errors]
            dirs = []
        await self.send(
            {
                "type": "modules_list",
                "modules": listing["modules"],
                "errors": errors,
                "dirs": dirs,
            }
        )

    async def _handle_run_module(self, msg: dict[str, Any]) -> None:
        """Run a plugin module on a sweep.

        Expected message::

            {type: "run_module", module: "hot_gates", sweep: 0,
             params: {threshold: 50}, file_id: "..."}

        Success: ``{type: "module_result", module, sweep, params, elapsed_ms, blocks,
        file_id}``. Any failure: ``{type: "module_error", module, message}`` so the UI
        can always leave its busy state.
        """
        module_id = msg.get("module")
        label = module_id if isinstance(module_id, str) else None
        file_id = msg.get("file_id")
        try:
            with self._jobs.running(
                "module", file_id if isinstance(file_id, str) else None
            ) as cancel:
                await self._run_module(msg, cancel)
        except (plugins.ModuleCancelled, derived.RunCancelledError):
            await self.send(
                {"type": "module_error", "module": label, "message": "Cancelled", "cancelled": True}
            )
        except plugins.ModuleError as exc:
            logger.warning("Module '%s' error: %s", label, exc)
            await self.send({"type": "module_error", "module": label, "message": str(exc)})
        except KeyError as exc:
            message = str(exc.args[0]) if exc.args else "not found"
            await self.send({"type": "module_error", "module": label, "message": message})
        except Exception as exc:
            logger.error("Module '%s' failed: %s\n%s", label, exc, traceback.format_exc())
            await self.send(
                {
                    "type": "module_error",
                    "module": label,
                    "message": (
                        f"Module '{label}' failed ({type(exc).__name__}: {exc}). Try another "
                        "sweep or different parameters; the sidecar log has the full traceback."
                    ),
                }
            )
        finally:
            # The module reports progress while it runs; always close the bar so the status
            # line does not stay stuck at e.g. "Classifying hydrometeors (40%)".
            await self.send_progress(100, "Module finished")

    async def _run_module(self, msg: dict[str, Any], cancel: threading.Event) -> None:
        module_id = msg.get("module")
        sweep = msg.get("sweep", 0)
        params = msg.get("params", {})
        if not module_id or not isinstance(module_id, str):
            raise plugins.ModuleError("run_module: missing or invalid 'module'")
        if isinstance(sweep, bool) or not isinstance(sweep, int) or sweep < 0:
            raise plugins.ModuleError("run_module: 'sweep' must be a non-negative integer")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise plugins.ModuleError("run_module: 'params' must be an object")
        if module_id not in plugins.MODULES:
            raise plugins.ModuleError(f"Unknown module '{module_id}'")
        params = self._web_module_params(module_id, params)
        all_sweeps = msg.get("all_sweeps", False)
        if all_sweeps is not None and not isinstance(all_sweeps, bool):
            raise plugins.ModuleError("run_module: 'all_sweeps' must be a boolean")

        resolved_id, reader = _resolve_reader(msg.get("file_id"))
        datatree = reader.datatree
        if all_sweeps:
            await self._run_module_all_sweeps(msg, module_id, params, resolved_id, reader, cancel)
            return
        if datatree is None:
            raise plugins.ModuleError("No file is open")
        overrides = _module_overrides(reader, msg)

        var_overrides = _overrides_store().get(resolved_id, reader._path)
        await self.send_progress(5, f"Running module {module_id}...")
        loop = asyncio.get_running_loop()
        progress_sync = self._progress_callback(loop)
        # ``cancel`` is also set when the connection (this task) goes away, see below

        def _run() -> tuple[dict[str, Any], dict[str, Any], list[tuple[dict[str, Any], list]]]:
            payload, fields = plugins.run_module(
                module_id,
                datatree,
                sweep,
                params,
                get_sweep_dataset=lambda i: _sweep_dataset(reader, i),
                progress=progress_sync,
                return_fields=True,
                is_cancelled=cancel.is_set,
                var_overrides=var_overrides,
                overrides=overrides,
            )
            packed = _pack_module_fields(module_id, fields, _sweep_dataset(reader, sweep))
            return payload, fields, packed

        try:
            payload, fields, packed = await loop.run_in_executor(None, _run)
        except BaseException:
            cancel.set()  # a still-running module is abandoned; its late result is dropped
            raise
        out_file_id = resolved_id or msg.get("file_id")
        await self.send({"type": "module_result", **payload, "file_id": out_file_id})
        blocks = {b.get("name"): b for b in payload.get("blocks", []) if b.get("type") == "field"}
        for name, (fields_hdr, arrays) in zip(fields, packed, strict=True):
            block = blocks.get(name, {})
            await self._send_binary_chunks(
                arrays,
                {
                    "type": "module_field",
                    "module": module_id,
                    "name": name,
                    "sweep": sweep,
                    "units": str(block.get("units") or ""),
                    "file_id": out_file_id,
                    "provenance": layer_provenance.derived_field(
                        name=str(block.get("title") or name),
                        module=module_id,
                        params=payload.get("params"),
                        valid_time=layer_provenance.reader_context(reader)["scan_time"],
                    ).to_dict(),
                    **fields_hdr,
                    **_field_range(block, arrays[2]),
                },
            )

    async def _run_module_all_sweeps(
        self,
        msg: dict[str, Any],
        module_id: str,
        params: dict[str, Any],
        resolved_id: str | None,
        reader: RadarReader,
        cancel: threading.Event,
    ) -> None:
        """Run a per-sweep module on every sweep and register its fields as derived variables.

        Success: ``module_volume_result`` (see :func:`_volume_result_message`) followed by
        ``derived_variables``. Failure: ``ModuleError`` -> ``module_error`` as for one sweep.
        """
        spec = plugins.MODULES[module_id]
        if not spec.per_sweep:
            raise plugins.ModuleError(
                "This module computes a whole-volume product and cannot be run per sweep"
            )
        missing = spec.missing_packages()
        if missing:
            raise plugins.ModuleError(
                f"{spec.title} needs the Python package {', '.join(missing)} "
                f"(pip install {' '.join(missing)})"
            )
        plugins._coerce_params(spec.params, params)  # bad parameters fail the run, not each sweep
        datatree = reader.datatree
        if datatree is None:
            raise plugins.ModuleError("No file is open")
        n_sweeps = len(sorted_sweep_names(datatree))
        out_file_id = resolved_id or msg.get("file_id")
        overrides = _module_overrides(reader, msg)

        await self.send_progress(2, f"{spec.title}: starting on {n_sweeps} sweeps")
        loop = asyncio.get_running_loop()
        progress_sync = self._progress_callback(loop)

        def _grid(i: int) -> tuple[int, int]:
            ds = reader.get_sweep_dataset(i, mask=False)
            return int(ds["azimuth"].size), int(ds["range"].size)

        def _sweep_moments(i: int) -> set[str]:
            data_vars = reader.get_sweep_dataset(i, mask=False).data_vars
            return {str(k) for k, v in data_vars.items() if v.ndim >= 2}

        def _run() -> tuple[derived.VolumeRun, list[tuple[str, dict[int, np.ndarray], dict]]]:
            run = derived.run_all_sweeps(
                module_id,
                datatree,
                n_sweeps,
                params,
                get_sweep_dataset=lambda i: _sweep_dataset(reader, i),
                get_sweep_variables=_sweep_moments,
                get_sweep_grid=_grid,
                progress=progress_sync,
                is_cancelled=cancel.is_set,
                var_overrides=_overrides_store().get(resolved_id, reader._path),
                overrides=overrides,
            )
            return run, derived.build_variables(run)

        try:
            run, built = await loop.run_in_executor(None, _run)
        except BaseException:
            cancel.set()  # the worker stops after the sweep it is on
            raise

        # The file may have been closed or replaced (process, re-open) while we computed.
        still_open = resolved_id is None or _readers().get(resolved_id) is reader
        if not still_open or reader.datatree is not datatree:
            raise plugins.ModuleError(
                "The file was closed or changed while the module was running; result discarded"
            )
        store = derived.store_for(reader)
        assert store is not None
        evicted: list[str] = []
        try:
            for name, arrays, meta in built:
                evicted.extend(store.put_variable(name, arrays, meta))
        except derived.DerivedError as exc:
            raise plugins.ModuleError(str(exc)) from exc
        names = {name for name, _, _ in built}
        evicted = [n for n in dict.fromkeys(evicted) if n not in names or not store.has(n)]
        await self.send(_volume_result_message(run, built, store, evicted, out_file_id))
        await self._send_derived_list(out_file_id, reader, evicted=evicted)

    async def _send_derived_list(
        self,
        file_id: str | None,
        reader: RadarReader,
        *,
        evicted: list[str] | None = None,
        dropped: list[str] | None = None,
    ) -> None:
        store = derived.store_for(reader, create=False)
        msg: dict[str, Any] = {
            "type": "derived_variables",
            "file_id": file_id,
            "variables": [
                {
                    **v,
                    "provenance": layer_provenance.from_derived_info(
                        v, layer_provenance.reader_context(reader)["scan_time"]
                    ).to_dict(),
                }
                for v in (store.variables() if store is not None else [])
            ],
        }
        if evicted:
            msg["evicted"] = evicted
        if dropped:
            msg["dropped"] = dropped
        await self.send(msg)

    async def _handle_list_derived(self, msg: dict[str, Any]) -> None:
        """``{type: "list_derived", file_id?}`` -> ``{type: "derived_variables", ...}``."""
        resolved_id, reader = _resolve_reader(msg.get("file_id"))
        await self._send_derived_list(resolved_id or msg.get("file_id"), reader)

    async def _handle_clear_derived(self, msg: dict[str, Any]) -> None:
        """``{type: "clear_derived", file_id?, name?}``: drop one (or all) derived variables."""
        name = msg.get("name")
        if name is not None and not isinstance(name, str):
            await self.send_error("clear_derived: 'name' must be a string")
            return
        resolved_id, reader = _resolve_reader(msg.get("file_id"))
        store = derived.store_for(reader, create=False)
        dropped = store.drop(name) if store is not None else []
        await self._send_derived_list(resolved_id or msg.get("file_id"), reader, dropped=dropped)

    # ------------------------------------------------------------------
    # Processing pipelines ("recipes") -- see engine/pipeline.py, docs/PIPELINES.md
    # ------------------------------------------------------------------

    async def _handle_list_pipeline_steps(self, msg: dict[str, Any]) -> None:
        """``{type: "list_pipeline_steps"}`` -> ``{type: "pipeline_steps", steps}``."""
        await self.send({"type": "pipeline_steps", "steps": pipeline.list_steps()})

    async def _send_recipes(self) -> None:
        listing = pipeline.list_recipes()
        await self.send({"type": "recipes_list", **listing})

    async def _handle_list_recipes(self, msg: dict[str, Any]) -> None:
        """``{type: "list_recipes"}`` -> ``{type: "recipes_list", recipes, errors, dir}``."""
        await self._send_recipes()

    async def _pipeline_guard(self, label: str, coro: Any, *, progress: bool = False) -> None:
        """Run a pipeline handler body; any failure becomes ``pipeline_error`` (UI leaves busy)."""
        try:
            await coro
        except (plugins.ModuleError, derived.DerivedError) as exc:
            logger.warning("Pipeline '%s' error: %s", label, exc)
            await self.send({"type": "pipeline_error", "action": label, "message": str(exc)})
        except derived.RunCancelledError:
            cancelled = {"type": "pipeline_error", "action": label, "cancelled": True}
            await self.send({**cancelled, "message": "Cancelled"})
        except (KeyError, IndexError) as exc:
            message = str(exc.args[0]) if exc.args else "not found"
            await self.send({"type": "pipeline_error", "action": label, "message": message})
        except Exception as exc:
            logger.error("Pipeline '%s' failed: %s\n%s", label, exc, traceback.format_exc())
            msg = f"{type(exc).__name__}: {exc}"
            await self.send({"type": "pipeline_error", "action": label, "message": msg})
        finally:
            if progress:  # long-running handlers: always close the status-bar progress
                await self.send_progress(100, "Pipeline finished")

    async def _handle_save_recipe(self, msg: dict[str, Any]) -> None:
        """``{type: "save_recipe", recipe}`` -> ``recipe_saved`` then ``recipes_list``."""

        async def body() -> None:
            recipe = pipeline.save_recipe(msg.get("recipe"))
            await self.send({"type": "recipe_saved", "recipe": recipe.to_dict()})
            await self._send_recipes()

        await self._pipeline_guard("save_recipe", body())

    async def _handle_delete_recipe(self, msg: dict[str, Any]) -> None:
        """``{type: "delete_recipe", name}`` -> ``recipe_deleted`` then ``recipes_list``."""

        async def body() -> None:
            name = msg.get("name")
            gone = pipeline.delete_recipe(name)
            await self.send({"type": "recipe_deleted", "name": name, "existed": gone})
            await self._send_recipes()

        await self._pipeline_guard("delete_recipe", body())

    async def _handle_export_recipe_script(self, msg: dict[str, Any]) -> None:
        """``{type: "export_recipe_script", recipe}`` -> ``recipe_script`` (filename, script)."""

        async def body() -> None:
            recipe = pipeline.parse_recipe(msg.get("recipe"), strict_name=False)
            await self.send(
                {
                    "type": "recipe_script",
                    "name": recipe.name,
                    "filename": f"{recipe.slug}.py",
                    "script": pipeline.recipe_to_script(recipe),
                }
            )

        await self._pipeline_guard("export_recipe_script", body())

    async def _handle_preview_pipeline(self, msg: dict[str, Any]) -> None:
        """Run a recipe on ONE sweep without storing anything.

        ``{type: "preview_pipeline", file_id?, sweep, recipe}`` -> ``pipeline_preview`` (per-step
        stats + the ``Before`` / ``After`` / ``Removed by step`` field blocks) followed by one
        ``module_field`` binary payload per field (module ``"pipeline"``), the format the UI
        already shows on the PPI for module results.
        """

        async def body() -> None:
            sweep = msg.get("sweep", 0)
            if isinstance(sweep, bool) or not isinstance(sweep, int) or sweep < 0:
                raise plugins.ModuleError("preview_pipeline: 'sweep' must be a non-negative int")
            recipe = pipeline.parse_recipe(msg.get("recipe"), strict_name=False)
            resolved_id, reader = _resolve_reader(msg.get("file_id"))
            datatree = reader.datatree
            if datatree is None:
                raise plugins.ModuleError("No file is open")
            await self.send_progress(5, f"Previewing {recipe.name}...")
            loop = asyncio.get_running_loop()
            progress_sync = self._progress_callback(loop)

            def _run() -> tuple[Any, list[dict[str, Any]], list[tuple[dict[str, Any], list]]]:
                ctx = plugins.ModuleContext(
                    datatree, sweep, get_sweep_dataset=lambda i: _sweep_dataset(reader, i)
                )
                run = pipeline.run_recipe_on_sweep(recipe, ctx, progress=progress_sync)
                blocks, fields = pipeline.preview_fields(run, recipe)
                packed = _pack_module_fields("pipeline", fields, _sweep_dataset(reader, sweep))
                return run, blocks, packed

            run, blocks, packed = await loop.run_in_executor(None, _run)
            out_file_id = resolved_id or msg.get("file_id")
            await self.send(
                {
                    "type": "pipeline_preview",
                    "file_id": out_file_id,
                    "sweep": sweep,
                    "recipe": recipe.name,
                    "target": recipe.target,
                    "steps": run.steps,
                    "valid_before": run.n_valid_before,
                    "valid_after": run.n_valid_after,
                    "notes": run.notes,
                    "elapsed_ms": run.elapsed_ms,
                    "blocks": blocks,
                }
            )
            by_name = {b["name"]: b for b in blocks}
            for name, (hdr, arrays) in zip(by_name, packed, strict=True):
                await self._send_binary_chunks(
                    arrays,
                    {
                        "type": "module_field",
                        "module": "pipeline",
                        "name": name,
                        "sweep": sweep,
                        "units": str(by_name[name].get("units") or ""),
                        "file_id": out_file_id,
                        **hdr,
                        **_field_range(by_name[name], arrays[2]),
                    },
                )

        await self._pipeline_guard("preview_pipeline", body(), progress=True)

    async def _handle_run_pipeline(self, msg: dict[str, Any]) -> None:
        """Run a recipe and store ONE derived variable.

        ``{type: "run_pipeline", file_id?, recipe, all_sweeps?: true, sweep?}`` -> progress, then
        ``pipeline_result`` (stats, skipped sweeps, the derived entry) and ``derived_variables``.
        Sweeps lacking a role the recipe needs are skipped with a reason, as for modules.
        """

        async def body() -> None:
            all_sweeps = msg.get("all_sweeps", True)
            sweep = msg.get("sweep", 0)
            if not isinstance(all_sweeps, bool):
                raise plugins.ModuleError("run_pipeline: 'all_sweeps' must be a boolean")
            if isinstance(sweep, bool) or not isinstance(sweep, int) or sweep < 0:
                raise plugins.ModuleError("run_pipeline: 'sweep' must be a non-negative integer")
            recipe = pipeline.parse_recipe(msg.get("recipe"), strict_name=True)
            resolved_id, reader = _resolve_reader(msg.get("file_id"))
            datatree = reader.datatree
            if datatree is None:
                raise plugins.ModuleError("No file is open")
            n_sweeps = len(sorted_sweep_names(datatree))
            out_file_id = resolved_id or msg.get("file_id")
            await self.send_progress(2, f"{recipe.name}: starting")
            loop = asyncio.get_running_loop()
            progress_sync = self._progress_callback(loop)
            cancel = threading.Event()
            cancel_key = str(out_file_id)
            _pipeline_cancels()[cancel_key] = cancel

            def _sweep_moments(i: int) -> set[str]:
                data_vars = reader.get_sweep_dataset(i, mask=False).data_vars
                return {str(k) for k, v in data_vars.items() if v.ndim >= 2}

            def _run() -> tuple[Any, list[tuple[str, dict[int, np.ndarray], dict]]]:
                run = pipeline.run_recipe_all_sweeps(
                    recipe,
                    datatree,
                    n_sweeps,
                    get_sweep_dataset=lambda i: _sweep_dataset(reader, i),
                    get_sweep_variables=_sweep_moments,
                    progress=progress_sync,
                    is_cancelled=cancel.is_set,
                    sweeps=None if all_sweeps else [sweep],
                )
                return run, pipeline.build_variables(run)

            try:
                run, built = await loop.run_in_executor(None, _run)
            except BaseException:
                cancel.set()  # the worker stops after the step it is on
                raise
            finally:
                cancels = _pipeline_cancels()
                if cancels.get(cancel_key) is cancel:
                    del cancels[cancel_key]

            still_open = resolved_id is None or _readers().get(resolved_id) is reader
            if not still_open or reader.datatree is not datatree:
                raise plugins.ModuleError(
                    "The file was closed or changed while the pipeline ran; result discarded"
                )
            store = derived.store_for(reader)
            assert store is not None
            evicted: list[str] = []
            for name, arrays, meta in built:
                evicted.extend(store.put_variable(name, arrays, meta))
            names = {name for name, _, _ in built}
            evicted = [n for n in dict.fromkeys(evicted) if n not in names or not store.has(n)]
            listing = {v["name"]: v for v in store.variables()}
            entries = [
                {
                    "name": name,
                    "label": meta["label"],
                    "field": meta["field"],
                    "units": meta["units"],
                    "vmin": meta["vmin"],
                    "vmax": meta["vmax"],
                    "cmap": meta["cmap"],
                    "categories": None,
                    "sweeps": sorted(arrays),
                    "skipped": run.skipped,
                }
                for name, arrays, meta in built
                if name in listing
            ]
            await self.send(
                {
                    "type": "pipeline_result",
                    "file_id": out_file_id,
                    "recipe": recipe.name,
                    "target": recipe.target,
                    "name": built[0][0] if built else None,
                    "derived": entries,
                    "steps": run.steps,
                    "per_sweep": run.per_sweep,
                    "skipped": run.skipped,
                    "elapsed_ms": run.elapsed_ms,
                    "evicted": evicted,
                    "params": built[0][2]["params"] if built else None,
                }
            )
            await self._send_derived_list(out_file_id, reader, evicted=evicted)

        await self._pipeline_guard("run_pipeline", body(), progress=True)

    async def _handle_cancel_module(self, msg: dict[str, Any]) -> None:
        """``{type: "cancel_module", file_id?}``: stop the module run(s) of this connection.

        The waiting request ends at once with ``module_error`` (``cancelled: true``); a module
        that checks ``ctx.cancelled`` / calls ``ctx.progress`` also stops computing. Answer:
        ``{type: "cancel_requested", kind: "module", count}``.
        """
        file_id = msg.get("file_id")
        count = self._jobs.cancel("module", file_id if isinstance(file_id, str) else None)
        await self.send({"type": "cancel_requested", "kind": "module", "count": count})

    async def _handle_cancel_export(self, msg: dict[str, Any]) -> None:
        """``{type: "cancel_export", file_id?}``: stop the running export / batch / animation.

        The job stops at its next progress report (between stages, files or frames), removes
        its half-written file and answers ``{type: "export_cancelled"}``; files a batch had
        already finished stay. Answer to this message: ``cancel_requested`` (``kind: "export"``).
        """
        file_id = msg.get("file_id")
        count = self._jobs.cancel("export", file_id if isinstance(file_id, str) else None)
        await self.send({"type": "cancel_requested", "kind": "export", "count": count})

    async def _handle_cancel_pipeline(self, msg: dict[str, Any]) -> None:
        """``{type: "cancel_pipeline", file_id?}``: stop a running pipeline after its step."""
        resolved_id, _ = _resolve_reader(msg.get("file_id"))
        event = _pipeline_cancels().get(str(resolved_id or msg.get("file_id")))
        if event is not None:
            event.set()

    # ------------------------------------------------------------------
    # Text export (CSV / GeoJSON written by the UI)
    # ------------------------------------------------------------------

    async def _handle_save_text_file(self, msg: dict[str, Any]) -> None:
        """Write a UTF-8 text file chosen by the user (atomic; no silent overwrite).

        ``{type: "save_text_file", path, content, overwrite?}`` -> ``{type: "file_saved", path}``
        """
        path = msg.get("path")
        content = msg.get("content")
        if not isinstance(path, str) or not path.strip():
            await self.send_error("save_text_file: 'path' must be a non-empty string")
            return
        if not isinstance(content, str):
            await self.send_error("save_text_file: 'content' must be a string")
            return
        if "\x00" in path:
            await self.send_error("save_text_file: invalid path")
            return
        if len(content) > SAVE_TEXT_MAX_BYTES:
            await self.send_error(
                f"save_text_file: content exceeds {SAVE_TEXT_MAX_BYTES // (1024 * 1024)} MB"
            )
            return
        data = content.encode("utf-8")
        if len(data) > SAVE_TEXT_MAX_BYTES:
            await self.send_error(
                f"save_text_file: content exceeds {SAVE_TEXT_MAX_BYTES // (1024 * 1024)} MB"
            )
            return
        if self.session is not None:
            # Web mode: a bare file name inside the session; the reply carries a download URL.
            try:
                if len(data) > WEB_SAVE_TEXT_MAX_BYTES:
                    raise web.WebError(
                        f"content exceeds {WEB_SAVE_TEXT_MAX_BYTES // (1024 * 1024)} MB"
                    )
                self.session.check_quota(len(data))
                web_target = self.session.export_target(path, ext="txt", prefix="radarx_file_")
            except web.WebError as exc:
                await self.send_error(f"save_text_file: {exc}")
                return
            await asyncio.get_running_loop().run_in_executor(
                None, _atomic_write, web_target, lambda tmp: tmp.write_bytes(data)
            )
            await self.send({"type": "file_saved", **self.session.download_info(web_target)})
            return
        target = Path(path).expanduser()
        if not target.parent.is_dir():
            await self.send_error(f"save_text_file: folder does not exist: {target.parent}")
            return
        if target.is_dir():
            await self.send_error(f"save_text_file: path is a folder: {target}")
            return
        if target.exists() and msg.get("overwrite") is not True:
            await self.send_error(
                f"save_text_file: {target} already exists (pass overwrite=true to replace it)"
            )
            return

        await asyncio.get_running_loop().run_in_executor(
            None, _atomic_write, target, lambda tmp: tmp.write_bytes(data)
        )
        await self.send({"type": "file_saved", "path": str(target)})

    async def _handle_save_image_file(self, msg: dict[str, Any]) -> None:
        """Write a PNG image (base64) the user chose a path for (desktop only; atomic).

        ``{type: "save_image_file", path, data_base64, overwrite?}`` ->
        ``{type: "file_saved", path}``.
        The web service has no user-chosen paths: the browser saves the image itself.
        """
        path = msg.get("path")
        content = msg.get("data_base64")
        if self.session is not None:
            await self.send_error("save_image_file: not available in the web service")
            return
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            await self.send_error("save_image_file: 'path' must be a non-empty string")
            return
        if not isinstance(content, str):
            await self.send_error("save_image_file: 'data_base64' must be a string")
            return
        try:
            data = base64.b64decode(content, validate=True)
        except ValueError:
            await self.send_error("save_image_file: 'data_base64' is not valid base64")
            return
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            await self.send_error("save_image_file: only PNG images are supported")
            return
        if len(data) > SAVE_TEXT_MAX_BYTES:
            await self.send_error(
                f"save_image_file: image exceeds {SAVE_TEXT_MAX_BYTES // (1024 * 1024)} MB"
            )
            return
        target = Path(path).expanduser()
        if not target.parent.is_dir():
            await self.send_error(f"save_image_file: folder does not exist: {target.parent}")
            return
        if target.is_dir():
            await self.send_error(f"save_image_file: path is a folder: {target}")
            return
        if target.exists() and msg.get("overwrite") is not True:
            await self.send_error(
                f"save_image_file: {target} already exists (pass overwrite=true to replace it)"
            )
            return
        await asyncio.get_running_loop().run_in_executor(
            None, _atomic_write, target, lambda tmp: tmp.write_bytes(data)
        )
        await self.send({"type": "file_saved", "path": str(target)})

    # ------------------------------------------------------------------
    # Multi-radar mosaics (engine/mosaic.py, needs the optional radarx package)
    # ------------------------------------------------------------------

    async def _handle_create_mosaic(self, msg: dict[str, Any]) -> None:
        """Build a mosaic of the open files ``file_ids`` and stream it back.

        ``{type: "create_mosaic", request_id, file_ids: [...], variable, product, height_m?,
        threshold_dbz?, grid: {...}, merge: {...}, vmin?, vmax?}`` ->
        progress messages, then ``{type: "mosaic_result", request_id, mosaic_id, ...,
        png_bytes, chunks}`` followed by ``chunks`` binary frames with the PNG. Any failure:
        ``{type: "mosaic_error", request_id, message}``; progress always closes at 100.
        """
        request_id = msg.get("request_id")
        try:
            await self._create_mosaic(msg, request_id)
        except (mosaic.MosaicError, plugins.ModuleError) as exc:
            logger.warning("Mosaic error: %s", exc)
            await self._mosaic_error(request_id, str(exc))
        except UnknownFileError as exc:
            await self._mosaic_error(request_id, str(exc))
        except ImportError as exc:
            await self._mosaic_error(
                request_id,
                f"Multi-radar mosaics need the Python package radarx: {exc} (pip install radarx)",
            )
        except Exception as exc:
            logger.error("Mosaic failed: %s\n%s", exc, traceback.format_exc())
            await self._mosaic_error(request_id, f"{type(exc).__name__}: {exc}")
        finally:
            await self.send_progress(100, "Mosaic finished")

    async def _mosaic_error(self, request_id: Any, message: str) -> None:
        await self.send({"type": "mosaic_error", "request_id": request_id, "message": message})

    async def _create_mosaic(self, msg: dict[str, Any], request_id: Any) -> None:
        file_ids = msg.get("file_ids")
        if (
            not isinstance(file_ids, list)
            or not file_ids
            or not all(isinstance(f, str) and f for f in file_ids)
        ):
            raise mosaic.MosaicError("create_mosaic: 'file_ids' must be a non-empty list")
        if len(set(file_ids)) != len(file_ids):
            raise mosaic.MosaicError("create_mosaic: 'file_ids' contains duplicates")
        if len(file_ids) > 24:
            raise mosaic.MosaicError("create_mosaic: at most 24 radars per mosaic")
        variable = msg.get("variable", "DBZH")
        product = msg.get("product", "cappi")
        for key in ("grid", "merge"):
            if msg.get(key) is not None and not isinstance(msg[key], dict):
                raise mosaic.MosaicError(f"create_mosaic: '{key}' must be an object")
        pairs = []
        for fid in file_ids:
            _, reader = _resolve_reader(fid)  # UnknownFileError for closed files
            if reader.datatree is None:
                raise mosaic.MosaicError(f"File {fid} is not open")
            pairs.append((fid, reader))
        # Fail early (before the progress bar) when radarx is missing.
        mosaic._radarx()

        loop = asyncio.get_running_loop()
        progress_sync = self._progress_callback(loop)
        await self.send_progress(5, "Building mosaic...")

        def _build() -> mosaic.MosaicRecord:
            return mosaic.build_mosaic(
                pairs,
                variable,
                product,
                msg.get("grid"),
                msg.get("merge"),
                height_m=msg.get("height_m", 3000.0),
                threshold_dbz=msg.get("threshold_dbz", 18.0),
                vmin=msg.get("vmin"),
                vmax=msg.get("vmax"),
                min_rhohv=msg.get("min_rhohv"),
                progress=progress_sync,
                keep_volume=msg.get("keep_volume"),
                value_mask=msg.get("value_mask"),
            )

        t0 = time.perf_counter()
        # one heavy build at a time (radarx uses every core), even if this request is abandoned
        record = await _run_holding(_locks.get("mosaic"), _build)
        if self.session is not None:  # only the creator may probe / export this mosaic
            self.session.owned["mosaics"].add(record.mosaic_id)
        rec_header = record.header()
        header = {
            "type": "mosaic_result",
            "request_id": request_id,
            **rec_header,
            "provenance": layer_provenance.for_mosaic_header(rec_header),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }
        await self._send_binary_chunks([record.png], header, length_key="png_bytes")

    # ------------------------------------------------------------------
    # Autopilot: inspect / plan / run any set of inputs (engine/autopilot/ws.py)
    # ------------------------------------------------------------------

    async def _handle_autopilot(self, msg: dict[str, Any]) -> None:
        """The ``auto_*`` messages; the protocol is in ``engine/autopilot/ws.py``. A failure is an
        ``auto_error``; ``auto_inspect`` / ``auto_run`` always close progress."""
        from engine.autopilot import ws as auto_ws
        from engine.autopilot.inspect import AutopilotError

        kind = msg.get("type")
        request_id = msg.get("request_id")
        if kind == "auto_cancel":
            await self.send(
                {
                    "type": "auto_cancelled",
                    "request_id": request_id,
                    "found": auto_ws.cancel(request_id),
                }
            )
            return
        cancel = auto_ws.cancel_event(request_id)
        try:
            if web.is_web_mode():
                raise AutopilotError(
                    "Autopilot reads local folders: use the desktop app or rxd auto"
                )
            loop = asyncio.get_running_loop()
            progress_sync = self._progress_callback(loop)
            fn = {
                "auto_inspect": auto_ws.inspect,
                "auto_plan": auto_ws.plan,
                "auto_run": auto_ws.run,
            }[str(kind)]
            if kind != "auto_plan":
                await self.send_progress(2, "Autopilot...")
            out = await loop.run_in_executor(None, fn, msg, progress_sync, cancel.is_set)
            await self.send({"type": f"{kind}_result", "request_id": request_id, **out})
        except AutopilotError as exc:
            if "cancel" in str(exc):
                await self.send({"type": "auto_cancelled", "request_id": request_id})
            else:
                await self.send(
                    {
                        "type": "auto_error",
                        "request_id": request_id,
                        "kind": kind,
                        "message": str(exc),
                    }
                )
        except ImportError as exc:
            await self.send(
                {
                    "type": "auto_error",
                    "request_id": request_id,
                    "kind": kind,
                    "message": f"This step needs a Python package: {exc}",
                }
            )
        except Exception as exc:
            logger.error("Autopilot failed: %s\n%s", exc, traceback.format_exc())
            await self.send(
                {
                    "type": "auto_error",
                    "request_id": request_id,
                    "kind": kind,
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
        finally:
            auto_ws.forget(request_id)
            if kind != "auto_plan":
                await self.send_progress(100, "Autopilot finished")

    # ------------------------------------------------------------------
    # Hydrology: rain rate / accumulation / gauges / basins (engine/hydro/ws.py)
    # ------------------------------------------------------------------

    async def _handle_qpe(self, msg: dict[str, Any]) -> None:
        """The ``qpe_*`` messages (info, run, layer, adjust, series, export); the protocol is in
        ``engine/hydro/ws.py``. A failure is a ``qpe_error``; ``qpe_run`` always closes progress."""
        from engine.hydro import ws as qpe_ws
        from engine.hydro.qpe import QpeError

        kind = msg.get("type")
        request_id = msg.get("request_id")
        try:
            if web.is_web_mode():
                raise QpeError(
                    "The rainfall tools read local gauge and basin files: use the desktop app"
                )
            loop = asyncio.get_running_loop()
            if kind == "qpe_run":
                file_ids = msg.get("file_ids")
                if (
                    not isinstance(file_ids, list)
                    or not file_ids
                    or not all(isinstance(f, str) and f for f in file_ids)
                ):
                    raise QpeError("qpe_run: 'file_ids' must be a non-empty list of open files")
                if len(set(file_ids)) != len(file_ids):
                    raise QpeError("qpe_run: 'file_ids' contains duplicates")
                readers = []
                for fid in file_ids:
                    _, reader = _resolve_reader(fid)
                    if reader.datatree is None:
                        raise QpeError(f"File {fid} is not open")
                    readers.append(reader)
                progress_sync = self._progress_callback(loop)
                await self.send_progress(3, "Rainfall estimate...")
                out = await _run_holding(
                    _locks.get("mosaic"), lambda: qpe_ws.run(msg, readers, progress_sync)
                )
                await self.send({"type": "qpe_result", "request_id": request_id, **out})
            elif kind in ("qpe_info", "qpe_layer", "qpe_adjust", "qpe_series"):
                fn = {
                    "qpe_info": qpe_ws.info,
                    "qpe_layer": qpe_ws.layer,
                    "qpe_adjust": qpe_ws.adjust,
                    "qpe_series": qpe_ws.series,
                }[kind]
                out = await loop.run_in_executor(None, fn, msg)
                await self.send({"type": f"{kind}_result", "request_id": request_id, **out})
            elif kind == "qpe_export":
                path = await loop.run_in_executor(None, qpe_ws.export, msg)
                await self.send({"type": "file_saved", "path": str(path)})
            else:  # pragma: no cover - the dispatch table only routes the types above
                raise QpeError(f"unknown QPE message {kind!r}")
        except (QpeError, plugins.ModuleError, UnknownFileError) as exc:
            logger.warning("QPE error: %s", exc)
            await self.send(
                {"type": "qpe_error", "request_id": request_id, "kind": kind, "message": str(exc)}
            )
        except ImportError as exc:
            await self.send(
                {
                    "type": "qpe_error",
                    "request_id": request_id,
                    "kind": kind,
                    "message": f"The rainfall tools need the Python package radarx: {exc} "
                    "(pip install radarx)",
                }
            )
        except Exception as exc:
            logger.error("QPE failed: %s\n%s", exc, traceback.format_exc())
            await self.send(
                {
                    "type": "qpe_error",
                    "request_id": request_id,
                    "kind": kind,
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
        finally:
            if kind == "qpe_run":
                await self.send_progress(100, "Rainfall estimate finished")

    # ------------------------------------------------------------------
    # Nowcast: advection extrapolation with ensemble (engine/nowcast/ws.py)
    # ------------------------------------------------------------------

    async def _handle_nowcast(self, msg: dict[str, Any]) -> None:
        """The ``nowcast_*`` messages (run, cancel, export); the protocol is in
        ``engine/nowcast/ws.py``. A failure is a ``nowcast_error``; ``nowcast_run`` always closes
        progress. One heavy run at a time (the mosaic lock: the gridding uses radarx)."""
        from engine.nowcast import ws as nowcast_ws
        from engine.nowcast.run import NowcastCancelled, NowcastError

        kind = msg.get("type")
        request_id = msg.get("request_id")
        key = request_id if isinstance(request_id, str) and request_id else None
        if kind == "nowcast_cancel":
            if key is not None:
                _nowcast_cancel_event(key).set()
            return
        cancel = _nowcast_cancel_event(key)
        try:
            loop = asyncio.get_running_loop()
            if kind == "nowcast_run":
                readers: list[tuple[str, Any]] = []
                records: list[Any] = []
                file_ids = msg.get("file_ids")
                mosaic_ids = msg.get("mosaic_ids")
                frames_path = msg.get("frames_path")
                if sum(x is not None for x in (file_ids, mosaic_ids, frames_path)) != 1:
                    raise NowcastError(
                        "nowcast_run: give exactly one of 'file_ids', 'mosaic_ids' or 'frames_path'"
                    )
                if file_ids is not None:
                    if (
                        not isinstance(file_ids, list)
                        or not file_ids
                        or not all(isinstance(f, str) and f for f in file_ids)
                    ):
                        raise NowcastError(
                            "nowcast_run: 'file_ids' must be a non-empty list of open files"
                        )
                    if len(set(file_ids)) != len(file_ids):
                        raise NowcastError("nowcast_run: 'file_ids' contains duplicates")
                    for fid in file_ids:
                        _, reader = _resolve_reader(fid)
                        if reader.datatree is None:
                            raise NowcastError(f"File {fid} is not open")
                        readers.append((str(getattr(reader, "_path", None) or fid), reader))
                elif mosaic_ids is not None:
                    if (
                        not isinstance(mosaic_ids, list)
                        or not mosaic_ids
                        or not all(isinstance(f, str) and f for f in mosaic_ids)
                        or len(set(mosaic_ids)) != len(mosaic_ids)
                    ):
                        raise NowcastError(
                            "nowcast_run: 'mosaic_ids' must be a non-empty list of distinct ids"
                        )
                    records = [mosaic.MOSAICS.get(mid) for mid in mosaic_ids]
                elif web.is_web_mode():
                    raise NowcastError("A gridded frames file is read in the desktop app")
                progress_sync = self._progress_callback(loop)
                await self.send_progress(3, "Nowcast...")

                def _run() -> dict[str, Any]:
                    if cancel.is_set():
                        raise NowcastCancelled("cancelled")
                    return nowcast_ws.run(msg, readers, progress_sync, cancel.is_set, records)

                try:
                    out = await _run_holding(_locks.get("mosaic"), _run)
                except BaseException:
                    cancel.set()
                    raise
                await self.send({"type": "nowcast_result", "request_id": request_id, **out})
            elif kind == "nowcast_layers":
                out = await loop.run_in_executor(None, nowcast_ws.layers, msg)
                await self.send({"type": "nowcast_layers_result", "request_id": request_id, **out})
            elif kind == "nowcast_export":
                if web.is_web_mode():
                    raise NowcastError("Saving a nowcast to a file is part of the desktop app")
                path = await loop.run_in_executor(None, nowcast_ws.export, msg)
                await self.send({"type": "file_saved", "path": str(path)})
            else:  # pragma: no cover - the dispatch table only routes the types above
                raise NowcastError(f"unknown nowcast message {kind!r}")
        except NowcastCancelled:
            await self.send({"type": "nowcast_cancelled", "request_id": request_id})
        except (NowcastError, mosaic.MosaicError, UnknownFileError) as exc:
            logger.warning("Nowcast error: %s", exc)
            await self.send(
                {
                    "type": "nowcast_error",
                    "request_id": request_id,
                    "kind": kind,
                    "message": str(exc),
                }
            )
        except ImportError as exc:
            await self.send(
                {
                    "type": "nowcast_error",
                    "request_id": request_id,
                    "kind": kind,
                    "message": f"The nowcast grids the volumes with radarx: {exc} "
                    "(pip install radarx)",
                }
            )
        except Exception as exc:
            logger.error("Nowcast failed: %s\n%s", exc, traceback.format_exc())
            await self.send(
                {
                    "type": "nowcast_error",
                    "request_id": request_id,
                    "kind": kind,
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
        finally:
            if key is not None:
                _nowcast_runs().pop(key, None)
            if kind == "nowcast_run":
                await self.send_progress(100, "Nowcast finished")

    async def _handle_mosaic_probe(self, msg: dict[str, Any]) -> None:
        """``{type: "mosaic_probe", mosaic_id, lon, lat}`` -> ``mosaic_probe_result``."""
        mosaic_id = msg.get("mosaic_id")
        try:
            if not isinstance(mosaic_id, str) or not mosaic_id:
                raise mosaic.MosaicError("mosaic_probe: missing 'mosaic_id'")
            self._web_own_mosaic(mosaic_id)
            result = await asyncio.get_running_loop().run_in_executor(
                None, mosaic.probe, mosaic_id, msg.get("lon"), msg.get("lat")
            )
        except mosaic.MosaicError as exc:
            await self.send(
                {
                    "type": "mosaic_error",
                    "request_id": msg.get("request_id"),
                    "mosaic_id": mosaic_id,
                    "message": str(exc),
                }
            )
            return
        await self.send({"type": "mosaic_probe_result", **result})

    async def _handle_export_mosaic(self, msg: dict[str, Any]) -> None:
        """``{type: "export_mosaic", mosaic_id, format: "netcdf"|"png", path, overwrite?}``.

        Success: ``{type: "file_saved", path}``; failure: ``mosaic_error``. The PNG export also
        writes ``<name>.pgw`` (world file) and ``<name>.prj`` (EPSG:3857) next to it.
        """
        mosaic_id = msg.get("mosaic_id")
        try:
            path = await self._export_mosaic(msg)
        except mosaic.MosaicError as exc:
            await self.send(
                {
                    "type": "mosaic_error",
                    "request_id": msg.get("request_id"),
                    "mosaic_id": mosaic_id,
                    "message": str(exc),
                }
            )
            return
        except OSError as exc:
            await self.send(
                {
                    "type": "mosaic_error",
                    "request_id": msg.get("request_id"),
                    "mosaic_id": mosaic_id,
                    "message": f"export_mosaic: could not write the file: {exc}",
                }
            )
            return
        if self.session is not None:
            await self.send({"type": "file_saved", **self.session.download_info(path)})
            return
        await self.send({"type": "file_saved", "path": str(path)})

    def _web_own_mosaic(self, mosaic_id: str) -> None:
        if self.session is not None and mosaic_id not in self.session.owned["mosaics"]:
            raise mosaic.MosaicError(f"Unknown or expired mosaic '{mosaic_id}'")

    def _web_save_name(self, requested: Any, label: str, ext: str = "nc") -> str:
        """Web mode: turn a requested bare file name into a path in the session folder."""
        assert self.session is not None
        try:
            self.session.check_quota()
            return str(self.session.export_target(requested, ext=ext))
        except web.WebError as exc:
            raise (
                mosaic.MosaicError(f"{label}: {exc}")
                if label == "export_mosaic"
                else mdop.MultiDopplerError(f"{label}: {exc}")
            ) from exc

    async def _export_mosaic(self, msg: dict[str, Any]) -> Path:
        mosaic_id = msg.get("mosaic_id")
        fmt = msg.get("format", "netcdf")
        path = msg.get("path")
        if not isinstance(mosaic_id, str) or not mosaic_id:
            raise mosaic.MosaicError("export_mosaic: missing 'mosaic_id'")
        if fmt not in ("netcdf", "png"):
            raise mosaic.MosaicError("export_mosaic: 'format' must be 'netcdf' or 'png'")
        if self.session is not None:
            path = self._web_save_name(path, "export_mosaic", "png" if fmt == "png" else "nc")
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            raise mosaic.MosaicError("export_mosaic: 'path' must be a non-empty string")
        self._web_own_mosaic(mosaic_id)
        record = mosaic.MOSAICS.get(mosaic_id)
        target = Path(path).expanduser()
        if not target.parent.is_dir():
            raise mosaic.MosaicError(f"export_mosaic: folder does not exist: {target.parent}")
        if target.is_dir():
            raise mosaic.MosaicError(f"export_mosaic: path is a folder: {target}")
        sidecars = [target.with_suffix(".pgw"), target.with_suffix(".prj")] if fmt == "png" else []
        overwrite = msg.get("overwrite") is True or self.session is not None
        for existing in [target, *sidecars]:
            if existing.exists() and not overwrite:
                raise mosaic.MosaicError(
                    f"export_mosaic: {existing} already exists (pass overwrite=true to replace it)"
                )
        loop = asyncio.get_running_loop()
        if fmt == "netcdf":
            await loop.run_in_executor(
                None, _atomic_write, target, lambda tmp: mosaic.write_netcdf(record, tmp)
            )
        else:
            await loop.run_in_executor(
                None, _atomic_write, target, lambda tmp: tmp.write_bytes(mosaic.png_bytes(record))
            )
            await loop.run_in_executor(
                None,
                _atomic_write,
                sidecars[0],
                lambda tmp: tmp.write_text(mosaic.world_file(record), encoding="ascii"),
            )
            await loop.run_in_executor(
                None,
                _atomic_write,
                sidecars[1],
                lambda tmp: tmp.write_text(mosaic.projection_wkt(), encoding="ascii"),
            )
        return target

    # ==================================================================
    # BEGIN 3-D volumes for the 3-D view (merged mosaic field, wind grids)
    # ==================================================================

    async def _handle_get_mosaic_volume(self, msg: dict[str, Any]) -> None:
        """``{type: "get_mosaic_volume", request_id, mosaic_id}`` -> ``mosaic_volume`` + frames.

        The header (grid geometry, levels, value range, radar sites, ``byte_length``, ``chunks``)
        is followed by one uint8 array ``(nz, ny, nx)`` (x fastest; 0 = no echo). Failure:
        ``mosaic_error`` (with ``request_id``).
        """
        mosaic_id = msg.get("mosaic_id")
        try:
            if not isinstance(mosaic_id, str) or not mosaic_id:
                raise mosaic.MosaicError("get_mosaic_volume: missing 'mosaic_id'")
            self._web_own_mosaic(mosaic_id)
            header, volume = await asyncio.get_running_loop().run_in_executor(
                None, mosaic.volume_message, mosaic_id
            )
        except mosaic.MosaicError as exc:
            await self.send(
                {
                    "type": "mosaic_error",
                    "request_id": msg.get("request_id"),
                    "mosaic_id": mosaic_id,
                    "message": str(exc),
                    "for": "volume",
                }
            )
            return
        await self._send_binary_chunks(
            [volume],
            {"type": "mosaic_volume", "request_id": msg.get("request_id"), **header},
            length_key="byte_length",
        )

    # Fields of a wind analysis the 3-D view needs (name order of the binary block).
    _WIND_VOLUME_FIELDS = (
        "u",
        "v",
        "w",
        "speed",
        "dbz",
        "crossing_angle",
        "uncertainty",
        "n_radars",
        "divergence",
    )
    # What the 3-D view shows next to the flow (QC limits, coverage, ...): copied from the header.
    _WIND_VOLUME_SUMMARY = (
        "method",
        "boundary",
        "coverage_pct",
        "dual_coverage_pct",
        "w_valid_pct",
        "valid_cells",
        "total_cells",
        "min_crossing_angle",
        "qc",
        "notes",
        "has_uncertainty",
    )

    async def _handle_get_wind_volume(self, msg: dict[str, Any]) -> None:
        """``{type: "get_wind_volume", request_id, wind_id}`` -> ``wind_volume`` + frames.

        Header: ``wind_id``, ``fields`` (name, units), ``shape`` = ``[n_fields, nz, ny, nx]``,
        ``grid`` (as in ``multidoppler_result``), ``radars`` (with grid x_m / y_m), ``byte_length``
        and ``chunks``; then one float32 block of ``shape`` (C order, NaN = no wind). Failure:
        ``multidoppler_error`` (with ``request_id``).
        """
        wind_id = msg.get("wind_id")
        try:
            if not isinstance(wind_id, str) or not wind_id:
                raise mdop.MultiDopplerError("get_wind_volume: missing 'wind_id'")
            if self.session is not None and wind_id not in self.session.owned["winds"]:
                raise mdop.MultiDopplerError(f"Unknown or expired wind analysis '{wind_id}'")
            record = mdop.WIND_RESULTS.get(wind_id)
            names = [n for n in self._WIND_VOLUME_FIELDS if n in mdop.FIELD_ORDER]
            full = record.header()
            info = {f["name"]: f for f in full["fields"]}
            block = np.ascontiguousarray(
                np.stack([record.fields[mdop.FIELD_ORDER.index(n)] for n in names])
            )
            proj = mdop._aeqd(*record.origin)
            radars = []
            for r in record.radars:
                row = dict(r)
                lat, lon = r.get("latitude"), r.get("longitude")
                if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                    x, y = proj(lon, lat)
                    row["x_m"], row["y_m"] = float(x), float(y)
                radars.append(row)
        except mdop.MultiDopplerError as exc:
            await self._multidoppler_error(msg.get("request_id"), str(exc), wind_id=wind_id)
            return
        header = {
            "type": "wind_volume",
            "request_id": msg.get("request_id"),
            "wind_id": wind_id,
            "fields": [info[n] for n in names],
            "shape": [len(names), *block.shape[1:]],
            "grid": full["grid"],
            "radars": radars,
            "analysis_time": full.get("analysis_time"),
            "summary": {k: full[k] for k in self._WIND_VOLUME_SUMMARY if k in full},
            "provenance": layer_provenance.for_winds_header(full),
        }
        await self._send_binary_chunks([block], header, length_key="byte_length")

    # ==================================================================
    # END 3-D volumes for the 3-D view
    # ==================================================================

    # ==================================================================
    # BEGIN multi-Doppler wind retrieval (engine/multidoppler.py, needs radarx)
    # ==================================================================

    async def _handle_run_multidoppler(self, msg: dict[str, Any]) -> None:
        """Retrieve u / v / w from two or more open radar files and stream the grids back.

        ``{type: "run_multidoppler", request_id, file_ids: [...], grid: {...}, options: {...}}``
        -> progress messages, then ``{type: "multidoppler_result", request_id, wind_id, fields,
        shape, grid, radars, coverage_pct, crossing_angle, background, ..., byte_length, chunks}``
        followed by ``chunks`` binary frames holding a float32 array of ``shape`` =
        ``[n_fields, nz, ny, nx]`` (C order). Failure: ``{type: "multidoppler_error",
        request_id, message}``; ``multidoppler_cancel`` -> ``{type: "multidoppler_cancelled"}``.
        Progress always closes at 100.
        """
        request_id = msg.get("request_id")
        key = request_id if isinstance(request_id, str) and request_id else None
        cancel = _multidoppler_cancel_event(key)
        try:
            await self._run_multidoppler(msg, request_id, cancel)
        except mdop.MultiDopplerCancelledError:
            await self.send({"type": "multidoppler_cancelled", "request_id": request_id})
        except (mdop.MultiDopplerError, UnknownFileError) as exc:
            logger.warning("Multi-Doppler error: %s", exc)
            await self._multidoppler_error(request_id, str(exc))
        except ImportError as exc:
            await self._multidoppler_error(
                request_id,
                f"Multi-Doppler wind retrieval needs the Python package radarx: {exc} "
                "(pip install radarx)",
            )
        except Exception as exc:
            logger.error("Multi-Doppler failed: %s\n%s", exc, traceback.format_exc())
            await self._multidoppler_error(request_id, f"{type(exc).__name__}: {exc}")
        finally:
            if key is not None:
                _mdop_runs().pop(key, None)
            await self.send_progress(100, "Wind retrieval finished")

    async def _multidoppler_error(self, request_id: Any, message: str, **extra: Any) -> None:
        await self.send(
            {"type": "multidoppler_error", "request_id": request_id, "message": message, **extra}
        )

    async def _run_multidoppler(
        self, msg: dict[str, Any], request_id: Any, cancel: threading.Event
    ) -> None:
        file_ids = msg.get("file_ids")
        if (
            not isinstance(file_ids, list)
            or not file_ids
            or not all(isinstance(f, str) and f for f in file_ids)
        ):
            raise mdop.MultiDopplerError("run_multidoppler: 'file_ids' must be a non-empty list")
        if len(set(file_ids)) != len(file_ids):
            raise mdop.MultiDopplerError("run_multidoppler: 'file_ids' contains duplicates")
        for key in ("grid", "options"):
            if msg.get(key) is not None and not isinstance(msg[key], dict):
                raise mdop.MultiDopplerError(f"run_multidoppler: '{key}' must be an object")
        pairs = []
        for fid in file_ids:
            _, reader = _resolve_reader(fid)  # UnknownFileError for closed files
            if reader.datatree is None:
                raise mdop.MultiDopplerError(f"File {fid} is not open")
            pairs.append((fid, reader))
        # Fail early (before any heavy work) on bad parameters or a missing radarx.
        grid = mdop.GridSpec.from_dict(msg.get("grid"))
        options = mdop.Options.from_dict(msg.get("options"))
        if len(pairs) >= 2:
            mdop._radarx_mod()

        loop = asyncio.get_running_loop()
        progress_sync = self._progress_callback(loop)
        await self.send_progress(3, "Starting wind retrieval...")

        def _build() -> mdop.WindRecord:
            return mdop.run_multidoppler(
                pairs, grid, options, progress=progress_sync, cancel=cancel.is_set
            )

        def _build_unless_cancelled() -> mdop.WindRecord:
            if cancel.is_set():  # cancelled while queued behind another run
                raise mdop.MultiDopplerCancelledError("cancelled")
            return _build()

        t0 = time.perf_counter()
        try:
            # one heavy run at a time (radarx is threaded), even if this request is abandoned
            record = await _run_holding(_locks.get("multidoppler"), _build_unless_cancelled)
        except BaseException:
            cancel.set()  # connection gone / task cancelled: the worker stops at its next check
            raise
        if self.session is not None:  # only the creator may export this wind analysis
            self.session.owned["winds"].add(record.wind_id)
        rec_header = record.header()
        header = {
            "type": "multidoppler_result",
            "request_id": request_id,
            **rec_header,
            "provenance": layer_provenance.for_winds_header(rec_header),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        }
        await self._send_binary_chunks([record.fields], header, length_key="byte_length")

    async def _handle_multidoppler_cancel(self, msg: dict[str, Any]) -> None:
        """``{type: "multidoppler_cancel", request_id}``: stop that run at its next stage."""
        request_id = msg.get("request_id")
        if isinstance(request_id, str) and request_id:
            _multidoppler_cancel_event(request_id).set()

    # ------------------------------------------------------------------
    # Command Studio: every rxd command as a form (engine/cli_schema.py)
    # ------------------------------------------------------------------

    async def _handle_cli_describe(self, msg: dict[str, Any]) -> None:
        """``{type: "cli_describe", request_id}`` -> ``{type: "cli_schema", request_id, ...}``."""
        from engine import cli_schema

        schema = await asyncio.get_running_loop().run_in_executor(None, cli_schema.describe)
        await self.send({"type": "cli_schema", "request_id": msg.get("request_id"), **schema})

    async def _handle_cli_run(self, msg: dict[str, Any]) -> None:
        """``{type: "cli_run", request_id, argv: [...], cwd?}`` -> progress, then ``cli_result``.

        The command runs in the engine (one at a time, the ``cli`` lock); a failure of any kind
        is a ``cli_result`` with a friendly message, never an error frame or a traceback.
        """
        from engine import cli_schema

        request_id = msg.get("request_id")
        argv = msg.get("argv")
        reply: dict[str, Any] = {"type": "cli_result", "request_id": request_id}
        if self.session is not None:  # web mode: the engine's files are not the visitor's
            text = "Command Studio runs commands on this computer: it is part of the desktop app."
            await self.send({**reply, **cli_schema.refused(text)})
            return
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            text = "cli_run needs 'argv', a list of words."
            await self.send({**reply, **cli_schema.refused(text)})
            return
        cwd = msg.get("cwd") if isinstance(msg.get("cwd"), str) else None
        cancel = threading.Event()
        key = str(request_id or "")
        _CLI_CANCELS[key] = cancel
        loop = asyncio.get_running_loop()
        progress_sync = self._progress_callback(loop)
        await self.send_progress(2, f"rxd {argv[0] if argv else ''}: starting")
        try:
            result = await _run_holding(
                _locks.get("cli"),
                lambda: cli_schema.run_command(
                    argv, progress=progress_sync, is_cancelled=cancel.is_set, cwd=cwd
                ),
            )
        except BaseException:
            cancel.set()  # the worker stops at its next progress tick
            raise
        finally:
            if _CLI_CANCELS.get(key) is cancel:
                del _CLI_CANCELS[key]
            await self.send_progress(100, "Command finished")
        await self.send({**reply, **result})

    async def _handle_cli_cancel(self, msg: dict[str, Any]) -> None:
        """``{type: "cli_cancel", request_id}``: stop that command at its next progress tick."""
        event = _CLI_CANCELS.get(str(msg.get("request_id") or ""))
        if event is not None:
            event.set()

    async def _handle_export_multidoppler(self, msg: dict[str, Any]) -> None:
        """``{type: "export_multidoppler", wind_id, path, overwrite?}`` -> ``file_saved``."""
        wind_id = msg.get("wind_id")
        path = msg.get("path")
        try:
            if not isinstance(wind_id, str) or not wind_id:
                raise mdop.MultiDopplerError("export_multidoppler: missing 'wind_id'")
            if not isinstance(path, str) or not path.strip() or "\x00" in path:
                raise mdop.MultiDopplerError(
                    "export_multidoppler: 'path' must be a non-empty string"
                )
            if self.session is not None:
                if wind_id not in self.session.owned["winds"]:
                    raise mdop.MultiDopplerError(f"Unknown or expired wind analysis '{wind_id}'")
                path = self._web_save_name(path, "export_multidoppler")
            record = mdop.WIND_RESULTS.get(wind_id)
            target = Path(path).expanduser()
            if not target.parent.is_dir():
                raise mdop.MultiDopplerError(
                    f"export_multidoppler: folder does not exist: {target.parent}"
                )
            if target.is_dir():
                raise mdop.MultiDopplerError(f"export_multidoppler: path is a folder: {target}")
            if target.exists() and msg.get("overwrite") is not True and self.session is None:
                raise mdop.MultiDopplerError(
                    f"export_multidoppler: {target} already exists "
                    "(pass overwrite=true to replace it)"
                )
            await asyncio.get_running_loop().run_in_executor(
                None, _atomic_write, target, lambda tmp: mdop.write_netcdf(record, tmp)
            )
        except mdop.MultiDopplerError as exc:
            await self._multidoppler_error(msg.get("request_id"), str(exc), wind_id=wind_id)
            return
        except OSError as exc:
            await self._multidoppler_error(
                msg.get("request_id"),
                f"export_multidoppler: could not write the file: {exc}",
                wind_id=wind_id,
            )
            return
        if self.session is not None:
            await self.send({"type": "file_saved", **self.session.download_info(target)})
            return
        await self.send({"type": "file_saved", "path": str(target)})

    # ==================================================================
    # END multi-Doppler wind retrieval
    # ==================================================================


#: Cancel flags of running / about-to-run ``run_multidoppler`` requests, by request_id.
_multidoppler_runs: dict[str, threading.Event] = {}
_MULTIDOPPLER_MAX_PENDING = 64


def _multidoppler_cancel_event(request_id: str | None) -> threading.Event:
    """The cancel flag of a request (created on demand, so a cancel may arrive first)."""
    if request_id is None:
        return threading.Event()
    runs = _mdop_runs()
    event = runs.get(request_id)
    if event is None:
        while len(runs) >= _MULTIDOPPLER_MAX_PENDING:
            runs.pop(next(iter(runs)))
        event = runs[request_id] = threading.Event()
    return event


# request_id -> cancel flag of a nowcast run (the same on-demand pattern as multi-Doppler)
_nowcast_runs_table: dict[str, threading.Event] = {}


def _nowcast_runs() -> dict[str, threading.Event]:
    return _nowcast_runs_table


def _nowcast_cancel_event(request_id: str | None) -> threading.Event:
    if request_id is None:
        return threading.Event()
    runs = _nowcast_runs()
    event = runs.get(request_id)
    if event is None:
        while len(runs) >= _MULTIDOPPLER_MAX_PENDING:
            runs.pop(next(iter(runs)))
        event = runs[request_id] = threading.Event()
    return event


def _atomic_write(target: Path, write: Callable[[Path], Any]) -> None:
    """Write ``target`` atomically: ``write(tmp)`` fills a sibling temp file that replaces it."""
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        write(tmp)
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _module_overrides(reader: RadarReader, msg: dict[str, Any]):
    """Roles a module run should read from derived variables (``inputs`` of ``run_module``).

    ``{"inputs": {"velocity": "pipeline:my_recipe_vradh"}}`` makes the module see that derived
    variable wherever it asks for the velocity. See docs/PIPELINES.md.
    """
    return pipeline.module_overrides(derived.store_for(reader, create=False), msg.get("inputs"))


def _pack_module_fields(
    module_id: str, fields: dict[str, Any], sweep_ds: Any
) -> list[tuple[dict[str, Any], list[np.ndarray]]]:
    """Validate module fields against the sweep grid and pack them for the wire.

    Returns one ``(header_fields, [azimuth_f32, range_f32, values_f32])`` per field, in
    ``fields`` order. Raises :class:`plugins.ModuleError` naming the module on a mismatch.
    """
    azimuth = np.ascontiguousarray(sweep_ds["azimuth"].values, dtype=np.float32)
    range_m = np.ascontiguousarray(sweep_ds["range"].values, dtype=np.float32)
    expected = (azimuth.size, range_m.size)
    packed: list[tuple[dict[str, Any], list[np.ndarray]]] = []
    for name, arr in fields.items():
        values = np.asarray(arr)
        if values.shape != expected:
            raise plugins.ModuleError(
                f"Module '{module_id}' field '{name}' has shape {tuple(values.shape)} but the "
                f"sweep grid is (n_azimuth, n_range) = {expected}"
            )
        values = np.array(values, dtype=np.float32, order="C", copy=True)
        np.nan_to_num(
            values, copy=False, nan=NODATA_SENTINEL, posinf=NODATA_SENTINEL, neginf=NODATA_SENTINEL
        )
        header = {
            "n_az": int(azimuth.size),
            "n_range": int(range_m.size),
            "azimuth_bytes": azimuth.nbytes,
            "range_bytes": range_m.nbytes,
            "values_bytes": values.nbytes,
            "max_range": float(range_m.max()) if range_m.size else 0.0,
        }
        packed.append((header, [azimuth, range_m, values]))
    return packed


def _field_range(block: dict[str, Any], values: np.ndarray) -> dict[str, float]:
    """vmin/vmax for the header: the block's, else the observed range (categorical fields)."""
    vmin, vmax = block.get("vmin"), block.get("vmax")
    if vmin is None or vmax is None:
        valid = values[values != NODATA_SENTINEL]
        lo = float(valid.min()) if valid.size else 0.0
        hi = float(valid.max()) if valid.size else 1.0
        vmin = lo if vmin is None else vmin
        vmax = hi if vmax is None else vmax
    if vmax <= vmin:
        vmax = vmin + 1.0
    return {"vmin": float(vmin), "vmax": float(vmax)}


#: Requests that only make sense for PPI sweeps (rays that differ in azimuth): message type ->
#: what the user asked for. A range-height (RHI) scan is refused with a clear message instead of
#: producing nonsense (or an obscure error) from code that assumes a polar map.
_PPI_ONLY_FEATURES: dict[str, str] = {
    "get_volume": "3D volume",
    "get_cross_section_3d": "3D cross-section",
    "run_module": "Running a module",
    "create_mosaic": "Building a mosaic",
    "run_multidoppler": "Wind retrieval",
    "identify_cells": "Storm cell identification",
    "track_cells": "Storm cell tracking",
}


def _ppi_only_block(msg_type: Any, msg: dict[str, Any]) -> str | None:
    """Why *msg* cannot run on the targeted RHI data, or ``None`` when it can."""
    feature = _PPI_ONLY_FEATURES.get(msg_type) if isinstance(msg_type, str) else None
    if feature is None:
        return None
    ids = msg.get("file_ids") if isinstance(msg.get("file_ids"), list) else [msg.get("file_id")]
    for fid in ids:
        try:
            _, reader = _resolve_reader(fid if isinstance(fid, str) else None)
            kinds = reader.sweep_kinds()
        except Exception:  # unknown file etc.: the handler reports that itself
            continue
        if not kinds:
            continue
        sweep = msg.get("sweep")
        if msg_type in ("run_module", "identify_cells", "track_cells"):
            # per-sweep requests: only the targeted sweep matters
            is_index = isinstance(sweep, int) and not isinstance(sweep, bool)
            if is_index and 0 <= sweep < len(kinds) and kinds[sweep] == "rhi":
                return f"{feature} needs PPI sweeps: sweep {sweep} is a range-height (RHI) scan."
            continue
        if "ppi" not in kinds:
            return f"{feature} needs PPI sweeps: this file only holds range-height (RHI) scans."
    return None


def _rhi_rays_of(reader: RadarReader, sweep: int) -> rhi.RhiRays | None:
    """Ray order of *sweep* when it is an RHI (``None`` for PPIs, and when it cannot be told)."""
    try:
        return rhi.rhi_rays(reader.get_sweep_dataset(sweep, mask=False))
    except Exception as exc:
        logger.debug("RHI detection failed for sweep %s: %s", sweep, exc)
        return None


def _derived_sweep_data(
    reader: RadarReader, sweep: int, variable: str
) -> tuple[dict[str, Any], list[np.ndarray]] | None:
    """Wire-format payload of a derived variable on *sweep*; ``None`` if *variable* is not one.

    Same frames as a normal variable; the header additionally carries ``derived: true`` and the
    variable's label, categories, colormap, units and the one colour range shared by all sweeps.
    """
    store = derived.store_for(reader, create=False)
    if store is None or not store.has(variable):
        return None
    ds = reader.get_sweep_dataset(sweep, mask=False)
    got = store.get(sweep, variable)
    if got is None:
        available = sorted({str(v) for v in ds.data_vars} | set(store.names_on_sweep(sweep)))
        raise KeyError(f"Variable '{variable}' not in sweep {sweep}. Available: {available}")
    values, meta = got
    azimuth, range_m = ds["azimuth"].values, ds["range"].values
    if values.shape != (azimuth.size, range_m.size):
        raise ValueError(f"Derived variable '{variable}' no longer matches sweep {sweep}'s grid")
    import xarray as xr

    da = xr.DataArray(
        values,
        dims=("azimuth", "range"),
        coords={"azimuth": azimuth, "range": range_m},
        attrs={"units": meta.get("units", "")},
    )
    fields, arrays = _pack_field(da, variable, rays=rhi.rhi_rays(ds))
    fields["provenance"] = layer_provenance.from_derived_info(
        meta, layer_provenance.reader_context(reader)["scan_time"]
    ).to_dict()
    fields.update(
        derived=True,
        label=meta.get("label", variable),
        categories=meta.get("categories"),
        cmap=meta.get("cmap"),
        units=meta.get("units", ""),
    )
    if meta.get("vmin") is not None and meta.get("vmax") is not None:
        fields.update(vmin=float(meta["vmin"]), vmax=float(meta["vmax"]))
    return fields, arrays


def _volume_result_message(
    run: derived.VolumeRun,
    built: list[tuple[str, dict[int, np.ndarray], dict[str, Any]]],
    store: derived.DerivedStore,
    evicted: list[str],
    file_id: str | None,
) -> dict[str, Any]:
    """The ``module_volume_result`` message for a finished all-sweeps run."""
    listing = {v["name"]: v for v in store.variables()}
    entries = []
    for name, arrays, meta in built:
        if name not in listing:  # evicted again by a later variable of the same run
            continue
        made = set(arrays)
        skipped = list(run.skipped) + [
            {"sweep": s, "reason": f"module returned no '{meta['field']}' field on this sweep"}
            for s in sorted(set(run.per_sweep_ms) - made)
        ]
        entries.append(
            {
                "name": name,
                "label": meta["label"],
                "field": meta["field"],
                "units": meta["units"],
                "vmin": meta["vmin"],
                "vmax": meta["vmax"],
                "cmap": meta["cmap"],
                "categories": meta["categories"],
                "sweeps": sorted(made),
                "skipped": sorted(skipped, key=lambda r: r["sweep"]),
            }
        )
    return {
        "type": "module_volume_result",
        "module": run.module,
        "file_id": file_id,
        "params": run.params,
        "elapsed_ms": run.elapsed_ms,
        "derived": entries,
        "summaries": run.summaries,
        "evicted": evicted,
    }


def _loader_for(reader: RadarReader, variable: str) -> Callable[[int], Any]:
    """``sweep_loader`` for code that reads one moment: only *variable* is decoded and masked."""

    def load(index: int) -> Any:
        return reader.get_sweep_dataset(index, only=[variable])

    return load


def _sweep_dataset(reader: RadarReader, index: int):
    """Full ``xr.Dataset`` of one sweep, loading lazy (ARCO) sweeps on demand."""
    getter = getattr(reader, "get_sweep_dataset", None)  # public API if the reader has one
    if getter is not None:
        return getter(index)
    nodes = reader._sweep_nodes()
    if index < 0 or index >= len(nodes):
        raise IndexError(f"Sweep index {index} out of range (0..{len(nodes) - 1})")
    ds = nodes[index].to_dataset()
    loader = getattr(reader, "_load_arco_sweep", None)
    is_arco = getattr(reader, "_format", None) == "nexrad_arco"
    if not ds.data_vars and loader is not None and is_arco:
        ds = loader(index)  # placeholder sweep: fetch the real data
    return ds


async def _dispatch(handler: ConnectionHandler, raw_message: str | bytes) -> None:
    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8", errors="replace")
    await handler.handle_message(raw_message)


#: Most message tasks one web connection may have in flight at once.
WEB_MAX_INFLIGHT = 16


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


#: Most request tasks one desktop connection may have in flight at once. Generous: the app's
#: scheduler keeps far fewer than this running; the limit only stops a runaway client from
#: piling up unbounded work. ``XRADAR_DESKTOP_MAX_INFLIGHT`` overrides it.
DESKTOP_MAX_INFLIGHT = _env_int("XRADAR_DESKTOP_MAX_INFLIGHT", 32)

#: Messages the desktop in-flight limit never refuses: liveness, the account gate and every
#: cancel (cheap, and the way the user gets out of a busy engine).
_ALWAYS_ADMITTED = frozenset({"ping", "get_web_info"}) | AUTH_MESSAGES

#: The answer to a request that arrives while the connection is at its in-flight limit.
BUSY_REPLY: dict[str, str] = {
    "type": "error",
    "code": "busy",
    "message": "Too many requests in flight; wait for them to finish",
}


def _always_admitted(msg_type: str | None) -> bool:
    """True for messages the in-flight limit lets through (ping, account, any cancel)."""
    if msg_type is None:
        return False
    return msg_type in _ALWAYS_ADMITTED or "cancel" in msg_type


def _web_message_type(raw_message: str | bytes) -> str | None:
    """Cheap peek at a message's ``type`` (for idle accounting); None when not JSON."""
    try:
        text = (
            raw_message.decode("utf-8", "replace")
            if isinstance(raw_message, bytes)
            else raw_message
        )
        if len(text) > 4096:
            text = text[:4096]
        match = re.search(r'"type"\s*:\s*"([^"]{1,64})"', text)
        return match.group(1) if match else None
    except Exception:
        return None


async def _idle_watch(ws: ServerConnection, ctx: web.SessionContext, tasks: set[Any]) -> None:
    """Close a web connection that has been idle (no real messages) for the idle timeout."""
    limit = ctx.settings.idle_timeout_s
    interval = max(0.05, min(30.0, limit / 4))
    while True:
        await asyncio.sleep(interval)
        if tasks:  # a long request is still running: not idle
            ctx.touch()
        elif ctx.idle_seconds() >= limit:
            logger.info("Closing idle web session %s", ctx.id)
            with contextlib.suppress(Exception):
                await ws.close(code=1000, reason="Idle timeout")
            return


def _end_web_session(ctx: web.SessionContext) -> None:
    """Free everything a web session owns: readers, caches, shared-LRU entries, its folder."""
    state: _ConnState | None = ctx.state
    if state is not None:
        for reader in list(state.file_readers.values()):
            with contextlib.suppress(Exception):
                store = derived.store_for(reader, create=False)
                if store is not None:
                    store.drop()
                reader.close()
        state.file_readers.clear()
        state.generations.clear()
        with contextlib.suppress(Exception):
            state.renderer.release_memory()
    for mosaic_id in ctx.owned["mosaics"]:
        mosaic.MOSAICS.discard(mosaic_id)
    for wind_id in ctx.owned["winds"]:
        mdop.WIND_RESULTS.discard(wind_id)
    ctx.close()
    gc.collect()


async def _handle_web_connection(ws: ServerConnection) -> None:
    remote = ws.remote_address
    if not _origin_allowed(ws):
        request = getattr(ws, "request", None)
        headers = getattr(request, "headers", None)
        origin = headers.get("Origin") if headers is not None else None
        logger.warning("Rejected WebSocket connection from %s with origin %s", remote, origin)
        await ws.close(code=1008, reason="Origin not allowed")
        return

    _ensure_context_executor(asyncio.get_running_loop())
    try:
        ctx = web.manager().open_session()
    except web.WebError as exc:
        await ws.send(json.dumps({"type": "error", "code": "server_busy", "message": str(exc)}))
        await ws.close(code=1013, reason="Server at capacity")
        return
    ctx.state = _ConnState()
    logger.info("Web client connected from %s (session %s)", remote, ctx.id)
    handler = ConnectionHandler(ws, session=ctx)
    tasks: set[asyncio.Task[Any]] = set()
    watcher = asyncio.get_running_loop().create_task(_idle_watch(ws, ctx, tasks))

    try:
        await handler.send(web.web_info())
        async for raw_message in ws:
            if _web_message_type(raw_message) != "ping":
                ctx.touch()
            if not ctx.limiter.allow():
                await handler.send(
                    {
                        "type": "error",
                        "code": "rate_limited",
                        "message": "Too many requests; slow down",
                    }
                )
                if ctx.limiter.consecutive_denied > 200:
                    await ws.close(code=1008, reason="Rate limit exceeded")
                    break
                continue
            if len(tasks) >= WEB_MAX_INFLIGHT:
                await handler.send(dict(BUSY_REPLY))
                continue
            task = asyncio.get_running_loop().create_task(_dispatch(handler, raw_message))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    except websockets.exceptions.ConnectionClosed as exc:
        logger.info("Web client %s disconnected: %s", remote, exc)
    except Exception as exc:
        logger.error("Unexpected error for %s: %s\n%s", remote, exc, traceback.format_exc())
    finally:
        watcher.cancel()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(watcher, return_exceptions=True)
        _end_web_session(ctx)
        logger.info("Web session %s from %s closed and cleaned up", ctx.id, remote)


async def _handle_connection(
    ws: ServerConnection, *, tracker: Any = None, remote_engine: bool = False
) -> None:
    """Serve one desktop-style client until it disconnects.

    ``rxd serve`` (engine/serve.py) passes ``remote_engine=True`` after its own token handshake, and
    a ``tracker`` (``touch(message_type, tasks)``) that it uses for the idle timeout.
    """
    if web.is_web_mode():
        await _handle_web_connection(ws)
        return
    remote = ws.remote_address
    if not _origin_allowed(ws):
        request = getattr(ws, "request", None)
        headers = getattr(request, "headers", None)
        origin = headers.get("Origin") if headers is not None else None
        logger.warning("Rejected WebSocket connection from %s with origin %s", remote, origin)
        await ws.close(code=1008, reason="Origin not allowed")
        return

    logger.info("Client connected from %s", remote)
    handler = ConnectionHandler(ws, remote_engine=True) if remote_engine else ConnectionHandler(ws)
    # Each message runs as its own task so one slow request (export, QC, cloud open)
    # never blocks pings or other requests. Binary responses stay contiguous thanks
    # to the handler's send lock.
    tasks: set[asyncio.Task[Any]] = set()
    # Only requests count towards the in-flight limit; ping, account and cancel messages
    # always run uncounted, so the user can still cancel out of a busy engine.
    counted: set[asyncio.Task[Any]] = set()

    try:
        async for raw_message in ws:
            message_type = _web_message_type(raw_message)
            if tracker is not None:
                tracker.touch(message_type, tasks)
            admitted = _always_admitted(message_type)
            if not admitted and len(counted) >= DESKTOP_MAX_INFLIGHT:
                await handler.send(dict(BUSY_REPLY))
                continue
            task = asyncio.get_running_loop().create_task(_dispatch(handler, raw_message))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            if not admitted:
                counted.add(task)
                task.add_done_callback(counted.discard)
    except websockets.exceptions.ConnectionClosed as exc:
        logger.info("Client %s disconnected: %s", remote, exc)
    except Exception as exc:
        logger.error("Unexpected error for %s: %s\n%s", remote, exc, traceback.format_exc())
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Connection from %s closed", remote)


def _preload_heavy_imports() -> None:
    """Warm the imports the first open + first render need (xradar, palettes, scipy)."""
    preload_backends()
    try:
        _palettes()
    except Exception as exc:  # the render reports the real problem later
        logger.debug("palette preload failed: %s", exc)
    try:
        import scipy.ndimage  # noqa: F401  (deferred at import: QC, 3D volume, tracking use it)
    except Exception as exc:
        logger.debug("scipy preload failed: %s", exc)


async def _run_server(
    port: int,
    host: str | None = None,
    *,
    handler: Callable[[ServerConnection], Any] | None = None,
    on_ready: Callable[[int], None] | None = None,
    stop_event: asyncio.Event | None = None,
    serve_options: dict[str, Any] | None = None,
) -> None:
    """Run the WebSocket server until SIGINT / SIGTERM (or ``stop_event``).

    ``rxd serve`` supplies its own ``handler`` (the token handshake), ``on_ready`` (it prints the
    address instead of ``READY:``), ``stop_event`` (the idle timeout) and ``serve_options``; port 0
    picks a free port and ``on_ready`` receives the real one.
    """
    web_mode = web.is_web_mode()
    if web_mode:
        web.apply_web_defaults(
            system_memory_mb=(lambda b: b // (1024 * 1024) if b else None)(system_memory_bytes())
        )
        removed = web.manager().purge_stale_dirs()
        if removed:
            logger.info("Removed %d stale web session folder(s)", removed)
        _ensure_context_executor(asyncio.get_running_loop())
    try:
        plugins.discover_modules()
    except Exception:
        logger.exception("Module discovery failed (continuing without plugins)")
    stop_event = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    serve_kwargs: dict[str, Any] = {
        # Incoming limit must admit the largest save_text_file payload (+ JSON overhead).
        "max_size": max(WS_MAX_SIZE, SAVE_TEXT_MAX_BYTES + 1024 * 1024),
        "ping_interval": 30,
        "ping_timeout": 60,
    }
    if web_mode:
        serve_kwargs.update(
            max_size=web.settings().max_ws_message_mb * 1024 * 1024,
            process_request=web.process_request,  # /health, /download/..., Origin checks
            compression=None,  # chunks are already compact; no CPU for inflate / deflate
            server_header=None,
            max_queue=8,
        )
    serve_kwargs.update(serve_options or {})
    bind_host = host or ("127.0.0.1" if web_mode else "localhost")
    async with websockets.serve(
        handler or _handle_connection, bind_host, port, **serve_kwargs
    ) as listener:
        bound = listener.sockets[0].getsockname()[1] if port == 0 else port
        logger.info("radarx-desktop engine listening on ws://%s:%d", bind_host, bound)
        if on_ready is not None:
            on_ready(bound)
        else:
            print(f"READY:{bound}", flush=True)
        # Import xradar while the user picks a file, so the first open does not pay for it.
        threading.Thread(target=_preload_heavy_imports, name="preload", daemon=True).start()
        limit = watchdog.stall_limit_s()
        guard = (
            watchdog.LoopWatchdog(
                limit, lambda stalled: watchdog.exit_stalled(stalled, _crash_log_file)
            ).start()
            if limit
            else None
        )
        try:
            await stop_event.wait()
        finally:
            if guard is not None:
                guard.stop()

    if web_mode:  # remove every session folder on shutdown
        for ctx in list(web.manager().sessions.values()):
            _end_web_session(ctx)
    logger.info("Server shut down cleanly")


_CRASH_LOG_MAX_BYTES = 512 * 1024
_crash_log_file: Any = None  # kept open for faulthandler


def enable_crash_log() -> Path | None:
    """Make a native crash (SIGSEGV, SIGBUS, SIGABRT ...) leave a trace.

    A crash inside netCDF-C / HDF5 kills the process without any Python traceback, and the
    packaged engine has no console: the app only saw "connection lost". ``faulthandler`` writes the
    Python stack of every thread to ``~/.xradar-desktop/engine-crash.log`` (web mode: to stderr, a
    service must not write into a home folder). Returns the log path, or None.
    """
    import faulthandler

    global _crash_log_file
    if web.is_web_mode():
        faulthandler.enable(all_threads=True)
        return None
    path = Path.home() / ".xradar-desktop" / "engine-crash.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _CRASH_LOG_MAX_BYTES:
            path.unlink()  # keep it small: only the newest crashes matter
        _crash_log_file = open(path, "a", buffering=1, encoding="utf-8")  # noqa: SIM115
        _crash_log_file.write(
            f"--- engine {ENGINE_ID} pid {os.getpid()} started "
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} (a crash report follows if it crashes) ---\n"
        )
        faulthandler.enable(file=_crash_log_file, all_threads=True)
        return path
    except OSError:  # read-only home, no disk ...: stderr still reaches the shell's log
        faulthandler.enable(all_threads=True)
        return None


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="radarx-desktop engine server")
    parser.add_argument(
        "--port", type=int, help="WebSocket port (web mode: defaults to the PORT environment)"
    )
    parser.add_argument(
        "--host", help="interface to bind (default: localhost; 127.0.0.1 in web mode)"
    )
    parser.add_argument(
        "--web", action="store_true", help="public web-service mode (same as XRADAR_WEB=1)"
    )
    args = parser.parse_args()
    if args.web:
        os.environ["XRADAR_WEB"] = "1"
        web.reload_settings()
    enable_crash_log()
    port = args.port
    if port is None and web.is_web_mode() and os.environ.get("PORT", "").strip().isdigit():
        port = int(os.environ["PORT"])
    if port is None:
        parser.error("--port is required (or set PORT in web mode)")

    low = 1 if web.is_web_mode() else 1024  # containers may bind low ports
    if not (low <= port <= 65535):
        print(f"Port must be between {low} and 65535, got {port}", file=sys.stderr)
        sys.exit(1)

    try:
        asyncio.run(_run_server(port, args.host))
    except KeyboardInterrupt:
        logger.info("Interrupted — exiting")


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()  # needed by frozen builds that spawn worker processes
    main()

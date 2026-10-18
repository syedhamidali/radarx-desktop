"""Web mode: run the engine as a public, anonymous, multi-user service.

The desktop engine trusts its caller (it is a sidecar of one local app). A public deployment
cannot: any visitor can send any message. Web mode (``XRADAR_WEB=1`` or ``server.py --web``)
therefore changes the trust model and this module holds everything that enforces it:

* :class:`WebSettings` -- limits and the origin allow-list, all from environment variables.
* :class:`SessionContext` -- one per WebSocket connection: a private temp directory, the upload
  manager, the path guard, the download registry entries and a rate limiter.
* :class:`SessionManager` -- the concurrent-session cap and the download-token registry.
* :func:`process_request` -- the HTTP side (``GET /health``, ``GET /download/<token>/<name>``)
  and the Origin check of the WebSocket upgrade.

The module imports nothing from the rest of the engine so it can be used (and tested) alone.
The wire protocol is documented in ``docs/WEB.md``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import math
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

logger = logging.getLogger("xradar-server.web")

#: Largest decoded payload of one ``upload_chunk``.
MAX_CHUNK_BYTES = 1024 * 1024
#: Tab-separated "do not touch" message shown for every refused server-side path.
LOCAL_PATHS_DISABLED = (
    "Opening files from the server is disabled; upload a file or use the cloud tab"
)
OUTPUT_NAME_ONLY = (
    "Choose a file name only (no folders): files are saved in your private session and "
    "offered as a download"
)
SESSION_DIR_PREFIX = "xradar-sess-"
#: Public NEXRAD Level II buckets users may open from (anonymous access, public data only).
PUBLIC_NEXRAD_BUCKETS = frozenset({"unidata-nexrad-level2", "noaa-nexrad-level2"})

_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_FILENAME_BAD_RE = re.compile(r"[^A-Za-z0-9._-]+")
_S3_KEY_RE = re.compile(r"^[A-Za-z0-9._/=+-]{1,512}$")
_MAX_ACTIVE_UPLOADS = 2
_MAX_UPLOADS_PER_SESSION = 200


class WebError(Exception):
    """A refusal with a message that is safe to show to the (untrusted) client."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default
    return max(minimum, value)


def _env_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default
    if math.isnan(value) or math.isinf(value):
        return default
    return max(minimum, value)


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class WebSettings:
    """Web-mode configuration (see ``docs/WEB.md`` for the environment variables)."""

    enabled: bool = False
    allowed_origins: tuple[str, ...] = ()
    max_upload_mb: int = 200
    max_session_mb: int = 600
    max_sessions: int = 8
    max_open_files: int = 3
    idle_timeout_s: int = 1800
    download_ttl_s: int = 600
    tmp_root: str = ""
    rate_per_s: float = 100.0
    rate_burst: int = 200
    max_ws_message_mb: int = 8

    @classmethod
    def from_env(cls) -> WebSettings:
        origins = tuple(
            o.strip().rstrip("/").lower()
            for o in os.environ.get("XRADAR_ALLOWED_ORIGINS", "").split(",")
            if o.strip()
        )
        tmp_root = os.environ.get("XRADAR_WEB_TMP", "").strip() or tempfile.gettempdir()
        return cls(
            enabled=_truthy(os.environ.get("XRADAR_WEB")),
            allowed_origins=origins,
            max_upload_mb=_env_int("XRADAR_MAX_UPLOAD_MB", 200),
            max_session_mb=_env_int("XRADAR_MAX_SESSION_MB", 600),
            max_sessions=_env_int("XRADAR_MAX_SESSIONS", 8),
            max_open_files=_env_int("XRADAR_MAX_OPEN_FILES", 3),
            idle_timeout_s=_env_int("XRADAR_IDLE_TIMEOUT_S", 1800),
            download_ttl_s=_env_int("XRADAR_DOWNLOAD_TTL_S", 600),
            tmp_root=tmp_root,
            rate_per_s=_env_float("XRADAR_RATE_PER_S", 100.0),
            rate_burst=_env_int("XRADAR_RATE_BURST", 200),
        )

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_session_bytes(self) -> int:
        return self.max_session_mb * 1024 * 1024


_settings: WebSettings | None = None
_settings_lock = threading.Lock()
MANAGER: SessionManager | None = None


def settings() -> WebSettings:
    """The active settings (read from the environment on first use)."""
    global _settings
    if _settings is None:
        with _settings_lock:
            if _settings is None:
                _settings = WebSettings.from_env()
    return _settings


def reload_settings() -> WebSettings:
    """Re-read the environment (tests; the CLI after applying ``--web``). Resets the registry."""
    global _settings, MANAGER
    with _settings_lock:
        _settings = WebSettings.from_env()
        MANAGER = None
    return _settings


def is_web_mode() -> bool:
    return settings().enabled


def manager() -> SessionManager:
    global MANAGER
    if MANAGER is None:
        with _settings_lock:
            if MANAGER is None:
                MANAGER = SessionManager(settings())
    return MANAGER


def apply_web_defaults(*, system_memory_mb: int | None = None) -> None:
    """Environment defaults a public deployment should have (never overrides explicit values)."""
    os.environ.setdefault("XRADAR_MODULE_TIMEOUT_S", "120")
    s = settings()
    if not os.environ.get("XRADAR_MAX_OPEN_MB", "").strip():
        # Per-session budget: share ~60 % of the machine between a few busy sessions, with a floor
        # large enough for one NEXRAD volume. Operators should still set it explicitly.
        mem = system_memory_mb or 1024
        os.environ["XRADAR_MAX_OPEN_MB"] = str(
            max(400, int(0.6 * mem / max(1, min(s.max_sessions, 3))))
        )


# ---------------------------------------------------------------------------
# Origins, names, rate limiting
# ---------------------------------------------------------------------------


def origin_allowed(
    origin: str | None, host: str | None, cfg: WebSettings, *, allow_missing: bool
) -> bool:
    """Is a request with this ``Origin`` header acceptable?

    ``*`` in ``XRADAR_ALLOWED_ORIGINS`` allows everything (and only when set explicitly). With an
    empty list only same-origin requests (``Origin`` host equals ``Host``) pass. A missing
    ``Origin`` (non-browser client, plain navigation) passes only when *allow_missing*.
    """
    if origin is None or origin == "":
        return allow_missing
    if "*" in cfg.allowed_origins:
        return True
    norm = origin.strip().rstrip("/").lower()
    if norm == "null":
        return False
    if cfg.allowed_origins:
        return norm in cfg.allowed_origins
    if not host:
        return False
    parts = urlsplit(norm)
    return bool(parts.netloc) and parts.netloc == host.strip().lower()


def cors_origin(origin: str | None, cfg: WebSettings) -> str | None:
    """Value of ``Access-Control-Allow-Origin`` for a response to *origin* (None = omit)."""
    if "*" in cfg.allowed_origins:
        return "*"
    if origin and origin.strip().rstrip("/").lower() in cfg.allowed_origins:
        return origin
    return None


def sanitize_filename(name: Any, default: str = "file", max_len: int = 120) -> str:
    """A safe single path component: ASCII letters, digits, ``._-``; no separators, never hidden.

    The extension is preserved (format sniffing and downloads rely on it).
    """
    if not isinstance(name, str):
        return default
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = _FILENAME_BAD_RE.sub("_", base).lstrip(".")
    if not any(c.isalnum() for c in base):
        return default
    if len(base) > max_len:
        stem, _, ext = base.rpartition(".")
        ext = ext[:16]
        base = stem[: max_len - len(ext) - 1] + "." + ext if stem else base[:max_len]
    return base


def is_bare_name(name: Any) -> bool:
    """True when *name* is a plain file name: no folders, no ``..``, no ``~``, no scheme."""
    if not isinstance(name, str) or not name.strip() or len(name) > 255:
        return False
    if any(c in name for c in ("/", "\\", "\x00", ":")):
        return False
    return not (name.startswith(("~", ".")) or name.strip() != name)


class RateLimiter:
    """Token bucket: *rate* messages per second sustained, *burst* at once."""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.burst = float(burst)
        self.tokens = float(burst)
        self.stamp = time.monotonic()
        self.consecutive_denied = 0

    def allow(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self.tokens = min(self.burst, self.tokens + max(0.0, now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            self.consecutive_denied = 0
            return True
        self.consecutive_denied += 1
        return False


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------


@dataclass
class _Upload:
    upload_id: str
    filename: str
    size: int
    final: Path
    part: Path
    fh: Any
    received: int = 0
    next_seq: int = 0
    started: float = field(default_factory=time.monotonic)


def scan_hdf5_for_external_references(path: str) -> None:
    """Refuse HDF5/NetCDF-4 files that reference data outside themselves.

    HDF5 external links, external raw-data storage and virtual datasets let a crafted upload make
    the reader open *another path on the server*. Best effort: skipped when h5py is missing, and
    a file h5py cannot parse is left to the real reader (which will fail on it too).
    """
    try:
        import h5py
    except ImportError:
        return
    try:
        if not h5py.is_hdf5(path):
            return
        handle = h5py.File(path, "r")
    except Exception:
        return
    seen: set[int] = set()
    count = 0
    bad = "This file references data outside itself (external HDF5 links), which is not allowed"

    def walk(group: Any, depth: int) -> None:
        nonlocal count
        if depth > 64:
            raise WebError("This file is nested too deeply to open safely")
        for name in group:
            count += 1
            if count > 200_000:
                raise WebError("This file has too many objects to open safely")
            link = group.get(name, getlink=True)
            if isinstance(link, h5py.ExternalLink):
                raise WebError(bad)
            if not isinstance(link, h5py.HardLink):
                continue  # soft links resolve inside this file
            try:
                obj = group[name]
            except Exception:
                continue
            key = hash(obj.id)
            if key in seen:
                continue
            seen.add(key)
            if isinstance(obj, h5py.Dataset):
                if obj.is_virtual or obj.external:
                    raise WebError(bad)
            elif isinstance(obj, h5py.Group):
                walk(obj, depth + 1)

    try:
        walk(handle, 0)
    except WebError:
        raise
    except Exception as exc:  # unreadable structure: let the real open report it
        logger.debug("hdf5 external-reference scan skipped: %s", exc)
    finally:
        with contextlib.suppress(Exception):
            handle.close()


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class SessionContext:
    """Everything private to one WebSocket connection in web mode."""

    def __init__(self, mgr: SessionManager, cfg: WebSettings) -> None:
        self.manager = mgr
        self.settings = cfg
        self.id = secrets.token_hex(8)
        root = Path(cfg.tmp_root).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        # mkdtemp creates the directory with mode 0700.
        self.dir = Path(tempfile.mkdtemp(prefix=SESSION_DIR_PREFIX, dir=root)).resolve()
        self.real_dir = os.path.normcase(os.path.realpath(self.dir))
        self.created = time.monotonic()
        self.last_active = self.created
        self.limiter = RateLimiter(cfg.rate_per_s, cfg.rate_burst)
        self.uploads: dict[str, _Upload] = {}
        self._upload_count = 0
        self.closed = False
        #: server-side per-connection engine state (set by server.py)
        self.state: Any = None
        #: ids of mosaics / wind results this session created (they live in shared LRU caches)
        self.owned: dict[str, set[str]] = {"mosaics": set(), "winds": set()}
        self.recipe_count_limit = 50
        self._lock = threading.Lock()

    # -- directories -------------------------------------------------------

    def _sub(self, name: str) -> Path:
        path = self.dir / name
        path.mkdir(mode=0o700, exist_ok=True)
        return path

    @property
    def uploads_dir(self) -> Path:
        return self._sub("uploads")

    @property
    def exports_dir(self) -> Path:
        return self._sub("exports")

    @property
    def recipes_dir(self) -> Path:
        return self._sub("recipes")

    # -- activity ----------------------------------------------------------

    def touch(self) -> None:
        self.last_active = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_active

    # -- quota -------------------------------------------------------------

    def used_bytes(self) -> int:
        """Bytes on disk in the session directory (symlinks are never followed)."""
        total = 0
        stack = [str(self.dir)]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                            else:
                                total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            continue
            except OSError:
                continue
        return total

    def pending_bytes(self) -> int:
        return sum(max(0, u.size - u.received) for u in self.uploads.values())

    def check_quota(self, extra: int = 0) -> None:
        if self.used_bytes() + self.pending_bytes() + extra > self.settings.max_session_bytes:
            raise WebError(
                f"Session storage quota of {self.settings.max_session_mb} MB would be exceeded; "
                "close files or reconnect to start a fresh session"
            )

    # -- path guard --------------------------------------------------------

    def contains(self, real_path: str) -> bool:
        """Is *real_path* (already resolved) strictly inside the session directory?"""
        norm = os.path.normcase(real_path)
        if norm == self.real_dir:
            return False
        try:
            return os.path.commonpath([norm, self.real_dir]) == self.real_dir
        except ValueError:  # different drives
            return False

    def resolve_input_path(self, raw: Any) -> str:
        """The real path of an *input* file, which must live inside this session's directory.

        Refuses relative paths, ``..`` components, ``~``, URL schemes (``file://``, ``s3://``,
        ``C:``), NUL bytes, absolute paths elsewhere and symlinks that lead out. The error never
        says whether the path exists.
        """
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw or len(raw) > 4096:
            raise WebError(LOCAL_PATHS_DISABLED)
        if raw.startswith("~") or _SCHEME_RE.match(raw):
            raise WebError(LOCAL_PATHS_DISABLED)
        if ".." in re.split(r"[\\/]", raw) or not os.path.isabs(raw):
            raise WebError(LOCAL_PATHS_DISABLED)
        real = os.path.realpath(raw)
        if not self.contains(real):
            raise WebError(LOCAL_PATHS_DISABLED)
        return real

    def export_target(self, requested: Any, *, ext: str, prefix: str = "xradar_export_") -> Path:
        """Where an export named *requested* (a bare file name, or None) is written.

        Always inside ``<session>/exports``; the name never escapes it. Existing files are not
        overwritten (``-2``, ``-3`` ... are appended).
        """
        if requested is None or requested == "":
            name = f"{prefix}{secrets.token_hex(4)}.{ext}"
        else:
            if not is_bare_name(requested):
                raise WebError(OUTPUT_NAME_ONLY)
            name = sanitize_filename(requested, default=f"{prefix}{secrets.token_hex(4)}")
            if "." not in name:
                name = f"{name}.{ext}"
        return self._unique(self.exports_dir, name)

    def export_subdir(self, requested: Any, *, prefix: str = "xradar_batch_") -> Path:
        """A fresh sub-folder of ``<session>/exports`` for a batch export."""
        if requested is None or requested == "":
            name = f"{prefix}{secrets.token_hex(4)}"
        else:
            if not is_bare_name(requested):
                raise WebError(OUTPUT_NAME_ONLY)
            name = sanitize_filename(requested, default=f"{prefix}{secrets.token_hex(4)}")
        path = self._unique(self.exports_dir, name)
        path.mkdir(mode=0o700)
        return path

    @staticmethod
    def _unique(directory: Path, name: str) -> Path:
        candidate = directory / name
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        n = 1
        while candidate.exists():
            n += 1
            candidate = directory / (f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}")
        return candidate

    # -- downloads ---------------------------------------------------------

    def register_download(self, path: str | os.PathLike[str]) -> str:
        """A relative ``/download/<token>/<name>`` URL for a file inside the session directory."""
        real = os.path.realpath(path)
        if not self.contains(real) or not os.path.isfile(real):
            raise WebError("That file is not available for download")
        return self.manager.register_download(self, real)

    def download_info(self, path: str | os.PathLike[str]) -> dict[str, Any]:
        """``{"path": basename, "download_url": ...}`` for a finished export."""
        real = os.path.realpath(path)
        return {"path": os.path.basename(real), "download_url": self.register_download(real)}

    def check_export_quota(self, written: list[str | os.PathLike[str]]) -> None:
        """After an export: delete *written* and refuse when the session is over its quota."""
        if self.used_bytes() <= self.settings.max_session_bytes:
            return
        for item in written:
            with contextlib.suppress(OSError):
                if os.path.isdir(item):
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    os.remove(item)
        raise WebError(
            f"Session storage quota of {self.settings.max_session_mb} MB exceeded; "
            "the export was discarded"
        )

    # -- uploads -----------------------------------------------------------

    def upload_start(self, upload_id: Any, filename: Any, size: Any) -> None:
        cfg = self.settings
        if not isinstance(upload_id, str) or not _UPLOAD_ID_RE.match(upload_id):
            raise WebError("Invalid upload_id (use 1-64 letters, digits, '-' or '_')")
        if upload_id in self.uploads:
            raise WebError(f"Upload '{upload_id}' is already in progress")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise WebError("Invalid upload size (must be a positive integer number of bytes)")
        if size > cfg.max_upload_bytes:
            raise WebError(f"File is larger than the upload limit of {cfg.max_upload_mb} MB")
        if len(self.uploads) >= _MAX_ACTIVE_UPLOADS:
            raise WebError("Too many uploads at once; wait for the current upload to finish")
        if self._upload_count >= _MAX_UPLOADS_PER_SESSION:
            raise WebError("Too many uploads in this session; reconnect to start a fresh one")
        self.check_quota(size)
        name = sanitize_filename(filename, default="upload")
        folder = self.uploads_dir / secrets.token_hex(4)
        folder.mkdir(mode=0o700)
        final = folder / name
        part = folder / (name + ".part")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(part, flags, 0o600)
        self.uploads[upload_id] = _Upload(upload_id, name, size, final, part, os.fdopen(fd, "wb"))
        self._upload_count += 1

    def upload_chunk(self, upload_id: Any, seq: Any, data: Any) -> int:
        up = self._active_upload(upload_id)
        try:
            if isinstance(seq, bool) or not isinstance(seq, int) or seq != up.next_seq:
                raise WebError(f"Out-of-order chunk (expected seq {up.next_seq}, got {seq!r})")
            if not isinstance(data, str):
                raise WebError("Chunk 'data' must be a base64 string")
            if len(data) > (MAX_CHUNK_BYTES * 4) // 3 + 8:
                raise WebError(f"Chunk is larger than {MAX_CHUNK_BYTES // 1024} KiB")
            try:
                raw = base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise WebError("Chunk 'data' is not valid base64") from exc
            if not raw:
                raise WebError("Empty chunk")
            if len(raw) > MAX_CHUNK_BYTES:
                raise WebError(f"Chunk is larger than {MAX_CHUNK_BYTES // 1024} KiB")
            if up.received + len(raw) > up.size:
                raise WebError("Received more data than the declared file size")
            up.fh.write(raw)
            up.received += len(raw)
            up.next_seq += 1
        except (WebError, OSError) as exc:
            self.upload_abort(upload_id)
            if isinstance(exc, OSError):
                raise WebError("Could not store the upload (server storage error)") from exc
            raise
        return up.received

    def upload_end(self, upload_id: Any) -> Path:
        up = self._active_upload(upload_id)
        try:
            if up.received != up.size:
                raise WebError(
                    f"Upload incomplete: declared {up.size} bytes but received {up.received}"
                )
            up.fh.flush()
            up.fh.close()
            scan_hdf5_for_external_references(str(up.part))
            os.replace(up.part, up.final)
        except (WebError, OSError) as exc:
            self.upload_abort(upload_id)
            if isinstance(exc, OSError):
                raise WebError("Could not store the upload (server storage error)") from exc
            raise
        del self.uploads[upload_id]
        return up.final

    def upload_abort(self, upload_id: str) -> None:
        up = self.uploads.pop(upload_id, None)
        if up is None:
            return
        with contextlib.suppress(Exception):
            up.fh.close()
        shutil.rmtree(up.final.parent, ignore_errors=True)

    def _active_upload(self, upload_id: Any) -> _Upload:
        up = self.uploads.get(upload_id) if isinstance(upload_id, str) else None
        if up is None:
            raise WebError("Unknown upload (send upload_start first)")
        return up

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Remove the session directory and forget its download tokens (idempotent)."""
        with self._lock:
            if self.closed:
                return
            self.closed = True
        for up in list(self.uploads.values()):
            with contextlib.suppress(Exception):
                up.fh.close()
        self.uploads.clear()
        self.manager.release(self)
        shutil.rmtree(self.dir, ignore_errors=True)


@dataclass
class _DownloadEntry:
    session_id: str
    path: str
    name: str
    expires: float


class SessionManager:
    """Concurrent sessions and the download-token registry."""

    def __init__(self, cfg: WebSettings) -> None:
        self.settings = cfg
        self.sessions: dict[str, SessionContext] = {}
        self.downloads: dict[str, _DownloadEntry] = {}
        self._lock = threading.Lock()

    def open_session(self) -> SessionContext:
        with self._lock:
            if len(self.sessions) >= self.settings.max_sessions:
                raise WebError(
                    f"The server is at capacity ({self.settings.max_sessions} concurrent "
                    "sessions); please try again in a few minutes"
                )
            ctx = SessionContext(self, self.settings)
            self.sessions[ctx.id] = ctx
        return ctx

    def release(self, ctx: SessionContext) -> None:
        with self._lock:
            self.sessions.pop(ctx.id, None)
            for token in [t for t, e in self.downloads.items() if e.session_id == ctx.id]:
                del self.downloads[token]

    def register_download(self, ctx: SessionContext, real_path: str) -> str:
        name = os.path.basename(real_path)
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._purge_locked()
            self.downloads[token] = _DownloadEntry(
                ctx.id, real_path, name, time.monotonic() + self.settings.download_ttl_s
            )
        return f"/download/{token}/{quote(name)}"

    def _purge_locked(self) -> None:
        now = time.monotonic()
        for token in [t for t, e in self.downloads.items() if e.expires <= now]:
            del self.downloads[token]

    def lookup_download(self, token: str, name: str) -> tuple[SessionContext, str] | None:
        """The (session, real path) behind a valid download URL, else None."""
        with self._lock:
            self._purge_locked()
            entry = self.downloads.get(token)
            ctx = self.sessions.get(entry.session_id) if entry else None
        if entry is None or ctx is None or ctx.closed or name != entry.name:
            return None
        real = os.path.realpath(entry.path)
        if not ctx.contains(real) or not os.path.isfile(real) or os.path.islink(entry.path):
            return None
        return ctx, real

    def purge_stale_dirs(self) -> int:
        """Delete session directories left behind by a crashed process; returns the count."""
        root = Path(self.settings.tmp_root).expanduser()
        removed = 0
        cutoff = time.time() - max(3600, self.settings.idle_timeout_s * 2)
        live = {str(c.dir) for c in self.sessions.values()}
        with contextlib.suppress(OSError):
            for entry in root.iterdir():
                if (
                    entry.name.startswith(SESSION_DIR_PREFIX)
                    and entry.is_dir()
                    and not entry.is_symlink()
                    and str(entry.resolve()) not in live
                    and entry.stat().st_mtime < cutoff
                ):
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
        return removed


def web_info(cfg: WebSettings | None = None) -> dict[str, Any]:
    """The ``web_info`` message sent on connect."""
    cfg = cfg or settings()
    return {
        "type": "web_info",
        "web_mode": True,
        "limits": {
            "max_upload_mb": cfg.max_upload_mb,
            "max_sessions": cfg.max_sessions,
            "idle_timeout_s": cfg.idle_timeout_s,
            "max_open_files": cfg.max_open_files,
        },
        "features": {"uploads": True, "cloud": True, "local_paths": False, "downloads": True},
    }


def check_s3_path(s3_path: Any) -> str:
    """Accept only ``s3://<public NEXRAD bucket>/<key>`` (no ``..``); returns it normalised."""
    if not isinstance(s3_path, str) or not s3_path.startswith("s3://"):
        raise WebError("Only public NEXRAD archive files can be opened from the cloud")
    bucket, _, key = s3_path[5:].partition("/")
    if (
        bucket not in PUBLIC_NEXRAD_BUCKETS
        or not _S3_KEY_RE.match(key)
        or ".." in key.split("/")
        or key.startswith("/")
    ):
        raise WebError("Only public NEXRAD archive files can be opened from the cloud")
    return f"s3://{bucket}/{key}"


# ---------------------------------------------------------------------------
# HTTP side (websockets ``process_request`` hook)
# ---------------------------------------------------------------------------

_SAFE_TYPES = {
    ".png": "image/png",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
}


def _response(
    status: int, reason: str, body: bytes, headers: list[tuple[str, str]], *, ctype: str
) -> Any:
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    h = Headers(headers)
    h["Content-Type"] = ctype
    h["Content-Length"] = str(len(body))
    h["X-Content-Type-Options"] = "nosniff"
    h["Referrer-Policy"] = "no-referrer"
    return Response(status, reason, h, body)


def _plain(status: int, reason: str, text: str, extra: list[tuple[str, str]] | None = None) -> Any:
    return _response(
        status,
        reason,
        text.encode("utf-8"),
        [("Cache-Control", "no-store"), *(extra or [])],
        ctype="text/plain; charset=utf-8",
    )


def _content_disposition(name: str) -> str:
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


async def handle_http(
    method: str,
    target: str,
    headers: Any,
    *,
    mgr: SessionManager | None = None,
    cfg: WebSettings | None = None,
) -> Any | None:
    """Answer an HTTP request, or return None to let a WebSocket upgrade proceed.

    ``GET /health`` -> 200 ``ok``; ``GET /download/<token>/<name>`` -> the file; other methods
    -> 405; every request whose ``Origin`` is not allowed -> 403.
    """
    cfg = cfg or settings()
    mgr = mgr or manager()
    origin = headers.get("Origin")
    is_upgrade = (headers.get("Upgrade") or "").lower() == "websocket"
    cors = cors_origin(origin, cfg)
    cors_headers: list[tuple[str, str]] = []
    if cors:
        cors_headers = [("Access-Control-Allow-Origin", cors), ("Vary", "Origin")]

    if not origin_allowed(origin, headers.get("Host"), cfg, allow_missing=not is_upgrade):
        logger.warning("Rejected request (origin %r, upgrade=%s)", origin, is_upgrade)
        return _plain(403, "Forbidden", "Origin not allowed")

    if method.upper() != "GET":
        return _plain(405, "Method Not Allowed", "Method not allowed", [("Allow", "GET")])

    path = urlsplit(target).path
    if path == "/health":
        return _plain(200, "OK", "ok", cors_headers)

    if path.startswith("/download/"):
        parts = path.split("/")  # ["", "download", token, name]
        if len(parts) != 4 or not parts[2] or not parts[3]:
            return _plain(404, "Not Found", "Not found", cors_headers)
        found = mgr.lookup_download(parts[2], unquote(parts[3]))
        if found is None:
            return _plain(404, "Not Found", "Download not found or expired", cors_headers)
        _, real = found
        try:
            body = await asyncio.get_running_loop().run_in_executor(None, _read_file, real)
        except OSError:
            return _plain(404, "Not Found", "Download not found or expired", cors_headers)
        name = os.path.basename(real)
        ctype = _SAFE_TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")
        return _response(
            200,
            "OK",
            body,
            [
                ("Content-Disposition", _content_disposition(name)),
                ("Cache-Control", "no-store"),
                *cors_headers,
            ],
            ctype=ctype,
        )

    if is_upgrade:
        return None
    return _plain(404, "Not Found", "Not found", cors_headers)


def _read_file(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


async def process_request(connection: Any, request: Any) -> Any | None:
    """``websockets`` ``process_request`` hook (the library only delivers GET requests)."""
    return await handle_http("GET", request.path, request.headers)

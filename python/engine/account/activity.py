"""The activity log: which features an account uses, and nothing else (docs/PRIVACY.md).

ONE code path serves every surface. ``rxd`` (``cli.main``), the Python API (``engine.api``) and the
engine's WebSocket dispatch (``server.py``) all call :func:`record`; there is no other writer.

What is recorded: an event name from :data:`EVENTS` and at most one short, allow-listed detail value
(the name of the command or algorithm, a file *format family* such as ``NEXRAD``, an export format,
an error *class*). What is never recorded: file names or paths, coordinates or place names, radar
site identifiers, file contents, or any text a person typed. :func:`clean_detail` enforces this on
this side and the ``activity_events`` table enforces the same allow-list in the database
(``supabase/migrations/20261021000000_activity.sql``), so a bug here cannot widen what is stored.

Delivery never blocks or fails a command: events go to a small local queue file (capped), and a
background daemon thread (or, in a short-lived command, one bounded flush at exit) sends them in
batches with the signed-in user's own token. Network errors are silent and retried later. The
signed-in user can read back everything that was sent (Account dialog, "Activity").
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import json
import platform
import re
import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine.account import config, http
from engine.account.errors import AccountServerError
from engine.account.store import SessionStore, _FileLock, config_dir, device_id

#: Event name -> the detail keys it may carry. Anything else is dropped before it is queued.
EVENTS: dict[str, tuple[str, ...]] = {
    "app_start": (),
    "sign_in": (),
    "sign_out": (),
    "command": ("command",),
    "module_run": ("module",),
    "file_opened": ("format",),
    "export": ("format",),
    "mosaic_run": ("type",),
    "winds_run": ("type",),
    "animate_run": ("type",),
    "qpe_run": ("type",),
    "error": ("class",),
}
SURFACES = ("app", "cli", "api", "engine")

#: A detail value: letters, digits, space, underscore, plus, hyphen. No dot, slash, colon or
#: backslash can appear, so a path, a URL or an e-mail address cannot get through.
_VALUE = re.compile(r"^[A-Za-z0-9_ +\-]{1,64}$")

QUEUE_MAX_EVENTS = 500
QUEUE_MAX_BYTES = 200_000
BATCH_SIZE = 100
FLUSH_INTERVAL_S = 15.0
#: A runaway script must not flood the queue: at most this many events per rolling hour per process.
MAX_EVENTS_PER_HOUR = 400

#: Which surface the current code runs for (``cli.main`` sets "cli"; the default is "api").
_surface: contextvars.ContextVar[str] = contextvars.ContextVar("radarx_surface", default="api")

_lock = threading.Lock()
_recent: deque[float] = deque()
_memory_only: list[dict[str, Any]] = []  # events of the web service: never written to disk
_thread: threading.Thread | None = None
_background = True
_atexit_done = False
_wake = threading.Event()


# -- vocabulary ---------------------------------------------------------------------------------

_FAMILIES = {
    "nexrad_level2": "NEXRAD",
    "nexrad_arco": "NEXRAD",
    "nexrad": "NEXRAD",
    "cfradial1": "CfRadial",
    "cfradial2": "CfRadial",
    "odim": "ODIM",
    "gamic": "GAMIC",
    "iris": "IRIS",
    "furuno": "Furuno",
    "rainbow": "Rainbow",
    "uf": "UF",
    "imd": "IMD",
    "datamet": "Datamet",
    "hpl": "HPL",
    "metek": "Metek",
}


def format_family(label: Any) -> str:
    """Family of a reader format label (``nexrad_level2`` -> ``NEXRAD``), else ``Other``."""
    return _FAMILIES.get(str(label or "").strip().lower(), "Other")


def module_label(module_id: str, source: Any = None) -> str:
    """A built-in algorithm's id, or ``custom`` for a user's plug-in (any name is possible)."""
    if source is not None:
        try:
            builtin = Path(__file__).resolve().parents[2] / "modules"
            if Path(str(source)).resolve().parent != builtin:
                return "custom"
        except OSError:
            return "custom"
    return str(module_id)


def clean_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _VALUE.match(value) else None


def clean_detail(event: str, detail: Mapping[str, Any]) -> dict[str, str]:
    """Keep only the allow-listed keys of ``event`` whose values are plain short words."""
    allowed = EVENTS.get(event, ())
    out: dict[str, str] = {}
    for key in allowed:
        value = clean_value(detail.get(key))
        if value is not None:
            out[key] = value
    return out


# -- surface ------------------------------------------------------------------------------------


def set_surface(name: str) -> contextvars.Token:
    return _surface.set(name if name in SURFACES else "api")


def reset_surface(token: contextvars.Token) -> None:
    _surface.reset(token)


@contextlib.contextmanager
def surface(name: str) -> Iterator[None]:
    token = set_surface(name)
    try:
        yield
    finally:
        reset_surface(token)


def set_background(on: bool) -> bool:
    """Allow (default) or forbid the background sender thread (short-lived commands flush at exit).

    Returns the previous setting.
    """
    global _background
    previous = _background
    _background = on
    return previous


# -- recording ----------------------------------------------------------------------------------


def _queue_path() -> Path:
    return config_dir() / "activity-queue.jsonl"


def _now_iso(wall: float | None = None) -> str:
    return (
        datetime.fromtimestamp(time.time() if wall is None else wall, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3]
        + "Z"
    )


def _os_family() -> str:
    return {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(
        platform.system(), "Other"
    )


def app_version() -> str:
    import os

    value = os.environ.get("XRADAR_DESKTOP_VERSION", "").strip()
    if not value:
        try:
            from importlib.metadata import version

            value = version("radarx-desktop-engine")
        except Exception:
            value = "unknown"
    return value[:40]


def build_event(
    event: str,
    surface_name: str | None = None,
    *,
    device: str | None = None,
    os_name: str | None = None,
    **detail: Any,
) -> dict[str, Any] | None:
    """The row for ``event`` (None for an event name that is not allowed)."""
    if event not in EVENTS:
        return None
    surf = surface_name or _surface.get()
    if surf not in SURFACES:
        surf = "api"
    return {
        "event": event,
        "surface": surf,
        "occurred_at": _now_iso(),
        "device_id": device or device_id(),
        "app_version": app_version(),
        "os": os_name or _os_family(),
        "detail": clean_detail(event, detail),
    }


def _allowed_now() -> bool:
    wall = time.monotonic()
    while _recent and wall - _recent[0] > 3600:
        _recent.popleft()
    if len(_recent) >= MAX_EVENTS_PER_HOUR:
        return False
    _recent.append(wall)
    return True


def _signed_in_user() -> str | None:
    stored = SessionStore().load()
    return stored.user_id if stored and stored.user_id else None


def record(
    event: str,
    *,
    surface: str | None = None,
    token: str | None = None,
    device: str | None = None,
    os_name: str | None = None,
    **detail: Any,
) -> None:
    """Queue one activity event. Never raises, never blocks on the network, a no-op when no account
    is signed in or the development bypass is on.

    ``token`` (the web service only) keeps the event in memory and sends it with that user's access
    token; it is never written to disk, and ``device`` / ``os_name`` replace the values that would
    otherwise describe the server instead of the visitor.
    """
    try:
        if config.auth_disabled():
            return
        row = build_event(event, surface, device=device, os_name=os_name, **detail)
        if row is None:
            return
        with _lock:
            if not _allowed_now():
                return
        if token is not None:
            with _lock:
                _memory_only.append({"row": row, "token": token})
                del _memory_only[:-QUEUE_MAX_EVENTS]
        else:
            uid = _signed_in_user()
            if uid is None:
                return
            _append_to_queue({**row, "uid": uid})
        _ensure_sender()
    except Exception:
        return


def record_error(
    exc: BaseException, *, surface: str | None = None, token: str | None = None
) -> None:
    """Record that an error happened: only the exception CLASS name (a message may name files)."""
    record("error", surface=surface, token=token, **{"class": type(exc).__name__})


def _append_to_queue(line: dict[str, Any]) -> None:
    path = _queue_path()
    with _FileLock(path.with_suffix(".lock"), timeout=2.0):
        lines = _read_lines(path)
        lines.append(json.dumps(line, separators=(",", ":")))
        _write_lines(path, lines)


def _read_lines(path: Path) -> list[str]:
    try:
        return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []


def _write_lines(path: Path, lines: list[str]) -> None:
    from engine.account.store import write_private

    lines = lines[-QUEUE_MAX_EVENTS:]
    while lines and sum(len(x) + 1 for x in lines) > QUEUE_MAX_BYTES:
        lines.pop(0)  # drop the oldest
    if lines:
        write_private(path, "\n".join(lines) + "\n")
    else:
        with contextlib.suppress(OSError):
            path.unlink()


def queued_count() -> int:
    return len(_read_lines(_queue_path())) + len(_memory_only)


# -- sending ------------------------------------------------------------------------------------


def _file_session_token() -> tuple[str, str] | None:
    """(access token, user id) of the shared session if it is usable right now, else None."""
    from engine.account import core  # local import: core imports this module lazily too

    try:
        account = core.check_stored_session(SessionStore())
    except Exception:
        return None
    stored = SessionStore().load()
    if stored is None or account.stale:
        return None  # the server would refuse an expired token; wait for a fresh one
    return stored.access_token, stored.user_id


def _post(rows: list[dict[str, Any]], access_token: str) -> str:
    """Send one batch. Returns "sent", "drop" (the server refused it for good) or "retry"."""
    try:
        http.request_json(
            "POST",
            f"{config.project_url()}/rest/v1/activity_events",
            headers={
                "apikey": config.publishable_key(),
                "Authorization": f"Bearer {access_token}",
                "Prefer": "return=minimal",
            },
            body=rows,
        )
        return "sent"
    except AccountServerError as exc:
        if exc.status in (400, 403, 404, 409, 413, 422):
            return "drop"  # a batch the database refuses would be refused forever
        return "retry"


def _to_wire(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        k: row[k]
        for k in ("event", "surface", "occurred_at", "device_id", "app_version", "os", "detail")
        if k in row
    }


def flush(timeout: float = 5.0) -> int:
    """Send what is queued (bounded by ``timeout`` seconds). Returns the number of events sent."""
    deadline = time.monotonic() + max(0.1, timeout)
    sent = 0
    try:
        sent += _flush_memory(deadline)
        if config.auth_disabled():
            return sent
        auth = _file_session_token()
        if auth is None:
            return sent
        token, uid = auth
        while time.monotonic() < deadline:
            claimed = _claim(uid)
            if not claimed:
                break
            outcome = _post([_to_wire(json.loads(x)) for x in claimed], token)
            if outcome == "retry":
                _requeue(claimed)
                break
            if outcome == "sent":
                sent += len(claimed)
    except Exception:
        pass
    return sent


def _claim(uid: str) -> list[str]:
    """Take up to ``BATCH_SIZE`` lines of this user out of the queue (others' lines are dropped)."""
    path = _queue_path()
    with _FileLock(path.with_suffix(".lock"), timeout=2.0):
        lines = _read_lines(path)
        mine: list[str] = []
        keep: list[str] = []
        for ln in lines:
            try:
                row_uid = json.loads(ln).get("uid")
            except ValueError:
                continue
            if row_uid != uid:
                continue
            (mine if len(mine) < BATCH_SIZE else keep).append(ln)
        _write_lines(path, keep)
        return mine


def _requeue(lines: list[str]) -> None:
    path = _queue_path()
    with _FileLock(path.with_suffix(".lock"), timeout=2.0):
        _write_lines(path, lines + _read_lines(path))


def _flush_memory(deadline: float) -> int:
    with _lock:
        pending = list(_memory_only)
        _memory_only.clear()
    if not pending:
        return 0
    sent = 0
    by_token: dict[str, list[dict[str, Any]]] = {}
    for item in pending:
        by_token.setdefault(item["token"], []).append(item["row"])
    for token, rows in by_token.items():
        for i in range(0, len(rows), BATCH_SIZE):
            chunk = rows[i : i + BATCH_SIZE]
            if time.monotonic() >= deadline:
                outcome = "retry"
            else:
                outcome = _post([_to_wire(r) for r in chunk], token)
            if outcome == "sent":
                sent += len(chunk)
            elif outcome == "retry":
                with _lock:
                    _memory_only[:0] = [{"row": r, "token": token} for r in chunk]
                    del _memory_only[:-QUEUE_MAX_EVENTS]
    return sent


def _sender() -> None:
    while True:
        _wake.wait(FLUSH_INTERVAL_S)
        _wake.clear()
        try:
            if queued_count():
                flush(timeout=10.0)
        except Exception:
            pass


def _ensure_sender() -> None:
    global _thread, _atexit_done
    if not _background:
        return
    with _lock:
        if not _atexit_done:
            _atexit_done = True
            atexit.register(_flush_at_exit)
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_sender, name="radarx-activity", daemon=True)
            _thread.start()


def _flush_at_exit() -> None:
    with contextlib.suppress(Exception):
        if queued_count():
            flush(timeout=2.0)

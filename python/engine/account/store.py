"""The one per-user session file shared by the app, the engine, ``rxd`` and ``import rxd``.

Location (the OS user-config folder, one folder per user, never inside a project):

* macOS    ``~/Library/Application Support/radarx-desktop/session.json``
* Windows  ``%APPDATA%\\radarx-desktop\\session.json``
* Linux    ``$XDG_CONFIG_HOME/radarx-desktop/session.json`` (default ``~/.config/...``)

``RADARX_CONFIG_DIR`` replaces the folder (tests, containers). The file is written with mode 0600
through a temporary file and an atomic rename, and every read-modify-write holds a lock file, so
the app's engine and a command-line run at the same moment cannot corrupt it or lose an update.

What is in it (see docs/ACCOUNTS.md):

``source``        who owns the refresh chain: ``"cli"`` (``rxd login``; the file also holds the
                  refresh token and ``rxd`` refreshes it), ``"app"`` (the desktop app signed in;
                  only the short-lived access token is kept here, because two programs rotating
                  one refresh token would make the server end the session for both) or ``"pat"``
                  (``rxd login --token``: ``pat_token`` is the personal access token, a long-lived
                  secret as sensitive as a password, and ``access_token`` holds the one-hour
                  *entitlement token* it was exchanged for).
``issued_at``     the ``iat`` of the newest access token the server has issued for this session:
                  the moment the server last vouched for the account. The 30-day offline rule
                  counts from here.
``high_water``    the latest clock reading ever seen with this session; a clock set backwards
                  cannot extend the offline period.

Where the secrets are kept: in the operating system's keychain when there is one that works
(:mod:`engine.account.vault`; the file then holds only the non-secret fields and
``"storage": "keychain"``), otherwise in this file with mode 0600, exactly as before. Nothing is
ever moved to the file without saying so (``rxd whoami`` shows where the sign-in is kept and why).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from engine.account import vault

APP_FOLDER = "radarx-desktop"
#: The fields of a session that are secrets (kept in the keychain when there is one).
SECRET_FIELDS = ("access_token", "refresh_token", "pat_token")
LOCK_TIMEOUT_S = 10.0
#: How long a process waits for the right to renew the session (others may be renewing it).
REFRESH_LOCK_TIMEOUT_S = 20.0
#: A renewal lock file older than this is taken over. A renewal makes one network call that is cut
#: off after 5 s, so a healthy holder is done long before; across machines only the age can tell.
REFRESH_LOCK_STALE_S = 60.0


def config_dir() -> Path:
    override = os.environ.get("RADARX_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
    return base / APP_FOLDER


@dataclass
class StoredSession:
    access_token: str
    user_id: str
    email: str
    source: str = "cli"
    refresh_token: str | None = None
    #: a personal access token (source "pat" only): the long-lived secret of an automated job
    pat_token: str | None = None
    issued_at: float = 0.0
    high_water: float = 0.0
    saved_at: float = 0.0
    version: int = 1
    extra: dict[str, Any] = field(default_factory=dict)
    #: where the secrets of this session were read from / written to: "file" or "keychain"
    #: (runtime information, not part of the secret data)
    storage: str = "file"
    #: why the keychain is not used, when it is not (shown by ``rxd whoami``)
    storage_note: str = ""

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("extra", None)
        data.pop("storage", None)
        data.pop("storage_note", None)
        return data

    @classmethod
    def from_json(cls, data: Any) -> StoredSession | None:
        if not isinstance(data, dict):
            return None
        try:
            access = data["access_token"]
            if not isinstance(access, str) or not access:
                return None
            refresh = data.get("refresh_token")
            pat = data.get("pat_token")
            source = data.get("source", "cli")
            if source not in ("app", "cli", "pat"):
                source = "cli"
            return cls(
                access_token=access,
                user_id=str(data.get("user_id") or ""),
                email=str(data.get("email") or ""),
                source=source,
                refresh_token=refresh if isinstance(refresh, str) and refresh else None,
                pat_token=pat if source == "pat" and isinstance(pat, str) and pat else None,
                issued_at=float(data.get("issued_at") or 0.0),
                high_water=float(data.get("high_water") or 0.0),
                saved_at=float(data.get("saved_at") or 0.0),
                storage="keychain" if data.get("storage") == "keychain" else "file",
                storage_note=str(data.get("storage_note") or "")[:200],
            )
        except (KeyError, TypeError, ValueError):
            return None


class _FileLock:
    """An exclusive lock on ``<name>.lock`` (``flock`` on POSIX, ``msvcrt.locking`` on Windows)."""

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_S) -> None:
        self.path = path
        self.timeout = timeout
        self._fh: Any = None

    def __enter__(self) -> _FileLock:
        _make_private_dir(self.path.parent)
        self._fh = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._try_lock()
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise TimeoutError(f"could not lock {self.path}") from None
                time.sleep(0.05)

    def _try_lock(self) -> None:
        if os.name == "nt":
            import msvcrt

            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, *exc: object) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


_process_locks: dict[str, threading.Lock] = {}
_process_locks_guard = threading.Lock()


def _process_lock(path: Path) -> threading.Lock:
    """One in-process lock per lock file. ``flock`` is per open file on a local disk, but on a
    network file system (Linux NFS emulates it with byte-range locks) it is per PROCESS, so the
    threads of one process would not exclude each other."""
    key = str(path)
    with _process_locks_guard:
        return _process_locks.setdefault(key, threading.Lock())


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":  # os.kill(pid, 0) would TERMINATE a process there: age only
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # EPERM: it exists and belongs to someone else
        return True
    return True


class _RefreshLock:
    """The right to renew the session, held by ONE process (or thread) at a time, on one computer
    or on many that share the folder (a home directory on a network file system).

    Three layers, taken in this order:

    1. an in-process lock (threads of one process);
    2. an exclusively created lock file ``session.refresh.lock`` (``O_CREAT | O_EXCL`` is atomic on
       local disks and on NFS v3 and later) holding the host name, process id, time and a random
       token. This is what works where ``flock`` does not;
    3. the ordinary ``flock`` on ``session.lock`` (:class:`_FileLock`), which also keeps the plain
       writers of the session file out.

    A lock file left by a process that died is taken over: at once when it names this host and a
    process that no longer exists, otherwise when it is older than ``REFRESH_LOCK_STALE_S``. Waiting
    uses a random back-off (jitter) so many waiters do not collide in step, and gives up with
    ``TimeoutError`` after ``timeout`` seconds (the caller keeps the session and does not delete
    anything).
    """

    def __init__(
        self,
        directory: Path,
        timeout: float | None = None,
        stale_after: float | None = None,
    ) -> None:
        self.dir = directory
        self.path = directory / "session.refresh.lock"
        self.timeout = REFRESH_LOCK_TIMEOUT_S if timeout is None else timeout
        self.stale_after = REFRESH_LOCK_STALE_S if stale_after is None else stale_after
        self._token = ""
        self._mine: threading.Lock | None = None
        self._flock: _FileLock | None = None
        self._held = False

    # -- the lock file -----------------------------------------------------------------------
    def _create(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        info = {
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "time": time.time(),
            "token": self._token,
        }
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(info))
        return True

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _is_stale(self, info: dict[str, Any]) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return False  # gone: the next create will tell
        pid = info.get("pid")
        same_host = info.get("host") == socket.gethostname()
        if same_host and isinstance(pid, int) and pid > 0 and not _pid_alive(pid):
            return True
        return age > self.stale_after

    def _break_stale(self, seen: dict[str, Any]) -> None:
        """Remove the stale lock that ``seen`` describes, without removing a fresh one by mistake:
        move it aside first, check what was moved, and put it back if it was not the stale one."""
        aside = self.path.with_name(f"{self.path.name}.stale.{os.getpid()}.{secrets.token_hex(4)}")
        try:
            os.rename(self.path, aside)
        except OSError:
            return  # someone else got there first
        try:
            moved = json.loads(aside.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            moved = {}
        if seen.get("token") and moved.get("token") != seen.get("token"):
            with contextlib.suppress(OSError):  # not the stale one: give it back if still free
                os.link(aside, self.path)
        with contextlib.suppress(OSError):
            aside.unlink()

    # -- context manager ---------------------------------------------------------------------
    def __enter__(self) -> _RefreshLock:
        _make_private_dir(self.dir)
        deadline = time.monotonic() + self.timeout
        mine = _process_lock(self.path)
        if not mine.acquire(timeout=max(0.0, self.timeout)):
            raise TimeoutError(f"could not lock {self.path}")
        self._mine = mine
        try:
            self._token = secrets.token_hex(8)
            delay = 0.02
            while not self._create():
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"could not lock {self.path}")
                info = self._read()
                if self._is_stale(info):
                    self._break_stale(info)
                    continue
                time.sleep(random.uniform(delay / 2, delay * 1.5))  # jitter
                delay = min(delay * 1.5, 0.25)
            self._held = True
            left = max(0.05, deadline - time.monotonic())
            self._flock = _FileLock(self.dir / "session.lock", timeout=left).__enter__()
        except BaseException:
            self._release()
            raise
        return self

    def _release(self) -> None:
        if self._flock is not None:
            with contextlib.suppress(Exception):
                self._flock.__exit__(None, None, None)
            self._flock = None
        if self._held:
            # remove the file only while it is still OURS (it may have been taken over as stale)
            if self._read().get("token") == self._token:
                with contextlib.suppress(OSError):
                    self.path.unlink()
            self._held = False
        if self._mine is not None:
            self._mine.release()
            self._mine = None

    def __exit__(self, *exc: object) -> None:
        self._release()


def _make_private_dir(directory: Path) -> None:
    """Create ``directory`` (mode 0700). A folder that already exists is left as it is: it may be
    one the user chose with RADARX_CONFIG_DIR and shares with other things."""
    if directory.exists():
        return
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(directory, 0o700)


def _replace(tmp: str, path: Path) -> None:
    """``os.replace`` that retries a few times: on Windows another program (a virus scanner, a
    second radarx process reading the file) can hold it for a moment and the rename fails with
    PermissionError."""
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 5 or os.name != "nt":
                raise
            time.sleep(0.05 * (attempt + 1))


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically, mode 0600 (temp file + rename in the same folder)."""
    _make_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)  # mkstemp already made the file 0600
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        _replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class SessionStore:
    """Reads and writes ``session.json`` (lock-free reads, locked read-modify-write)."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or config_dir()
        self.path = self.directory / "session.json"
        self._lock_path = self.directory / "session.lock"
        #: what the last keychain write stored (see vault.discard)
        self._handle: dict[str, Any] | None = None

    # -- reading ---------------------------------------------------------------------------
    def load(self) -> StoredSession | None:
        """The stored session, or None when absent or unreadable (never raises)."""
        try:
            return self.load_strict()
        except OSError:
            return None

    def load_strict(self) -> StoredSession | None:
        """Like :meth:`load`, but a file that exists and cannot be read (permissions, a folder in
        its place, a failing disk) or a keychain that cannot be read (locked, refused) raises
        ``OSError`` instead of looking like "not signed in". Absent or corrupt content is None."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if isinstance(data, dict) and data.get("storage") == "keychain":
            # the secrets are in the keychain; VaultError (an OSError) means "could not read it"
            raw = vault.read(self.account)
            if raw is None:
                return None  # the keychain no longer has it (reset, another user): not signed in
            try:
                kept = json.loads(raw)
            except ValueError:
                return None
            if not isinstance(kept, dict):
                return None
            for name in SECRET_FIELDS:
                if isinstance(kept.get(name), str):
                    data[name] = kept[name]
        return StoredSession.from_json(data)

    @property
    def account(self) -> str:
        """The name of this session's keychain entry: one per configuration folder."""
        digest = hashlib.sha256(str(self.directory.resolve()).encode("utf-8")).hexdigest()
        return f"session-{digest[:16]}"

    # -- writing ---------------------------------------------------------------------------
    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        with _FileLock(self._lock_path):
            yield

    @contextlib.contextmanager
    def refresh_locked(self, timeout: float | None = None) -> Iterator[None]:
        """Exclusive right to renew the session: one renewal at a time across threads, processes
        and (via an exclusively created lock file) computers sharing the folder. Raises
        ``TimeoutError`` when it cannot be had in time."""
        with _RefreshLock(self.directory, timeout):
            yield

    # -- "the account server could not be reached" marker (shared by every process) -----------
    @property
    def _netdown_path(self) -> Path:
        return self.directory / "session.netdown"

    def mark_network_down(self, now: float) -> None:
        """Note that renewing just failed for a network reason, so the other processes sharing this
        folder do not each wait out the same timeout (a herd of jobs during an outage)."""
        with contextlib.suppress(OSError):
            write_private(self._netdown_path, repr(float(now)))

    def network_down_recently(self, now: float, window: float) -> bool:
        try:
            marked = float(self._netdown_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return False
        return abs(now - marked) < window

    def clear_network_down(self) -> None:
        with contextlib.suppress(OSError):
            self._netdown_path.unlink()

    def _write(self, session: StoredSession, *, allow_keychain: bool = True) -> None:
        """Write the session: the secrets to the keychain when one works (then the file holds none
        of them), otherwise into the 0600 file. A keychain that fails while writing NEVER costs the
        tokens: they go to the file instead, and ``storage_note`` says why."""
        session.saved_at = time.time()
        data = session.to_json()
        every = {name: data.get(name) for name in SECRET_FIELDS}  # (None for the ones not set)
        secret = {name: value for name, value in every.items() if value}
        for name in SECRET_FIELDS:
            data.pop(name, None)
        note = ""
        where = "file"
        self._handle = None
        if vault.mode() == "file":
            note = "by your choice (RADARX_SECRET_STORE=file)"
        elif allow_keychain:
            usable, _name, reason = vault.status()
            if usable:
                try:
                    self._handle = vault.write(self.account, json.dumps(secret))
                    where = "keychain"
                except vault.VaultError as exc:
                    note = f"the keychain could not be written ({exc}); the file is used instead"
            else:
                note = reason
        data["storage"] = where
        if where == "file":
            data.update(every)  # exactly the file as it always was (secret keys present)
            data["storage_note"] = note
        had_entry = self._storage_marker() == "keychain"
        write_private(self.path, json.dumps(data, indent=1))
        session.storage, session.storage_note = where, note
        if where == "file" and had_entry and vault.mode() == "file":
            # switched to the file by choice: do not leave a copy in the keychain behind
            with contextlib.suppress(OSError):
                vault.delete(self.account, even_if_off=True)

    def save(self, session: StoredSession) -> None:
        with self.locked():
            self._write(session)

    def update(
        self, change: Callable[[StoredSession | None], StoredSession | None]
    ) -> StoredSession | None:
        """Read, change and write back under the lock. ``change`` may return None to clear."""
        with self.locked():
            new = change(self.load())
            if new is None:
                self._unlink()
            else:
                self._write(new)
            return new

    def clear(self) -> None:
        """Remove the session (file and keychain entry). A keychain entry that cannot be removed
        raises (the file is kept, so signing out can be tried again)."""
        with self.locked():
            self._remove_keychain_entry(strict=True)
            self._unlink_file()
        self.clear_network_down()

    def _remove_keychain_entry(self, *, strict: bool, even_if_off: bool = False) -> None:
        """Remove this session's keychain entry (if there is a keychain and an entry). With
        ``strict`` a failure to remove an entry the file points to raises ``VaultError``."""
        marker = self._storage_marker()
        if (
            marker != "keychain"
            and not even_if_off
            and not (vault.mode() == "auto" and vault.available())
        ):
            return
        try:
            vault.delete(self.account, even_if_off=even_if_off)
        except vault.VaultError:
            if strict and marker == "keychain":
                raise

    def _storage_marker(self) -> str:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "file"
        return (
            "keychain" if isinstance(data, dict) and data.get("storage") == "keychain" else "file"
        )

    def _unlink(self) -> None:
        """Remove the session after the server ended it: best effort for the keychain entry."""
        with contextlib.suppress(OSError):
            self._remove_keychain_entry(strict=False)
        self._unlink_file()

    def _unlink_file(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink()

    def migrate_to_keychain(self) -> bool:
        """Move a file session into the keychain, once, and only if the keychain works: write it
        there, read it back, compare, and only then rewrite the file without the secrets. Any
        failure leaves the file exactly as it was. Returns True when the session moved."""
        if vault.mode() != "auto":
            return False
        try:
            with self.locked():
                current = self.load_strict()
                if current is None or current.storage != "file" or not vault.available():
                    return False
                before = {name: getattr(current, name) for name in SECRET_FIELDS}
                original = self.path.read_bytes()
                self._write(current)
                if current.storage != "keychain":
                    return False
                back = self.load_strict()
                if back is None or {name: getattr(back, name) for name in SECRET_FIELDS} != before:
                    write_private(self.path, original.decode("utf-8"))  # undo: exactly as it was
                    if self._handle is not None:
                        vault.discard(self.account, self._handle)
                    return False
                return True
        except (OSError, TimeoutError):
            return False

    def peek(self) -> dict[str, Any] | None:
        """The NON-secret fields of the session file (user id, e-mail, times, ``source``), read
        without touching the keychain: cheap, never prompts, never fails because it is locked."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def note_time(self, now: float, *, min_step_s: float = 3600.0) -> None:
        """Remember ``now`` as the latest clock reading (at most about once an hour)."""
        seen = self.peek()
        if seen is None:
            return
        try:
            high_water = float(seen.get("high_water") or 0.0)
        except (TypeError, ValueError):
            return
        if now < high_water + min_step_s:
            return
        try:
            with self.locked():
                latest = self.load()
                if latest is not None and now > latest.high_water:
                    latest.high_water = now
                    self._write(latest)
        except (OSError, TimeoutError):
            pass


_DEVICE_ID = re.compile(r"[A-Za-z0-9]{8,80}")


def valid_device_id(value: str) -> bool:
    """ASCII letters and digits only (what the database accepts), 8 to 80 of them."""
    return bool(_DEVICE_ID.fullmatch(value))


def device_id(directory: Path | None = None) -> str:
    """A random id of this install (kept across sign-outs). Carries no personal data."""
    directory = directory or config_dir()
    path = directory / "device_id"
    with contextlib.suppress(OSError):
        text = path.read_text(encoding="utf-8").strip()
        if valid_device_id(text):
            return text
    new = uuid.uuid4().hex
    with contextlib.suppress(OSError):
        write_private(path, new)
    return new


def set_device_id(value: str, directory: Path | None = None) -> None:
    """Adopt the id the desktop app uses for this install (so both show as one computer)."""
    if not valid_device_id(value):
        return
    directory = directory or config_dir()
    with contextlib.suppress(OSError):
        path = directory / "device_id"
        if not path.exists() or path.read_text(encoding="utf-8").strip() != value:
            write_private(path, value)

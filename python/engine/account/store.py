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
                  refresh token and ``rxd`` refreshes it) or ``"app"`` (the desktop app signed in;
                  only the short-lived access token is kept here, because two programs rotating
                  one refresh token would make the server end the session for both).
``issued_at``     the ``iat`` of the newest access token the server has issued for this session:
                  the moment the server last vouched for the account. The 30-day offline rule
                  counts from here.
``high_water``    the latest clock reading ever seen with this session; a clock set backwards
                  cannot extend the offline period.

Where the system has a credential store (Windows Credential Manager, macOS Keychain, Linux Secret
Service) the two secrets, ``access_token`` and ``refresh_token``, live there and the file keeps the
rest plus ``"credential_store": "keyring"``; without one the file holds everything and is readable
by its owner only. See ``credentials.py``; a plain file from an earlier version moves into the
store the first time it is read (docs/ACCOUNTS.md).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from engine.account import credentials

logger = logging.getLogger("radarx.account")
APP_FOLDER = "radarx-desktop"
LOCK_TIMEOUT_S = 10.0


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
    issued_at: float = 0.0
    high_water: float = 0.0
    saved_at: float = 0.0
    version: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("extra", None)
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
            source = data.get("source", "cli")
            return cls(
                access_token=access,
                user_id=str(data.get("user_id") or ""),
                email=str(data.get("email") or ""),
                source=source if source in ("app", "cli") else "cli",
                refresh_token=refresh if isinstance(refresh, str) and refresh else None,
                issued_at=float(data.get("issued_at") or 0.0),
                high_water=float(data.get("high_water") or 0.0),
                saved_at=float(data.get("saved_at") or 0.0),
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


def write_private(path: Path, text: str, *, secret: bool = False) -> None:
    """Write ``text`` to ``path`` atomically (temp file + rename in the same folder), mode 0600.

    ``secret=True`` (a file that holds tokens) also makes it owner-only on Windows, where the mode
    means nothing: an access list that names only the current user. That runs ``icacls`` (about
    0.1 s), so it is not done for the small files that are written often."""
    _make_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)  # mkstemp already made the file 0600 (POSIX)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        if secret and os.name == "nt":  # the rename keeps the access list of the temporary file
            credentials.restrict_to_owner(tmp)
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
        # The credential store is for the real per-user folder; a folder chosen with
        # RADARX_CONFIG_DIR (tests, containers) or passed in keeps everything in its own file.
        self._default_location = (
            directory is None and not os.environ.get("RADARX_CONFIG_DIR", "").strip()
        )
        self._lock_depth = 0
        self._warned = False

    def _use_credential_store(self) -> bool:
        return credentials.wanted(self._default_location)

    # -- reading ---------------------------------------------------------------------------
    def load(self) -> StoredSession | None:
        """The stored session, or None when absent or unreadable (never raises)."""
        try:
            return self.load_strict()
        except OSError:
            return None

    def load_strict(self) -> StoredSession | None:
        """Like :meth:`load`, but a file that exists and cannot be read (permissions, a folder in
        its place, a failing disk) raises ``OSError`` instead of looking like "not signed in".
        Absent or corrupt content is None."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if isinstance(data, dict) and data.get(credentials.MARK) == credentials.MARK_VALUE:
            secrets = credentials.read_secrets()  # raises OSError when the store itself fails
            if not secrets:
                return None
            return StoredSession.from_json({**data, **secrets})
        session = StoredSession.from_json(data)
        if session is not None:
            self._move_into_credential_store(session)
        return session

    def _move_into_credential_store(self, session: StoredSession) -> None:
        """A plain file from an earlier version: move its tokens into the credential store and
        rewrite the file without them. Best effort; on any trouble the file stays as it is."""
        if self._lock_depth or not self._use_credential_store():
            return  # inside a locked change the write that follows does it
        try:
            credentials.write_secrets(  # a failing store is found out before the file is touched
                {n: getattr(session, n) for n in credentials.SECRET_FIELDS if getattr(session, n)}
            )
            with self.locked():
                text = self.path.read_text(encoding="utf-8")
                if json.loads(text).get(credentials.MARK) == credentials.MARK_VALUE:
                    return
                self._write(session, keep_time=True)
        except (OSError, ValueError, TimeoutError):
            pass

    # -- writing ---------------------------------------------------------------------------
    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        with _FileLock(self._lock_path):
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1

    def _write(self, session: StoredSession, *, keep_time: bool = False) -> None:
        if not keep_time:
            session.saved_at = time.time()
        data = session.to_json()
        if self._use_credential_store():
            secrets = {name: data[name] for name in credentials.SECRET_FIELDS if data.get(name)}
            try:
                credentials.write_secrets(secrets)
                if credentials.read_secrets() != secrets:  # never drop the plain text on a hope
                    raise credentials.CredentialStoreError("the credential store did not keep it")
                for name in credentials.SECRET_FIELDS:
                    data.pop(name, None)
                data[credentials.MARK] = credentials.MARK_VALUE
            except credentials.CredentialStoreError as exc:
                if not self._warned:
                    self._warned = True
                    logger.warning("%s; the sign-in stays in its private file", exc)
        write_private(
            self.path,
            json.dumps(data, indent=1),
            secret=credentials.MARK not in data,  # tokens in the file: owner-only
        )

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
        with self.locked():
            self._unlink()

    def _unlink(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink()
        if self._default_location or credentials.mode() == "keyring":
            credentials.delete_secrets()

    def note_time(self, now: float, *, min_step_s: float = 3600.0) -> None:
        """Remember ``now`` as the latest clock reading (at most about once an hour)."""
        cur = self.load()
        if cur is None or now < cur.high_water + min_step_s:
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

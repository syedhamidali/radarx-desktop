"""``rxd serve``: the engine on a cluster or server, for the app on your own computer.

The app talks to its engine over a WebSocket. This module starts that same engine (``server.py``) as
a long-running process on the machine where the data lives and lets **one** app reach it through an
SSH port forward (``ssh -L PORT:localhost:PORT user@cluster``). Nothing else is exposed.

Why it is safe to run next to other users (a login node is shared; anyone there can open
``localhost`` ports):

* The socket is bound to ``127.0.0.1`` only. Any other ``--host`` is refused; there is no switch
  for it.
* Every run makes a new random token (:func:`secrets.token_urlsafe`, 256 bits). It is printed once,
  to the terminal of the person who started the run, and is **never** written to a log, to the
  engine's records, to an error text or to the command line of another process. The only other
  place it can go is a file the user asks for with ``--token-file`` (mode 0600).
* The first message of a connection must be ``{"type": "engine_auth", "token": ...}``; it is read
  before anything else, compared in constant time and, if it is missing or wrong, the connection is
  closed with code 1008 without a single other message having been looked at. A failed attempt is
  slowed down (the delay grows with the number of recent failures) and ends the connection; the
  next attempt needs a new connection.
* One app at a time: while an authenticated client is connected a second one is refused (1013).
  A client that has not yet authenticated does not take the place, so a stranger who merely opens
  a socket cannot lock the owner out.
* The browser-side checks ``Host`` (loopback names only: DNS rebinding) and ``Origin`` (the app's,
  or none) are applied before the upgrade.
* Nothing runs without an idle limit: after ``--idle-minutes`` without a request from the app the
  engine stops itself (pings and sign-in refreshes do not count).

What the app may do once it is in: the same as the app does with its own engine, on the files of
the account the engine runs as. The engine runs as the user who started ``rxd serve``, so it can
read and write what that user can and nothing more (the operating system enforces that, not this
code). The upload rules of the public web service do not apply (``docs/WEB.md``); see
``docs/cluster.md`` for the plain statement.

The sign-in is still needed twice: ``rxd serve`` refuses to start without a signed-in account on
the cluster, and the app hands over its own access token on the connection as it does for its
built-in engine (``auth_session``). The cluster's own session file is never overwritten by it.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import hmac
import json
import logging
import os
import secrets
import socket
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("radarx-serve")

#: The only interfaces ``rxd serve`` binds. ``localhost`` is bound as ``127.0.0.1``.
LOOPBACK_HOSTS = {"127.0.0.1": "127.0.0.1", "localhost": "127.0.0.1"}
#: ``Host`` header names that reach this engine (any port: the forwarded port may differ).
HOST_HEADER_NAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})
#: ``(scheme, host)`` of the pages that may open a connection: the app and its dev server.
ORIGINS = frozenset(
    {
        ("tauri", "localhost"),
        ("http", "tauri.localhost"),
        ("https", "tauri.localhost"),
        ("http", "localhost"),
        ("http", "127.0.0.1"),
    }
)
MIN_PORT = 1024
MAX_PORT = 65535
DEFAULT_IDLE_MINUTES = 120
HANDSHAKE_TIMEOUT_S = 10.0
#: Connections that have not authenticated yet; more are turned away at once. High on purpose: it is
#: only a guard for memory, and a low number would let a stranger's idle sockets keep the owner out.
MAX_PENDING_HANDSHAKES = 128
AUTH_CLOSE = (1008, "Authentication failed")
BUSY_CLOSE = (1013, "Another app is already connected to this engine")
#: Messages that do not count as use (the app pings, and renews its sign-in, by itself).
NOT_ACTIVITY = frozenset({"ping", "get_web_info", "auth_session", "auth_status", "auth_signout"})
#: Variables a batch system sets inside a job (the node is then usually not reachable directly).
BATCH_ENV = ("SLURM_JOB_ID", "PBS_JOBID", "LSB_JOBID", "JOB_ID", "SGE_TASK_ID")


class ServeError(ValueError):
    """The command was used wrongly (a host that is not this computer, a port out of range ...)."""


def resolve_host(host: str | None) -> str:
    """The address to bind: only this computer. Anything else is refused."""
    name = (host or "127.0.0.1").strip().lower()
    if name not in LOOPBACK_HOSTS:
        raise ServeError(
            f"--host {host!r} is refused: rxd serve listens on this computer only (127.0.0.1). "
            "Other computers reach it through an SSH port forward, see docs/cluster.md."
        )
    return LOOPBACK_HOSTS[name]


def check_port(port: int | None) -> int:
    """0 (pick a free port) or a port a normal user may bind."""
    if port is None:
        return 0
    if port == 0 or MIN_PORT <= port <= MAX_PORT:
        return port
    raise ServeError(
        f"--port must be {MIN_PORT}-{MAX_PORT} (or left out for a free one), got {port}"
    )


def new_token() -> str:
    """A fresh per-run secret: 32 random bytes, URL-safe (43 characters)."""
    return secrets.token_urlsafe(32)


def token_matches(supplied: object, expected: str) -> bool:
    """Constant-time comparison; anything that is not a string never matches."""
    if not isinstance(supplied, str):
        return False
    return hmac.compare_digest(supplied.encode("utf-8", "replace"), expected.encode("utf-8"))


def host_header_ok(value: str | None) -> bool:
    """``Host`` must name this computer (blocks DNS-rebinding pages); the port does not matter."""
    if not value:
        return False
    name = value.strip().lower()
    if name.startswith("["):
        name = name.split("]", 1)[0] + "]"
    else:
        name = name.rsplit(":", 1)[0] if ":" in name else name
    return name in HOST_HEADER_NAMES


def origin_ok(value: str | None) -> bool:
    """No ``Origin`` (a program) or the app's own; any other web page is refused."""
    if value is None:
        return True
    try:
        parts = urlsplit(value)
        return (parts.scheme, parts.hostname or "") in ORIGINS and not parts.username
    except ValueError:
        return False


class FailureLimiter:
    """Slows failed attempts: more recent failures mean a longer wait.

    It never refuses a correct token, so nobody can lock the owner out by guessing; it only makes
    guessing slow (and the token cannot be guessed anyway: 256 random bits).
    """

    def __init__(
        self,
        base_s: float = 0.25,
        cap_s: float = 5.0,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_s, self.cap_s, self.window_s, self._clock = base_s, cap_s, window_s, clock
        self._failures: list[float] = []

    def record(self) -> float:
        """Note one failed attempt; returns how long to wait before ending the connection."""
        now = self._clock()
        self._failures = [t for t in self._failures if now - t < self.window_s]
        self._failures.append(now)
        return min(self.cap_s, self.base_s * 2 ** (len(self._failures) - 1))


@dataclass
class ServeConfig:
    """What ``rxd serve`` was asked to do."""

    host: str = "127.0.0.1"
    port: int = 0
    idle_minutes: float = DEFAULT_IDLE_MINUTES
    token_file: Path | None = None
    #: Tests shorten these.
    handshake_timeout_s: float = HANDSHAKE_TIMEOUT_S
    max_pending: int = MAX_PENDING_HANDSHAKES
    fail_delay_base_s: float = 0.25


def write_token_file(path: Path, token: str) -> None:
    """Write the token where only this user can read it (mode 0600, symbolic links not followed)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)  # a file that existed with wider rights is tightened
        if hasattr(os, "getuid") and os.fstat(fd).st_uid != os.getuid():
            raise ServeError(f"{path} belongs to another user; choose a file of your own")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(token + "\n")
    finally:
        if fd != -1:
            os.close(fd)


def in_batch_job() -> bool:
    return any(os.environ.get(name) for name in BATCH_ENV)


def banner(
    *,
    host: str,
    port: int,
    token: str | None,
    token_file: Path | None,
    user: str,
    node: str,
    idle_minutes: float,
    batch: bool = False,
) -> str:
    """What the person at the terminal reads: the address, the token and the exact ``ssh`` line.

    ``token`` is None when it went to a file instead (then the path is shown, never the token).
    """
    forward = f"ssh -L {port}:localhost:{port} {user}@{node}"
    lines = [
        "Radarx engine is running. Keep this window open; press Ctrl+C to stop it.",
        "",
        f"  Address   {host}:{port}",
    ]
    if token is not None:
        lines.append(f"  Token     {token}")
        lines.append("            (shown once, valid for this run only; treat it like a password)")
    else:
        lines.append(f"  Token     written to {token_file} (readable only by you)")
    lines += [
        "",
        "On your own computer open a terminal and run (use the host name you log in with):",
        "",
        f"  {forward}",
        "",
    ]
    if batch:
        lines += [
            "This looks like a batch job; reach a compute node through the login node:",
            "",
            f"  ssh -J {user}@LOGIN-NODE -L {port}:localhost:{port} {user}@{node}",
            "",
        ]
    lines += [
        "Then, in the app: Settings > Engine > Connect to a remote engine (address and token).",
        (
            f"The engine stops by itself after {idle_minutes:g} minutes without use."
            if idle_minutes
            else "There is no idle limit (--idle-minutes 0): stop it yourself when you are done."
        ),
    ]
    return "\n".join(lines)


class RemoteEngine:
    """One ``rxd serve`` run: the token, the single client slot, the idle clock, the server."""

    def __init__(
        self,
        config: ServeConfig,
        token: str | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._token = token or new_token()
        self.host = resolve_host(config.host)
        check_port(config.port)
        self.limiter = FailureLimiter(base_s=config.fail_delay_base_s, clock=clock)
        self._clock = clock
        self.port: int | None = None
        self.ready = asyncio.Event()
        self.stop = asyncio.Event()
        self.client: Any = None
        self._tasks: set[Any] | None = None
        self._pending = 0
        self._last_use = clock()
        self.stop_reason = ""

    # -- the secret ------------------------------------------------------------------------

    @property
    def token(self) -> str:
        """For the one place that shows it (the start banner) and the tests."""
        return self._token

    def __repr__(self) -> str:  # the token must never reach a log through a repr
        return f"<RemoteEngine {self.host}:{self.port}>"

    # -- the HTTP part of the upgrade --------------------------------------------------------

    def process_request(self, connection: Any, request: Any) -> Any:
        """Refuse a wrong ``Host`` or a foreign web page before the WebSocket is opened."""
        headers = request.headers
        if not host_header_ok(headers.get("Host")) or not origin_ok(headers.get("Origin")):
            logger.warning("Refused a connection with an unexpected Host or Origin header")
            return connection.respond(403, "Forbidden\n")
        return None

    # -- one connection ------------------------------------------------------------------------

    async def handle(self, ws: Any) -> None:
        import server

        if self._pending >= self.config.max_pending:
            await ws.close(1013, "Too many connections starting")
            return
        self._pending += 1
        try:
            authenticated = await self._authenticate(ws)
        finally:
            self._pending -= 1
        if not authenticated:
            return
        if self.client is not None:
            with contextlib.suppress(Exception):
                await ws.send(
                    json.dumps(
                        {
                            "type": "error",
                            "code": "engine_busy",
                            "message": "Another app is already connected to this engine.",
                        }
                    )
                )
            await ws.close(*BUSY_CLOSE)
            return
        self.client = ws
        try:
            await ws.send(json.dumps({"type": "engine_auth_ok", "remote": True}))
        except Exception:
            self.client = None
            return
        self._last_use = self._clock()
        logger.info("App connected (%s)", ws.remote_address)
        try:
            await server._handle_connection(ws, tracker=self, remote_engine=True)
        finally:
            self.client = None
            self._tasks = None
            self._last_use = self._clock()
            logger.info("App disconnected")

    async def _authenticate(self, ws: Any) -> bool:
        """Read the first message and check the token; end the connection (1008) unless right."""
        ok = False
        try:
            first = await asyncio.wait_for(ws.recv(), self.config.handshake_timeout_s)
            if isinstance(first, str):
                try:
                    message = json.loads(first)
                except (ValueError, RecursionError):
                    message = None
                ok = (
                    isinstance(message, dict)
                    and message.get("type") == "engine_auth"
                    and token_matches(message.get("token"), self._token)
                )
        except (TimeoutError, asyncio.TimeoutError):
            ok = False
        except Exception:
            # a closed or broken socket, a frame that is too large ...: nothing to reply to
            logger.info("A connection ended before it authenticated")
            return False
        if ok:
            return True
        delay = self.limiter.record()
        logger.warning("Refused a connection without a valid token")
        if delay > 0:
            await asyncio.sleep(delay)
        with contextlib.suppress(Exception):
            await ws.close(*AUTH_CLOSE)
        return False

    # -- use and idle -------------------------------------------------------------------------

    def touch(self, message_type: str | None, tasks: set[Any]) -> None:
        """Called by the server for every message of the connected app."""
        self._tasks = tasks
        if message_type not in NOT_ACTIVITY:
            self._last_use = self._clock()

    async def _idle_watch(self) -> None:
        limit = self.config.idle_minutes * 60
        if limit <= 0:
            return
        interval = max(0.05, min(30.0, limit / 4))
        while not self.stop.is_set():
            await asyncio.sleep(interval)
            if self._tasks:  # a request is still running: that is use
                self._last_use = self._clock()
            elif self._clock() - self._last_use >= limit:
                self.stop_reason = f"idle for {self.config.idle_minutes:g} minutes"
                logger.info("Stopping: %s", self.stop_reason)
                self.stop.set()
                return

    # -- the run -----------------------------------------------------------------------------------

    async def run(self, on_ready: Callable[[RemoteEngine], None] | None = None) -> None:
        """Serve until Ctrl+C / SIGTERM or the idle limit; returns after a clean shutdown."""
        import server

        # websockets logs every frame at DEBUG, and the first frame carries the token
        for name in ("websockets", "websockets.server", "websockets.client"):
            logging.getLogger(name).setLevel(logging.WARNING)
        server.EXPECTED_SESSION_TOKEN = (
            None  # the app's own sidecar handshake; this one has its own
        )

        def ready(port: int) -> None:
            self.port = port
            self.ready.set()
            if on_ready is not None:
                on_ready(self)

        watcher = asyncio.get_running_loop().create_task(self._idle_watch())
        try:
            await server._run_server(
                self.config.port,
                self.host,
                handler=self.handle,
                on_ready=ready,
                stop_event=self.stop,
                serve_options={
                    "process_request": self.process_request,
                    "compression": None,  # no CPU on a shared node; `ssh -C` compresses if wanted
                    "server_header": None,
                    "ping_interval": 10,  # a dropped tunnel frees the slot within ~30 s
                    "ping_timeout": 20,
                },
            )
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


def start_banner(engine: RemoteEngine, *, show_token: bool) -> str:
    """The text printed at start (the token only when it was not sent to a file)."""
    try:
        user = getpass.getuser()
    except Exception:
        user = "USER"
    assert engine.port is not None
    return banner(
        host=engine.host,
        port=engine.port,
        token=engine.token if show_token else None,
        token_file=engine.config.token_file,
        user=user,
        node=socket.gethostname() or "CLUSTER",
        idle_minutes=engine.config.idle_minutes,
        batch=in_batch_job(),
    )


def remove_quietly(path: Path | None) -> None:
    if path is not None:
        with contextlib.suppress(OSError):
            if stat.S_ISREG(os.lstat(path).st_mode):
                path.unlink()

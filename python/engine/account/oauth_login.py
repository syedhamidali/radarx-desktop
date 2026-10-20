"""``rxd login --browser``: sign in with Google / Apple / Facebook from a terminal.

The same flow as the desktop app (``src/lib/auth/oauth.ts`` and ``src-tauri/src/oauth.rs``):

1. make a PKCE pair (a random secret *verifier* that never leaves this process, and its SHA-256
   *challenge*);
2. open a one-shot listener on ``127.0.0.1`` at a random port;
3. open the system browser at the project's ``/auth/v1/authorize`` address (also printed, for SSH);
4. wait, with a time limit, for ONE request to ``/auth/callback`` that carries a ``code`` or an
   ``error``; everything else (favicons, scanners, other programs, half-open connections) is
   answered 404 and ignored, each connection on its own short-lived thread;
5. trade the code and the verifier for a session at the account server and store it like a
   password login.

Why there is no ``state`` parameter: the project's redirect allow-list is matched against the whole
redirect address (``http://127.0.0.1:*/auth/callback``), so a ``?state=...`` query on it would not
match and the server would send the code to the site address instead. The attack ``state`` stops
(login CSRF: somebody makes your browser deliver THEIR code to your listener) is stopped by PKCE
itself: the server only accepts a code together with the verifier whose challenge started that
sign-in, so a foreign code fails at the exchange and nothing is stored. The listener also answers
only requests whose ``Host`` header is ``127.0.0.1:<port>``.

Provider-supplied text (``error_description`` and the like) is never shown, only fixed messages.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import re
import secrets
import socket
import sys
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from engine.account import core, gotrue
from engine.account.errors import AccountServerError

PROVIDERS = ("google", "apple", "facebook")
PROVIDER_NAMES = {"google": "Google", "apple": "Apple", "facebook": "Facebook"}

#: How long the terminal waits for the browser (seconds).
WAIT_S = 300.0
#: How long one connection may take to send its request line and headers (seconds, in total).
READ_TIMEOUT_S = 1.0
#: At most this many connections are served at once; beyond that the OLDEST (an idle one: every
#: connection is cut off after READ_TIMEOUT_S) is closed to make room, never the newest.
MAX_CONNECTIONS = 64
_MAX_REQUEST = 8192

_PAGE = (
    "<!doctype html><meta charset=utf-8><title>radarx</title>"
    '<body style="font:16px system-ui;background:#070c16;color:#e0ebf7;display:grid;'
    'place-items:center;height:100vh;margin:0"><div style="text-align:center">'
    '<h2 style="color:#2bd9f5">radarx</h2>'
    "<p>You can close this tab and return to the terminal.</p></div></body>"
)

#: What a code from the account server looks like (a UUID today); anything else is not the answer.
_CODE = re.compile(r"[A-Za-z0-9_.~-]{1,512}")
_ERROR = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class OAuthFailed(Exception):  # noqa: N818 - reads better than OAuthFailedError
    """The browser sign-in did not complete; ``str(exc)`` is a fixed, friendly sentence."""


def new_pkce() -> tuple[str, str]:
    """``(verifier, challenge)``: 64 random URL-safe characters and their S256 hash (RFC 7636)."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def parse_callback(request: str, port: int) -> dict[str, str] | None:
    """The ``code`` or ``error`` of a ``GET /auth/callback?...`` request, else None.

    A request counts only when it is a GET of exactly ``/auth/callback``, is addressed to
    ``127.0.0.1:<port>`` and carries a non-empty, plausible ``code`` or ``error``. Nothing else
    may end the wait.
    """
    lines = request.split("\r\n") if "\r\n" in request else request.split("\n")
    parts = lines[0].split()
    if len(parts) != 3 or parts[0] != "GET" or not parts[2].startswith("HTTP/1."):
        return None
    host = ""
    for line in lines[1:]:
        if line.lower().startswith("host:"):
            host = line.split(":", 1)[1].strip().lower()
            break
    if host != f"127.0.0.1:{port}":
        return None
    try:
        target = urlsplit(parts[1])
    except ValueError:
        return None
    if target.path != "/auth/callback":
        return None
    query = parse_qs(target.query, keep_blank_values=False, max_num_fields=20)
    code = (query.get("code") or [""])[0]
    error = (query.get("error") or [""])[0]
    if code and _CODE.fullmatch(code):
        return {"code": code}
    if error and not code and _ERROR.fullmatch(error):
        return {"error": error}
    return None


class LoopbackListener:
    """A one-shot HTTP listener on ``127.0.0.1`` (random port) for the OAuth redirect."""

    def __init__(self, *, wait: float | None = None) -> None:
        self.wait = WAIT_S if wait is None else wait
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._sock.bind(("127.0.0.1", 0))
            self._sock.listen(2 * MAX_CONNECTIONS)
            self._sock.settimeout(0.1)
        except OSError:
            self._sock.close()
            raise
        self.port: int = self._sock.getsockname()[1]
        self._lock = threading.Lock()
        self._active = 0
        self._open: dict[socket.socket, float] = {}
        self._result: dict[str, str] | None = None
        self._done = threading.Event()
        self._deadline: float | None = None

    # -- one connection ----------------------------------------------------------------------
    def _serve(self, conn: socket.socket) -> None:
        try:
            deadline = time.monotonic() + READ_TIMEOUT_S
            data = b""
            while b"\r\n\r\n" not in data and b"\n\n" not in data and len(data) < _MAX_REQUEST:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                conn.settimeout(left)
                try:
                    chunk = conn.recv(2048)
                except (TimeoutError, OSError):
                    break
                if not chunk:
                    break
                data += chunk
            found = parse_callback(data.decode("latin-1"), self.port)
            body = _PAGE if found else ""
            status = "200 OK" if found else "404 Not Found"
            head = (
                f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body.encode())}\r\nCache-Control: no-store\r\n"
                "Referrer-Policy: no-referrer\r\nConnection: close\r\n\r\n"
            )
            try:
                conn.settimeout(1.0)
                conn.sendall(head.encode() + body.encode())
            except OSError:
                pass
            if found:
                with self._lock:
                    if self._result is None:  # the first real redirect wins
                        self._result = found
                self._done.set()
        finally:
            with contextlib.suppress(OSError):
                conn.close()
            with self._lock:
                self._active -= 1
                self._open.pop(conn, None)

    def rearm(self) -> None:
        """Forget the redirect that was received and wait for another (the time limit is the same):
        for a redirect that turned out not to belong to this sign-in."""
        with self._lock:
            self._result = None
            self._done.clear()

    def wait_for_redirect(self, cancelled: Callable[[], bool] | None = None) -> dict[str, str]:
        """Block until the real redirect arrives. Raises :class:`OAuthFailed` on timeout/cancel."""
        if self._deadline is None:
            self._deadline = time.monotonic() + self.wait
        deadline = self._deadline
        while not self._done.is_set():
            if cancelled is not None and cancelled():
                raise OAuthFailed("The sign-in was cancelled.")
            if time.monotonic() > deadline:
                raise OAuthFailed(
                    f"Timed out after {int(self.wait)} seconds waiting for the browser. "
                    "Nothing was changed; run the command again to retry."
                )
            try:
                conn, _addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with self._lock:
                if self._active >= MAX_CONNECTIONS and self._open:
                    oldest = min(self._open, key=self._open.__getitem__)
                    with contextlib.suppress(OSError):
                        oldest.close()  # its thread sees the error and ends
                self._active += 1
                self._open[conn] = time.monotonic()
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()
        with self._lock:
            result = self._result
        if result is None:
            raise OAuthFailed("The sign-in stopped before it finished.")
        return result

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.close()

    def __enter__(self) -> LoopbackListener:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# -- the whole flow -----------------------------------------------------------------------------


def failure_message(error: str) -> str:
    """A fixed sentence for the provider's ``error`` code (its own text is never shown)."""
    if error.lower() == "access_denied":
        return "The sign-in was cancelled."
    return "The sign-in was not completed. Please try again."


def has_display() -> bool:
    """Could a browser window open here? False over SSH and on a Linux box with no display (where
    ``webbrowser`` would start a text-mode browser inside the terminal)."""
    if any(os.environ.get(v) for v in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")):
        return False
    if sys.platform.startswith("linux") or sys.platform.endswith("bsd"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def open_system_browser(url: str) -> bool:
    """Ask the system to open ``url``; True when it says it did."""
    import webbrowser

    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:  # a broken desktop helper must not stop the sign-in
        return False


def check_provider(provider: str) -> None:
    """Raise :class:`OAuthFailed` unless the project has ``provider`` switched on."""
    if provider not in PROVIDERS:
        raise OAuthFailed(f"Unknown provider. Choose one of: {', '.join(PROVIDERS)}.")
    try:
        enabled = gotrue.enabled_providers()
    except AccountServerError as exc:
        raise OAuthFailed(gotrue.friendly_login_error(exc)) from None
    if provider not in enabled:
        listed = [p for p in PROVIDERS if p in enabled]
        other = (
            f" Switched on: {', '.join(PROVIDER_NAMES[p] for p in listed)}."
            if listed
            else " No social sign-in is switched on yet."
        )
        raise OAuthFailed(
            f"Sign in with {PROVIDER_NAMES[provider]} is not switched on yet for radarx.{other} "
            "E-mail and password (rxd login) always works."
        )


def login(
    provider: str,
    *,
    say: Callable[[str], None],
    open_browser: bool = True,
    wait: float | None = None,
    opener: Callable[[str], bool] | None = None,
    store: Any = None,
) -> core.Account:
    """Run the browser sign-in for ``provider`` and store the session. Raises
    :class:`OAuthFailed` (a friendly sentence) when it cannot finish."""
    check_provider(provider)
    verifier, challenge = new_pkce()
    try:
        listener = LoopbackListener(wait=wait)
    except OSError:
        raise OAuthFailed("Could not open a local port for the sign-in.") from None
    with listener:
        redirect = f"http://127.0.0.1:{listener.port}/auth/callback"
        url = gotrue.authorize_url(provider, redirect, challenge)
        name = PROVIDER_NAMES[provider]
        opened = False
        if open_browser and has_display():
            opened = (opener or open_system_browser)(url)
        if opened:
            say(f"Opened your browser to sign in with {name}. If it did not open, use:")
        else:
            say(f"Open this address in a browser on THIS computer to sign in with {name}:")
        say(f"  {url}")
        say(
            "The browser must run on this same machine: the answer comes back to a port on "
            "127.0.0.1 here.\nOver SSH or on a cluster that does not work; use an access token "
            "instead (rxd login --token, see docs/ACCOUNTS.md).\n"
            f"Waiting up to {int(listener.wait)} seconds (Ctrl-C to cancel) ..."
        )
        for attempt in range(3):
            answer = listener.wait_for_redirect()
            if "error" in answer:
                raise OAuthFailed(failure_message(answer["error"]))
            try:
                return core.login_with_code(answer["code"], verifier, store)
            except AccountServerError as exc:
                if exc.status in (400, 422):
                    # a redirect that is not for THIS sign-in (a stray or forged one: PKCE makes it
                    # useless): ignore it and keep waiting for the real one
                    if attempt < 2:
                        listener.rearm()
                        say(
                            "Ignored a redirect that did not belong to this sign-in; still waiting."
                        )
                        continue
                    raise OAuthFailed(
                        "The account server did not accept the sign-in code (it may have "
                        "expired). Please try again."
                    ) from None
                raise OAuthFailed(gotrue.friendly_login_error(exc)) from None
    raise OAuthFailed("The sign-in did not finish.")  # (not reached)

"""Who may open a WebSocket to the app's own engine (``server.py``, not the web service).

A browser lets any web page open ``ws://localhost:PORT``; only the ``Origin`` and ``Host`` headers
tell the engine who is knocking. The checks run in the HTTP part of the upgrade, so a refused page
gets a plain 403 and no WebSocket is ever opened:

* ``Host`` must name this computer (loopback names only; the port does not matter): a page that
  makes its own name resolve to 127.0.0.1 (DNS rebinding) carries its own name in ``Host``.
* ``Origin`` is either absent (a program, not a web page) or exactly one of the pages the app
  itself uses: the installed app's webview (``tauri://localhost`` on macOS and Linux,
  ``http(s)://tauri.localhost`` on Windows) and the development server on its fixed port.

A program without an ``Origin`` is still stopped by the per-run token when the app started the
engine (``XRADAR_SESSION_TOKEN``, checked on every message) and by the account gate.
"""

from __future__ import annotations

from urllib.parse import urlsplit

#: The Vite development server, on a fixed port (``strictPort`` in its config, ``devUrl`` in Tauri).
DEV_SERVER_PORT = 1420

#: ``Host`` header names that reach this engine (any port).
HOST_NAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})
#: ``(scheme, host)`` of the installed app's webview; the port is never part of these origins.
APP_ORIGINS = frozenset(
    {
        ("tauri", "localhost"),
        ("http", "tauri.localhost"),
        ("https", "tauri.localhost"),
    }
)
DEV_HOSTS = frozenset({"localhost", "127.0.0.1"})


def host_ok(value: str | None) -> bool:
    """``Host`` names this computer. A missing ``Host`` is refused (every browser sends one)."""
    if not value:
        return False
    name = value.strip().lower()
    if name.startswith("["):
        name = name.split("]", 1)[0] + "]"
    else:
        name = name.rsplit(":", 1)[0] if ":" in name else name
    return name in HOST_NAMES


def origin_ok(value: str | None) -> bool:
    """No ``Origin`` (a program) or one of the app's own pages; every other web page is refused."""
    if value is None:
        return True
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return False
    if parts.username or parts.password or parts.path not in ("", "/") or parts.query:
        return False
    if parts.fragment:
        return False
    host = parts.hostname or ""
    if (parts.scheme, host) in APP_ORIGINS:
        return port is None
    return parts.scheme == "http" and host in DEV_HOSTS and port == DEV_SERVER_PORT


def process_request(connection, request):
    """``websockets`` hook: answer 403 to a wrong ``Host`` or a foreign ``Origin``, else go on."""
    headers = request.headers
    if host_ok(headers.get("Host")) and origin_ok(headers.get("Origin")):
        return None
    return connection.respond(403, "Forbidden\n")

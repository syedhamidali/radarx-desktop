"""The only place the account code touches the network (one function, easy to replace in tests)."""

from __future__ import annotations

import ssl
import threading
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from engine.account.config import HTTP_TIMEOUT_S
from engine.account.errors import AccountServerError

_USER_AGENT = "radarx-desktop-account/1"
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def url_allowed(url: str) -> bool:
    """https anywhere, plain http only to this machine (a local stack, tests). The host is parsed:
    ``http://127.0.0.1.evil.example`` and ``http://localhost@evil.example`` are not local."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme == "https":
        return bool(host)
    return parsed.scheme == "http" and host in _LOCAL_HOSTS and parsed.username is None


def _context() -> ssl.SSLContext:
    """The system trust store (so a TLS-intercepting corporate proxy whose root is installed works)
    plus certifi's bundle (for Pythons whose default store is empty, e.g. python.org on macOS)."""
    context = ssl.create_default_context()
    try:
        import certifi

        context.load_verify_locations(cafile=certifi.where())
    except Exception:  # the system store alone may be enough
        pass
    return context


def _request(
    method: str, url: str, headers: dict[str, str], data: bytes | None, timeout: float
) -> Any:
    import json

    req = urllib.request.Request(url, data=data, method=method)  # scheme checked above
    req.add_header("User-Agent", _USER_AGENT)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_context()) as resp:
            raw = resp.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        code, msg = "", ""
        try:
            payload = json.loads(exc.read(65536).decode("utf-8", "replace"))
            if isinstance(payload, dict):
                code = str(payload.get("error_code") or payload.get("error") or "")
                msg = str(
                    payload.get("msg")
                    or payload.get("message")
                    or payload.get("error_description")
                    or ""
                )
        except Exception:  # an unparseable error body is still an error
            pass
        raise AccountServerError(exc.code, code, msg) from None
    except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError) as exc:
        raise AccountServerError(0, "unreachable", type(exc).__name__) from None
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise AccountServerError(
            0, "bad_response", "the account server sent something unexpected"
        ) from exc


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: float = HTTP_TIMEOUT_S,
) -> Any:
    """One HTTPS request with a JSON body; returns the decoded JSON (None for an empty answer).

    Raises :class:`AccountServerError`: ``status`` 0 when the server cannot be reached (or does not
    answer within ``timeout`` seconds in TOTAL: connect, TLS and read together, not per step),
    otherwise
    the HTTP status with the server's ``error_code`` / ``msg`` when it sent them.
    """
    import json

    if not url_allowed(url):
        raise AccountServerError(0, "bad_url", "refusing a non-https account URL")
    timeout = max(0.05, float(timeout))
    data = None if body is None else json.dumps(body).encode("utf-8")
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = _request(method, url, dict(headers or {}), data, timeout)
        except BaseException as exc:  # handed to the caller below
            box["error"] = exc

    # A socket timeout limits each step, not the sum (a server that accepts and trickles bytes, or
    # a slow TLS handshake, can take several times as long): the deadline is enforced from outside.
    worker = threading.Thread(target=work, name="radarx-account-http", daemon=True)
    worker.start()
    worker.join(timeout + 0.25)
    if worker.is_alive():
        raise AccountServerError(0, "unreachable", "timeout")
    if "error" in box:
        raise box["error"]
    return box.get("value")

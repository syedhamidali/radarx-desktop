"""The only place the account code touches the network (one function, easy to replace in tests)."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any

from engine.account.config import HTTP_TIMEOUT_S
from engine.account.errors import AccountServerError

_USER_AGENT = "radarx-desktop-account/1"


def _context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: float = HTTP_TIMEOUT_S,
) -> Any:
    """One HTTPS request with a JSON body; returns the decoded JSON (None for an empty answer).

    Raises :class:`AccountServerError`: ``status`` 0 when the server cannot be reached, otherwise
    the HTTP status with the server's ``error_code`` / ``msg`` when it sent them. Only ``https``
    (and ``http`` to localhost, for a local stack) URLs are accepted.
    """
    if not (url.startswith("https://") or url.startswith(("http://127.0.0.1", "http://localhost"))):
        raise AccountServerError(0, "bad_url", "refusing a non-https account URL")
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", _USER_AGENT)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
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
        except Exception:
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

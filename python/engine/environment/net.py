"""Polite, bounded HTTP for environment data (standard library only, no new dependency).

Good manners built in, so a provider author cannot forget them:

* an honest ``User-Agent`` naming the app, its version and its home page;
* **offline mode** is checked before any socket is opened;
* a limit on simultaneous connections and a minimum gap between requests to one host;
* retries with exponential backoff and jitter for timeouts, connection resets and HTTP
  408/429/5xx, honouring ``Retry-After``; other 4xx answers are final;
* hard timeouts: connect, read, and a total deadline for the whole transfer;
* a byte cap per transfer (the size the user approved), a cancel flag checked between blocks;
* only ``http``/``https``; a redirect may not downgrade ``https`` to ``http``.
"""

from __future__ import annotations

import contextlib
import http.client
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from engine.environment.errors import FetchCancelled, FetchFailed, OfflineError

RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
HOME_PAGE = "https://radarx.labshope.com"
CHUNK = 256 * 1024

Progress = Callable[[int, int | None], None]


def default_user_agent() -> str:
    try:
        from engine import provenance

        version = provenance.software_version()
    except Exception:
        version = "unknown"
    return f"radarx-desktop/{version} (+{HOME_PAGE})"


class _NoDowngrade(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        old, new = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(newurl)
        if new.scheme not in ("http", "https") or (old.scheme == "https" and new.scheme != "https"):
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "redirect to an insecure or unsupported URL refused",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener(req: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.build_opener(_NoDowngrade).open(req, timeout=timeout)


def check_url(url: str) -> str:
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise FetchFailed(f"Refusing to fetch {url!r}: only http(s) URLs are allowed.")
    return url


class HttpClient:
    """Shared by all providers of one :class:`~engine.environment.core.Environment`."""

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        offline: Callable[[], bool] = lambda: False,
        max_concurrent: int = 2,
        retries: int = 3,
        backoff_s: float = 0.5,
        backoff_cap_s: float = 30.0,
        connect_timeout_s: float = 10.0,
        read_timeout_s: float = 30.0,
        total_timeout_s: float = 900.0,
        min_interval_s: float = 0.1,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.user_agent = user_agent or default_user_agent()
        self._offline = offline
        self.max_concurrent = max(1, int(max_concurrent))
        self.retries = max(0, int(retries))
        self.backoff_s = backoff_s
        self.backoff_cap_s = backoff_cap_s
        self.connect_timeout_s = connect_timeout_s
        self.read_timeout_s = read_timeout_s
        self.total_timeout_s = total_timeout_s
        self.min_interval_s = min_interval_s
        self._opener = opener or _default_opener
        self._sleep = sleep
        self._sem = threading.BoundedSemaphore(self.max_concurrent)
        self._host_lock = threading.Lock()
        self._last_hit: dict[str, float] = {}
        self.requests_made = 0  # counters for tests and diagnostics
        self.retries_made = 0

    # -- helpers --------------------------------------------------------------------------

    def set_max_concurrent(self, n: int) -> None:
        """Change the connection limit (applies to requests started from now on)."""
        self.max_concurrent = max(1, int(n))
        self._sem = threading.BoundedSemaphore(self.max_concurrent)

    def _wait(self, seconds: float, cancel: threading.Event | None) -> None:
        if seconds <= 0:
            return
        if self._sleep is not None:
            self._sleep(seconds)
            if cancel is not None and cancel.is_set():
                raise FetchCancelled("cancelled")
        elif cancel is not None:
            if cancel.wait(seconds):
                raise FetchCancelled("cancelled")
        else:
            time.sleep(seconds)

    def _politeness_gap(self, host: str, cancel: threading.Event | None) -> None:
        if self.min_interval_s <= 0:
            return
        with self._host_lock:
            now = time.monotonic()
            due = self._last_hit.get(host, 0.0) + self.min_interval_s
            self._last_hit[host] = max(now, due)
        self._wait(due - now, cancel)

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        wait = min(self.backoff_cap_s, self.backoff_s * (2**attempt))
        wait += random.uniform(0, wait * 0.25)
        if retry_after is not None:
            wait = max(wait, min(retry_after, 60.0))
        return wait

    @staticmethod
    def _retry_after(headers: Any) -> float | None:
        try:
            value = headers.get("Retry-After") if headers is not None else None
            return float(value) if value is not None and str(value).strip().isdigit() else None
        except (TypeError, ValueError):
            return None

    # -- the one transfer loop ------------------------------------------------------------

    def _transfer(
        self,
        url: str,
        sink: Callable[[bytes], None],
        reset: Callable[[], None],
        *,
        max_bytes: int | None,
        progress: Progress | None,
        cancel: threading.Event | None,
        headers: dict[str, str] | None,
        method: str | None = None,
        on_headers: Callable[[Any], None] | None = None,
    ) -> int:
        check_url(url)
        host = urllib.parse.urlparse(url).netloc
        deadline = time.monotonic() + self.total_timeout_s
        last_error: BaseException | None = None
        for attempt in range(self.retries + 1):
            if self._offline():
                raise OfflineError(
                    "Offline mode is on, so nothing is downloaded. Switch it off in the "
                    "environment-data settings, or use data already in the cache."
                )
            if cancel is not None and cancel.is_set():
                raise FetchCancelled("cancelled")
            if time.monotonic() > deadline:
                raise FetchFailed(f"Timed out after {self.total_timeout_s:.0f} s: {url}")
            retry_after: float | None = None
            try:
                with self._sem:
                    self._politeness_gap(host, cancel)
                    self.requests_made += 1
                    reset()
                    req = urllib.request.Request(
                        url,
                        headers={
                            "User-Agent": self.user_agent,
                            "Accept": "*/*",
                            **(headers or {}),
                        },
                        method=method,
                    )
                    timeout = max(self.connect_timeout_s, self.read_timeout_s)
                    with contextlib.closing(self._opener(req, timeout)) as resp:
                        if on_headers is not None:
                            on_headers(getattr(resp, "headers", None))
                        if method == "HEAD":
                            return 0
                        total = _content_length(resp)
                        if max_bytes is not None and total is not None and total > max_bytes:
                            raise FetchFailed(
                                f"{url}: the server announces {total} bytes, more than the "
                                f"{max_bytes} bytes that were approved. Nothing was kept."
                            )
                        done = 0
                        while True:
                            if cancel is not None and cancel.is_set():
                                raise FetchCancelled("cancelled")
                            if time.monotonic() > deadline:
                                raise TimeoutError("total time limit")
                            block = resp.read(CHUNK)
                            if not block:
                                break
                            done += len(block)
                            if max_bytes is not None and done > max_bytes:
                                raise FetchFailed(
                                    f"{url}: more than the approved {max_bytes} bytes arrived; "
                                    "the transfer was stopped and nothing was kept."
                                )
                            sink(block)
                            if progress:
                                progress(done, total)
                        if total is not None and done != total:
                            raise http.client.IncompleteRead(b"", total - done)
                        return done
            except (FetchCancelled, FetchFailed, OfflineError):
                raise
            except urllib.error.HTTPError as exc:
                last_error = exc
                # The error owns the open response (a socket); `last_error` and the exception
                # chain would keep it until the retries are over. Only code, reason and headers
                # are used below.
                exc.close()
                if exc.code not in RETRY_STATUS:
                    raise FetchFailed(f"{exc.code} {exc.reason}: {url}") from exc
                retry_after = self._retry_after(exc.headers)
            except (
                urllib.error.URLError,
                http.client.HTTPException,
                TimeoutError,
                ConnectionError,
                OSError,
            ) as exc:
                last_error = exc
            if attempt >= self.retries:
                break
            self.retries_made += 1
            self._wait(self._backoff(attempt, retry_after), cancel)
        detail = _describe(last_error)
        raise FetchFailed(
            f"Could not download {url} after {self.retries + 1} tries ({detail}). "
            "Check the connection and try again; files already cached are kept."
        )

    # -- public ---------------------------------------------------------------------------

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int = 8 * 1024 * 1024,
        cancel: threading.Event | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        """Small downloads (listings, JSON). Capped at *max_bytes*."""
        buf = bytearray()
        self._transfer(
            url,
            buf.extend,
            buf.clear,
            max_bytes=max_bytes,
            progress=None,
            cancel=cancel,
            headers=headers,
        )
        return bytes(buf)

    def head(
        self,
        url: str,
        *,
        cancel: threading.Event | None = None,
    ) -> dict[str, str]:
        """Response headers of a HEAD request (lower-case names): size and date, no body."""
        found: dict[str, str] = {}

        def grab(headers: Any) -> None:
            if headers is not None:
                found.update({str(k).lower(): str(v) for k, v in dict(headers.items()).items()})

        self._transfer(
            url,
            lambda b: None,
            lambda: found.clear(),
            max_bytes=None,
            progress=None,
            cancel=cancel,
            headers=None,
            method="HEAD",
            on_headers=grab,
        )
        return found

    def content_length(self, url: str, *, cancel: threading.Event | None = None) -> int | None:
        """Size in bytes of the file at *url* from a HEAD request, or ``None`` if not announced."""
        value = self.head(url, cancel=cancel).get("content-length", "")
        return int(value) if value.isdigit() else None

    def download(
        self,
        url: str,
        dest: Path,
        *,
        max_bytes: int | None = None,
        progress: Progress | None = None,
        cancel: threading.Event | None = None,
        headers: dict[str, str] | None = None,
    ) -> int:
        """Stream *url* to *dest* (replaced on each retry). Returns the bytes written."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        state: dict[str, Any] = {"fh": None}

        def reset() -> None:
            if state["fh"] is not None:
                state["fh"].close()
            state["fh"] = open(dest, "wb")  # noqa: SIM115 - closed in finally / reset

        def sink(block: bytes) -> None:
            state["fh"].write(block)

        try:
            return self._transfer(
                url,
                sink,
                reset,
                max_bytes=max_bytes,
                progress=progress,
                cancel=cancel,
                headers=headers,
            )
        except BaseException:
            if state["fh"] is not None:
                state["fh"].close()
                state["fh"] = None
            with contextlib.suppress(OSError):
                dest.unlink()
            raise
        finally:
            if state["fh"] is not None:
                state["fh"].close()


def _content_length(resp: Any) -> int | None:
    try:
        value = resp.headers.get("Content-Length")
        return int(value) if value is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _describe(exc: BaseException | None) -> str:
    if exc is None:
        return "unknown error"
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return f"{type(exc).__name__}: {exc}"

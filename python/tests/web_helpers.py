"""Shared helpers for the web-mode tests (fake WebSocket, upload driver, sample files)."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from websockets.datastructures import Headers

import server
from engine import web
from tests.conftest import make_synthetic_datatree

ALLOWED_ORIGIN = "https://app.example.com"


def make_netcdf(path: Path, *, n_azimuth: int = 24, n_range: int = 40) -> Path:
    """A small synthetic two-sweep radar volume (opens as cfradial2)."""
    tree = make_synthetic_datatree(n_azimuth=n_azimuth, n_range=n_range, elevations=[0.5, 1.5])
    tree.to_netcdf(path)
    return path


def make_session() -> web.SessionContext:
    """A fresh web session with engine state attached (as the connection handler does)."""
    ctx = web.manager().open_session()
    ctx.state = server._ConnState()
    return ctx


def close_session(ctx: web.SessionContext) -> None:
    server._end_web_session(ctx)


class WebClient:
    """Drives a :class:`server.ConnectionHandler` bound to a web session, recording replies."""

    def __init__(self, ctx: web.SessionContext | None = None) -> None:
        self.ctx = ctx or make_session()
        self.sent: list[dict[str, Any]] = []
        self.binary: list[bytes] = []
        self.ws = SimpleNamespace(remote_address=("127.0.0.1", 9))
        self.ws.send = self._send
        self.handler = server.ConnectionHandler(self.ws, session=self.ctx)  # type: ignore[arg-type]

    async def _send(self, raw: Any) -> None:
        if isinstance(raw, str):
            self.sent.append(json.loads(raw))
        else:
            self.binary.append(bytes(raw))

    async def call(self, msg: dict[str, Any]) -> list[dict[str, Any]]:
        """Send one message; return the JSON replies it produced (progress frames included)."""
        start = len(self.sent)
        await self.handler.handle_message(json.dumps(msg))
        return self.sent[start:]

    async def call_types(self, msg: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Like :meth:`call` but keyed by reply type (last one of each type wins)."""
        return {m["type"]: m for m in await self.call(msg)}

    async def upload(
        self,
        data: bytes,
        filename: str = "scan.nc",
        *,
        upload_id: str = "u1",
        chunk: int = 4096,
        declared_size: int | None = None,
    ) -> dict[str, Any]:
        """Run the whole upload protocol; return the final reply (complete or error)."""
        size = len(data) if declared_size is None else declared_size
        replies = await self.call(
            {"type": "upload_start", "upload_id": upload_id, "filename": filename, "size": size}
        )
        if replies[-1]["type"] != "upload_ready":
            return replies[-1]
        for seq, off in enumerate(range(0, len(data), chunk)):
            replies = await self.call(
                {
                    "type": "upload_chunk",
                    "upload_id": upload_id,
                    "seq": seq,
                    "data": base64.b64encode(data[off : off + chunk]).decode(),
                }
            )
            if replies[-1]["type"] != "upload_progress":
                return replies[-1]
        return (await self.call({"type": "upload_end", "upload_id": upload_id}))[-1]

    async def upload_and_open(self, path: Path, filename: str | None = None) -> str:
        """Upload *path*, open it; return the new file_id."""
        done = await self.upload(path.read_bytes(), filename or path.name)
        assert done["type"] == "upload_complete", done
        replies = await self.call({"type": "open_file", "path": done["path"]})
        opened = [m for m in replies if m["type"] == "file_opened"]
        assert opened, replies
        return opened[-1]["file_id"]

    def close(self) -> None:
        close_session(self.ctx)


class FakeWS:
    """Minimal stand-in for a ``websockets`` server connection (async-iterable, records sends)."""

    _END = object()

    def __init__(
        self,
        *,
        origin: str | None = ALLOWED_ORIGIN,
        host: str = "svc.example.com",
    ) -> None:
        headers = Headers({"Host": host})
        if origin is not None:
            headers["Origin"] = origin
        self.request = SimpleNamespace(headers=headers)
        self.remote_address = ("203.0.113.9", 4242)
        self.sent: list[Any] = []
        self.closed: tuple[int, str] | None = None
        self._queue: asyncio.Queue[Any] = asyncio.Queue()

    async def send(self, data: Any) -> None:
        self.sent.append(json.loads(data) if isinstance(data, str) else data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed is None:
            self.closed = (code, reason)
        self._queue.put_nowait(self._END)

    def feed(self, msg: dict[str, Any] | str) -> None:
        self._queue.put_nowait(msg if isinstance(msg, str) else json.dumps(msg))

    def finish(self) -> None:
        self._queue.put_nowait(self._END)

    def __aiter__(self) -> FakeWS:
        return self

    async def __anext__(self) -> Any:
        item = await self._queue.get()
        if item is self._END:
            raise StopAsyncIteration
        return item

    def types(self) -> list[str]:
        return [m["type"] for m in self.sent if isinstance(m, dict)]


async def wait_for(predicate: Any, timeout: float = 5.0) -> None:
    """Poll *predicate* until true (raises ``AssertionError`` on timeout)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)

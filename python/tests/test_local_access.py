"""Who may open a WebSocket to the app's own engine (engine/local_access.py).

Real WebSocket handshakes to a real in-process engine (``server._run_server``) on 127.0.0.1;
nothing leaves this machine and the handshake is not mocked.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

import server
from engine import local_access


@contextlib.asynccontextmanager
async def running_engine(token: str | None = None):
    """The app's engine on a free port; with *token* it behaves as when the app started it."""
    previous = server.EXPECTED_SESSION_TOKEN
    server.EXPECTED_SESSION_TOKEN = token
    stop = asyncio.Event()
    ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(
        server._run_server(0, "127.0.0.1", on_ready=ready.set_result, stop_event=stop)
    )
    try:
        yield await asyncio.wait_for(ready, 60)
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)
        server.EXPECTED_SESSION_TOKEN = previous


async def http_status(port: int, host: str, origin: str | None = None) -> str:
    """The status line of a WebSocket upgrade request with exactly this ``Host`` and ``Origin``."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [
        "GET / HTTP/1.1",
        f"Host: {host}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
        "Sec-WebSocket-Version: 13",
    ]
    if origin is not None:
        lines.append(f"Origin: {origin}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()
    status = (await asyncio.wait_for(reader.readline(), 10)).decode()
    writer.close()
    return status.strip()


async def ping(port: int, origin: str | None, token: str | None = None) -> dict:
    async with connect(f"ws://127.0.0.1:{port}", origin=origin) as ws:
        message = {"type": "ping"}
        if token is not None:
            message["token"] = token
        await ws.send(json.dumps(message))
        return json.loads(await asyncio.wait_for(ws.recv(), 10))


@pytest.mark.parametrize(
    "origin,allowed",
    [
        (None, True),
        ("tauri://localhost", True),
        ("http://tauri.localhost", True),
        ("https://tauri.localhost", True),
        ("http://localhost:1420", True),
        ("http://127.0.0.1:1420", True),
        ("https://evil.example", False),
        ("http://localhost:8080", False),  # some other local page, not the app's dev server
        ("http://localhost", False),
        ("https://localhost:1420", False),
        ("tauri://localhost:1420", False),
        ("http://localhost.evil.example", False),
        ("http://localhost.evil.example:1420", False),
        ("tauri://localhost.evil.example", False),
        ("http://tauri.localhost.evil.example", False),
        ("http://user@localhost:1420", False),
        ("http://localhost:1420/path", False),
        ("null", False),
        ("", False),
        ("file://", False),
    ],
)
def test_origin_rule(origin, allowed):
    assert local_access.origin_ok(origin) is allowed


@pytest.mark.parametrize(
    "host,allowed",
    [
        ("localhost:51234", True),
        ("127.0.0.1:51234", True),
        ("[::1]:51234", True),
        ("localhost", True),
        (None, False),
        ("", False),
        ("evil.example", False),
        ("evil.example:51234", False),
        ("127.0.0.1.evil.example:51234", False),
        ("localhost.evil.example", False),
    ],
)
def test_host_rule(host, allowed):
    assert local_access.host_ok(host) is allowed


@pytest.mark.parametrize("token", [None, "per-run-token"])
async def test_a_foreign_web_page_is_refused_with_403_before_any_websocket_opens(token):
    async with running_engine(token) as port:
        with pytest.raises(InvalidStatus) as err:
            await connect(f"ws://127.0.0.1:{port}", origin="https://evil.example")
        assert err.value.response.status_code == 403
        # even a page that knows the token does not get in
        status = await http_status(port, f"127.0.0.1:{port}", "https://evil.example")
        assert "403" in status


@pytest.mark.parametrize("token", [None, "per-run-token"])
async def test_a_page_on_another_local_port_is_refused(token):
    async with running_engine(token) as port:
        assert "403" in await http_status(port, f"localhost:{port}", "http://localhost:8080")
        assert "403" in await http_status(port, f"localhost:{port}", "http://127.0.0.1:3000")


async def test_a_page_whose_own_name_resolves_to_this_computer_is_refused():
    async with running_engine() as port:
        assert "403" in await http_status(port, "evil.example")
        assert "403" in await http_status(port, f"evil.example:{port}")
        assert "403" in await http_status(port, f"evil.example:{port}", "tauri://localhost")
        assert "101" in await http_status(port, f"localhost:{port}")
        assert "101" in await http_status(port, f"127.0.0.1:{port}")


@pytest.mark.parametrize(
    "origin",
    ["tauri://localhost", "http://tauri.localhost", "https://tauri.localhost", None],
)
async def test_the_apps_own_origins_and_a_program_without_origin_are_accepted(origin):
    async with running_engine("per-run-token") as port:
        reply = await ping(port, origin, token="per-run-token")
        assert reply["type"] == "pong"


async def test_the_development_server_origin_is_accepted():
    async with running_engine("per-run-token") as port:
        reply = await ping(port, "http://localhost:1420", token="per-run-token")
        assert reply["type"] == "pong"


async def test_a_program_without_origin_still_needs_the_per_run_token():
    async with running_engine("per-run-token") as port:
        reply = await ping(port, None)  # no token
        assert reply["type"] == "error"
        assert "pong" not in json.dumps(reply)
        wrong = await ping(port, None, token="guess")
        assert wrong["type"] == "error"

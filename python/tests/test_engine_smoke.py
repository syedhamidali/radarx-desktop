"""The engine end to end, cheaply: a real engine on a free port and a real WebSocket client.

Open a small synthetic file, ask for a sweep, close it, and check the engine gives its memory back.
No download, no real account server (the signed-out part uses the project's ``gate_on`` fixture).
docs/PROTOCOL.md describes the conversation this test follows.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

import server
from tests.web_helpers import make_netcdf

PYTHON_DIR = Path(__file__).resolve().parents[1]


@contextlib.asynccontextmanager
async def running_engine():
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


@pytest.fixture(autouse=True)
def _clean_server_state():
    """The engine keeps its open files in module state; start from none and leave none."""
    saved = (dict(server._file_readers), server._last_file_id)
    server._file_readers.clear()
    server._last_file_id = None
    yield
    server._file_readers.clear()
    server._file_readers.update(saved[0])
    server._last_file_id = saved[1]


class Client:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.binary: list[bytes] = []

    async def until(self, *types: str) -> dict:
        """The next JSON reply of one of *types* (binary frames on the way are kept)."""
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), 30)
            if isinstance(raw, bytes):
                self.binary.append(raw)
                continue
            reply = json.loads(raw)
            if reply.get("type") in types:
                return reply

    async def ask(self, message: dict, *types: str) -> dict:
        await self.ws.send(json.dumps(message))
        return await self.until(*types, "error", "auth_required")


async def test_open_a_file_read_a_sweep_close_it_and_get_the_memory_back(tmp_path):
    path = make_netcdf(tmp_path / "scan.nc", n_azimuth=24, n_range=40)
    async with running_engine() as port, connect(f"ws://127.0.0.1:{port}") as ws:
        client = Client(ws)
        before = await client.ask({"type": "ping"}, "pong")
        assert before["status"] == "ok" and before["file_open"] is False
        assert before["memory"]["files"] == 0 and before["engine_id"]

        opened = await client.ask({"type": "open_file", "path": str(path)}, "file_opened")
        assert opened["type"] == "file_opened", opened
        file_id = opened["file_id"]
        assert opened["provenance"]
        assert (await client.ask({"type": "ping"}, "pong"))["memory"]["files"] == 1

        head = await client.ask(
            {"type": "get_sweep_data", "variable": "DBZH", "sweep": 0, "file_id": file_id},
            "sweep_data",
        )
        assert head["type"] == "sweep_data", head
        assert (head["n_az"], head["n_range"]) == (24, 40)
        assert head["azimuth_bytes"] == 24 * 4 and head["range_bytes"] == 40 * 4
        assert head["values_bytes"] == 24 * 40 * 4
        while (
            len(b"".join(client.binary))
            < head["azimuth_bytes"] + head["range_bytes"] + head["values_bytes"]
        ):
            raw = await asyncio.wait_for(ws.recv(), 30)
            assert isinstance(raw, bytes)
            client.binary.append(raw)
        payload = b"".join(client.binary)
        offset = head["azimuth_bytes"] + head["range_bytes"]
        values = struct.unpack("<960f", payload[offset : offset + head["values_bytes"]])
        assert head["vmin"] <= min(values) and max(values) <= head["vmax"]
        assert head["units"] == "dBZ"

        closed = await client.ask({"type": "close_file", "file_id": file_id}, "file_closed")
        assert closed == {"type": "file_closed", "file_id": file_id}
        after = await client.ask({"type": "ping"}, "pong")
        assert after["file_open"] is False and after["memory"]["files"] == 0
        assert after["memory"]["open_mb"] <= before["memory"]["open_mb"] + 0.01
        assert after["engine_id"] == before["engine_id"]  # the same engine the whole time


async def test_bad_input_gets_clear_errors_and_the_engine_keeps_answering():
    async with running_engine() as port, connect(f"ws://127.0.0.1:{port}") as ws:
        client = Client(ws)
        for raw in ("{not json", "[1, 2]", "{}", '{"type": 5}'):
            await ws.send(raw)
            assert (await client.until("error"))["type"] == "error"
        missing = await client.ask({"type": "open_file", "path": "/no/such/file.nc"}, "file_opened")
        assert missing["type"] == "error" and missing["message"]
        assert (await client.ask({"type": "close_file"}, "file_closed"))["type"] == "error"
        assert (await client.ask({"type": "ping"}, "pong"))["status"] == "ok"


async def test_signed_out_the_engine_refuses_work_but_still_answers_ping(gate_on, tmp_path):
    path = make_netcdf(tmp_path / "scan.nc")
    async with running_engine() as port, connect(f"ws://127.0.0.1:{port}") as ws:
        client = Client(ws)
        assert (await client.ask({"type": "ping"}, "pong"))["status"] == "ok"
        refused = await client.ask({"type": "open_file", "path": str(path)}, "file_opened")
        assert refused["type"] == "auth_required" and refused["reason"] == "no_session"
        assert refused["request_type"] == "open_file"


def test_the_engine_prints_its_version_and_exits():
    done = subprocess.run(
        [sys.executable, str(PYTHON_DIR / "server.py"), "--version"],
        cwd=PYTHON_DIR,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert done.returncode == 0, done.stderr[-300:]
    assert done.stdout.startswith("radarx-desktop engine ") and "READY" not in done.stdout
    assert done.stdout.split()[-1] != ""

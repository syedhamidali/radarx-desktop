"""Manual stress run against a real engine process (not collected by pytest).

    cd python
    python tests/manual/ws_stress.py /path/to/volume.nc [--port 18765] [scenario ...]

Scenarios: ``malformed`` (hostile JSON and parameters), ``oversize`` (messages over the size
limit), ``drop`` (client vanishes in the middle of requests), ``flood`` (thousands of requests at
once), ``kill`` (SIGKILL the engine and start it again on the same port), ``leaks`` (open/close
cycles, printing resident memory and open descriptors). With no scenario name, all of them run.

Each scenario prints what happened; the engine must stay alive and answer a ``ping`` afterwards.
See docs/STABILITY.md for what the numbers mean. Needs ``websockets`` (an engine dependency).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import websockets

SERVER = Path(__file__).resolve().parents[2] / "server.py"


def start(port: int) -> subprocess.Popen[str]:
    env = {k: v for k, v in os.environ.items() if k != "XRADAR_SESSION_TOKEN"}
    proc = subprocess.Popen(
        [sys.executable, str(SERVER), "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line.startswith("READY"):
        raise SystemExit(f"engine did not start: {line!r}")
    return proc


def rss_mb(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    return int(out.stdout.strip() or 0) / 1024


def descriptors(pid: int) -> int:
    out = subprocess.run(["lsof", "-p", str(pid)], capture_output=True, text=True)
    return len(out.stdout.splitlines())


async def recv_until(ws: Any, pred: Any, timeout: float = 60) -> dict[str, Any] | None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            raw = await asyncio.wait_for(ws.recv(), max(0.05, end - time.time()))
        except TimeoutError:
            return None
        if isinstance(raw, bytes):
            continue
        msg = json.loads(raw)
        if pred(msg):
            return msg
    return None


async def alive(port: int) -> bool:
    async with websockets.connect(f"ws://localhost:{port}") as ws:
        await ws.send('{"type":"ping"}')
        return await recv_until(ws, lambda m: m["type"] == "pong", 10) is not None


async def open_file(ws: Any, path: str) -> dict[str, Any]:
    await ws.send(json.dumps({"type": "open_file", "path": path}))
    msg = await recv_until(ws, lambda m: m["type"] in ("file_opened", "error"), 180)
    if not msg or msg["type"] != "file_opened":
        raise SystemExit(f"could not open {path}: {msg}")
    return msg


HOSTILE = {
    "not json": "{{{",
    "array": "[1,2]",
    "unknown type": '{"type":"nope"}',
    "5000-digit integer": '{"type":"render","sweep":' + "9" * 5000 + "}",
    "200k nested arrays": "[" * 200_000 + "]" * 200_000,
    "NaN sweep": '{"type":"render","variable":"DBZH","sweep":NaN}',
    "list as file_id": '{"type":"get_sweep_data","variable":"A","sweep":0,"file_id":[1]}',
    "NUL in path": '{"type":"open_file","path":"/tmp/a\\u0000b"}',
    "100k path": json.dumps({"type": "open_file", "path": "/" + "a" * 100_000}),
    "folder as file": json.dumps({"type": "open_file", "path": "/"}),
    "infinite bins": '{"type":"get_histogram","variable":"DBZH","bins":Infinity}',
    "2e9 bins": '{"type":"get_histogram","variable":"DBZH","bins":2000000000}',
}


async def malformed(port: int, path: str) -> None:
    async with websockets.connect(f"ws://localhost:{port}", max_size=None) as ws:
        await open_file(ws, path)
        for name, payload in HOSTILE.items():
            t0 = time.time()
            await ws.send(payload)
            # an error or any typed reply counts; silence does not
            reply = await recv_until(ws, lambda m: m["type"] != "progress", 20)
            text = (reply or {}).get("message") or (reply or {}).get("type") or "NO REPLY"
            print(f"  {name:22s} {time.time() - t0:5.2f}s  {str(text)[:110]}")
    print("  engine alive:", await alive(port))


async def oversize(port: int, path: str) -> None:
    for mb in (22, 64):
        try:
            async with websockets.connect(f"ws://localhost:{port}", max_size=None) as ws:
                await ws.send('{"type":"ping","pad":"' + "x" * (mb * 1024 * 1024) + '"}')
                await asyncio.wait_for(ws.recv(), 10)
        except Exception as exc:  # the engine closes the connection (1009): that is the point
            print(f"  {mb} MB: {type(exc).__name__}")
    print("  engine alive:", await alive(port))


async def drop(port: int, path: str) -> None:
    for _ in range(3):
        ws = await websockets.connect(f"ws://localhost:{port}", max_size=None)
        opened = await open_file(ws, path)
        var = opened["data"]["variables"][0]
        for _i in range(30):
            await ws.send(
                json.dumps(
                    {
                        "type": "get_sweep_data",
                        "variable": var,
                        "sweep": 0,
                        "file_id": opened["file_id"],
                    }
                )
            )
        ws.transport.abort()  # no close frame
        await asyncio.sleep(0.2)
    print("  engine alive:", await alive(port))


async def flood(port: int, path: str) -> None:
    async with websockets.connect(f"ws://localhost:{port}", max_size=None) as ws:
        opened = await open_file(ws, path)
        fid, var = opened["file_id"], opened["data"]["variables"][0]
        sweeps = [s["index"] for s in opened["data"]["sweeps"]]
        n = 3000
        t0 = time.time()
        for i in range(n):
            await ws.send(
                json.dumps(
                    {
                        "type": "get_sweep_data",
                        "variable": var,
                        "sweep": sweeps[i % len(sweeps)],
                        "file_id": fid,
                    }
                )
            )
        await ws.send('{"type":"ping"}')
        got = 0
        while got < n:
            msg = await recv_until(ws, lambda m: m["type"] in ("sweep_data", "error"), 120)
            if msg is None:
                break
            got += 1
        print(f"  {got}/{n} answered in {time.time() - t0:.1f}s")
    print("  engine alive:", await alive(port))


async def kill(port: int, path: str) -> None:
    ids = []
    for i in range(3):
        proc = start(port)
        async with websockets.connect(f"ws://localhost:{port}", max_size=None) as ws:
            await ws.send('{"type":"ping"}')
            pong = await recv_until(ws, lambda m: m["type"] == "pong", 10)
            ids.append(pong["engine_id"])
            await ws.send(json.dumps({"type": "open_file", "path": path}))
            await asyncio.sleep(0.3)
            proc.send_signal(signal.SIGKILL)
            proc.wait()
        print(f"  engine #{i} killed mid-open; the next one binds the same port")
    print("  every restart has a new engine_id:", len(set(ids)) == len(ids))


async def leaks(port: int, path: str) -> None:
    pid = int(SCENARIO_STATE["pid"])
    async with websockets.connect(f"ws://localhost:{port}", max_size=None) as ws:
        for cycle in range(1, 51):
            opened = await open_file(ws, path)
            fid, var = opened["file_id"], opened["data"]["variables"][0]
            await ws.send(
                json.dumps({"type": "get_sweep_data", "variable": var, "sweep": 0, "file_id": fid})
            )
            await recv_until(ws, lambda m: m["type"] in ("sweep_data", "error"), 60)
            await ws.send(json.dumps({"type": "close_file", "file_id": fid}))
            await recv_until(ws, lambda m: m["type"] == "file_closed", 30)
            if cycle in (1, 10, 25, 50):
                print(
                    f"  cycle {cycle:2d}: rss {rss_mb(pid):6.0f} MB, descriptors {descriptors(pid)}"
                )


SCENARIO_STATE: dict[str, Any] = {}
SCENARIOS = {
    "malformed": malformed,
    "oversize": oversize,
    "drop": drop,
    "flood": flood,
    "kill": kill,
    "leaks": leaks,
}


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("file", help="a radar file the engine can open")
    parser.add_argument("scenarios", nargs="*", choices=[*SCENARIOS, []], default=[])
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    for name in args.scenarios or list(SCENARIOS):
        print(f"-- {name}")
        if name == "kill":
            await kill(args.port, args.file)
            continue
        proc = start(args.port)
        SCENARIO_STATE["pid"] = proc.pid
        try:
            await SCENARIOS[name](args.port, args.file)
        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    asyncio.run(main())

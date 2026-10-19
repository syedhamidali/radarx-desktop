"""Child process of ``test_stability_native.py``: hammers the NetCDF/HDF5 code paths.

Run as ``python native_child.py MODE FILE...``. A native crash kills this process (the parent test
sees the signal); a normal finish prints ``CHILD-OK``. Kept out of pytest's own process because a
segfault there would take the whole test run down.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import sys
import threading
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
from engine.reader import RadarReader
from tests.test_server import _make_handler


async def through_the_handler(paths: list[str], rounds: int) -> None:
    """open_file + the prefetch after it + get_sweep_data, as the app does, on overlapping files."""
    handler, _ = _make_handler()
    for i in range(rounds):
        path = paths[i % len(paths)]
        await handler.handle_message(json.dumps({"type": "open_file", "path": path}))
        opened = [m for m in handler._sent if m["type"] == "file_opened"]
        assert opened, handler._sent[-1:]
        fid, schema = opened[-1]["file_id"], opened[-1]["data"]
        await asyncio.gather(*list(server._background_tasks))
        var = schema["variables"][0]
        await handler.handle_message(
            json.dumps({"type": "get_sweep_data", "variable": var, "sweep": 0, "file_id": fid})
        )
        handler._sent.clear()
        handler._frames.clear()


def serial(paths: list[str], rounds: int) -> None:
    """Open the same files over and over, keeping the older readers alive (overlapping handles)."""
    rnd = random.Random(7)
    readers: list[RadarReader] = []
    for _ in range(rounds):
        reader = RadarReader()
        schema = reader.open_file(rnd.choice(paths))
        readers.append(reader)
        for sweep in schema["sweeps"][:2]:
            for var in (sweep.get("variables") or schema["variables"])[:2]:
                _ = reader.get_sweep_data(sweep["index"], var).values
        if len(readers) > 3:
            readers.pop(rnd.randrange(len(readers))).close()


def threads(paths: list[str], rounds: int, n_threads: int = 6) -> None:
    errors: list[BaseException] = []

    def work(seed: int) -> None:
        try:
            serial_one(paths, rounds, seed)
        except BaseException as exc:
            errors.append(exc)

    pool = [threading.Thread(target=work, args=(i,)) for i in range(n_threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    if errors:
        raise errors[0]


def serial_one(paths: list[str], rounds: int, seed: int) -> None:
    rnd = random.Random(seed)
    keep: list[RadarReader] = []
    for _ in range(rounds):
        reader = RadarReader()
        schema = reader.open_file(rnd.choice(paths))
        keep.append(reader)
        _ = reader.get_sweep_data(0, schema["variables"][0]).values
        if len(keep) > 2:
            keep.pop(0).close()


def main() -> None:
    mode, paths = sys.argv[1], sys.argv[2:]
    if mode == "handler":
        asyncio.run(through_the_handler(paths, 12))
    elif mode == "serial":
        serial(paths, 30)
    elif mode == "threads":
        threads(paths, 10)
    else:
        raise SystemExit(f"unknown mode {mode}")
    print("CHILD-OK")


if __name__ == "__main__":
    main()

"""Open/close many times: file handles, temp files and Python memory must not pile up."""

from __future__ import annotations

import asyncio
import gc
import gzip
import json
import os
import sys
import tempfile
import tracemalloc
from pathlib import Path

import pytest

import server
from tests.test_server import _make_handler
from tests.web_helpers import make_netcdf

pytestmark = [
    pytest.mark.filterwarnings("ignore::UserWarning"),
    pytest.mark.skipif(sys.platform == "win32", reason="counts descriptors through /dev/fd"),
]

CYCLES = 50


def _open_descriptors() -> int:
    return len(os.listdir("/dev/fd"))


async def _cycle(handler, path: str) -> None:
    await handler.handle_message(json.dumps({"type": "open_file", "path": path}))
    opened = next(m for m in reversed(handler._sent) if m["type"] == "file_opened")
    fid = opened["file_id"]
    for sweep in (0, 1):
        await handler.handle_message(
            json.dumps(
                {"type": "get_sweep_data", "variable": "DBZH", "sweep": sweep, "file_id": fid}
            )
        )
    await handler.handle_message(
        json.dumps(
            {
                "type": "render",
                "variable": "DBZH",
                "sweep": 0,
                "file_id": fid,
                "width": 80,
                "height": 60,
            }
        )
    )
    await asyncio.gather(*list(server._background_tasks))  # the prefetch after open
    await handler.handle_message(json.dumps({"type": "close_file", "file_id": fid}))
    handler._sent.clear()
    handler._frames.clear()


@pytest.fixture(autouse=True)
def _clean_server_state():
    saved = (dict(server._file_readers), server._last_file_id, dict(server._generations))
    server._file_readers.clear()
    server._generations.clear()
    server._last_file_id = None
    yield
    server._file_readers.clear()
    server._file_readers.update(saved[0])
    server._last_file_id = saved[1]
    server._generations.clear()
    server._generations.update(saved[2])


@pytest.mark.parametrize("packed", [False, True], ids=["netcdf", "netcdf-gz"])
async def test_repeated_open_and_close_does_not_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, packed: bool
) -> None:
    scratch = tmp_path / "systmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    path = make_netcdf(tmp_path / "scan.nc", n_azimuth=90, n_range=120)
    if packed:
        gz = tmp_path / "scan.nc.gz"
        gz.write_bytes(gzip.compress(path.read_bytes()))
        path = gz
    handler, _ = _make_handler()

    for _ in range(5):  # warm-up: imports, caches, thread pool
        await _cycle(handler, str(path))
    gc.collect()
    fds_before = _open_descriptors()
    tracemalloc.start()
    baseline = tracemalloc.take_snapshot()

    for _ in range(CYCLES):
        await _cycle(handler, str(path))
    gc.collect()

    grown = sum(
        stat.size_diff
        for stat in tracemalloc.take_snapshot().compare_to(baseline, "filename")
        if stat.size_diff > 0
    )
    tracemalloc.stop()
    assert _open_descriptors() - fds_before <= 3, "file handles accumulate across open/close"
    assert list(scratch.iterdir()) == [], "decompressed copies are left behind"
    assert not server._file_readers
    assert grown < 8 * 1024 * 1024, (
        f"Python memory grew by {grown / 1e6:.1f} MB over {CYCLES} cycles"
    )

"""Memory behaviour of the reader and of the frame gridding workers (cheap, synthetic).

Measured on real NEXRAD Level II volumes (``benchmarks/reader_memory.py``): opening, gridding and
closing 40 volumes in a row keeps the resident size on a plateau, and the Python-level memory
(tracemalloc) stays flat, so there is no per-volume leak in the reader or the mosaic path. These
tests keep it that way: a closed reader must not keep its data tree, and the pool that grids
frames must stay within the memory budget.
"""

from __future__ import annotations

import gc
import tracemalloc
import weakref

import pytest

from engine import memtrim
from engine.nowcast import frames as fr
from engine.reader import RadarReader
from tests.conftest import make_synthetic_datatree

GB = 1024**3


def test_closed_reader_lets_go_of_its_tree() -> None:
    reader = RadarReader()
    tree = make_synthetic_datatree(n_azimuth=8, n_range=8, elevations=[0.5, 1.5])
    reader.adopt_datatree(tree, "synthetic.nc", "cfradial1")
    reader.get_sweep_dataset(0)  # fills the node and kind caches
    ref = weakref.ref(tree)
    reader.close()
    del tree
    gc.collect()
    assert ref() is None, "RadarReader.close() left a reference to the data tree"


def test_many_open_close_cycles_do_not_accumulate_python_memory() -> None:
    def cycle() -> None:
        reader = RadarReader()
        tree = make_synthetic_datatree(n_azimuth=360, n_range=500, elevations=[0.5, 1.5, 2.5])
        reader.adopt_datatree(tree, "synthetic.nc", "cfradial1")
        for sweep in range(3):
            reader.get_sweep_dataset(sweep)
        reader.close()

    for _ in range(3):  # warm-up: imports, lazily built tables
        cycle()
    gc.collect()
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        for _ in range(25):
            cycle()
        gc.collect()
        grown = tracemalloc.get_traced_memory()[0] - base
    finally:
        tracemalloc.stop()
    # a leaked tree is about 3 MB per cycle here (75 MB over the loop); the allowance is noise
    assert grown < 5 * 1024 * 1024, f"python memory grew {grown / 1e6:.1f} MB over 25 cycles"


def test_memtrim_is_harmless() -> None:
    assert memtrim.trim() in (True, False)
    assert memtrim.trim(collect=False) in (True, False)


def test_workers_follow_the_memory_budget() -> None:
    # one volume per frame needs 2 x 1.2 GB of budget per worker
    assert fr.workers_for_memory(8, 1, 6 * GB) == 2
    assert fr.workers_for_memory(8, 1, 40 * GB) == 8
    # three radars per frame need more per worker
    assert fr.workers_for_memory(8, 3, 6 * GB) < fr.workers_for_memory(8, 1, 6 * GB) or (
        fr.workers_for_memory(8, 1, 6 * GB) == 1
    )
    # never fewer than one, never more than asked, unknown budget leaves the request alone
    assert fr.workers_for_memory(4, 1, 100) == 1
    assert fr.workers_for_memory(3, 1, 400 * GB) == 3
    assert fr.workers_for_memory(5, 2, None) == 5
    assert fr.workers_for_memory(1, 1, 6 * GB) == 1


def test_pool_is_capped_and_says_so(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """With a small budget the frame loader uses fewer workers than asked and notes it."""
    import numpy as np

    asked: dict[str, int] = {}

    class FakePool:
        def __init__(self, max_workers: int, **kw) -> None:
            asked["workers"] = max_workers
            asked.update(kw)

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def submit(self, fn, *args):
            from concurrent.futures import Future

            fut: Future = Future()
            fut.set_result(fn(*args))
            return fut

        def shutdown(self, **kw) -> None:
            return None

    t0 = np.datetime64("2024-05-01T00:00:00")

    def fake_grid_group(files, variable, product, grid):
        k = int(files[0][-2:])
        merged = np.full((4, 4), 10.0 + k, dtype=np.float32)
        return {
            "merged": merged,
            "covered": np.ones((4, 4), bool),
            "bbox": (-90.0, 35.0, -89.0, 36.0),
            "origin": (35.5, -89.5),
            "x0": -2000.0,
            "y0": -2000.0,
            "cell_m": 1000.0,
            "radars": [{"name": "KDMX", "latitude": 35.5, "longitude": -89.5}],
            "time": t0 + np.timedelta64(300 * k, "s"),
            "warnings": [],
        }

    import concurrent.futures

    monkeypatch.setattr(fr, "_grid_group", fake_grid_group)
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", FakePool)
    monkeypatch.setattr(fr, "_memory_budget_bytes", lambda: 3 * GB)
    names = [f"KDMX20240501_{h:02d}0000_V{h:02d}" for h in range(6)]
    seq = fr.from_radar_files([tmp_path / n for n in names], workers=6)
    assert seq.n == 6
    assert asked["workers"] == 1
    assert any("instead of 6" in n for n in seq.notes)

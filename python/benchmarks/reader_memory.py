"""RSS after each of N volumes opened, used and closed (not part of the package).

    python benchmarks/reader_memory.py MODE FILE [FILE ...]

MODE: ``open`` (open + close), ``sweeps`` (open, decode every sweep's reflectivity, close),
``grid`` (open + composite-grid with the mosaic engine + close, as the nowcast frame loader does).
One line per volume: resident memory after ``gc.collect()``. Aborts above 6 GB.
"""

from __future__ import annotations

import gc
import sys
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from watchdog import start_watchdog

start_watchdog(max_rss_gb=6, min_free_disk_gb=5, max_swap_gb=8)
proc = psutil.Process()


def rss() -> float:
    return proc.memory_info().rss / 1e9


def main(mode: str, files: list[str]) -> None:
    from engine.nowcast.frames import _grid_group
    from engine.reader import RadarReader

    grid = {"resolution_m": 1000.0, "max_range_km": 230.0, "extent": "auto"}
    print(f"{mode}: start {rss():.3f} GB")
    import os

    group = int(os.environ.get("GROUP", "1"))  # volumes mosaicked per frame (mode grid)
    if mode == "grid" and group > 1:
        files = [";".join(files[k : k + group]) for k in range(0, len(files), group)]
    for i, f in enumerate(files, 1):
        t0 = time.time()
        if mode == "grid":
            out = _grid_group(f.split(";"), "DBZH", "composite", grid)
            if i == 1:  # like the frame loader: the first frame fixes the grid of the others
                w, s_, e, n = out["bbox"]
                grid["extent"] = {"west": w, "south": s_, "east": e, "north": n}
            del out
        else:
            rd = RadarReader()
            rd.open_file(f)
            if mode == "sweeps":
                n = len(rd.get_schema()["sweeps"])
                for s in range(n):
                    ds = rd.get_sweep_dataset(s)
                    ds["DBZH"].values.sum()
                    del ds
            rd.close()
            del rd
        gc.collect()
        if os.environ.get(
            "TRIM"
        ):  # TRIM=1: give the allocator's free pages back, as the engine does
            from engine.memtrim import trim

            trim(collect=False)
        print(f"{i:2d} {Path(f).name}: {rss():.3f} GB  ({time.time() - t0:.1f}s)", flush=True)


if __name__ == "__main__":
    import os

    # REPEAT=4 cycles the list four times (a long run shows a plateau from a real leak)
    main(sys.argv[1], sys.argv[2:] * int(os.environ.get("REPEAT", "1")))

#!/usr/bin/env python
"""Engine micro-benchmark: wall time and peak memory of the work the app does most.

Not part of the installed package. Run from ``python/``::

    python benchmarks/bench_engine.py                      # synthetic NEXRAD-sized volumes
    python benchmarks/bench_engine.py FILE [FILE2 ...]     # real files (read only)
    python benchmarks/bench_engine.py FILE --repeat 5 --json out.json
    python benchmarks/bench_engine.py FILE --only open render

The first file is the main volume. Extra files (any radars; ideally the same time) feed the
mosaic stage; with fewer than two files, synthetic radars are added. Nothing is ever written
next to the inputs: exports go to a temporary folder.

Each stage reports ``first`` (first call in this process, caches cold), ``warm`` (best of the
repeats, per-data caches cleared but process-wide ones warm) and ``peak MB`` (tracemalloc peak
of one extra run: Python and NumPy allocations; library-internal native buffers are not seen,
see ``rss`` for the process high-water mark).
"""

from __future__ import annotations

import argparse
import gc
import itertools
import json
import logging
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import tracemalloc
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

PY_DIR = Path(__file__).resolve().parent.parent
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

import numpy as np
import xarray as xr

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

RESULTS: list[dict[str, Any]] = []
ELEVATIONS = [0.5, 0.9, 1.3, 1.8, 2.4, 3.1, 4.0, 5.1, 6.4, 8.0, 10.0, 12.5, 15.6, 19.5]
RE_EFF = 4.0 / 3.0 * 6371e3


# --------------------------------------------------------------------------- synthetic data


def make_synthetic_volume(
    lat: float = 35.33,
    lon: float = -97.28,
    name: str = "SYNT",
    n_az: int = 720,
    n_range: int = 1832,
    gate_m: float = 250.0,
    seed: int = 0,
) -> xr.DataTree:
    """A NEXRAD-sized volume (14 sweeps) with encoded uint8/uint16 moments and a storm."""
    rng = np.random.default_rng(seed)
    azimuth = (np.arange(n_az) + 0.5) * 360.0 / n_az
    range_m = gate_m / 2 + gate_m * np.arange(n_range)
    az = np.radians(azimuth)[:, None]
    sweeps: dict[str, xr.Dataset] = {}
    for i, el in enumerate(ELEVATIONS):
        s = range_m[None, :] * np.cos(np.radians(el))
        x, y = s * np.sin(az) / 1e3, s * np.cos(az) / 1e3
        core = np.exp(-(((x - 30) ** 2 + (y - 25) ** 2) / (2 * 15.0**2)))
        core2 = np.exp(-(((x + 40) ** 2 + (y + 10) ** 2) / (2 * 10.0**2)))
        dbz = -10 + 65 * (core + 0.7 * core2) * np.exp(-i / 9) + rng.normal(0, 2, core.shape)
        dbz = np.where(core + core2 > 0.02, dbz, np.nan)
        rho = np.where(np.isfinite(dbz), 0.97 + rng.normal(0, 0.02, dbz.shape), np.nan)
        zdr = np.where(np.isfinite(dbz), 0.8 + 0.03 * dbz + rng.normal(0, 0.3, dbz.shape), np.nan)
        vel = np.where(
            np.isfinite(dbz), 18 * np.sin(az - 0.6) + rng.normal(0, 1.5, dbz.shape), np.nan
        )
        phi = np.where(
            np.isfinite(dbz), np.cumsum(np.abs(rng.normal(0.05, 0.02, dbz.shape)), 1), np.nan
        )
        data = {}
        for var, arr, units, scale, offset in (
            ("DBZH", dbz, "dBZ", 0.5, -33.0),
            ("VRADH", vel, "m/s", 0.5, -64.5),
            ("ZDR", zdr, "dB", 0.0625, -7.9),
            ("RHOHV", rho, "", 0.00333, -0.2),
            ("PHIDP", phi, "degrees", 0.7, -0.7),
        ):
            arr = arr.astype(np.float32)
            v = xr.Variable(("azimuth", "range"), arr, {"units": units})
            v.encoding = {"scale_factor": scale, "add_offset": offset, "dtype": "uint8"}
            data[var] = v
        sweeps[f"/sweep_{i}"] = xr.Dataset(
            data
            | {
                "sweep_mode": ((), "azimuth_surveillance"),
                "sweep_fixed_angle": ((), float(el)),
            },
            coords={
                "azimuth": ("azimuth", azimuth),
                "range": ("range", range_m.astype(np.float32)),
                "elevation": ("azimuth", np.full(n_az, el)),
                "time": (
                    "azimuth",
                    np.datetime64("2024-05-01T20:00:00")
                    + (i * 30_000 + np.arange(n_az) * 8).astype("timedelta64[ms]"),
                ),
            },
        )
    root = xr.Dataset(
        coords={"latitude": np.float64(lat), "longitude": np.float64(lon), "altitude": 300.0},
        attrs={"instrument_name": name, "history": "synthetic"},
    )
    return xr.DataTree.from_dict({"/": root, **sweeps})


def reader_from_tree(tree: xr.DataTree):
    from engine.reader import RadarReader

    rd = RadarReader()
    rd._dtree = tree
    rd._format = "synthetic"
    return rd


# --------------------------------------------------------------------------- timing harness


def measure(
    stage: str,
    what: str,
    fn: Callable[[], Any],
    *,
    repeat: int,
    prep: Callable[[], None] | None = None,
    memory: bool = True,
    rss_peak: bool = False,
) -> Any:
    """Time ``fn`` (first call, then best of ``repeat``); one more run to measure memory.

    Memory is the tracemalloc peak (Python + NumPy allocations) or, with ``rss_peak`` and psutil
    installed, the process RSS high-water mark above its starting level (sampled every 2 ms; it
    also sees native library buffers and is not slowed down by tracing).
    """
    out = None
    times: list[float] = []
    cpus: list[float] = []
    try:
        for _ in range(max(1, repeat)):
            if prep:
                prep()
            gc.collect()
            t, c = time.perf_counter(), time.process_time()
            out = fn()
            times.append((time.perf_counter() - t) * 1000.0)
            cpus.append((time.process_time() - c) * 1000.0)
        peak = None
        if memory:
            if prep:
                prep()
            gc.collect()
            if rss_peak:
                peak = _rss_delta_peak(fn)
            else:
                tracemalloc.start()
                tracemalloc.reset_peak()
                base = tracemalloc.get_traced_memory()[0]
                fn()
                peak = (tracemalloc.get_traced_memory()[1] - base) / 1e6
                tracemalloc.stop()
    except Exception as exc:  # a stage the data cannot serve is reported, not fatal
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        RESULTS.append({"stage": stage, "what": what, "error": f"{type(exc).__name__}: {exc}"[:90]})
        return None
    RESULTS.append(
        {
            "stage": stage,
            "what": what,
            "first_ms": times[0],
            "warm_ms": min(times[1:]) if len(times) > 1 else None,
            "cpu_ms": min(cpus),
            "peak_mb": peak,
            "rss_mb": _rss_mb(),
        }
    )
    return out


def _rss_delta_peak(fn: Callable[[], Any]) -> float | None:
    """Run ``fn`` while sampling RSS; peak minus start in MB (None without psutil)."""
    try:
        import psutil
    except ImportError:
        return None
    import threading

    proc = psutil.Process()
    start = proc.memory_info().rss
    peak = [start]
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            peak[0] = max(peak[0], proc.memory_info().rss)
            time.sleep(0.002)

    th = threading.Thread(target=sample, daemon=True)
    th.start()
    try:
        fn()
    finally:
        stop.set()
        th.join()
    peak[0] = max(peak[0], proc.memory_info().rss)
    return (peak[0] - start) / 1e6


def _rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1e6 if sys.platform == "darwin" else r / 1e3


def print_table() -> None:
    w = max(len(r["what"]) for r in RESULTS)
    print(
        f"\n{'stage'.ljust(w)}  {'first ms':>10} {'warm ms':>10} {'cpu ms':>9} "
        f"{'peak MB':>9} {'rss MB':>8}"
    )
    print("-" * (w + 52))
    for r in RESULTS:
        if "error" in r:
            print(f"{r['what'].ljust(w)}  SKIPPED: {r['error']}")
            continue
        warm = f"{r['warm_ms']:.1f}" if r["warm_ms"] is not None else "-"
        peak = f"{r['peak_mb']:.1f}" if r["peak_mb"] is not None else "-"
        cpu = f"{r['cpu_ms']:.1f}" if r.get("cpu_ms") is not None else "-"
        print(
            f"{r['what'].ljust(w)}  {r['first_ms']:>10.1f} {warm:>10} {cpu:>9} {peak:>9} "
            f"{r['rss_mb']:>8.0f}"
        )


# --------------------------------------------------------------------------- stages


def stage_startup(repeat: int) -> None:
    """Cold process: import server, READY line, and the background preload."""
    py = sys.executable
    code = "import time; t=time.perf_counter(); import server; print((time.perf_counter()-t)*1000)"
    times = []
    for _ in range(max(2, repeat)):
        out = subprocess.run(
            [py, "-c", code], cwd=PY_DIR, capture_output=True, text=True, timeout=120
        ).stdout.strip()
        if out:
            times.append(float(out.splitlines()[-1]))
    if times:
        RESULTS.append(
            {
                "stage": "startup",
                "what": "import server (cold process)",
                "first_ms": times[0],
                "warm_ms": min(times[1:]) if len(times) > 1 else None,
                "peak_mb": None,
                "rss_mb": 0.0,
            }
        )
    # The READY line: process spawn to the "READY:<port>" message the shell waits for.
    ready = []
    for _ in range(max(2, repeat)):
        port = 20000 + (os.getpid() + len(ready)) % 20000
        t = time.perf_counter()
        proc = subprocess.Popen(
            [py, "server.py", "--port", str(port)],
            cwd=PY_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            line = proc.stdout.readline()
            if line.startswith("READY"):
                ready.append((time.perf_counter() - t) * 1000.0)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    if ready:
        RESULTS.append(
            {
                "stage": "startup",
                "what": "spawn -> READY line",
                "first_ms": ready[0],
                "warm_ms": min(ready[1:]) if len(ready) > 1 else None,
                "peak_mb": None,
                "rss_mb": 0.0,
            }
        )
    # Which heavy packages did importing the server drag in?
    probe = (
        "import sys, server; "
        "print(','.join(m for m in ('matplotlib','scipy','pyarrow','radarx','cmweather','xradar',"
        "'pandas','dask','PIL','pyproj','zarr','netCDF4','h5py') if m in sys.modules))"
    )
    out = subprocess.run([py, "-c", probe], cwd=PY_DIR, capture_output=True, text=True).stdout
    print(f"loaded by `import server`: {out.strip() or '(none of the heavy ones)'}")


def stage_open(path: str | None, tree_factory: Callable[[], xr.DataTree], repeat: int):
    from engine.reader import RadarReader, preload_backends

    preload_backends()
    if path:
        label = f"open_file {Path(path).name}"

        def fn():
            rd = RadarReader()
            rd.open_file(path)
            return rd

        rd = measure("open", label, fn, repeat=repeat, rss_peak=True)
        if rd is not None:
            return rd
    tree = tree_factory()
    rd = reader_from_tree(tree)
    measure(
        "open",
        "open_file (synthetic: schema only)",
        lambda: rd._build_schema(tree, "x"),
        repeat=repeat,
    )
    return rd


def _moment(rd, prefer=("DBZH", "DBZ", "reflectivity")) -> str:
    sch = rd.get_schema()
    for v in prefer:
        if v in sch["variables"]:
            return v
    return sch["variables"][0]


def _sweeps_with(rd, var: str) -> list[int]:
    return [s["index"] for s in rd.get_schema()["sweeps"] if var in (s.get("variables") or [var])]


def stage_sweeps(rd, repeat: int) -> str:
    import server
    from engine import arrow_bridge

    var = _moment(rd)
    idx = _sweeps_with(rd, var)
    s0 = idx[0]
    measure(
        "sweep",
        f"sweep_data pack {var} sweep {s0} (wire format)",
        lambda: server._pack_field(rd.get_sweep_data(s0, var), var),
        repeat=repeat,
    )

    def all_sweeps():
        for s in idx:
            server._pack_field(rd.get_sweep_data(s, var), var)

    measure("sweep", f"sweep switching: all {len(idx)} {var} sweeps", all_sweeps, repeat=repeat)
    da = rd.get_sweep_data(s0, var)
    measure(
        "sweep",
        "arrow IPC of one sweep (DataArray)",
        lambda: arrow_bridge.dataarray_to_arrow(da),
        repeat=repeat,
    )
    ds = rd.get_sweep_dataset(s0)
    measure(
        "sweep",
        "arrow IPC of one sweep (Dataset, all moments)",
        lambda: arrow_bridge.dataset_to_arrow(ds),
        repeat=repeat,
    )
    return var


def stage_render(rd, var: str, repeat: int) -> None:
    from engine import renderer
    from engine.renderer import RadarRenderer

    idx = _sweeps_with(rd, var)
    s0 = idx[0]

    def prep():
        renderer._INDEX_MAP_CACHE.clear()

    def first():
        r = RadarRenderer()
        return r.render_sweep(rd.get_sweep_data(s0, var), var, s0, 800, 600, cache_token="bench")

    measure(
        "render",
        f"PNG render {var} sweep {s0} 800x600 (cold geometry)",
        first,
        repeat=repeat,
        prep=prep,
    )

    def switching():
        r = RadarRenderer()
        for s in idx:
            r.render_sweep(rd.get_sweep_data(s, var), var, s, 800, 600, cache_token="bench")

    measure(
        "render", f"PNG render: switch through {len(idx)} sweeps 800x600", switching, repeat=repeat
    )

    def big():
        r = RadarRenderer()
        return r.render_sweep(rd.get_sweep_data(s0, var), var, s0, 2000, 2000, cache_token="b2")

    measure("render", "PNG render 2000x2000", big, repeat=repeat)


def stage_volume(rd, var: str, repeat: int) -> None:
    from engine.volume import extract_volume

    box = {"x_min": -100e3, "x_max": 100e3, "y_min": -100e3, "y_max": 100e3}
    for res in (100, 200):
        measure(
            "volume",
            f"3D extract_volume {var} res={res} +-100 km",
            lambda res=res: extract_volume(
                rd.datatree,
                var,
                box,
                resolution=res,
                z_max=15000.0,
                sweep_loader=rd.get_sweep_dataset,
            ),
            repeat=repeat,
        )


def stage_qc(rd, var: str, repeat: int) -> None:
    from engine.qc import QCPipeline

    da = rd.get_sweep_data(_sweeps_with(rd, var)[0], var)
    pipe = QCPipeline()
    pipe.add_step("noise_removal", {})
    pipe.add_step("despeckle", {})
    pipe.add_step("remove_clutter", {})
    measure(
        "qc",
        "QC pipeline: noise_removal+despeckle+remove_clutter",
        lambda: pipe.run(da),
        repeat=repeat,
    )
    p2 = QCPipeline()
    p2.add_step("smooth_data", {})
    measure("qc", "QC smooth_data", lambda: p2.run(da), repeat=repeat)


def stage_modules(rd, repeat: int) -> None:
    from engine import plugins

    plugins.discover_modules()
    for mod in ("storm_detection", "radarx_hid"):

        def fn(mod=mod):
            return plugins.run_module(
                mod,
                rd.datatree,
                0,
                {},
                get_sweep_dataset=rd.get_sweep_dataset,
                return_fields=True,
                timeout_s=None,
            )

        measure("module", f"module {mod} (sweep 0)", fn, repeat=repeat)


def stage_mosaic(readers: list, repeat: int) -> None:
    from engine import mosaic
    from engine.mosaic import MosaicCache

    if not mosaic.radarx_available():
        RESULTS.append({"stage": "mosaic", "what": "mosaic", "error": "radarx not installed"})
        return
    grid = {"resolution_m": 1000, "max_range_km": 150, "z_max_m": 12000, "z_step_m": 1000}
    for n in (2, 4):
        if len(readers) < n:
            continue
        rs = [(f"f{i}", r) for i, r in enumerate(readers[:n])]
        measure(
            "mosaic",
            f"mosaic cappi, {n} radars, 1 km grid",
            lambda rs=rs: mosaic.build_mosaic(
                rs, "DBZH", "cappi", grid, height_m=3000, cache=MosaicCache()
            ),
            repeat=repeat,
        )


def stage_export(rd, var: str, repeat: int) -> None:
    from engine.exporter import RadarExporter

    tmp = Path(tempfile.mkdtemp(prefix="xrd-bench-"))
    ex = RadarExporter()
    s0 = _sweeps_with(rd, var)[0]
    ext = {"png": ".png", "csv": ".csv", "netcdf": ".nc", "cfradial2": ".nc", "zarr": ".zarr"}
    counter = itertools.count()
    sizes: dict[str, float] = {}
    try:
        for fmt in ext:

            def fn(fmt=fmt):
                out = tmp / f"{fmt}_{next(counter)}{ext[fmt]}"
                path = Path(
                    ex.export(
                        rd.datatree, fmt, dpi=150, output_path=str(out), variable=var, sweep=s0
                    )
                )
                files = [f for f in path.rglob("*") if f.is_file()] if path.is_dir() else [path]
                sizes[fmt] = sum(f.stat().st_size for f in files) / 1e6
                shutil.rmtree(path) if path.is_dir() else path.unlink()  # keep the disk free

            measure("export", f"export {fmt}", fn, repeat=max(1, min(repeat, 2)))
            if fmt in sizes and "error" not in RESULTS[-1]:
                RESULTS[-1]["what"] += f" ({sizes[fmt]:.1f} MB out)"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- main

STAGES = ["startup", "open", "sweeps", "render", "volume", "qc", "modules", "mosaic", "export"]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("files", nargs="*", help="radar files (read only); none = synthetic volumes")
    ap.add_argument("--repeat", type=int, default=3, help="timed runs per stage (default 3)")
    ap.add_argument("--only", nargs="+", choices=STAGES, help="run just these stages")
    ap.add_argument("--no-memory", action="store_true", help="skip the tracemalloc run")
    ap.add_argument("--json", help="also write the results to this file")
    args = ap.parse_args()
    want = set(args.only or STAGES)
    from engine.reader import RadarReader

    print(f"python {platform.python_version()} on {platform.platform()} ({platform.machine()})")
    import numpy
    import xarray

    print(f"numpy {numpy.__version__}, xarray {xarray.__version__}, {os.cpu_count()} cpus")

    if "startup" in want:
        stage_startup(args.repeat)

    main_path = args.files[0] if args.files else None
    rd = (
        stage_open(main_path, make_synthetic_volume, args.repeat)
        if "open" in want or want - {"startup"}
        else None
    )
    if rd is None:
        print_table()
        return 0
    var = stage_sweeps(rd, args.repeat) if "sweeps" in want else _moment(rd)
    if "render" in want:
        stage_render(rd, var, args.repeat)
    if "volume" in want:
        stage_volume(rd, var, args.repeat)
    if "qc" in want:
        stage_qc(rd, var, args.repeat)
    if "modules" in want:
        stage_modules(rd, args.repeat)
    if "mosaic" in want:
        readers = [rd]
        for p in args.files[1:4]:
            other = RadarReader()
            other.open_file(p)
            readers.append(other)
        k = 0
        while len(readers) < 4 and len(args.files) < 2:
            k += 1
            readers.append(
                reader_from_tree(
                    make_synthetic_volume(35.33 + 0.9 * k, -97.28 + 0.7 * k, f"SYN{k}", seed=k)
                )
            )
        stage_mosaic(readers, args.repeat)
    if "export" in want:
        stage_export(rd, var, args.repeat)

    print_table()
    if args.json:
        Path(args.json).write_text(json.dumps(RESULTS, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

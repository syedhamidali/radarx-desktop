"""Data side of ``xrd animate``: grid the volumes, find and track the storm, keep a disk cache.

For each time step of a :class:`~engine.animate_plan.StepPlan` the volumes of the step's radars are
merged onto a 3-D grid (the same ``engine.mosaic`` code as ``xrd mosaic`` and the 3D tab). The
work is done **one time step at a time**: the radar files of one step are opened, gridded and
closed before the next step is touched, so memory is bounded by one step (a few radar volumes),
not by the length of the series. What survives a step is small: the cells and the column maximum of
the coarse grid, and the fine grid written to the cache folder.

Two passes, as in ``tools/storm_clip``:

1. **coarse** (2 km): the storm cells of every step; the strongest one is tracked from step to step;
2. **fine** (500 m): a box around the whole track, one grid per step, kept on disk.

Every result is written to a content-addressed cache (``GridCache``): a key made from the file
names, sizes and times plus the grid settings. So an interrupted job resumes where it stopped, and a
second run with another camera, look or length reuses the grids without opening a radar file.
Writes are atomic (a file is complete or absent).

The gridding itself is behind the small :class:`Gridder` interface so tests can run the whole
pipeline on synthetic grids.
"""

# ruff: noqa: E501

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import time
from collections.abc import Callable
from dataclasses import astuple, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from engine.animate_plan import AnimatePlanError, Step, StepPlan
from engine.animate_storm import (
    Cell,
    Eye,
    box_around,
    column_max_dbz,
    find_cells,
    find_eye,
    linear_track,
    storm_motion,
    track_storm,
)

Log = Callable[[str], None]


def default_cache_dir() -> Path:
    """``$XRADAR_ANIMATE_CACHE``, else ``~/.cache/xradar-desktop/animate``."""
    env = os.environ.get("XRADAR_ANIMATE_CACHE")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".cache" / "xradar-desktop" / "animate"


@dataclass
class GridOptions:
    """Settings of the gridding and of the storm search (all part of the cache key)."""

    detail_m: float = 500.0  # fine grid cell size
    coarse_m: float = 2000.0  # grid used to find and track the storm
    z_max_m: float = 16000.0
    crop_half_km: float = 60.0  # half width of the shown box around the storm
    max_range_km: float = 230.0
    cell_dbz: float = 45.0  # a convective cell: column maximum at least this
    min_cell_km2: float = 20.0
    track_mode: str = "cells"  # "cells" | "linear" | "eye"
    eye: bool = False  # also look for the echo-free eye of a tropical cyclone
    winds: bool = False  # multi-Doppler winds per step, to carry the echo along the observed flow
    wind_dx_m: float = 2000.0
    variable: str = "DBZH"

    def key_dict(self, kind: str) -> dict[str, Any]:
        """The settings that change the result of one kind of cached grid."""
        if kind == "coarse":
            return {
                "coarse_m": self.coarse_m,
                "z_max_m": self.z_max_m,
                "max_range_km": self.max_range_km,
                "cell_dbz": self.cell_dbz,
                "min_cell_km2": self.min_cell_km2,
                "variable": self.variable,
            }
        if kind == "fine":
            return {
                "detail_m": self.detail_m,
                "z_max_m": self.z_max_m,
                "max_range_km": self.max_range_km,
                "variable": self.variable,
            }
        return {"wind_dx_m": self.wind_dx_m}


# ---------------------------------------------------------------------------
# Results of one step
# ---------------------------------------------------------------------------


@dataclass
class Coarse:
    """What is kept of a coarse grid: its cells and the column maximum (small), not the volume."""

    cells: list[Cell]
    comp: np.ndarray  # (ny, nx) float32 dBZ, NaN = no echo
    x0: float
    y0: float
    cell_m: float
    origin: tuple[float, float]  # lat, lon of the grid's projection centre


@dataclass
class Fine:
    header: dict[str, Any]  # engine.mosaic.volume_message header (grid, sites, scale ...)
    volume: np.ndarray  # uint8 (nz, ny, nx), 0 = no echo


class Gridder(Protocol):
    """Turns the volumes of one time step into grids. Tests implement this on synthetic data."""

    def coarse(self, step: Step, opts: GridOptions) -> Coarse: ...

    def fine(self, step: Step, opts: GridOptions, extent: dict[str, float]) -> Fine: ...

    def winds(
        self,
        step: Step,
        opts: GridOptions,
        origin: tuple[float, float],
        extent_km: tuple[float, float, float, float],
        motion_ms: tuple[float, float],
    ) -> tuple[dict[str, Any], bytes] | None: ...


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------


def file_identity(path: Path) -> list[Any]:
    """Name, size and modification time: a changed file gets a new key."""
    try:
        st = path.stat()
        return [path.name, st.st_size, st.st_mtime_ns]
    except OSError:
        return [path.name, -1, -1]


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".part{os.getpid()}")
    tmp.write_bytes(data)
    tmp.replace(path)


class GridCache:
    """Gridded volumes on disk, addressed by what went into them.

    Layout: ``<root>/grid/coarse-<key>.npz``, ``fine-<key>.json`` + ``.bin``, ``wind-<key>.json`` +
    ``.bin``. A ``.json`` is written last, so its presence means the pair is complete.
    """

    def __init__(self, root: Path | str | None = None, fresh: bool = False) -> None:
        self.root = Path(root).expanduser() if root else default_cache_dir()
        self.dir = self.root / "grid"
        self.fresh = fresh  # ignore existing entries (they are overwritten)
        self.hits = {"coarse": 0, "fine": 0, "wind": 0}
        self.computed = {"coarse": 0, "fine": 0, "wind": 0}

    # -- keys ----------------------------------------------------------------------------
    def key(self, kind: str, step: Step, opts: GridOptions, extra: Any = None) -> str:
        payload = {
            "v": 1,
            "kind": kind,
            "files": sorted(
                [radar, *file_identity(v.path)] for radar, (v, _off) in step.files.items()
            ),
            "opts": opts.key_dict(kind),
            "extra": extra,
        }
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha1(blob).hexdigest()[:20]

    # -- coarse --------------------------------------------------------------------------
    def get_coarse(self, key: str) -> Coarse | None:
        p = self.dir / f"coarse-{key}.npz"
        if self.fresh or not p.is_file():
            return None
        try:
            with np.load(p) as z:
                cells = [Cell(*map(float, row)) for row in z["cells"]]
                geo = z["geo"]
                return Coarse(
                    cells,
                    z["comp"],
                    float(geo[0]),
                    float(geo[1]),
                    float(geo[2]),
                    (float(geo[3]), float(geo[4])),
                )
        except Exception:  # a damaged file is recomputed
            return None

    def put_coarse(self, key: str, c: Coarse) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.dir / f"coarse-{key}.npz"
        tmp = p.with_name(p.name + f".part{os.getpid()}")
        rows = np.array([astuple(x) for x in c.cells], dtype=float).reshape(-1, 6)
        with open(tmp, "wb") as fh:
            np.savez(
                fh,
                cells=rows,
                comp=c.comp.astype(np.float32),
                geo=np.array([c.x0, c.y0, c.cell_m, c.origin[0], c.origin[1]]),
            )
        tmp.replace(p)

    # -- fine volumes and winds (a json header + a binary) -------------------------------
    def paths(self, kind: str, key: str) -> tuple[Path, Path]:
        return self.dir / f"{kind}-{key}.json", self.dir / f"{kind}-{key}.bin"

    def get_pair(self, kind: str, key: str) -> dict[str, Any] | None:
        """The header of a complete cached pair, or None (absent, partial or damaged)."""
        hp, bp = self.paths(kind, key)
        if self.fresh or not hp.is_file() or not bp.is_file():
            return None
        try:
            header = json.loads(hp.read_text())
        except ValueError:
            return None
        want = header.get("_bytes")
        if want is not None and bp.stat().st_size != want:
            return None
        return header

    def put_pair(self, kind: str, key: str, header: dict[str, Any], data: bytes) -> dict[str, Any]:
        hp, bp = self.paths(kind, key)
        header = {**header, "_bytes": len(data)}
        _atomic_write(bp, data)
        _atomic_write(hp, json.dumps(header).encode())
        return header

    def name(self, kind: str, key: str) -> dict[str, str]:
        hp, bp = self.paths(kind, key)
        return {"header": hp.name, "bin": bp.name}


# ---------------------------------------------------------------------------
# The bundle (what the renderer loads)
# ---------------------------------------------------------------------------


@dataclass
class BuildStats:
    steps: int = 0
    coarse_cached: int = 0
    fine_cached: int = 0
    wind_cached: int = 0
    gridded_steps: int = 0  # steps that had to be (partly) gridded this run
    seconds: float = 0.0
    peak_rss_mb: float | None = None
    fine_mb_per_step: float = 0.0


@dataclass
class Bundle:
    manifest: dict[str, Any]
    store: Path  # folder holding the files the manifest names
    stats: BuildStats
    cells: list[list[Cell]] = field(default_factory=list)

    @property
    def track_km(self) -> list[tuple[float, float]]:
        return [(p[0], p[1]) for p in self.manifest["storm"]["track_km"]]


def peak_rss_mb() -> float | None:
    """Peak resident memory of this process in MB (None where the platform has no way to say)."""
    try:
        import resource
        import sys

        v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return v / (1024 * 1024) if sys.platform == "darwin" else v / 1024
    except Exception:
        return None


def _reproject(
    src: tuple[float, float], dst: tuple[float, float], x_km: float, y_km: float
) -> tuple[float, float]:
    """A point in km of the azimuthal-equidistant frame centred on ``src`` (lat, lon), in the frame
    centred on ``dst``. Identity (no pyproj call) when the centres are the same."""
    if abs(src[0] - dst[0]) < 1e-9 and abs(src[1] - dst[1]) < 1e-9:
        return x_km, y_km
    from engine import mosaic

    lon, lat = mosaic._aeqd(*src)(x_km * 1000.0, y_km * 1000.0, inverse=True)
    x, y = mosaic._aeqd(*dst)(lon, lat)
    return x / 1000.0, y / 1000.0


def _reproject_cell(c: Cell, src: tuple[float, float], dst: tuple[float, float]) -> Cell:
    x, y = _reproject(src, dst, c.x_km, c.y_km)
    return Cell(x, y, c.max_dbz, c.area_km2, c.top_km, c.score)


def build_bundle(
    plan: StepPlan,
    opts: GridOptions,
    cache: GridCache,
    gridder: Gridder,
    log: Log = lambda _m: None,
    title: str | None = None,
) -> Bundle:
    """Grid every time step (one at a time, from the cache where possible) and track the storm.

    Returns the manifest the renderer loads; the grids stay in ``cache`` (``Bundle.store``).
    Raises :class:`AnimatePlanError` when no storm can be found.
    """
    t_start = time.perf_counter()
    steps = plan.steps
    n = len(steps)
    stats = BuildStats(steps=n)
    times_s = plan.times_s
    touched: set[int] = set()

    # 1. coarse pass: cells + column maximum of each step ---------------------------------
    from engine import mosaic

    cells_per_step: list[list[Cell]] = []
    comps: list[Coarse] = []
    for i, st in enumerate(steps):
        key = cache.key("coarse", st, opts)
        got = cache.get_coarse(key)
        if got is not None:
            stats.coarse_cached += 1
            cache.hits["coarse"] += 1
            log(f"[{i + 1}/{n}] {st.time:%H:%M:%S} UTC  storm search: from the cache")
        else:
            log(f"[{i + 1}/{n}] {st.time:%H:%M:%S} UTC  storm search on a {opts.coarse_m:g} m grid")
            got = gridder.coarse(st, opts)
            cache.put_coarse(key, got)
            cache.computed["coarse"] += 1
            touched.add(i)
        comps.append(Coarse([], got.comp, got.x0, got.y0, got.cell_m, got.origin))
        # every step's cells in the frame of the first step (radars that come and go move the origin)
        cells_per_step.append([_reproject_cell(c, got.origin, comps[0].origin) for c in got.cells])
        if got.cells:
            top = got.cells[0]
            log(
                f"    {len(got.cells)} cell(s); strongest {top.max_dbz:.0f} dBZ, "
                f"{top.area_km2:.0f} km2, top {top.top_km:.1f} km"
            )
        else:
            log("    no cell above the threshold")

    if not cells_per_step[0]:
        raise AnimatePlanError(
            f"no convective cell of at least {opts.cell_dbz:g} dBZ and {opts.min_cell_km2:g} km2 "
            "in the first time step. The animation follows the strongest storm: lower "
            "--cell-dbz (for example 35), or start later with --start, or use only the radars "
            "that see the storm."
        )
    track = track_storm(cells_per_step, times_s)
    frame0 = comps[0].origin
    want_eyes = opts.eye or opts.track_mode == "eye"
    eyes: list[Eye | None] = [None] * n
    if want_eyes:
        for i, c in enumerate(comps):
            ax, ay = _reproject(frame0, c.origin, track[i].x_km, track[i].y_km)
            e = find_eye(c.comp, c.x0, c.y0, c.cell_m, (ax, ay))
            if e:
                ex, ey = _reproject(c.origin, frame0, e.x_km, e.y_km)
                e = Eye(ex, ey, e.radius_km)
            eyes[i] = e
    if opts.track_mode == "eye":
        track = [
            Cell(e.x_km, e.y_km, c.max_dbz, c.area_km2, c.top_km, c.score) if e else c
            for c, e in zip(track, eyes, strict=True)
        ]
    elif opts.track_mode == "linear":
        track = linear_track(track, times_s)

    proj0 = mosaic._aeqd(*frame0)
    track_lonlat = [proj0(c.x_km * 1000.0, c.y_km * 1000.0, inverse=True) for c in track]
    eye_lonlat = [
        proj0(e.x_km * 1000.0, e.y_km * 1000.0, inverse=True) if e else None for e in eyes
    ]

    # 2. the box around the whole track ---------------------------------------------------
    lon_c = float(np.mean([p[0] for p in track_lonlat]))
    lat_c = float(np.mean([p[1] for p in track_lonlat]))
    proj_c = mosaic._aeqd(lat_c, lon_c)
    track_c = [proj_c(lo, la) for lo, la in track_lonlat]
    box = box_around([(x / 1000.0, y / 1000.0) for x, y in track_c], opts.crop_half_km + 12.0)
    west, south = proj_c(box[0] * 1000.0, box[1] * 1000.0, inverse=True)
    east, north = proj_c(box[2] * 1000.0, box[3] * 1000.0, inverse=True)
    extent = {
        "west": round(west, 4),
        "south": round(south, 4),
        "east": round(east, 4),
        "north": round(north, 4),
    }
    motion0 = storm_motion([(x / 1000.0, y / 1000.0) for x, y in track_c], times_s)

    # 3. the fine grids (and winds), one step at a time -----------------------------------
    manifest_steps: list[dict[str, Any]] = []
    origin: tuple[float, float] | None = None
    sites: list[dict[str, Any]] = []
    for i, st in enumerate(steps):
        fkey = cache.key("fine", st, opts, extra=extent)
        header = cache.get_pair("fine", fkey)
        if header is not None:
            stats.fine_cached += 1
            cache.hits["fine"] += 1
            log(f"[{i + 1}/{n}] {st.time:%H:%M:%S} UTC  {opts.detail_m:g} m grid: from the cache")
        else:
            log(f"[{i + 1}/{n}] {st.time:%H:%M:%S} UTC  gridding at {opts.detail_m:g} m")
            fine = gridder.fine(st, opts, extent)
            header = cache.put_pair(
                "fine", fkey, fine.header, np.ascontiguousarray(fine.volume).tobytes()
            )
            cache.computed["fine"] += 1
            touched.add(i)
            stats.fine_mb_per_step = fine.volume.nbytes / 1e6
            del fine
        g = header["grid"]
        origin = (g["origin_lat"], g["origin_lon"])
        sites = header["radars"]

        wind_files = None
        if opts.winds:
            wkey = cache.key("wind", st, opts, extra={"extent": extent, "m": list(motion0)})
            wh = cache.get_pair("wind", wkey)
            if wh is not None:
                stats.wind_cached += 1
                cache.hits["wind"] += 1
                wind_files = cache.name("wind", wkey)
            else:
                ext_km = (
                    g["x0"] / 1000.0,
                    g["y0"] / 1000.0,
                    (g["x0"] + (g["nx"] - 1) * g["cell_m"]) / 1000.0,
                    (g["y0"] + (g["ny"] - 1) * g["cell_m"]) / 1000.0,
                )
                got = gridder.winds(st, opts, origin, ext_km, motion0)
                if got is not None:
                    cache.put_pair("wind", wkey, got[0], got[1])
                    cache.computed["wind"] += 1
                    touched.add(i)
                    wind_files = cache.name("wind", wkey)
        manifest_steps.append(
            {
                "index": i,
                "time_utc": st.time.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                "t_s": times_s[i],
                "files": {r: {"name": v.name, "offset_s": off} for r, (v, off) in st.files.items()},
                "mosaic": cache.name("fine", fkey),
                "wind": wind_files,
                "eye_lonlat": eye_lonlat[i],
                "eye_radius_km": eyes[i].radius_km if eyes[i] else None,
                "storm": {
                    "max_dbz": track[i].max_dbz,
                    "area_km2": track[i].area_km2,
                    "top_km": track[i].top_km,
                },
            }
        )
    assert origin is not None
    stats.gridded_steps = len(touched)

    proj_f = mosaic._aeqd(*origin)
    track_f = [proj_f(lo, la) for lo, la in track_lonlat]
    track_km = [(x / 1000.0, y / 1000.0) for x, y in track_f]
    for st_m in manifest_steps:
        ll = st_m.pop("eye_lonlat")
        r = st_m.pop("eye_radius_km")
        if ll is not None:
            ex, ey = proj_f(ll[0], ll[1])
            st_m["eye"] = {"x_km": ex / 1000.0, "y_km": ey / 1000.0, "radius_km": r}
        else:
            st_m["eye"] = None
    u, v = storm_motion(track_km, times_s)
    first = steps[0].time
    manifest = {
        "version": 1,
        "title": title or _default_title(plan, first),
        "case": ", ".join(plan.used_radars),
        "variable": opts.variable,
        "units": "dBZ",
        "origin": {"lat": origin[0], "lon": origin[1]},
        "crop_half_km": opts.crop_half_km,
        "detail_m": opts.detail_m,
        "steps": manifest_steps,
        "storm": {
            "track_km": track_km,
            "track_lonlat": track_lonlat,
            "motion_ms": [u, v],
            "speed_ms": math.hypot(u, v),
            "heading_deg": (math.degrees(math.atan2(u, v)) + 360.0) % 360.0,
        },
        "sites": sites,
        "animate": {
            "reference": plan.reference,
            "tolerance_s": plan.tolerance_s,
            "winds": bool(opts.winds and any(s["wind"] for s in manifest_steps)),
            "track_mode": opts.track_mode,
        },
    }
    stats.seconds = time.perf_counter() - t_start
    stats.peak_rss_mb = peak_rss_mb()
    return Bundle(manifest, cache.dir, stats, cells_per_step)


def _default_title(plan: StepPlan, first: datetime) -> str:
    return f"{', '.join(plan.used_radars)}  {first:%d %b %Y}"


# ---------------------------------------------------------------------------
# The real gridder (engine.mosaic on the radar files)
# ---------------------------------------------------------------------------


class EngineGridder:
    """Grids with :mod:`engine.mosaic`. One time step at a time: files are opened, gridded, closed."""

    def __init__(self, log: Log = lambda _m: None) -> None:
        self.log = log
        self.unreadable: dict[str, str] = {}  # radar -> why its files were left out (named once)
        self.live_groups = 0
        self.peak_groups = 0

    @contextlib.contextmanager
    def _group(self, step: Step):
        """Open the readable radars of one step; close them on exit (and count live groups)."""
        readers: list[tuple[str, Any]] = []
        self.live_groups += 1
        self.peak_groups = max(self.peak_groups, self.live_groups)
        try:
            for radar, (vol, _off) in sorted(step.files.items()):
                if radar in self.unreadable:
                    continue
                try:
                    readers.append((radar, _open_valid(vol.path)))
                except (ValueError, OSError) as exc:
                    self.unreadable[radar] = str(exc)[:110]
                    self.log(f"    skipping {radar}: {self.unreadable[radar]}")
            yield readers
        finally:
            for _, r in readers:
                with contextlib.suppress(Exception):
                    r.close()
            self.live_groups -= 1

    def _mosaic(self, step: Step, grid: dict[str, Any], opts: GridOptions, need: int):
        from engine import mosaic

        with self._group(step) as readers:
            if len(readers) < need:
                raise AnimatePlanError(
                    f"time step {step.time:%H:%M:%S} UTC: only {len(readers)} readable radar "
                    f"file(s) (need {need}); unreadable: "
                    + (", ".join(f"{r} ({w})" for r, w in self.unreadable.items()) or "none")
                )
            return mosaic.build_mosaic(
                readers,
                opts.variable,
                "composite",
                grid,
                height_m=3000.0,
                keep_volume=True,
                cache=None,
            )

    def coarse(self, step: Step, opts: GridOptions) -> Coarse:
        rec = self._mosaic(
            step,
            {
                "resolution_m": opts.coarse_m,
                "z_max_m": opts.z_max_m,
                "z_step_m": 1000.0,
                "max_range_km": opts.max_range_km,
            },
            opts,
            1,
        )
        if rec.volume is None or rec.volume_range is None or rec.volume_z_m is None:
            raise AnimatePlanError(rec.volume_note or "the mosaic has no 3-D volume")
        cells = find_cells(
            rec.volume,
            rec.volume_range,
            rec.volume_z_m / 1000.0,
            rec.x0,
            rec.y0,
            rec.cell_m,
            threshold_dbz=opts.cell_dbz,
            min_area_km2=opts.min_cell_km2,
        )
        comp = column_max_dbz(rec.volume, rec.volume_range).astype(np.float32)
        return Coarse(cells, comp, rec.x0, rec.y0, rec.cell_m, (rec.origin[0], rec.origin[1]))

    def fine(self, step: Step, opts: GridOptions, extent: dict[str, float]) -> Fine:
        from engine import mosaic

        rec = self._mosaic(
            step,
            {
                "resolution_m": opts.detail_m,
                "z_max_m": opts.z_max_m,
                "z_step_m": 500.0,
                "max_range_km": opts.max_range_km,
                "extent": extent,
            },
            opts,
            1,
        )
        cache = mosaic.MosaicCache(max_items=1, max_bytes=10**12)
        cache.put(rec)
        header, vol = mosaic.volume_message(rec.mosaic_id, cache)
        return Fine(header, np.ascontiguousarray(vol))

    def winds(self, step, opts, origin, extent_km, motion_ms):
        from engine.animate_winds import retrieve_step_winds

        return retrieve_step_winds(
            {r: v.path for r, (v, _o) in step.files.items() if r not in self.unreadable},
            origin,
            extent_km,
            motion_ms,
            _open_valid,
            dx_m=opts.wind_dx_m,
            log=self.log,
        )


def _open_valid(path: Path):
    """Open a volume and check that it is usable (complete sweeps, a sane site position)."""
    from engine.reader import RadarReader

    reader = RadarReader()
    reader.open_file(str(path))
    tree = getattr(reader, "datatree", None)
    if tree is not None:
        if not any(k.startswith("sweep_") for k in tree.children):
            raise ValueError("no complete sweeps in the file")
        root = tree.root.to_dataset()
        if "latitude" in root and "longitude" in root:
            lat = float(np.asarray(root["latitude"].values).ravel()[0])
            lon = float(np.asarray(root["longitude"].values).ravel()[0])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError(
                    f"site position {lat:g}, {lon:g} is out of range "
                    "(a TDWR file with unscaled coordinates)"
                )
    return reader

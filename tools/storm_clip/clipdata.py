"""Data side of the storm-clip tool: pick time steps, find and track the storm, build the data bundle.

Run with the sidecar's Python (it needs the engine and radarx), see ``docs/STORM_CLIP.md``::

    python/.venv/bin/python tools/storm_clip/clipdata.py --case ~/Downloads/SEVERE_CASES_TEST/mayfield \
        --out /tmp/storm_bundle

For several time steps of N radars it

1. picks, for each time step, the volume of every radar that is nearest in time,
2. builds a coarse 3-D mosaic per step and finds the strongest / largest convective cell (connected
   area of high column-maximum reflectivity), and tracks it from step to step,
3. builds a fine 3-D mosaic per step on a box around the storm track (``keep_volume``) and writes the
   merged volumes, the storm track and the radar sites into a bundle directory that the renderer
   (``render_clip.py``) loads in the app's 3-D viewer.

Nothing is written next to the radar files; no wind is retrieved.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

_NAME = re.compile(r"^(?P<site>[A-Z0-9]{4})(?P<date>\d{8})_(?P<time>\d{6})")


# ---------------------------------------------------------------------------
# Time steps
# ---------------------------------------------------------------------------


def parse_volume_time(path: str | Path) -> datetime | None:
    """UTC time from a NEXRAD style name such as ``KHPX20211211_020457_V06``."""
    m = _NAME.match(Path(path).name)
    if not m:
        return None
    return datetime.strptime(m["date"] + m["time"], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def list_volumes(case: str | Path) -> dict[str, list[tuple[datetime, Path]]]:
    """``{radar: [(time, path), ...]}`` from ``case/<RADAR>/<files>``, sorted by time."""
    out: dict[str, list[tuple[datetime, Path]]] = {}
    for d in sorted(Path(case).iterdir()):
        if not d.is_dir():
            continue
        vols = []
        for f in sorted(d.iterdir()):
            t = parse_volume_time(f)
            if t is not None and f.is_file():
                vols.append((t, f))
        if vols:
            out[d.name] = sorted(vols)
    return out


@dataclass
class Step:
    time: datetime  # reference time of the step
    files: dict[str, tuple[Path, float]]  # radar -> (path, offset in s from the reference time)


def choose_steps(
    volumes: dict[str, list[tuple[datetime, Path]]],
    reference: str | None = None,
    n_steps: int | None = None,
    max_offset_s: float = 330.0,
    min_changed_fraction: float = 0.5,
) -> list[Step]:
    """Time steps along the reference radar's volumes; every radar contributes its nearest volume.

    A step is dropped when fewer than two radars have a volume within ``max_offset_s`` of it, or when
    fewer than ``min_changed_fraction`` of its radars have a volume that differs from the previous step.
    """
    if not volumes:
        raise ValueError("no radar volumes found")
    ref = reference if reference in volumes else max(volumes, key=lambda r: len(volumes[r]))
    steps: list[Step] = []
    for t_ref, _ in volumes[ref]:
        files: dict[str, tuple[Path, float]] = {}
        for radar, vols in volumes.items():
            t, path = min(vols, key=lambda v: abs((v[0] - t_ref).total_seconds()))
            off = (t - t_ref).total_seconds()
            if abs(off) <= max_offset_s:
                files[radar] = (path, off)
        if len(files) >= 2:
            steps.append(Step(t_ref, files))
    # A step only adds information if most radars have a NEW volume in it (the last volumes of a
    # sequence often repeat for the radars that matter at the storm): drop the ones that do not.
    kept: list[Step] = []
    for st in steps:
        if kept:
            prev = kept[-1]
            shared = [r for r in st.files if r in prev.files]
            changed = sum(1 for r in shared if st.files[r][0] != prev.files[r][0])
            if shared and changed < min_changed_fraction * len(shared):
                continue
        kept.append(st)
    steps = kept
    if n_steps is not None:
        steps = steps[:n_steps]
    if len(steps) < 2:
        raise ValueError("need at least two usable time steps")
    return steps


def best_reference(
    volumes: dict[str, list[tuple[datetime, Path]]], **kw: Any
) -> tuple[str, list[Step]]:
    """The radar whose volume times make the best steps: most steps, then the smallest time offsets
    of the other radars' volumes (a radar with the longest sequence is not always the best clock)."""
    best: tuple[tuple[int, float], str, list[Step]] | None = None
    for radar in volumes:
        try:
            steps = choose_steps(volumes, radar, **kw)
        except ValueError:
            continue
        offs = [abs(off) for st in steps for _, off in st.files.values()]
        key = (len(steps), -float(np.mean(offs)))
        if best is None or key > best[0]:
            best = (key, radar, steps)
    if best is None:
        raise ValueError("need at least two usable time steps")
    return best[1], best[2]


# ---------------------------------------------------------------------------
# Storm finding and tracking
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    x_km: float  # reflectivity-weighted centre, km east of the grid origin
    y_km: float
    max_dbz: float
    area_km2: float
    top_km: float  # highest level (km ASL) with at least ``top_dbz`` in the cell
    score: float


def decode(volume: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """uint8 codes (0 = no echo) -> dBZ (NaN = no echo)."""
    out = lo + (volume.astype(np.float32) - 1.0) * (hi - lo) / 254.0
    out[volume == 0] = np.nan
    return out


def find_cells(
    volume: np.ndarray,
    value_range: tuple[float, float],
    z_km: np.ndarray,
    x0_m: float,
    y0_m: float,
    cell_m: float,
    threshold_dbz: float = 45.0,
    top_dbz: float = 18.0,
    min_area_km2: float = 20.0,
) -> list[Cell]:
    """Convective cells of a 3-D volume: connected areas of column-maximum reflectivity >= threshold.

    ``volume`` is the mosaic's uint8 ``(nz, ny, nx)`` field. The score favours strong, tall and large
    cells: ``max_dbz + 2 * top_km + 4 * log10(area_km2)``.
    """
    from scipy import ndimage

    lo, hi = value_range
    dbz = decode(volume, lo, hi)
    with np.errstate(all="ignore"):
        comp = np.nanmax(np.where(np.isfinite(dbz), dbz, -np.inf), axis=0)
    mask = comp >= threshold_dbz
    labels, n = ndimage.label(mask, structure=np.ones((3, 3), dtype=bool))
    cells: list[Cell] = []
    px_km2 = (cell_m / 1000.0) ** 2
    for i in range(1, n + 1):
        sel = labels == i
        area = float(sel.sum()) * px_km2
        if area < min_area_km2:
            continue
        w = np.clip(comp[sel] - threshold_dbz + 1.0, 1.0, None)
        jj, ii = np.nonzero(sel)
        x = x0_m + float((ii * w).sum() / w.sum()) * cell_m
        y = y0_m + float((jj * w).sum() / w.sum()) * cell_m
        col = np.nanmax(
            np.where(sel[None, :, :], np.where(np.isfinite(dbz), dbz, -np.inf), -np.inf),
            axis=(1, 2),
        )
        levels = np.nonzero(col >= top_dbz)[0]
        top = float(z_km[levels.max()]) if levels.size else 0.0
        mx = float(comp[sel].max())
        cells.append(
            Cell(x / 1000.0, y / 1000.0, mx, area, top, mx + 2.0 * top + 4.0 * math.log10(area))
        )
    return sorted(cells, key=lambda c: c.score, reverse=True)


def track_storm(
    cells_per_step: list[list[Cell]],
    times_s: list[float],
    max_jump_km: float = 35.0,
    n_seeds: int = 8,
) -> list[Cell]:
    """The best cell of the first step followed through the later steps (nearest centre, with the
    motion between the last two steps extrapolated). Returns one cell per step; if no cell can be
    followed for the whole clip, the track that goes furthest (then the best total score) is padded
    with the last cell moved along its motion.
    """
    if not cells_per_step or not cells_per_step[0]:
        raise ValueError("no convective cell found in the first time step")
    best: list[Cell] = []
    best_key: tuple[int, float] = (-1, -1.0)
    for seed in cells_per_step[0][:n_seeds]:
        track = [seed]
        vel = (0.0, 0.0)  # km per second
        for k in range(1, len(cells_per_step)):
            dt = times_s[k] - times_s[k - 1]
            px = track[-1].x_km + vel[0] * dt
            py = track[-1].y_km + vel[1] * dt
            cand = [
                (
                    math.hypot(c.x_km - px, c.y_km - py)
                    + 0.05 * abs(c.area_km2 - track[-1].area_km2) ** 0.5,
                    c,
                )
                for c in cells_per_step[k]
            ]
            cand = [(d, c) for d, c in cand if math.hypot(c.x_km - px, c.y_km - py) <= max_jump_km]
            if not cand:
                break
            nxt = min(cand, key=lambda t: t[0])[1]
            vel = ((nxt.x_km - track[-1].x_km) / dt, (nxt.y_km - track[-1].y_km) / dt)
            track.append(nxt)
        key = (len(track), sum(c.score for c in track))
        if key > best_key:
            best, best_key = track, key
    while len(best) < len(cells_per_step):  # lost the storm: carry it along its last motion
        k = len(best)
        dt = times_s[k] - times_s[k - 1]
        if len(best) >= 2:
            vx = (best[-1].x_km - best[-2].x_km) / (times_s[k - 1] - times_s[k - 2])
            vy = (best[-1].y_km - best[-2].y_km) / (times_s[k - 1] - times_s[k - 2])
        else:
            vx = vy = 0.0
        last = best[-1]
        best.append(
            Cell(
                last.x_km + vx * dt,
                last.y_km + vy * dt,
                last.max_dbz,
                last.area_km2,
                last.top_km,
                last.score,
            )
        )
    return best


def storm_motion(
    track_xy_km: list[tuple[float, float]], times_s: list[float]
) -> tuple[float, float]:
    """Mean storm motion (u east, v north) in m/s from the first to the last position."""
    dt = times_s[-1] - times_s[0]
    if dt <= 0:
        return 0.0, 0.0
    return (
        (track_xy_km[-1][0] - track_xy_km[0][0]) * 1000.0 / dt,
        (track_xy_km[-1][1] - track_xy_km[0][1]) * 1000.0 / dt,
    )


def box_around(
    points_km: list[tuple[float, float]], half_km: float
) -> tuple[float, float, float, float]:
    """(xmin, ymin, xmax, ymax) km: the points' bounding box grown by ``half_km`` on every side."""
    xs = [p[0] for p in points_km]
    ys = [p[1] for p in points_km]
    return min(xs) - half_km, min(ys) - half_km, max(xs) + half_km, max(ys) + half_km


# ---------------------------------------------------------------------------
# Building the bundle
# ---------------------------------------------------------------------------


def _open(path: Path):
    from engine.reader import RadarReader

    reader = RadarReader()
    reader.open_file(str(path))
    return reader


def _mosaic(step: Step, grid: dict[str, Any], keep_volume: bool = True):
    from engine import mosaic

    readers = []
    try:
        for radar, (path, _off) in sorted(step.files.items()):
            readers.append((radar, _open(path)))
        return mosaic.build_mosaic(
            readers,
            "DBZH",
            "composite",
            grid,
            height_m=3000.0,
            keep_volume=keep_volume,
            cache=None,
        )
    finally:
        for _, r in readers:
            with contextlib.suppress(Exception):
                r.close()


def build_bundle(
    case: Path,
    out: Path,
    n_steps: int | None = None,
    reference: str | None = "auto",
    detail_m: float = 500.0,
    crop_half_km: float = 60.0,
    z_max_m: float = 16000.0,
    coarse_m: float = 2000.0,
    log=print,
) -> dict[str, Any]:
    """Build the data bundle in ``out`` and return its manifest."""
    from engine import mosaic

    out.mkdir(parents=True, exist_ok=True)
    volumes = list_volumes(case)
    if reference in (None, "auto"):
        reference, steps = best_reference(volumes)
        steps = steps[:n_steps] if n_steps else steps
        log(f"reference radar for the time steps: {reference}")
    else:
        steps = choose_steps(volumes, reference, n_steps)
    t0 = steps[0].time
    times_s = [(s.time - t0).total_seconds() for s in steps]
    log(f"{len(steps)} time steps: " + ", ".join(s.time.strftime("%H:%M:%S") for s in steps))

    # 1. find and track the storm on coarse mosaics
    cells_per_step: list[list[Cell]] = []
    coarse_geo = None
    for i, step in enumerate(steps):
        log(f"[{i + 1}/{len(steps)}] coarse mosaic")
        rec = _mosaic(
            step,
            {
                "resolution_m": coarse_m,
                "z_max_m": z_max_m,
                "z_step_m": 1000.0,
                "max_range_km": 230.0,
            },
        )
        assert (
            rec.volume is not None and rec.volume_range is not None and rec.volume_z_m is not None
        )
        cells = find_cells(
            rec.volume, rec.volume_range, rec.volume_z_m / 1000.0, rec.x0, rec.y0, rec.cell_m
        )
        cells_per_step.append(cells)
        coarse_geo = rec.origin
        log(
            "    cells: "
            + ", ".join(
                f"{c.max_dbz:.0f} dBZ/{c.area_km2:.0f} km2/top {c.top_km:.1f}" for c in cells[:4]
            )
        )
    track = track_storm(cells_per_step, times_s)
    assert coarse_geo is not None
    proj0 = mosaic._aeqd(*coarse_geo)
    track_lonlat = [proj0(c.x_km * 1000.0, c.y_km * 1000.0, inverse=True) for c in track]
    log("storm track (lon, lat): " + ", ".join(f"({lo:.2f}, {la:.2f})" for lo, la in track_lonlat))

    # 2. the fine mosaics on a box around the whole track
    lon_c = float(np.mean([p[0] for p in track_lonlat]))
    lat_c = float(np.mean([p[1] for p in track_lonlat]))
    proj_c = mosaic._aeqd(lat_c, lon_c)
    track_c = [proj_c(lo, la) for lo, la in track_lonlat]  # metres in the fine frame
    box = box_around([(x / 1000.0, y / 1000.0) for x, y in track_c], crop_half_km + 12.0)
    west, south = proj_c(box[0] * 1000.0, box[1] * 1000.0, inverse=True)
    east, north = proj_c(box[2] * 1000.0, box[3] * 1000.0, inverse=True)
    extent = {"west": west, "south": south, "east": east, "north": north}
    grid = {
        "resolution_m": detail_m,
        "z_max_m": z_max_m,
        "z_step_m": 500.0,
        "max_range_km": 230.0,
        "extent": extent,
    }

    manifest_steps = []
    origin = None
    sites: list[dict[str, Any]] = []
    for i, step in enumerate(steps):
        log(f"[{i + 1}/{len(steps)}] detail mosaic at {detail_m:g} m")
        rec = _mosaic(step, grid)
        header, vol = mosaic.volume_message(rec.mosaic_id, _single(rec))
        stem = f"mosaic_{i}"
        (out / f"{stem}.json").write_text(json.dumps(header))
        (out / f"{stem}.bin").write_bytes(np.ascontiguousarray(vol).tobytes())
        origin = rec.origin
        sites = header["radars"]
        manifest_steps.append(
            {
                "index": i,
                "time_utc": step.time.isoformat(),
                "t_s": times_s[i],
                "files": {
                    r: {"name": p.name, "offset_s": off} for r, (p, off) in step.files.items()
                },
                "mosaic": {"header": f"{stem}.json", "bin": f"{stem}.bin"},
                "storm": {
                    "max_dbz": track[i].max_dbz,
                    "area_km2": track[i].area_km2,
                    "top_km": track[i].top_km,
                },
            }
        )
        log(
            f"    volume {vol.shape}, {vol.nbytes / 1e6:.1f} MB, {sum(1 for _ in header['radars'])} radars"
        )
    assert origin is not None
    # storm positions in the fine mosaic's frame (km, same frame as the volumes)
    proj_f = mosaic._aeqd(*origin)
    track_f = [proj_f(lo, la) for lo, la in track_lonlat]
    track_km = [(x / 1000.0, y / 1000.0) for x, y in track_f]
    u, v = storm_motion(track_km, times_s)
    manifest = {
        "version": 1,
        "title": "Storm in 3D",
        "case": case.name,
        "variable": "DBZH",
        "units": "dBZ",
        "origin": {"lat": origin[0], "lon": origin[1]},
        "crop_half_km": crop_half_km,
        "detail_m": detail_m,
        "steps": manifest_steps,
        "storm": {
            "track_km": track_km,
            "track_lonlat": track_lonlat,
            "motion_ms": [u, v],
            "speed_ms": math.hypot(u, v),
            "heading_deg": (math.degrees(math.atan2(u, v)) + 360.0) % 360.0,
        },
        "sites": sites,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    log(
        f"storm motion {math.hypot(u, v):.1f} m/s toward {manifest['storm']['heading_deg']:.0f} deg"
    )
    return manifest


def _single(rec):
    """A throwaway cache holding just ``rec`` (``volume_message`` reads a record from a cache)."""
    from engine import mosaic

    cache = mosaic.MosaicCache(max_items=1, max_bytes=10**12)
    cache.put(rec)
    return cache


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--case",
        required=True,
        type=Path,
        help="folder with one sub-folder of volumes per radar (read only)",
    )
    ap.add_argument(
        "--out",
        required=True,
        type=Path,
        help="bundle directory to write (not inside the case folder)",
    )
    ap.add_argument(
        "--steps",
        type=int,
        default=None,
        help="at most this many time steps (default: all that add information)",
    )
    ap.add_argument(
        "--reference",
        default="auto",
        help="radar whose volume times define the steps (default: the best one)",
    )
    ap.add_argument("--detail-m", type=float, default=500.0, help="fine mosaic resolution (m)")
    ap.add_argument(
        "--crop-km",
        type=float,
        default=60.0,
        help="half width of the shown box around the storm (km)",
    )
    a = ap.parse_args(argv)
    case = a.case.expanduser().resolve()
    out = a.out.expanduser().resolve()
    if case in out.parents or out == case:
        ap.error("--out must not be inside the radar case folder")
    build_bundle(case, out, a.steps, a.reference, a.detail_m, a.crop_km)
    print(f"bundle written to {out}")
    return 0


if __name__ == "__main__":
    # a clip is a one-off job: keep radarx's own threads from fighting the interpreter at exit
    sys.exit(main())

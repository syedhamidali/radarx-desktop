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

Optionally (on by default, ``--no-winds`` to skip) it also retrieves multi-Doppler winds for every
step (clipwinds.py) and reads observed lightning files given with ``--lightning`` (lightning_obs.py;
nothing is downloaded). Nothing is written next to the radar files.
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

from engine.animate_storm import (  # shared with `rxd animate`: finding and tracking the storm
    Cell,
    Eye,
    box_around,
    column_max_dbz,
    decode,
    find_cells,
    find_eye,
    linear_track,
    storm_motion,
    track_storm,
)

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
# Building the bundle
# ---------------------------------------------------------------------------


# Radars whose files the engine cannot read (for example the TDWR "V08" files): named once, skipped.
UNREADABLE: set[str] = set()


def _open(path: Path):
    from engine.reader import RadarReader

    reader = RadarReader()
    reader.open_file(str(path))
    return reader


def _open_valid(path: Path):
    """Open a volume and check that it is usable (complete sweeps, a sane site position)."""
    reader = _open(path)
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


def open_readable(files: dict[str, Path], opener=None) -> list[tuple[str, Any]]:
    """Open the radars' files; a radar whose file cannot be used is skipped (and named once)."""
    opener = opener or _open_valid
    readers = []
    for radar, path in sorted(files.items()):
        if radar in UNREADABLE:
            continue
        try:
            readers.append((radar, opener(path)))
        except ValueError as exc:
            UNREADABLE.add(radar)
            print(f"    skipping {radar}: {str(exc)[:110]}")
    return readers


def _mosaic(step: Step, grid: dict[str, Any], keep_volume: bool = True):
    from engine import mosaic

    readers: list = []
    try:
        readers = open_readable({r: p for r, (p, _off) in step.files.items()})
        if len(readers) < 2:
            raise ValueError("fewer than two readable radars in this time step")
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
    winds: bool = True,
    wind_dx_m: float = 2000.0,
    lightning_files: list[str] | None = None,
    track_mode: str = "cells",
    log=print,
) -> dict[str, Any]:
    """Build the data bundle in ``out`` and return its manifest.

    ``winds``: also retrieve multi-Doppler winds for every step (clipwinds.py) so the clip can carry
    the echo along the observed flow; a step without winds is translated only. ``lightning_files``:
    GLM netCDF or LMA .dat files read from disk (lightning_obs.py), never downloaded here.
    """
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
    comps: list[tuple[np.ndarray, float, float, float]] = []
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
        comps.append((column_max_dbz(rec.volume, rec.volume_range), rec.x0, rec.y0, rec.cell_m))
        coarse_geo = rec.origin
        log(
            "    cells: "
            + ", ".join(
                f"{c.max_dbz:.0f} dBZ/{c.area_km2:.0f} km2/top {c.top_km:.1f}" for c in cells[:4]
            )
        )
    track = track_storm(cells_per_step, times_s)
    eyes: list[Eye | None] = [
        find_eye(c, x0, y0, cm, (track[i].x_km, track[i].y_km))
        for i, (c, x0, y0, cm) in enumerate(comps)
    ]
    if any(eyes):
        log(
            "eye (echo-free hole): "
            + ", ".join(f"{e.radius_km:.0f} km" if e else "none" for e in eyes)
        )
    if track_mode == "eye":
        track = [
            Cell(e.x_km, e.y_km, c.max_dbz, c.area_km2, c.top_km, c.score) if e else c
            for c, e in zip(track, eyes, strict=True)
        ]
        log("storm track: the centres of the echo-free eye (the cell centre where there is none)")
    if track_mode == "linear":
        track = linear_track(track, times_s)
        log("storm track: least-squares straight line through the cell positions")
    assert coarse_geo is not None
    proj0 = mosaic._aeqd(*coarse_geo)
    track_lonlat = [proj0(c.x_km * 1000.0, c.y_km * 1000.0, inverse=True) for c in track]
    eye_lonlat = [
        proj0(e.x_km * 1000.0, e.y_km * 1000.0, inverse=True) if e else None for e in eyes
    ]
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
    motion0 = storm_motion([(x / 1000.0, y / 1000.0) for x, y in track_c], times_s)
    for i, step in enumerate(steps):
        log(f"[{i + 1}/{len(steps)}] detail mosaic at {detail_m:g} m")
        rec = _mosaic(step, grid)
        header, vol = mosaic.volume_message(rec.mosaic_id, _single(rec))
        stem = f"mosaic_{i}"
        (out / f"{stem}.json").write_text(json.dumps(header))
        (out / f"{stem}.bin").write_bytes(np.ascontiguousarray(vol).tobytes())
        origin = rec.origin
        sites = header["radars"]
        wind_files = None
        if winds:
            from clipwinds import retrieve_step_winds

            g = header["grid"]
            extent_km = (
                g["x0"] / 1000.0,
                g["y0"] / 1000.0,
                (g["x0"] + (g["nx"] - 1) * g["cell_m"]) / 1000.0,
                (g["y0"] + (g["ny"] - 1) * g["cell_m"]) / 1000.0,
            )
            got = retrieve_step_winds(
                {r: p for r, (p, _o) in step.files.items()},
                (origin[0], origin[1]),
                extent_km,
                motion0,
                _open_valid,
                dx_m=wind_dx_m,
                log=log,
            )
            if got is not None:
                (out / f"wind_{i}.json").write_text(json.dumps(got[0]))
                (out / f"wind_{i}.bin").write_bytes(got[1])
                wind_files = {"header": f"wind_{i}.json", "bin": f"wind_{i}.bin"}
        manifest_steps.append(
            {
                "index": i,
                "time_utc": step.time.isoformat(),
                "t_s": times_s[i],
                "files": {
                    r: {"name": p.name, "offset_s": off} for r, (p, off) in step.files.items()
                },
                "mosaic": {"header": f"{stem}.json", "bin": f"{stem}.bin"},
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
        log(
            f"    volume {vol.shape}, {vol.nbytes / 1e6:.1f} MB, {sum(1 for _ in header['radars'])} radars"
        )
    assert origin is not None
    # storm positions in the fine mosaic's frame (km, same frame as the volumes)
    proj_f = mosaic._aeqd(*origin)
    track_f = [proj_f(lo, la) for lo, la in track_lonlat]
    track_km = [(x / 1000.0, y / 1000.0) for x, y in track_f]
    for st in manifest_steps:
        ll = st.pop("eye_lonlat")
        if ll is not None:
            ex, ey = proj_f(ll[0], ll[1])
            st["eye"] = {"x_km": ex / 1000.0, "y_km": ey / 1000.0, "radius_km": st["eye_radius_km"]}
        else:
            st["eye"] = None
        st.pop("eye_radius_km")
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
    if lightning_files:
        from lightning_obs import read_flashes, to_bundle

        kind, flashes = read_flashes(lightning_files)
        recs = to_bundle(
            flashes,
            steps[0].time,
            steps[-1].time,
            lambda lon, lat: tuple(v / 1000.0 for v in proj_f(lon, lat)),
        )
        manifest["lightning"] = {
            "source": "observed",
            "kind": kind.upper(),
            "file": ", ".join(Path(f).name for f in lightning_files),
            "flashes": recs,
        }
        log(f"observed lightning ({kind.upper()}): {len(recs)} flashes in the clip window")
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
        "--no-winds",
        action="store_true",
        help="do not retrieve multi-Doppler winds (the clip then translates the echo only)",
    )
    ap.add_argument("--wind-dx-m", type=float, default=2000.0, help="wind grid spacing (m)")
    ap.add_argument(
        "--track",
        default="cells",
        choices=["cells", "linear", "eye"],
        help="storm track: the tracked cell positions (default), their least-squares straight line "
        "(steady motion), or the centres of the echo-free eye of a tropical cyclone",
    )
    ap.add_argument(
        "--lightning",
        nargs="+",
        default=None,
        metavar="FILE",
        help="observed lightning files read from disk: GLM L2 .nc or LMA .dat (never downloaded)",
    )
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
    build_bundle(
        case,
        out,
        a.steps,
        a.reference,
        a.detail_m,
        a.crop_km,
        winds=not a.no_winds,
        wind_dx_m=a.wind_dx_m,
        lightning_files=a.lightning,
        track_mode=a.track,
    )
    print(f"bundle written to {out}")
    return 0


if __name__ == "__main__":
    # a clip is a one-off job: keep radarx's own threads from fighting the interpreter at exit
    sys.exit(main())

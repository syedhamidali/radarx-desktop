#!/usr/bin/env python
"""Thin converter: the PERiLS bundle -> the storm-clip bundle of ``tools/storm_clip`` (``render_clip.py --bundle``).

    python/.venv/bin/python tools/perils_movie/to_clip_bundle.py \
        --bundle ~/Downloads/xradar-exports/perils_movie/bundle --out /tmp/perils_clip_bundle [--track tornado]

It writes ``mosaic_N.json`` / ``mosaic_N.bin`` (the merged reflectivity as uint8 codes, 0 = no echo, 1..255 ->
``vmin + (code - 1) * step``, exactly what ``engine.mosaic.volume_message`` produces), the storm track and the
radar sites, and a ``manifest.json`` in the layout of ``clipdata.build_bundle``. The observed lightning
becomes ``manifest["lightning"]`` (source observed, kind LMA: the flash initiation point and a strength from its
number of sources). Winds are not converted (the clip then translates the echo along the storm motion only).

``--track cells`` follows the strongest convective cell with the same functions as ``clipdata.py``;
``--track tornado`` follows the surveyed tornado position (straight line between the survey's begin and end
points; before and after its life the nearest end point), the right camera target for the EF2 tornado.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "python"))
sys.path.insert(0, str(HERE))

import pgrid
import tornadoes
from engine.animate_storm import Cell, find_cells, storm_motion, track_storm

VMIN, VMAX = -10.0, 70.0


def encode(dbz: np.ndarray) -> np.ndarray:
    step = (VMAX - VMIN) / 254.0
    code = np.zeros(dbz.shape, np.uint8)
    ok = np.isfinite(dbz)
    code[ok] = np.clip(np.round((dbz[ok] - VMIN) / step) + 1, 1, 255).astype(np.uint8)
    return code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--track", choices=["cells", "tornado"], default="tornado")
    a = ap.parse_args(argv)
    b = Path(a.bundle).expanduser()
    out = Path(a.out).expanduser()
    if b.resolve() in out.resolve().parents or b.resolve() == out.resolve():
        ap.error("--out must not be inside the bundle")
    out.mkdir(parents=True, exist_ok=True)
    man = json.loads((b / "manifest.json").read_text())
    g = man["grid"]
    lat0, lon0 = g["origin_lat"], g["origin_lon"]
    proj = pgrid.projection(lat0, lon0)
    t0 = datetime.fromisoformat(man["window"]["start"].replace("Z", "+00:00"))
    z_m = [float(z) * 1000.0 for z in g["z_km"]]
    nz, ny, nx = len(z_m), len(g["y_km"]), len(g["x_km"])
    sites = [
        {
            "name": r["name"],
            "file_id": r["name"],
            "latitude": r["lat"],
            "longitude": r["lon"],
            "altitude_m": r["alt_m"],
            "x_m": r["x_km"] * 1000.0,
            "y_m": r["y_km"] * 1000.0,
        }
        for r in man["radars"]
    ]
    header = {
        "variable": "DBZH",
        "units": "dBZ",
        "product": "composite",
        "grid": {
            "x0": g["x_km"][0] * 1000.0,
            "y0": g["y_km"][0] * 1000.0,
            "cell_m": 500.0,
            "nx": nx,
            "ny": ny,
            "nz": nz,
            "z": z_m,
            "origin_lat": lat0,
            "origin_lon": lon0,
        },
        "vmin": VMIN,
        "vmax": VMAX,
        "dtype": "uint8",
        "scale": {"offset": VMIN, "step": (VMAX - VMIN) / 254.0, "nodata": 0},
        "cmap": "NWSRef",
        "radars": sites,
    }
    steps = []
    cells_per_step = []
    times_s = []
    for s in man["steps"]:
        v = np.load(b / s["volume"])
        code = encode(v["dbz"])
        i = s["index"]
        h = {**header, "mosaic_id": f"perils_{i}", "analysis_time": s["time"]}
        (out / f"mosaic_{i}.json").write_text(json.dumps(h))
        (out / f"mosaic_{i}.bin").write_bytes(np.ascontiguousarray(code).tobytes())
        t_s = (datetime.fromisoformat(s["time"].replace("Z", "+00:00")) - t0).total_seconds()
        times_s.append(t_s)
        cells_per_step.append(
            find_cells(
                code, (VMIN, VMAX), np.array(z_m) / 1000.0, h["grid"]["x0"], h["grid"]["y0"], 500.0
            )
        )
        steps.append(
            {
                "index": i,
                "time_utc": s["time"],
                "t_s": t_s,
                "files": {
                    r: {"name": s["files"][r], "offset_s": o} for r, o in s["offsets_s"].items()
                },
                "mosaic": {"header": f"mosaic_{i}.json", "bin": f"mosaic_{i}.bin"},
                "wind": None,
                "eye": None,
                "storm": {"max_dbz": 0.0, "area_km2": 0.0, "top_km": 0.0},
            }
        )
    if a.track == "tornado":
        tor = [
            t
            for t in json.loads((b / man["tornadoes"]).read_text())
            if t["overlaps_window"] and t.get("in_domain")
        ]
        if not tor:
            ap.error("no tornado in the window: use --track cells")
        path = tor[0]["path"]
        track = []
        for t in times_s:
            p = tornadoes.position_at(tor[0], t) or (
                (path[0][1], path[0][2]) if t < path[0][0] else (path[-1][1], path[-1][2])
            )
            x, y = pgrid.xy_km(proj, p[0], p[1])
            track.append(Cell(float(x), float(y), 0.0, 0.0, 0.0, 0.0))
    else:
        track = track_storm(cells_per_step, times_s)
    for st, c in zip(steps, track, strict=True):
        st["storm"] = {"max_dbz": c.max_dbz, "area_km2": c.area_km2, "top_km": c.top_km}
    track_km = [(c.x_km, c.y_km) for c in track]
    track_lonlat = []
    for x, y in track_km:
        lon, lat = proj(x * 1000.0, y * 1000.0, inverse=True)
        track_lonlat.append((lon, lat))
    u, v = storm_motion(track_km, times_s)
    manifest = {
        "version": 1,
        "title": "PERiLS IOP2 QLCS and the EF2 tornado, 31 March 2022 01:04-01:20 UTC",
        "case": "perils_iop2",
        "variable": "DBZH",
        "units": "dBZ",
        "origin": {"lat": lat0, "lon": lon0},
        "crop_half_km": 60.0,
        "detail_m": 500.0,
        "steps": steps,
        "storm": {
            "track_km": track_km,
            "track_lonlat": track_lonlat,
            "motion_ms": [u, v],
            "speed_ms": math.hypot(u, v),
            "heading_deg": (math.degrees(math.atan2(u, v)) + 360.0) % 360.0,
        },
        "sites": sites,
    }
    if man.get("lma"):
        L = json.loads((b / man["lma"]).read_text())
        recs = []
        for f in L["flashes"]:
            s0 = f["sources"][0]
            x, y = pgrid.xy_km(proj, s0[1], s0[2])
            recs.append(
                {
                    "t_s": f["t_start_s"],
                    "x": float(x),
                    "y": float(y),
                    "z": float(s0[3]) / 1000.0,
                    "energy": float(
                        min(1.0, 0.2 + 0.8 * math.log1p(f["n_sources"]) / math.log1p(300))
                    ),
                }
            )
        manifest["lightning"] = {
            "source": "observed",
            "kind": "LMA",
            "file": "lma/flashes.json",
            "flashes": recs,
        }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(
        f"clip bundle written to {out}: {len(steps)} steps, storm track '{a.track}', {len(manifest.get('lightning', {}).get('flashes', []))} flashes"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

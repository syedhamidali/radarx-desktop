"""A SYNTHETIC storm for tests and demos (never the real PERiLS data): a 10 minute window in a
2 minute clip (speedup 5), 40 flashes at various ranges, three stations with a rain core and a
gust-front wind step, one station hail event and a tornado track passing the camera.

``synthetic_raw()`` returns the raw JSON-style dicts (SPEC v1); ``synthetic_scenario()`` parses them.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .bundle import Bundle, parse_flashes, parse_stations, parse_tornadoes
from .geo import EARTH_RADIUS_M
from .timeline import CameraPath, Timeline

LAT0, LON0 = 33.50, -88.50
WINDOW_START = "2022-03-30T20:00:00Z"
DATA_START = "2022-03-30T20:00:00Z"
DATA_END = "2022-03-30T20:10:00Z"


def offset_ll(lat, lon, east_m, north_m):
    dlat = np.degrees(north_m / EARTH_RADIUS_M)
    dlon = np.degrees(east_m / (EARTH_RADIUS_M * np.cos(np.radians(lat))))
    return float(lat + dlat), float(lon + dlon)


def synthetic_raw(seed: int = 7, with_tornado: bool = True, with_hail: bool = True) -> dict:
    rng = np.random.default_rng(seed)
    flashes = []
    for i in range(40):
        t0 = float(rng.uniform(20, 560)) if i > 5 else float(rng.uniform(280, 320))  # a burst
        rng_m = float(rng.choice([1500, 3000, 6000, 12000, 25000, 40000], p=[0.07, 0.15, 0.25, 0.25, 0.2, 0.08]))
        brg = float(rng.uniform(0, 2 * np.pi))
        cla, clo = offset_ll(LAT0, LON0, rng_m * np.sin(brg), rng_m * np.cos(brg))
        n = int(rng.integers(20, 600))
        dur = float(rng.uniform(0.2, 1.5))
        ts = np.sort(rng.uniform(0, dur, n))
        # a channel that wanders for a few km
        steps = rng.normal(0, 250, (n, 2)).cumsum(axis=0)
        alts = 6000 + rng.normal(0, 1500, n)
        srcs = []
        for k in range(n):
            la, lo = offset_ll(cla, clo, steps[k, 0], steps[k, 1])
            srcs.append([float(t0 + ts[k]), la, lo, float(abs(alts[k])), float(rng.normal(8, 6))])
        flashes.append({
            "id": f"f{i:03d}", "t_start_s": t0, "t_end_s": t0 + dur, "centroid": [cla, clo, 6000.0],
            "n_sources": n, "sources": srcs,
        })
    t = np.arange(0, 601, 30.0)
    stations = []
    for j, (de, dn) in enumerate([(300.0, 200.0), (4000.0, -2500.0), (-12000.0, 9000.0)]):
        la, lo = offset_ll(LAT0, LON0, de, dn)
        core = np.exp(-0.5 * ((t - (300 + 40 * j)) / 70.0) ** 2)
        speed = 3.0 + 1.5 * np.sin(t / 50.0) + 17.0 * (t > 300 + 20 * j) * (1 - np.exp(-(t - 300 - 20 * j) / 40.0)).clip(0)
        st = {
            "id": f"S{j}", "name": f"Synthetic station {j}", "type": "pips", "lat": la, "lon": lo, "alt_m": 90.0,
            "series": {
                "t_s": t.tolist(),
                "rain_rate_mmh": (2 + 70 * core).tolist(),
                "dm_mm": (1.0 + 2.5 * core).tolist(),
                "dbz": (28 + 30 * core).tolist(),
                "wind_speed_ms": speed.tolist(),
                "wind_dir_deg": (170 + 130 * (t > 300)).astype(float).tolist(),
                "temperature_c": (22 - 6 * (t > 300)).astype(float).tolist(),
                "pressure_hpa": (1005 + 2 * (t > 300)).astype(float).tolist(),
            },
            "events": [],
        }
        if with_hail and j == 0:
            st["events"].append({"type": "hail_candidate", "t_s": 290.0, "t_end_s": 330.0, "dm_mm": 12.0,
                                 "dmax_mm": 14.0, "counts": 40, "dbz": 61.0, "rain_rate_mmh": 90.0})
        stations.append(st)
    tors = []
    if with_tornado:
        a = offset_ll(LAT0, LON0, -3000.0, -2000.0)
        b = offset_ll(LAT0, LON0, 6000.0, 4000.0)
        tors.append({"id": "T1", "name": "Synthetic tornado", "ef": "EF2",
                     "path": [{"t_s": 250.0, "lat": a[0], "lon": a[1]}, {"t_s": 420.0, "lat": b[0], "lon": b[1]}]})
    return {
        "manifest": {"window": {"start": WINDOW_START, "end": "2022-03-30T20:10:00Z"}},
        "flashes": {"t0": WINDOW_START, "flashes": flashes},
        "stations": stations,
        "tornadoes": tors,
        "timeline": {"data_start": DATA_START, "data_end": DATA_END, "clip_duration_s": 120.0, "fps": 30, "speedup": 5.0},
        "camera": [
            {"t_clip_s": float(tc), "lat": offset_ll(LAT0, LON0, 0.0, 25.0 * tc)[0], "lon": LON0,
             "alt_m": 1500.0, "heading_deg": float(tc * 0.75), "pitch_deg": -10.0}
            for tc in np.linspace(0, 120, 13)
        ],
    }


def synthetic_dsd_arrays(t=None):
    """Exponential drop spectra whose Dm follows the rain core (1.0 to 3.0 mm), 30 s steps."""
    from .dsd import fall_speed_ms

    t = np.arange(0, 601, 30.0) if t is None else t
    d = np.linspace(0.25, 9.0, 36)
    core = np.exp(-0.5 * ((t - 300.0) / 70.0) ** 2)
    dm = 1.0 + 2.0 * core
    n = np.exp(-np.outer(3.67 / dm, d)) * (8000.0 * (0.05 + core))[:, None]
    return t, d, n, fall_speed_ms(d)


def synthetic_scenario(**kw):
    raw = synthetic_raw(**kw)
    b = Bundle(
        window_start=WINDOW_START, window_end=None,
        flashes=parse_flashes(raw["flashes"], WINDOW_START),
        stations=parse_stations(raw["stations"]),
        tornadoes=parse_tornadoes(raw["tornadoes"], WINDOW_START),
        manifest=raw["manifest"],
    )
    from .dsd import Dsd

    t, d, n, vt = synthetic_dsd_arrays()
    b.stations[0].dsd = Dsd(t, d, np.full_like(d, d[1] - d[0]), n, vt, False, "synthetic")
    tl = Timeline.from_dict(raw["timeline"], WINDOW_START)
    return b, tl, CameraPath(raw["camera"])


def write_synthetic_bundle(out_dir) -> Path:
    """Write the synthetic scenario as a SPEC v1 bundle folder + timeline.json + camera_path.json."""
    raw = synthetic_raw()
    d = Path(out_dir)
    (d / "lma").mkdir(parents=True, exist_ok=True)
    (d / "stations").mkdir(exist_ok=True)
    (d / "tornadoes").mkdir(exist_ok=True)
    t, dd, n, _ = synthetic_dsd_arrays()
    np.savez(d / "stations" / "S0_dsd.npz", t_s=t, diameter_mm=dd, nd=n)
    (d / "manifest.json").write_text(json.dumps(raw["manifest"]))
    (d / "lma" / "flashes.json").write_text(json.dumps(raw["flashes"]))
    (d / "stations" / "stations.json").write_text(json.dumps(raw["stations"]))
    (d / "tornadoes" / "tornadoes.json").write_text(json.dumps(raw["tornadoes"]))
    (d.parent / "timeline.json").write_text(json.dumps(raw["timeline"]))
    (d.parent / "camera_path.json").write_text(json.dumps(raw["camera"]))
    return d

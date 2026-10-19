"""A SYNTHETIC bundle in the layout of SPEC v1 (docs/PERILS_MOVIE.md), for developing and testing the
renderer before (and without) the real PERiLS data.

Everything in it is made up: a smooth squall line with a bow, a trailing stratiform region, flashes
that follow the line, rain-rate series sampled from the made-up field, two soundings, a tornado path
and rotation tracks. It exists to exercise the code paths; no frame made from it may be published as
data. Run::

    python tools/perils_movie/render/synth_bundle.py --out /tmp/synth_bundle [--steps 7] [--step-s 120]
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

LAT0, LON0 = 33.0, -87.5
KM_PER_DEG = 111.19
MOTION_BEARING = 75.0  # degrees, toward which the line moves
SPEED_MS = 22.0


def ll_to_xy(lat: float, lon: float) -> tuple[float, float]:
    return (lon - LON0) * KM_PER_DEG * math.cos(math.radians(LAT0)), (lat - LAT0) * KM_PER_DEG


def xy_to_ll(x: float, y: float) -> tuple[float, float]:
    return LAT0 + y / KM_PER_DEG, LON0 + x / (KM_PER_DEG * math.cos(math.radians(LAT0)))


def _frames():
    b = math.radians(MOTION_BEARING)
    m = np.array([math.sin(b), math.cos(b)])  # unit vector of the motion (x east, y north)
    a = np.array([-m[1], m[0]])  # along the line
    return m, a


def smooth_noise(shape, seed, scale):
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(seed)
    f = gaussian_filter(rng.standard_normal(shape), scale, mode="wrap")
    return f / (f.std() + 1e-9)


def make_field(xs, ys, zs, t_s, seed=3):
    """dBZ (nz, ny, nx), NaN where there is no echo. Noise is anchored to the moving line."""
    m, a = _frames()
    X, Y = np.meshgrid(xs, ys)
    shift = SPEED_MS * t_s / 1000.0
    # coordinates riding with the line, so the texture travels with it
    along = X * a[0] + Y * a[1]
    across = X * m[0] + Y * m[1] - shift + 6.0  # km, front at 0
    n1 = smooth_noise((361, 361), seed, 4.0)
    n2 = smooth_noise((361, 361), seed + 1, 1.5)

    def pick(nf):  # sample the noise field in line coordinates
        i = np.clip(((along + 90) / 0.5).astype(int), 0, 360)
        j = np.clip(((across + 60) / 0.5).astype(int) % 361, 0, 360)
        return nf[j, i]

    noise, noise2 = pick(n1), pick(n2)
    bow = 14.0 * np.exp(-(((along + 5) / 38.0) ** 2))
    w = across - bow
    conv = 52 + 6 * noise + 3 * noise2 - 0.0009 * along**2
    conv_mask = np.exp(-(((w - 0.5) / 3.2) ** 2))
    strat = 30 + 4 * noise
    strat_mask = np.clip(1 - np.clip(-w - 4, 0, None) / 38.0, 0, 1) * (w < 2)
    strat_mask *= np.clip(1 - np.abs(along) / 95.0, 0, 1) ** 0.5
    ahead = np.exp(-np.clip(w, 0, None) / 4.0) * (w > 0)
    base = np.maximum(conv * conv_mask + 22 * ahead * conv_mask, strat * strat_mask * 0.9)
    base = np.where(base < 8, np.nan, base)
    nz = len(zs)
    out = np.full((nz, len(ys), len(xs)), np.nan, np.float32)
    top = 7.0 + 5.0 * conv_mask + noise2 * 0.4
    for k, z in enumerate(zs):
        prof = np.where(z < 4.0, 0.0, -(z - 4.0) * 3.2)  # dB drop above 4 km
        bb = 4.0 * np.exp(-(((z - 3.4) / 0.4) ** 2)) * strat_mask * (1 - conv_mask)
        d = base + prof + bb
        d = np.where(z > top, np.nan, d)
        out[k] = np.where(d < 3, np.nan, d)
    return out


def write(out: Path, n_steps: int, step_s: float) -> None:
    out = Path(out)
    for d in ("volumes", "context", "lma", "stations", "soundings", "tornadoes"):
        (out / d).mkdir(parents=True, exist_ok=True)
    xs = np.arange(-90, 90.01, 0.5)
    ys = np.arange(-90, 90.01, 0.5)
    zs = np.arange(0, 10.01, 0.25)
    t0 = datetime(2022, 3, 30, 22, 0, 0, tzinfo=timezone.utc)
    radars = [
        {"name": "KBMX", "lat": 33.172, "lon": -86.770, "alt_m": 197, "type": "WSR-88D"},
        {"name": "KEOX", "lat": 31.46, "lon": -85.459, "alt_m": 132, "type": "WSR-88D"},
        {"name": "KGWX", "lat": 33.897, "lon": -88.329, "alt_m": 165, "type": "WSR-88D"},
        {"name": "COW1", "lat": 33.40, "lon": -87.0, "alt_m": 120, "type": "mobile X-band"},
    ]
    steps = []
    for k in range(n_steps):
        t = k * step_s
        dbz = make_field(xs, ys, zs, t)
        hid = np.zeros(dbz.shape, np.uint8)
        fin = np.isfinite(dbz)
        hid[fin & (dbz > 5)] = 2
        hid[(dbz > 45) & (zs[:, None, None] > 4.5)] = 8
        hid[(dbz > 56) & (zs[:, None, None] > 3.0)] = 9
        cm = np.nanmax(np.where(fin, dbz, -9), axis=0)
        cs = np.zeros(dbz.shape[1:], np.uint8)
        cs[cm > 20] = 1
        cs[cm > 42] = 2
        et40 = np.zeros(dbz.shape[1:], np.float32)
        for kk, z in enumerate(zs):
            et40 = np.where(np.nan_to_num(dbz[kk], nan=-9) >= 40, z, et40)
        np.savez_compressed(
            out / "volumes" / f"step_{k:04d}.npz",
            dbz=dbz.astype(np.float32),
            nradars=np.where(fin, 3, 0).astype(np.uint8),
            hid=hid,
            conv_strat=cs,
            echotop40_km=et40,
            time_iso=np.array((t0 + timedelta(seconds=t)).isoformat()),
        )
        # MRMS composite on a 1 km grid, a wider box (+-250 km): the same made-up line
        mx = np.arange(-250, 250.01, 1.0)
        mf = make_field(mx, mx, np.array([1.0, 4.0]), t)
        mcomp = np.nanmax(np.where(np.isfinite(mf), mf, -30), axis=0)
        mcomp = np.where(mcomp < 5, np.nan, mcomp).astype(np.float32)
        np.savez_compressed(out / "context" / f"mrms_{k:04d}.npz", refl=mcomp, x_km=mx, y_km=mx)
        steps.append(
            {
                "index": k,
                "time": (t0 + timedelta(seconds=t)).isoformat().replace("+00:00", "Z"),
                "radars": ["KBMX", "KGWX", "KEOX"],
                "offsets_s": {"KBMX": -40 + (17 * k) % 50, "KGWX": 25, "KEOX": -90},
                "volume": f"volumes/step_{k:04d}.npz",
                "mrms": f"context/mrms_{k:04d}.npz",
            }
        )
    dur = (n_steps - 1) * step_s
    # lightning: synthetic flashes along the line, random-walk "sources"
    rng = np.random.default_rng(5)
    m, a = _frames()
    flashes = []
    for i in range(int(dur * 0.35)):
        ts = float(rng.uniform(0, dur - 3))
        al = float(rng.normal(0, 28))
        lead = SPEED_MS * ts / 1000.0 - 6.0 + rng.normal(0, 3)
        x = lead * m[0] + al * a[0]
        y = lead * m[1] + al * a[1]
        n = int(rng.integers(25, 140))
        px, py, pz = x, y, 7000.0 + rng.normal(0, 800)
        src = []
        for j in range(n):
            px += rng.normal(0, 0.6) + 0.15
            py += rng.normal(0, 0.6)
            pz += rng.normal(0, 350)
            la, lo = xy_to_ll(px, py)
            src.append([round(ts + j * 0.012 * (1 + rng.random()), 4), la, lo, float(np.clip(pz, 1500, 12000)), float(rng.uniform(-10, 25))])
        c = np.mean([[s[1], s[2], s[3]] for s in src], axis=0)
        flashes.append({"id": i, "t_start_s": src[0][0], "t_end_s": src[-1][0], "centroid": [float(c[0]), float(c[1]), float(c[2])], "n_sources": n, "sources": src})
    flashes.sort(key=lambda f: f["t_start_s"])
    (out / "lma" / "flashes.json").write_text(json.dumps({"t0": steps[0]["time"], "flashes": flashes}))
    # stations: made-up series from the made-up field at the station
    stations = []
    sites = [("S1", 10.0, -14.0), ("S2", 24.0, 6.0), ("S3", -8.0, 22.0), ("S4", 36.0, -30.0), ("S5", 5.0, 40.0)]
    ts = np.arange(0, dur + 1, 10.0)
    for sid, sx, sy in sites:
        la, lo = xy_to_ll(sx, sy)
        z = np.array([float(np.nan_to_num(make_field(np.array([sx]), np.array([sy]), np.array([0.5]), t)[0, 0, 0], nan=0)) for t in ts])
        rr = np.where(z > 15, (10 ** (z / 10.0) / 200.0) ** (1 / 1.6), 0.0)
        stations.append(
            {
                "id": sid, "name": f"Disdrometer {sid}", "type": "disdrometer", "lat": la, "lon": lo, "alt_m": 120.0,
                "series": {
                    "t_s": ts.tolist(), "rain_rate_mmh": rr.round(2).tolist(), "dbz": z.round(1).tolist(),
                    "dm_mm": np.where(rr > 0, 1.0 + 0.45 * np.log1p(rr), np.nan).round(2).tolist(),
                    "wind_speed_ms": (3 + 0.3 * rr.clip(0, 40)).round(1).tolist(), "wind_dir_deg": (200 + 0 * ts).tolist(),
                    "temperature_c": (22 - 0.05 * rr.clip(0, 40)).round(1).tolist(), "pressure_hpa": (1005 + 0 * ts).tolist(),
                },
            }
        )
    (out / "stations" / "stations.json").write_text(json.dumps(stations).replace("NaN", "null"))
    # soundings: a made-up unstable profile
    snd = []
    for si, (site, la, lo, off) in enumerate([("Sounding A", 33.2, -87.6, -1800), ("Sounding B", 32.9, -86.9, 400)]):
        p = np.array([1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150], float)
        zz = np.array([120, 780, 1500, 3100, 4300, 5800, 7500, 9700, 11000, 12500, 14100], float)
        tt = np.array([24, 20, 17, 8, 1, -9, -21, -38, -47, -55, -62], float)
        td = np.array([19, 16, 13, 2, -6, -18, -30, -50, -58, -65, -72], float)
        sp = np.array([8, 14, 18, 22, 26, 30, 36, 46, 50, 52, 40], float)
        di = np.array([170, 190, 205, 225, 235, 245, 250, 255, 255, 255, 255], float)
        u = -sp * np.sin(np.radians(di))
        v = -sp * np.cos(np.radians(di))
        rows = "\n".join(f"{p_:.0f},{z_:.0f},{t_:.1f},{d_:.1f},{u_:.1f},{v_:.1f}" for p_, z_, t_, d_, u_, v_ in zip(p, zz, tt, td, u, v))
        (out / "soundings" / f"sounding_{si}.csv").write_text("pressure_hpa,height_m,temperature_c,dewpoint_c,u_ms,v_ms\n" + rows)
        drift = [[float(tq), la + 0.00004 * tq, lo + 0.00009 * tq, float(min(14000.0, 5.0 * (tq + 1800)))] for tq in np.arange(-600, 721, 120)]
        snd.append(
            {
                "id": f"snd{si}", "site": site, "launch": (t0 + timedelta(seconds=off)).isoformat().replace("+00:00", "Z"), "lat": la, "lon": lo,
                "profile_csv": f"soundings/sounding_{si}.csv", "drift": drift, "diagnostics": {"cape_jkg": 2100 + 300 * si, "shear06_ms": 24},
            }
        )
    (out / "soundings" / "index.json").write_text(json.dumps(snd))
    (out / "context" / "environment.json").write_text(
        json.dumps({"source": "SYNTHETIC test values", "class": "analysis", "valid": steps[0]["time"], "cape_jkg": 2200, "cin_jkg": -35, "shear06_ms": 24.0, "srh01_m2s2": 210, "freezing_level_m": 3900, "lcl_m": 700})
    )
    # tornado path: along the leading line from 40% to 80% of the window
    pts = []
    for tt_ in np.arange(0.4 * dur, 0.8 * dur + 1, 20.0):
        lead = SPEED_MS * tt_ / 1000.0 - 5.5
        la, lo = xy_to_ll(lead * m[0] + 6 * a[0], lead * m[1] + 6 * a[1])
        pts.append({"t_s": float(tt_), "lat": la, "lon": lo})
    (out / "tornadoes" / "tornadoes.json").write_text(
        json.dumps([{"id": "T1", "rating": "EF2", "width_m": 250.0, "start": pts[0], "end": pts[-1], "path": pts, "source": "SYNTHETIC"}])
    )
    (out / "context" / "rotation_tracks.json").write_text(
        json.dumps([{"id": "R1", "radar": "COW1", "resolution_m": 180.0, "points": [{**p_, "vrot_ms": 28 + 6 * math.sin(i / 3), "tds": 0.4 <= i / len(pts) <= 0.9} for i, p_ in enumerate(pts)]}])
    )
    manifest = {
        "title": "SYNTHETIC test bundle", "case": "synthetic",
        "window": {"start": steps[0]["time"], "end": steps[-1]["time"], "duration_s": dur},
        "grid": {"origin_lat": LAT0, "origin_lon": LON0, "x_km": xs.tolist(), "y_km": ys.tolist(), "z_km": zs.tolist()},
        "steps": steps, "radars": radars, "lma": "lma/flashes.json", "stations": "stations/stations.json",
        "soundings": "soundings/index.json", "environment": "context/environment.json",
        "tornadoes": "tornadoes/tornadoes.json", "rotation_tracks": "context/rotation_tracks.json",
        "sources": [{"name": "SYNTHETIC", "class": "observed", "instrument": "none", "credit": "made up", "licence_note": "n/a", "what": "test data"}],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"synthetic bundle: {n_steps} steps of {step_s:g} s, {len(flashes)} flashes, {len(stations)} stations -> {out}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--steps", type=int, default=7)
    ap.add_argument("--step-s", type=float, default=120.0)
    a = ap.parse_args(argv)
    write(Path(a.out), a.steps, a.step_s)


if __name__ == "__main__":
    main()

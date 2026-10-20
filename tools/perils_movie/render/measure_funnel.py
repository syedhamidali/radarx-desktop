"""Measure the rotation of the SIMULATED funnel in the frames of the film and compare it with the model's own wind.

The tornado simulation (docs/TORNADO_SIM.md) textures the funnel with noise carried by its swirl, so the streaks of the funnel
wall slide sideways across it. At a height z the wall is at the radius r_w(z) (where the pressure deficit of the vortex
saturates the air), and the part of the wall nearest the camera moves across the picture at the model's tangential wind
v(r_w, z), that is omega * r_w. In each decoded frame this script

  1. projects the vortex axis (the survey track of ``tornado_seed.json`` at the data time of the frame, local metres) into the
     picture with the camera the simulation drew (the camera dump of render_movie.py ``--cam-dump``),
  2. cuts a band at the height z, centred on the axis in each of two frames ``dt`` apart (so that the movement of the tornado and
     of the camera drops out), takes the horizontal texture profile of the central part of the funnel,
  3. finds the sideways shift between the two bands (cross-correlation, sub-pixel), turns it into m/s with the scale of the
     picture at the depth of the axis, and compares it with the model's v(r_w, z) (``render_report.json``: extras.sim.numbers.wall).

The ratio measured / modelled, and the period of one turn of the wall they imply, are what the film is held to (within 10 %).

    python measure_funnel.py --frames <dir> --cams cams.json --report render_report.json --seed tornado_seed.json \\
        --timeline timeline.json --from-frame 1490 --to-frame 1680
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

M_PER_DEG_LAT = 110574.0
M_PER_DEG_LON_EQ = 111320.0


def local_axis(seed: dict, td: float):
    """Local metres (east, north of the first track point) of the survey track at data time ``td``."""
    tr = seed["track"]
    t = [p["t"] for p in tr]
    lat = float(np.interp(td, t, [p["lat"] for p in tr]))
    lon = float(np.interp(td, t, [p["lon"] for p in tr]))
    lat0, lon0 = tr[0]["lat"], tr[0]["lon"]
    return (lon - lon0) * M_PER_DEG_LON_EQ * math.cos(math.radians(lat0)), (
        lat - lat0
    ) * M_PER_DEG_LAT


def camera_basis(pos, target):
    fwd = np.asarray(target, float) - np.asarray(pos, float)
    fwd /= np.linalg.norm(fwd)
    right = np.cross(fwd, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    up = np.cross(right, fwd)
    return fwd, right, up


def project(p, pos, fwd, right, up, tan_y, w, h):
    """Pixel (x, y), depth (m) and pixels per metre at that depth of the point ``p`` (local metres)."""
    v = np.asarray(p, float) - np.asarray(pos, float)
    d = float(v @ fwd)
    tan_x = tan_y * w / h
    x = w / 2 + (float(v @ right) / d / tan_x) * (w / 2)
    y = h / 2 - (float(v @ up) / d / tan_y) * (h / 2)
    return x, y, d, (h / 2) / (d * tan_y)


def estimate_shift(a: np.ndarray, b: np.ndarray, with_peak: bool = False):
    """Shift (pixels, + to the right) of profile ``b`` against ``a``: windowed cross-correlation with a parabolic peak.

    With ``with_peak`` also the height of the peak as a correlation coefficient (1 = identical profiles), the confidence."""
    a = np.asarray(a, float) - np.mean(a)
    b = np.asarray(b, float) - np.mean(b)
    n = len(a)
    win = np.hanning(n)
    a, b = a * win, b * win
    f = np.fft.rfft(a, 2 * n)
    g = np.fft.rfft(b, 2 * n)
    cc = np.fft.irfft(np.conj(f) * g, 2 * n)
    cc = np.concatenate([cc[-n + 1 :], cc[:n]])  # lags -(n-1) .. n-1
    k = int(np.argmax(cc))
    peak = float(cc[k] / max(np.sqrt(np.sum(a * a) * np.sum(b * b)), 1e-12))
    if 0 < k < len(cc) - 1:
        y0, y1, y2 = cc[k - 1], cc[k], cc[k + 1]
        den = y0 - 2 * y1 + y2
        frac = 0.5 * (y0 - y2) / den if den != 0 else 0.0
    else:
        frac = 0.0
    shift = k - (n - 1) + frac
    return (shift, peak) if with_peak else shift


def band_profile(lum: np.ndarray, x_c: float, y_c: float, half_w: int, half_h: int):
    """Horizontal texture profile of a band centred on (x_c, y_c): rows averaged, large scales removed; None off the picture."""
    h, w = lum.shape
    x0, x1 = round(x_c - half_w), round(x_c + half_w)
    y0, y1 = round(y_c - half_h), round(y_c + half_h)
    if x0 < 8 or x1 > w - 8 or y0 < 0 or y1 > h:
        return None
    prof = lum[y0:y1, x0:x1].mean(axis=0)
    k = max(5, (half_w // 2) | 1)
    box = np.convolve(np.pad(prof, k // 2, mode="edge"), np.ones(k) / k, mode="valid")
    return prof - box


def measure(
    frames_dir: Path,
    cams: dict,
    seed: dict,
    wall: list[dict],
    td_of_frame,
    frames: range,
    dt_frames: int = 3,
    heights=(40.0, 60.0, 80.0, 100.0),
    log=print,
    width_frac: float = 0.7,
    band_rows: int = 30,
    min_peak: float = 0.6,
) -> dict:
    from PIL import Image

    def load(i):
        for ext in ("png", "jpg"):
            f = frames_dir / f"frame_{i:05d}.{ext}"
            if f.exists():
                return np.asarray(Image.open(f).convert("L"), float)
        return None

    by_z = {w["z"]: w for w in wall if w["r"] > 0}
    rows = []
    for i in frames:
        j = i + dt_frames
        ci, cj = cams.get(str(i)), cams.get(str(j))
        if not ci or not cj or "sim" not in ci or "sim" not in cj:
            continue
        a, b = load(i), load(j)
        if a is None or b is None:
            continue
        h_px, w_px = a.shape
        t_i, t_j = td_of_frame(i), td_of_frame(j)
        for z in heights:
            wz = min(by_z.values(), key=lambda q: abs(q["z"] - z)) if by_z else None
            if wz is None:
                continue
            res = []
            for ci_, _img, t in ((ci, a, t_i), (cj, b, t_j)):
                s = ci_["sim"]
                fwd, right, up = camera_basis(s["pos"], s["target"])
                tan_y = math.tan(math.radians(s["fovDeg"]) / 2)
                ax, ay = local_axis(seed, t)
                x, y, d, ppm = project((ax, ay, z), s["pos"], fwd, right, up, tan_y, w_px, h_px)
                res.append((x, y, d, ppm))
            (x1, y1, d1, p1), (x2, y2, _d2, _p2) = res
            half_w = int(max(14, width_frac * wz["r"] * p1))
            if half_w > 220 or d1 < 120:
                continue
            pa = band_profile(a, x1, y1, half_w, band_rows)
            pb = band_profile(b, x2, y2, half_w, band_rows)
            if pa is None or pb is None or np.std(pa) < 0.6 or np.std(pb) < 0.6:
                continue
            shift, peak = estimate_shift(pa, pb, with_peak=True)
            if peak < min_peak:
                continue
            dt = t_j - t_i
            v_meas = (
                shift / p1 / dt
            )  # m/s, + to the right of the picture (the near wall of a counter-clockwise vortex)
            rows.append(
                {
                    "frame": i,
                    "z": z,
                    "depth_m": d1,
                    "px_per_m": p1,
                    "shift_px": shift,
                    "peak": peak,
                    "v_meas_ms": v_meas,
                    "v_model_ms": wz["vt"],
                    "ratio": v_meas / wz["vt"] if wz["vt"] else None,
                    "r_wall_m": wz["r"],
                }
            )
    if not rows:
        raise SystemExit("no usable band: nothing to fit")
    ratios = np.array([r["ratio"] for r in rows])
    out = {
        "bands": len(rows),
        "median_ratio": float(np.median(ratios)),
        "mean_ratio": float(np.mean(ratios)),
        "p25_p75": [float(np.percentile(ratios, 25)), float(np.percentile(ratios, 75))],
        "share_within_10pct": float(np.mean(np.abs(ratios - 1.0) <= 0.10)),
        "median_period_ratio": float(np.median(1.0 / np.maximum(ratios, 1e-6))),
        "model_wall": [w for w in wall if w["r"] > 0],
        "by_height": {
            str(int(z)): float(
                np.median([r["ratio"] for r in rows if r["z"] == z] or [float("nan")])
            )
            for z in heights
        },
    }
    log(json.dumps(out, indent=1))
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--frames", required=True)
    ap.add_argument("--cams", required=True)
    ap.add_argument("--report", required=True, help="render_report.json (extras.sim.numbers.wall)")
    ap.add_argument("--seed", required=True, help="tornado_seed.json (the survey track)")
    ap.add_argument("--timeline", required=True)
    ap.add_argument("--from-frame", type=int, required=True)
    ap.add_argument("--to-frame", type=int, required=True)
    ap.add_argument("--step", type=int, default=6)
    ap.add_argument("--dt-frames", type=int, default=3)
    ap.add_argument("--min-peak", type=float, default=0.6)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    tl = json.loads(Path(a.timeline).read_text())
    td = np.asarray(tl["mapping"]["t_data_s"], float)
    seed = json.loads(Path(a.seed).read_text())["seed"]
    rep = json.loads(Path(a.report).read_text())
    wall = rep["extras"]["sim"]["numbers"]["wall"]
    res = measure(
        Path(a.frames),
        json.loads(Path(a.cams).read_text()),
        seed,
        wall,
        lambda i: float(td[i]),
        range(a.from_frame, a.to_frame + 1, a.step),
        dt_frames=a.dt_frames,
        min_peak=a.min_peak,
    )
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()

"""Measure the rotation of the tornado in the RENDERED frames and compare it with the number it was drawn with.

The funnel's rotation indicator (a thin circular arrow on the ground whose cyan arrowhead sits at the azimuth
``omega * t_data`` around the vortex) is the tracer: this script finds the arrowhead in each decoded frame, projects
its pixel back onto the ground plane with the camera of that frame (camera_path.json: position, heading, pitch, lens),
turns that into an azimuth about the surveyed tornado position, unwraps it along the frames and fits its rate against
the DATA time of the frames (timeline.json). The fitted angular rate and period are compared with
``events.json -> tornado.vortex`` (omega, period): the check of the film, not of the physics.

    python measure_rotation.py --frames <dir> --camera camera_path.json --timeline timeline.json \\
        --events events.json --perils perils.json [--from-frame 1580 --to-frame 1900 --step 3]

Frames are PNG or JPEG files ``frame_NNNNN.*`` (render_movie.py). Needs numpy and pillow.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

TRACER = {"r_max": 70, "g_min": 215, "b_min": 170, "b_max": 255}  # the arrowhead is (0, 255, 230)
RING_Z_KM = (
    0.012  # height of the ring above the ground in the scene (km, before the vertical exaggeration)
)


def camera_basis(heading_deg: float, pitch_deg: float):
    """Forward, right and up unit vectors (x east, y north, z up) of the frame's camera."""
    h, p = math.radians(heading_deg), math.radians(pitch_deg)
    fwd = np.array([math.sin(h) * math.cos(p), math.cos(h) * math.cos(p), math.sin(p)])
    right = np.array([math.cos(h), -math.sin(h), 0.0])
    up = np.cross(right, fwd)
    return fwd, right, up


def project(point, eye, fwd, right, up, tan_x, tan_y, w, h):
    """Pixel (x, y) and depth of a scene point (km; z already scaled by the vertical exaggeration)."""
    v = np.asarray(point, float) - np.asarray(eye, float)
    d = float(v @ fwd)
    px, py = float(v @ right), float(v @ up)
    return w / 2 + px / d / tan_x * (w / 2), h / 2 - py / d / tan_y * (h / 2), d


def backproject(px, py, eye, fwd, right, up, tan_x, tan_y, w, h, z_plane):
    """Where the ray through pixel (px, py) meets the horizontal plane z = z_plane (scene km), or None."""
    dirv = fwd + ((px - w / 2) / (w / 2)) * tan_x * right - ((py - h / 2) / (h / 2)) * tan_y * up
    if abs(dirv[2]) < 1e-9:
        return None
    t = (z_plane - eye[2]) / dirv[2]
    if t <= 0:
        return None
    return np.asarray(eye, float) + t * dirv


def find_tracer(rgb: np.ndarray, min_pixels: int = 25):
    """Centroid (x, y) and pixel count of the cyan arrowhead in an RGB frame, or None."""
    r, g, b = rgb[..., 0].astype(int), rgb[..., 1].astype(int), rgb[..., 2].astype(int)
    m = (
        (r < TRACER["r_max"])
        & (g > TRACER["g_min"])
        & (b > TRACER["b_min"])
        & (b < TRACER["b_max"])
    )
    n = int(m.sum())
    if n < min_pixels:
        return None
    ys, xs = np.nonzero(m)
    return float(xs.mean()), float(ys.mean()), n


def fit_rate(az: np.ndarray, t: np.ndarray) -> tuple[float, float]:
    """Unwrap ``az`` (rad) and fit a line against ``t`` (s): (rate rad/s, rms residual rad)."""
    u = np.unwrap(np.asarray(az, float))
    a = np.polyfit(np.asarray(t, float), u, 1)
    res = u - np.polyval(a, t)
    return float(a[0]), float(np.sqrt(np.mean(res**2)))


def measure(
    frames_dir: Path,
    camera: list[dict],
    timeline: dict,
    tornado_xy,
    omega_expected: float,
    ring_radius_km: float | None = None,
    frames: range | None = None,
    log=print,
    cams: dict | None = None,
) -> dict:
    """Azimuth of the tracer around the tornado in each frame and the fitted rate against the data time."""
    from PIL import Image

    td = np.asarray(timeline["mapping"]["t_data_s"], float)
    az, tt, radii, used = [], [], [], []
    for i in frames or range(len(camera)):
        fp = next(
            (f for ext in ("jpg", "png") if (f := frames_dir / f"frame_{i:05d}.{ext}").exists()),
            None,
        )
        if fp is None:
            continue
        rgb = np.asarray(Image.open(fp).convert("RGB"))
        hit = find_tracer(rgb)
        if hit is None:
            continue
        c = camera[i]
        h_px, w_px = rgb.shape[:2]
        ve = c["vexag"]
        dumped = (cams or {}).get(str(i))
        if dumped:  # the camera as the scene drew the frame
            eye = tuple(dumped["eye"])
            fwd, right, up = (np.array(dumped[k], float) for k in ("fwd", "right", "up"))
            tan_x, tan_y = dumped["tanX"], dumped["tanY"]
        else:  # rebuilt from camera_path.json
            fov = c.get("fov_deg", 45.0)
            tan_y = math.tan(math.radians(fov) / 2)
            tan_x = tan_y * w_px / h_px
            eye = (c["x_km"], c["y_km"], c["z_km"] * ve)
            fwd, right, up = camera_basis(c["heading_deg"], c["pitch_deg"])
        p = backproject(
            hit[0], hit[1], eye, fwd, right, up, tan_x, tan_y, w_px, h_px, RING_Z_KM * ve
        )
        if p is None:
            continue
        cx, cy = tornado_xy(float(td[i]))
        az.append(math.atan2(p[1] - cy, p[0] - cx))
        radii.append(math.hypot(p[0] - cx, p[1] - cy))
        tt.append(float(td[i]))
        used.append(i)
    if len(az) < 6:
        raise SystemExit(f"only {len(az)} frames with a visible tracer: nothing to fit")
    rate, rms = fit_rate(np.array(az), np.array(tt))
    out = {
        "frames_used": len(az),
        "first_frame": used[0],
        "last_frame": used[-1],
        "data_seconds_covered": tt[-1] - tt[0],
        "measured_omega_rad_s": rate,
        "measured_period_s": 2 * math.pi / abs(rate) if rate else None,
        "expected_omega_rad_s": omega_expected,
        "expected_period_s": 2 * math.pi / omega_expected,
        "relative_error": (abs(rate) - omega_expected) / omega_expected,
        "sense": "counter-clockwise from above" if rate > 0 else "clockwise from above",
        "azimuth_rms_residual_deg": math.degrees(rms),
        "median_radius_m": float(np.median(radii)) * 1000.0,
    }
    if ring_radius_km is not None:
        out["expected_radius_m"] = ring_radius_km * 1000.0
    log(json.dumps(out, indent=1))
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--frames", required=True)
    ap.add_argument("--camera", required=True)
    ap.add_argument("--timeline", required=True)
    ap.add_argument("--events", required=True)
    ap.add_argument("--perils", required=True)
    ap.add_argument("--from-frame", type=int, default=None)
    ap.add_argument("--to-frame", type=int, default=None)
    ap.add_argument("--step", type=int, default=1)
    ap.add_argument(
        "--cams",
        default=None,
        help="camera dump of render_movie.py --cam-dump (else camera_path.json)",
    )
    ap.add_argument("--out", default=None, help="write the numbers as JSON")
    a = ap.parse_args(argv)
    camera = json.loads(Path(a.camera).read_text())
    timeline = json.loads(Path(a.timeline).read_text())
    ev = json.loads(Path(a.events).read_text())
    perils = json.loads(Path(a.perils).read_text())
    vx = ev["tornado"]["vortex"]
    path = next(t["path"] for t in perils["tornadoes"] if len(t.get("path", [])) >= 2)
    pt = np.array([q["t"] for q in path])
    px = np.array([q["x"] for q in path])
    py = np.array([q["y"] for q in path])

    def tornado_xy(t):
        return float(np.interp(t, pt, px)), float(np.interp(t, pt, py))

    lo = (
        a.from_frame
        if a.from_frame is not None
        else round(vx["real_time_from_clip_s"] * timeline["fps"])
    )
    hi = (
        a.to_frame if a.to_frame is not None else round(vx["real_time_to_clip_s"] * timeline["fps"])
    )
    res = measure(
        Path(a.frames),
        camera,
        timeline,
        tornado_xy,
        vx["omega_rad_s"],
        ring_radius_km=1.25 * vx["radius_m"] / 1000.0,
        frames=range(lo, hi + 1, a.step),
        cams=json.loads(Path(a.cams).read_text()) if a.cams else None,
    )
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()

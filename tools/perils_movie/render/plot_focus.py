"""Figures of the tornado focus plan: for every angle, a plan view (column-maximum reflectivity of the nearest observed step,
azimuthal shear, the surveyed tornado, the camera and its view cone) so that the choice of every angle can be checked against
the radar picture. Needs matplotlib (the engine environment).

    python plot_focus.py --bundle <bundle> --perils <clip_bundle>/perils.json --camera <camera_path.json> \
        --timeline <timeline.json> --out <folder>
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np


def view_cone(ax, x, y, heading, hfov, length, **kw):
    a0, a1 = math.radians(heading - hfov / 2), math.radians(heading + hfov / 2)
    xs = [x, x + length * math.sin(a0), x + length * math.sin(a1), x]
    ys = [y, y + length * math.cos(a0), y + length * math.cos(a1), y]
    ax.fill(xs, ys, **kw)


def main(argv=None) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--perils", required=True)
    ap.add_argument("--camera", required=True)
    ap.add_argument("--timeline", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--half-km", type=float, default=10.0)
    ap.add_argument("--prefix", default="focus")
    a = ap.parse_args(argv)
    bundle, out = Path(a.bundle), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    man = json.loads((bundle / "manifest.json").read_text())
    g = man["grid"]
    xs, ys = np.array(g["x_km"]), np.array(g["y_km"])
    p = json.loads(Path(a.perils).read_text())
    cam = json.loads(Path(a.camera).read_text())
    tl = json.loads(Path(a.timeline).read_text())
    beats = [b for b in tl["focus"]["beats"] if b["name"].startswith("focus_")]
    path = next(t["path"] for t in p["tornadoes"] if len(t.get("path", [])) >= 2)
    pt = np.array([q["t"] for q in path])
    px = np.array([q["x"] for q in path])
    py = np.array([q["y"] for q in path])
    series = (p.get("focus") or {}).get("series", [])
    fig, axes = plt.subplots(2, 3, figsize=(18, 12))
    for ax, b in zip(axes.ravel(), beats, strict=False):
        tm = 0.5 * (b["t0"] + b["t1"])
        td = float(np.interp(tm, tl["mapping"]["t_clip_s"], tl["mapping"]["t_data_s"]))
        k = int(np.clip(round(td / 60.0), 0, len(man["steps"]) - 1))
        v = np.load(bundle / man["steps"][k]["volume"])
        cm = v["cmax"]
        tx, ty = float(np.interp(td, pt, px)), float(np.interp(td, pt, py))
        ix = np.abs(xs - tx) <= a.half_km
        iy = np.abs(ys - ty) <= a.half_km
        X, Y = np.meshgrid(xs[ix], ys[iy])
        sub = cm[np.ix_(iy, ix)]
        im = ax.pcolormesh(X, Y, sub, cmap="turbo", vmin=10, vmax=60, shading="auto")
        ax.contour(X, Y, sub, levels=[40, 50], colors=["k", "w"], linewidths=[0.6, 1.0])
        shear = v["rotation_ll"][np.ix_(iy, ix)]
        ax.contour(X, Y, shear, levels=[0.012], colors=["magenta"], linewidths=1.0)
        ax.plot(px, py, "r--", lw=1.2)
        ax.plot([tx], [ty], marker="*", ms=16, mfc="yellow", mec="k")
        i = min(len(cam) - 1, round(tm * tl["fps"]))
        c = cam[i]
        hfov = 2 * math.degrees(math.atan(math.tan(math.radians(c["fov_deg"]) / 2) * 16 / 9))
        view_cone(
            ax,
            c["x_km"],
            c["y_km"],
            c["heading_deg"],
            hfov,
            0.6 * a.half_km,
            color="cyan",
            alpha=0.35,
        )
        ax.plot([c["x_km"]], [c["y_km"]], "o", color="cyan", mec="k", ms=8)
        # the camera positions of the whole beat, thin
        for cc in cam[round(b["t0"] * tl["fps"]) : round(b["t1"] * tl["fps"]) : 15]:
            ax.plot([cc["x_km"]], [cc["y_km"]], ".", color="white", ms=3)
        m = series[k] if k < len(series) else None
        if m and m.get("x") is not None:
            ax.plot([m["x"]], [m["y"]], "o", mfc="none", mec="magenta", ms=14, mew=2)
        ax.set_xlim(tx - a.half_km, tx + a.half_km)
        ax.set_ylim(ty - a.half_km, ty + a.half_km)
        ax.set_aspect("equal")
        hh, mm, ss = int(td // 3600), int(td % 3600 // 60), int(td % 60)
        t_utc = f"01:{4 + int(td // 60):02d}:{ss:02d}" if td < 960 else "01:20:00"
        ax.set_title(
            f"{b['name']}  clip {tm:.1f} s, data {t_utc} UTC (step {k}), x{b['speedup_mean']:.1f}\n"
            f"camera {b['tornado_distance_m']} m from the tornado, bearing {b['camera_bearing_from_tornado_deg']:.0f} deg, "
            f"{b['camera_height_m']:.0f} m high, looking {c['heading_deg']:.0f} deg, lens {b['fov_deg']:.0f} deg",
            fontsize=9,
        )
        _ = (hh, mm)
    fig.colorbar(im, ax=axes.ravel().tolist(), shrink=0.6, label="column-max reflectivity, dBZ")
    fig.savefig(out / f"{a.prefix}_plan_views.png", dpi=70, bbox_inches="tight")
    print("wrote", out / f"{a.prefix}_plan_views.png")


if __name__ == "__main__":
    main()

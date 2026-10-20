"""Verify the geometry of the PERiLS movie against the data bundle: the coordinate chain, the time chain, where the surveyed
tornado is relative to the radar echo and to the camera in the tornado shot.

Reads the bundle (read only), the plan files of the film (timeline.json, camera_path.json, events.json) and the converted
clip bundle (perils.json); writes ``geometry_numbers.json`` and figures into ``--out``. The numbers and what they mean are
written up in ``verification_geometry.md`` next to the film (by hand, from this output).

    python verify_geometry.py --bundle <bundle> [--manifest manifest_v2.json] --plan <folder with timeline/camera/events> \
        --perils <perils.json> --out <folder>

What it checks (verification_geometry.md says what the numbers mean):

* the coordinate chain: projection of radar sites, stations and the surveyed ends (sphere AEQD of the bundle against the WGS84
  ellipsoid), the film's own km against the bundle's, height above sea level against above ground;
* the time chain: the clip time of every frame against the data time, the volume steps, the radars' time offsets inside a step
  and the survey times;
* the tornado against the radar echo and the radar rotation at five times (plan-view figures), the lightning against the echo
  core, the disdrometer's reflectivity against the radar's at its place (a registration and timing check away from the tornado);
* the camera against the tornado, the echo and the line at the beats of the film's tornado focus.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

R_SPHERE = 6370997.0  # the bundle's projection: azimuthal equidistant on a sphere of this radius (Py-ART pyart_aeqd)


def projections(lat0: float, lon0: float):
    import pyproj

    sph = pyproj.Proj(proj="aeqd", lat_0=lat0, lon_0=lon0, R=R_SPHERE)
    wgs = pyproj.Proj(proj="aeqd", lat_0=lat0, lon_0=lon0, datum="WGS84")
    return sph, wgs


def xy_km(proj, lat, lon):
    x, y = proj(np.asarray(lon, float), np.asarray(lat, float))
    return np.asarray(x) / 1000.0, np.asarray(y) / 1000.0


def bearing(dx, dy):
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def coordinate_chain(bundle: Path, man: dict, plan: dict, perils: dict) -> dict:
    g = man["grid"]
    lat0, lon0 = g["origin_lat"], g["origin_lon"]
    sph, wgs = projections(lat0, lon0)
    out: dict = {
        "origin": [lat0, lon0],
        "bundle_projection": g["projection"],
        "x_axis": g["x_axis"],
        "y_axis": g["y_axis"],
    }

    def compare(name, items):
        rows = []
        for it in items:
            if it.get("lat") is None or it.get("x_km") is None:
                continue
            xs, ys = xy_km(sph, it["lat"], it["lon"])
            xw, yw = xy_km(wgs, it["lat"], it["lon"])
            rows.append(
                {
                    "name": it.get("name") or it.get("id"),
                    "bundle_xy_km": [it["x_km"], it["y_km"]],
                    "sphere_aeqd_km": [float(xs), float(ys)],
                    "wgs84_aeqd_km": [float(xw), float(yw)],
                    "err_sphere_km": float(math.hypot(xs - it["x_km"], ys - it["y_km"])),
                    "err_wgs84_km": float(math.hypot(xw - it["x_km"], yw - it["y_km"])),
                }
            )
        e_s = [r["err_sphere_km"] for r in rows]
        e_w = [r["err_wgs84_km"] for r in rows]
        out[name] = {
            "n": len(rows),
            "sphere_err_km": {"max": max(e_s), "mean": float(np.mean(e_s))},
            "wgs84_err_km": {"max": max(e_w), "mean": float(np.mean(e_w))},
            "rows": rows,
        }

    compare("radars", man["radars"])
    stations = json.loads((bundle / "stations" / "stations.json").read_text())
    compare("stations", [s for s in stations if not s.get("moving")])
    tor = next(
        t
        for t in json.loads((bundle / "tornadoes" / "tornadoes.json").read_text())
        if t["id"] == "NSED_1035605"
    )
    compare(
        "tornado_ends",
        [
            {"name": "begin", **tor["begin"]},
            {
                "name": "end",
                "lat": tor["end_point"]["lat"],
                "lon": tor["end_point"]["lon"],
                "x_km": tor["end_point"]["x_km"],
                "y_km": tor["end_point"]["y_km"],
            },
        ],
    )
    # bearings: between radar pairs from the geodesic, from the bundle's x,y and from the film's Geo
    from pyproj import Geod

    geod = Geod(ellps="sphere")
    rad = man["radars"]
    br = []
    for a, b in [(0, 1), (0, 3), (2, 3), (1, 4)]:
        A, Bq = rad[a], rad[b]
        az, _baz, dist = geod.inv(A["lon"], A["lat"], Bq["lon"], Bq["lat"])
        br.append(
            {
                "pair": f"{A['name']}->{Bq['name']}",
                "geodesic_deg": az % 360,
                "bundle_xy_deg": bearing(Bq["x_km"] - A["x_km"], Bq["y_km"] - A["y_km"]),
                "distance_km": dist / 1000.0,
                "xy_distance_km": math.hypot(Bq["x_km"] - A["x_km"], Bq["y_km"] - A["y_km"]),
            }
        )
    out["bearings"] = br
    # the film's own points: the tornado path of perils.json (film frame) against the bundle frame
    p0 = perils["tornadoes"][0]["path"]
    out["film_frame_tornado"] = {
        "film_begin_km": [p0[0]["x"], p0[0]["y"]],
        "bundle_begin_km": [tor["begin"]["x_km"], tor["begin"]["y_km"]],
        "film_end_km": [p0[-1]["x"], p0[-1]["y"]],
        "bundle_end_km": [tor["end_point"]["x_km"], tor["end_point"]["y_km"]],
        "begin_offset_km": math.hypot(
            p0[0]["x"] - tor["begin"]["x_km"], p0[0]["y"] - tor["begin"]["y_km"]
        ),
        "end_offset_km": math.hypot(
            p0[-1]["x"] - tor["end_point"]["x_km"], p0[-1]["y"] - tor["end_point"]["y_km"]
        ),
        "film_origin": [perils["origin"]["lat"], perils["origin"]["lon"]],
    }
    return out


def time_chain(man: dict, plan: dict, perils: dict, bundle: Path) -> dict:
    tl = plan["timeline"]
    mt = np.asarray(tl["mapping"]["t_clip_s"], float)
    md = np.asarray(tl["mapping"]["t_data_s"], float)
    steps = man["steps"]
    from datetime import datetime

    def parse(s):
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    t0 = parse(man["window"]["start"])
    rel = [(parse(s["time"]) - t0).total_seconds() for s in steps]
    tors = json.loads((bundle / "tornadoes" / "tornadoes.json").read_text())
    tor = next(t for t in tors if t["id"] == "NSED_1035605")
    ts, te = (
        (parse(tor["begin"]["time"]) - t0).total_seconds(),
        (parse(tor["end_point"]["time"]) - t0).total_seconds(),
    )
    rows = []
    for tc in (0, 15, 30, 44, 46, 48, 50, 54, 58, 62, 66, 70, 80, 100, 120):
        td = float(np.interp(tc, mt, md))
        k = int(np.floor(td / 60.0))
        k = min(k, len(steps) - 2)
        s = (td - rel[k]) / (rel[k + 1] - rel[k])
        clock = t0.timestamp() + td
        rows.append(
            {
                "clip_s": tc,
                "data_s": td,
                "utc": datetime.utcfromtimestamp(clock).strftime("%H:%M:%S"),
                "between_steps": [k, k + 1],
                "weight_on_next_step": s,
                "nearest_step_offset_s": float(min(abs(td - r) for r in rel)),
                "survey_alive": bool(ts <= td <= te),
                "survey_fraction": float((td - ts) / (te - ts)),
            }
        )
    offs = [list(s["offsets_s"].values()) for s in steps]
    allo = np.concatenate([np.asarray(o, float) for o in offs])
    return {
        "survey_start_s": ts,
        "survey_end_s": te,
        "step_times_s": [*rel[:3], "...", *rel[-2:]],
        "radar_offsets_within_a_step_s": {
            "min": float(allo.min()),
            "max": float(allo.max()),
            "median_abs": float(np.median(np.abs(allo))),
        },
        "clip_frames": rows,
        "tornado_shot": {
            "clip_s": [46.0, 70.0],
            "data_s": [float(np.interp(46.0, mt, md)), float(np.interp(70.0, mt, md))],
            "speedup": [
                float(
                    np.interp(
                        46.5, mt, tl["speedup"][: len(mt)] if len(tl["speedup"]) == len(mt) else md
                    )
                ),
                1.0,
            ],
        },
    }


def survey_xy(tor: dict, t_s: float, t0_s: float, t1_s: float):
    f = (t_s - t0_s) / (t1_s - t0_s)
    b, e = tor["begin"], tor["end_point"]
    return b["x_km"] + (e["x_km"] - b["x_km"]) * f, b["y_km"] + (e["y_km"] - b["y_km"]) * f


def nearest_cell_distance(mask, xs, ys, x, y):
    jj, ii = np.nonzero(mask)
    if not len(ii):
        return None
    d = np.hypot(xs[ii] - x, ys[jj] - y)
    k = int(np.argmin(d))
    return float(d[k]), float(xs[ii[k]]), float(ys[jj[k]])


def echo_at_steps(bundle: Path, man: dict, steps: list[int], out: Path, plan: dict) -> dict:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap

    g = man["grid"]
    xs, ys = (np.asarray(g[k], float) for k in ("x_km", "y_km"))
    tors = json.loads((bundle / "tornadoes" / "tornadoes.json").read_text())
    tor = next(t for t in tors if t["id"] == "NSED_1035605")
    rot = json.loads((bundle / "context" / "rotation_tracks.json").read_text())
    nws = LinearSegmentedColormap.from_list(
        "nws",
        [
            (0, "#04e9e7"),
            (0.2, "#019ff4"),
            (0.3, "#02fd02"),
            (0.45, "#fdf802"),
            (0.6, "#fd9500"),
            (0.75, "#fd0000"),
            (0.9, "#bc0000"),
            (1, "#f800fd"),
        ],
    )
    res = {}
    for k in steps:
        z = np.load(bundle / man["steps"][k]["volume"])
        w = np.load(bundle / "winds" / f"step_{k:04d}.npz")
        t_s = 60.0 * k
        sx, sy = survey_xy(tor, t_s, 240.0, 960.0)
        cmax = np.nan_to_num(z["cmax"], nan=-10.0)
        asl = np.nan_to_num(z["rotation_ll"], nan=0.0)
        vil = np.nan_to_num(z["vil"], nan=0.0)
        et = np.nan_to_num(z["echotop18_km"], nan=0.0)
        X, Y = np.meshgrid(xs, ys)
        dist = np.hypot(X - sx, Y - sy)
        row = {"step": k, "utc": str(z["time_iso"]), "survey_xy_km": [sx, sy]}
        for r in (2.0, 5.0, 10.0):
            m = dist <= r
            j = np.unravel_index(np.argmax(np.where(m, cmax, -99)), cmax.shape)
            row[f"cmax_max_within_{int(r)}km"] = {
                "dbz": float(cmax[j]),
                "offset_km": [float(xs[j[1]] - sx), float(ys[j[0]] - sy)],
                "distance_km": float(dist[j]),
                "bearing_deg": bearing(float(xs[j[1]] - sx), float(ys[j[0]] - sy)),
            }
        for thr in (40, 45, 50):
            nd = nearest_cell_distance(cmax >= thr, xs, ys, sx, sy)
            row[f"nearest_{thr}dbz"] = (
                None
                if nd is None
                else {
                    "distance_km": nd[0],
                    "offset_km": [nd[1] - sx, nd[2] - sy],
                    "bearing_deg": bearing(nd[1] - sx, nd[2] - sy),
                }
            )
        m10 = (dist <= 10.0) & (cmax >= 40)
        if m10.any():
            cx, cy = float(X[m10].mean()), float(Y[m10].mean())
            row["core_centroid_40dbz_within_10km"] = {
                "offset_km": [cx - sx, cy - sy],
                "distance_km": math.hypot(cx - sx, cy - sy),
                "bearing_deg": bearing(cx - sx, cy - sy),
                "area_km2": float(m10.sum() * 0.25),
            }
        row["cmax_at_survey_dbz"] = float(
            cmax[int(np.argmin(abs(ys - sy))), int(np.argmin(abs(xs - sx)))]
        )
        row["vil_max_within_10km"] = float(vil[dist <= 10].max())
        row["echotop18_max_within_10km_km"] = float(et[dist <= 10].max())
        m5 = dist <= 5.0
        j = np.unravel_index(np.argmax(np.where(m5, asl, -99)), asl.shape)
        row["rotation_ll_max_within_5km"] = {
            "as_s-1": float(asl[j]),
            "offset_km": [float(xs[j[1]] - sx), float(ys[j[0]] - sy)],
            "distance_km": float(dist[j]),
        }
        ps = next(p for p in rot["per_step"] if p["index"] == k)
        cands = sorted(
            (
                {
                    "xy_km": [v["x_km"], v["y_km"]],
                    "dist_km": math.hypot(v["x_km"] - sx, v["y_km"] - sy),
                    "peak_as": v["peak_as"],
                    "vrot_ms": v["vrot_ms"],
                    "radar": v["radar"],
                }
                for v in ps["vortices"]
            ),
            key=lambda c: c["dist_km"],
        )
        row["rotation_candidates_nearest"] = cands[:3]
        for c in row["rotation_candidates_nearest"]:
            c["offset_km"] = [c["xy_km"][0] - sx, c["xy_km"][1] - sy]
            c["bearing_deg"] = bearing(*c["offset_km"])
        kz = int(np.argmin(abs(w["z_km"] - 0.5)))
        usr, vsr = np.asarray(w["u_sr"][kz], float), np.asarray(w["v_sr"][kz], float)
        wx, wy = np.asarray(w["x_km"], float), np.asarray(w["y_km"], float)
        WX, WY = np.meshgrid(wx, wy)
        near = np.hypot(WX - sx, WY - sy) <= 5.0
        okw = bool(np.isfinite(usr[near]).any())
        row["winds_sr_0p5km_within_5km"] = {
            "valid_fraction": float(np.isfinite(usr[near]).mean()),
            "mean_sr_wind_ms": [float(np.nanmean(usr[near])), float(np.nanmean(vsr[near]))]
            if okw
            else None,
            "max_vorticity_s-1": float(np.nanmax(np.asarray(w["vorticity"][kz], float)[near]))
            if okw
            else None,
        }
        res[str(k)] = row
        h = 15.0
        fig, ax = plt.subplots(1, 3, figsize=(18, 6.3))
        sel = abs(xs - sx) <= h
        sely = abs(ys - sy) <= h
        ext = [xs[sel][0] - 0.25, xs[sel][-1] + 0.25, ys[sely][0] - 0.25, ys[sely][-1] + 0.25]

        def sub(a, sely=sely, sel=sel):
            return a[np.ix_(sely, sel)]

        a0 = ax[0]
        im = a0.imshow(sub(cmax), origin="lower", extent=ext, cmap=nws, vmin=0, vmax=65)
        a0.contour(
            xs[sel], ys[sely], sub(cmax), levels=[40, 50], colors=["k", "w"], linewidths=[0.7, 1.0]
        )
        a0.set_title(f"column-max reflectivity (dBZ), {str(z['time_iso'])[11:19]} UTC")
        fig.colorbar(im, ax=a0, shrink=0.8)
        a1 = ax[1]
        im = a1.imshow(sub(asl), origin="lower", extent=ext, cmap="magma_r", vmin=0, vmax=0.03)
        a1.contour(
            xs[sel], ys[sely], sub(cmax), levels=[40, 50], colors=["c", "b"], linewidths=[0.6, 0.9]
        )
        a1.set_title("low-level azimuthal shear (s-1, 0-2 km max); contours 40/50 dBZ")
        fig.colorbar(im, ax=a1, shrink=0.8)
        a2 = ax[2]
        spd = np.hypot(usr, vsr)
        wsel = abs(wx - sx) <= h
        wsely = abs(wy - sy) <= h
        wext = [wx[wsel][0] - 0.5, wx[wsel][-1] + 0.5, wy[wsely][0] - 0.5, wy[wsely][-1] + 0.5]
        im = a2.imshow(
            spd[np.ix_(wsely, wsel)], origin="lower", extent=wext, cmap="viridis", vmin=0, vmax=30
        )
        step = 2
        qx, qy = np.meshgrid(wx[wsel][::step], wy[wsely][::step])
        a2.quiver(
            qx,
            qy,
            usr[np.ix_(wsely, wsel)][::step, ::step],
            vsr[np.ix_(wsely, wsel)][::step, ::step],
            color="w",
            scale=400,
            width=0.003,
        )
        a2.contour(
            xs[sel], ys[sely], sub(cmax), levels=[40, 50], colors=["k", "r"], linewidths=[0.5, 0.8]
        )
        a2.set_title("storm-relative wind at 0.5 km (m/s, retrieved)")
        fig.colorbar(im, ax=a2, shrink=0.8)
        for a_ in ax:
            a_.plot([sx], [sy], marker="*", color="r", ms=16, mec="k", label="survey position")
            a_.plot(
                [tor["begin"]["x_km"], tor["end_point"]["x_km"]],
                [tor["begin"]["y_km"], tor["end_point"]["y_km"]],
                "r--",
                lw=1,
            )
            for c in ps["vortices"]:
                if abs(c["x_km"] - sx) <= h and abs(c["y_km"] - sy) <= h:
                    a_.plot([c["x_km"]], [c["y_km"]], marker="o", mfc="none", mec="m", ms=11, mew=2)
            a_.set_xlim(sx - h, sx + h)
            a_.set_ylim(sy - h, sy + h)
            a_.set_xlabel("km east of the grid origin")
            a_.set_aspect("equal")
        ax[0].legend(loc="upper left", fontsize=8)
        fig.suptitle(
            f"step {k}, {str(z['time_iso'])[11:19]} UTC: survey position (red star), rotation candidates (magenta rings), survey path (dashed)",
            y=0.99,
        )
        fig.tight_layout()
        fig.savefig(out / f"echo_step_{k:02d}.png", dpi=80)
        plt.close(fig)
    return res


def camera_geometry(bundle: Path, man: dict, plan: dict, out: Path, perils: dict) -> dict:
    """Camera against the surveyed tornado, the line and the strong echo at the start, middle and end of every beat of the plan."""
    sph, _w = projections(man["grid"]["origin_lat"], man["grid"]["origin_lon"])
    cam = plan["camera"]
    tl = plan["timeline"]
    mt = np.asarray(tl["mapping"]["t_clip_s"], float)
    md = np.asarray(tl["mapping"]["t_data_s"], float)
    tors = json.loads((bundle / "tornadoes" / "tornadoes.json").read_text())
    tor = next(t for t in tors if t["id"] == "NSED_1035605")
    g = man["grid"]
    xs, ys = np.asarray(g["x_km"], float), np.asarray(g["y_km"], float)
    beats = (tl.get("focus") or {}).get("beats") or []
    times = []
    if beats:
        for bt in beats:
            times += [
                (bt["name"], bt["t0"] + 0.02),
                (bt["name"], 0.5 * (bt["t0"] + bt["t1"])),
                (bt["name"], bt["t1"] - 0.02),
            ]
    else:
        times = [("tornado", t) for t in (44, 48, 52, 56, 60, 64, 68, 70)]
    cache: dict = {}
    rows = []
    for name, tc in times:
        c = cam[min(len(cam) - 1, round(tc * 30))]
        cx, cy = xy_km(sph, c["lat"], c["lon"])
        td = float(np.interp(tc, mt, md))
        sx, sy = survey_xy(tor, td, 240.0, 960.0)
        d = math.hypot(sx - cx, sy - cy)
        brg = bearing(sx - cx, sy - cy)
        off = ((brg - c["heading_deg"] + 180) % 360) - 180
        k = int(np.clip(round(td / 60.0), 0, len(man["steps"]) - 1))
        if k not in cache:
            z = np.load(bundle / man["steps"][k]["volume"])
            cache[k] = np.nan_to_num(z["cmax"], nan=-10.0)
        cmax = cache[k]
        core = nearest_cell_distance(cmax >= 50, xs, ys, sx, sy)
        row = {
            "beat": name,
            "clip_s": round(tc, 2),
            "data_s": td,
            "camera_bundle_xy_km": [float(cx), float(cy)],
            "film_xy_km": [c["x_km"], c["y_km"]],
            "film_vs_bundle_frame_km": math.hypot(c["x_km"] - cx, c["y_km"] - cy),
            "tornado_bundle_xy_km": [sx, sy],
            "distance_to_tornado_m": 1000 * d,
            "bearing_to_tornado_deg": brg,
            "bearing_tornado_to_camera_deg": (brg + 180.0) % 360.0,
            "camera_heading_deg": c["heading_deg"],
            "tornado_off_axis_deg": off,
            "height_m": c["alt_m"],
            "pitch_deg": c["pitch_deg"],
            "fov_deg": c.get("fov_deg"),
            "hfov_deg": None
            if c.get("fov_deg") is None
            else 2 * math.degrees(math.atan(math.tan(math.radians(c["fov_deg"]) / 2) * 16 / 9)),
        }
        if core is not None:
            cb = bearing(core[1] - cx, core[2] - cy)
            row["nearest_50dbz_from_tornado_km"] = core[0]
            row["nearest_50dbz_bearing_from_camera_deg"] = cb
            row["nearest_50dbz_off_axis_deg"] = ((cb - c["heading_deg"] + 180) % 360) - 180
            row["nearest_50dbz_in_view"] = (
                bool(abs(row["nearest_50dbz_off_axis_deg"]) <= row["hfov_deg"] / 2)
                if row["hfov_deg"]
                else None
            )
        rows.append(row)
    return {"rows": rows}


def lma_vs_echo(perils: dict, bundle: Path, man: dict, steps: list[int]) -> dict:
    """Where the lightning sources of the minute around a step are against the strong echo (registration of the LMA with the radars)."""
    g = man["grid"]
    xs, ys = np.asarray(g["x_km"], float), np.asarray(g["y_km"], float)
    src = np.concatenate(
        [np.asarray(f["s"], float) for f in perils["lma"]["flashes"] if len(f["s"])]
    )
    out = {}
    X, Y = np.meshgrid(xs, ys)
    for k in steps:
        t = 60.0 * k
        m = (src[:, 0] >= t - 30) & (src[:, 0] < t + 30)
        if m.sum() < 20:
            continue
        sx_, sy_ = src[m, 1], src[m, 2]
        z = np.load(bundle / man["steps"][k]["volume"])
        cmax = np.nan_to_num(z["cmax"], nan=-10.0)
        row = {
            "n_sources": int(m.sum()),
            "lma_centroid_km": [float(np.median(sx_)), float(np.median(sy_))],
            "lma_median_alt_km": float(np.median(src[m, 3])),
        }
        # the echo the lightning belongs to: the area within 25 km of the LMA centroid
        near = np.hypot(X - row["lma_centroid_km"][0], Y - row["lma_centroid_km"][1]) <= 25.0
        for thr in (40, 50):
            sel = near & (cmax >= thr)
            if sel.any():
                cx, cy = float(X[sel].mean()), float(Y[sel].mean())
                row[f"echo_centroid_{thr}dbz_km"] = [cx, cy]
                row[f"lma_minus_echo_{thr}dbz_km"] = [
                    row["lma_centroid_km"][0] - cx,
                    row["lma_centroid_km"][1] - cy,
                ]
        # the fraction of the sources over echo of at least 30 / 40 dBZ (column maximum) at their position
        ii = np.clip(np.round((sx_ - xs[0]) / (xs[1] - xs[0])).astype(int), 0, len(xs) - 1)
        jj = np.clip(np.round((sy_ - ys[0]) / (ys[1] - ys[0])).astype(int), 0, len(ys) - 1)
        c_at = cmax[jj, ii]
        row["fraction_over_30dbz"] = float((c_at >= 30).mean())
        row["fraction_over_40dbz"] = float((c_at >= 40).mean())
        out[str(k)] = row
    return out


def station_vs_echo(bundle: Path, man: dict, perils: dict) -> dict:
    """The disdrometer's reflectivity at its place against the radar's lowest level there: spatial registration and timing away
    from the tornado (the bias says nothing about the tornado)."""
    g = man["grid"]
    xs, ys, zs = (np.asarray(g[k], float) for k in ("x_km", "y_km", "z_km"))
    out = {}
    for st in perils["stations"]:
        ser = st.get("series") or {}
        if "dbz" not in ser or not ser.get("t_s"):
            continue
        ts = np.asarray(ser["t_s"], float)
        dv = np.asarray([np.nan if v is None else v for v in ser["dbz"]], float)
        ok = np.isfinite(dv)
        if ok.sum() < 20:
            continue
        zi = int(np.argmin(abs(zs - 0.5)))
        rad = []
        steps_t = []
        for k in range(len(man["steps"])):
            z = np.load(bundle / man["steps"][k]["volume"])
            i = int(np.argmin(abs(xs - st["x"])))
            j = int(np.argmin(abs(ys - st["y"])))
            rad.append(
                float(np.nanmean(np.asarray(z["dbz"][zi, j - 1 : j + 2, i - 1 : i + 2], float)))
            )
            steps_t.append(60.0 * k)
        rad = np.asarray(rad)
        steps_t = np.asarray(steps_t)
        res = []
        for lag in range(-240, 241, 20):
            # the station series averaged around (step time + lag) against the radar value of the step
            vals = []
            for tt in steps_t:
                m = (ts >= tt + lag - 30) & (ts <= tt + lag + 30) & ok
                vals.append(float(np.mean(dv[m])) if m.sum() else np.nan)
            vals = np.asarray(vals)
            good = np.isfinite(vals) & np.isfinite(rad)
            if good.sum() >= 8 and np.std(vals[good]) > 1e-6 and np.std(rad[good]) > 1e-6:
                res.append(
                    (
                        lag,
                        float(np.corrcoef(vals[good], rad[good])[0, 1]),
                        float(np.mean(vals[good] - rad[good])),
                    )
                )
        if res:
            best = max(res, key=lambda r: r[1])
            out[st["id"]] = {
                "xy_km": [st["x"], st["y"]],
                "radar_dbz_at_station_by_step": [round(float(v), 1) for v in rad],
                "best_lag_s": best[0],
                "correlation_at_best_lag": best[1],
                "correlation_at_zero_lag": next((r[1] for r in res if r[0] == 0), None),
                "mean_station_minus_radar_db_at_best_lag": best[2],
                "note": "lag > 0: the station sees what the radar saw at the step that many seconds EARLIER",
            }
    return out


def heights(man: dict, perils: dict) -> dict:
    alts = [
        s.get("altKm", 0.0) * 1000.0
        for s in perils["stations"]
        if not s.get("moving") and s.get("altKm") is not None
    ]
    rad = {r["name"]: r.get("altKm", 0.0) * 1000.0 for r in perils["radars"]}
    return {
        "z_reference": man["grid"].get("z_reference"),
        "lowest_grid_levels_km": man["grid"]["z_km"][:5],
        "station_altitude_m": {"min": min(alts), "median": float(np.median(alts)), "max": max(alts)}
        if alts
        else None,
        "radar_altitude_m": rad,
        "note": "the grid height is above mean sea level; the film draws heights over a flat ground at 0 m; the ground of the tornado's neighbourhood is "
        "about the median station altitude: a height of 0.5 km above sea level is that much less above the ground",
    }


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--bundle", required=True)
    ap.add_argument(
        "--manifest",
        default="manifest.json",
        help="manifest.json (reflectivity merge v1) or manifest_v2.json (merge v2)",
    )
    ap.add_argument("--plan", required=True)
    ap.add_argument("--perils", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    bundle, plan_dir, out = Path(a.bundle), Path(a.plan), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    man = json.loads((bundle / a.manifest).read_text())
    perils = json.loads(Path(a.perils).read_text())
    plan = {
        "timeline": json.loads((plan_dir / "timeline.json").read_text()),
        "camera": json.loads((plan_dir / "camera_path.json").read_text()),
        "events": json.loads((plan_dir / "events.json").read_text()),
    }
    res = {
        "coordinate_chain": coordinate_chain(bundle, man, plan, perils),
        "time_chain": time_chain(man, plan, perils, bundle),
    }
    res["echo"] = echo_at_steps(bundle, man, [4, 7, 8, 10, 13, 16], out, plan)
    res["camera"] = camera_geometry(bundle, man, plan, out, perils)
    res["lma_vs_echo"] = lma_vs_echo(perils, bundle, man, [4, 7, 10, 13, 16])
    res["station_vs_echo"] = station_vs_echo(bundle, man, perils)
    res["heights"] = heights(man, perils)
    res["manifest"] = {
        "file": a.manifest,
        "spec_version": man.get("spec_version"),
        "reflectivity_v2": bool(man.get("reflectivity_v2")),
    }
    (out / "geometry_numbers.json").write_text(json.dumps(res, indent=1))
    c = res["coordinate_chain"]
    for k in ("radars", "stations", "tornado_ends"):
        print(
            k,
            c[k]["n"],
            "sphere err km",
            c[k]["sphere_err_km"],
            "wgs84 err km",
            c[k]["wgs84_err_km"],
        )
    print(json.dumps(c["film_frame_tornado"], indent=1))
    print(json.dumps(c["bearings"], indent=1))
    t = res["time_chain"]
    for r in t["clip_frames"]:
        print(r)
    print({k: v for k, v in t.items() if k != "clip_frames"})


if __name__ == "__main__":
    main()

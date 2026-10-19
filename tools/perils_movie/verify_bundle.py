#!/usr/bin/env python
"""Verification figures and numbers for a built bundle (read only): ``verification.json`` and PNG figures.

    python/.venv/bin/python tools/perils_movie/verify_bundle.py --bundle ~/Downloads/xradar-exports/perils_movie/bundle \
        --out /tmp/perils_verify

Figures: merged volume vs one radar, flashes over the reflectivity, station series with the outflow passage,
a Skew-T of the sondes launched in the window, the tornado track over reflectivity and rotation, a vertical
cross-section with the hydrometeor class and the debris flag, and the winds with their validation numbers.
Every number is also written to ``verification.json`` (quoted in docs/PERILS_BUNDLE.md).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "python"))
sys.path.insert(0, str(HERE))

import hailconv
import pgrid
import tornadoes

UTC = timezone.utc
ORIGIN = (33.575864857, -88.54851899)
X = np.arange(-90.0, 90.001, 0.5)


def load_json(p: Path):
    return json.loads(p.read_text())


def arr(vals):
    return np.array([np.nan if v is None else v for v in vals], float)


def merged_vs_single(b: Path, man, out: Path, num: dict) -> None:
    k = 6  # 1.5 km above sea level
    step = man["steps"][len(man["steps"]) // 2]
    v = np.load(b / step["volume"])
    root = Path(num["_data"]) / "GRID"
    name = "KGWX" if "KGWX" in step["radars"] else step["radars"][0]
    fn = step["files"][name]
    g = pgrid.load_grid(next((root / name).glob(fn)), name)
    kd = g.fields["dbz"][k]
    md = v["dbz"][k]
    both = np.isfinite(kd) & np.isfinite(md)
    d = (md - kd)[both]
    num["merge_vs_single"] = {
        "step": step["index"],
        "time": step["time"],
        "single_radar": name,
        "offsets_s": step["offsets_s"],
        "level_km": 1.5,
        "coverage_pct_single": round(100 * float(np.isfinite(kd).mean()), 1),
        "coverage_pct_merged": round(100 * float(np.isfinite(md).mean()), 1),
        "n_common_cells": int(both.sum()),
        "mean_diff_dB": round(float(d.mean()), 2),
        "rms_diff_dB": round(float(np.sqrt((d**2).mean())), 2),
        "corr": round(float(np.corrcoef(md[both], kd[both])[0, 1]), 3),
        "mean_nradars_where_merged": round(float(v["nradars"][k][np.isfinite(md)].mean()), 2),
        "cells_with_1_2_3plus_radars_pct": [
            round(100 * float((v["nradars"][k][np.isfinite(md)] == n).mean()), 1) for n in (1, 2)
        ]
        + [round(100 * float((v["nradars"][k][np.isfinite(md)] >= 3).mean()), 1)],
    }
    fg, ax = plt.subplots(1, 4, figsize=(22, 5.6))
    for a, (t, f, lim, cm) in zip(
        ax,
        [
            (f"merged dBZ, 1.5 km ASL, {step['time'][11:19]}Z", md, (0, 65), "turbo"),
            (f"{name} alone ({step['offsets_s'][name]:+.0f} s)", kd, (0, 65), "turbo"),
            ("radars per voxel", v["nradars"][k].astype(float), (0, 5), "viridis"),
            ("merged minus single (dB)", md - kd, (-15, 15), "RdBu_r"),
        ],
        strict=True,
    ):
        im = a.pcolormesh(X, X, f, vmin=lim[0], vmax=lim[1], cmap=cm, shading="nearest")
        a.set_aspect("equal")
        a.set_title(t)
        plt.colorbar(im, ax=a, shrink=0.75)
        for r in man["radars"]:
            a.plot(r["x_km"], r["y_km"], "k^", ms=5)
    fg.savefig(out / "fig1_merged_vs_single.png", dpi=60, bbox_inches="tight")
    plt.close(fg)


def flash_map(b: Path, man, out: Path, num: dict) -> None:
    L = load_json(b / man["lma"])
    step = man["steps"][len(man["steps"]) // 2]
    v = np.load(b / step["volume"])
    ref = float(np.cos(np.radians(ORIGIN[0])))
    src = np.array([s for f in L["flashes"] for s in f["sources"]])
    sx = (src[:, 2] - ORIGIN[1]) * 111.2 * ref
    sy = (src[:, 1] - ORIGIN[0]) * 111.2
    fg, ax = plt.subplots(1, 2, figsize=(18, 8))
    a = ax[0]
    a.pcolormesh(X, X, v["cmax"], vmin=0, vmax=65, cmap="turbo", shading="nearest", alpha=0.8)
    sel = slice(None, None, 6)
    sc = a.scatter(sx[sel], sy[sel], c=src[sel, 0], s=1.5, cmap="gray_r", vmin=0, vmax=960)
    plt.colorbar(sc, ax=a, shrink=0.7, label="source time (s since 01:04)")
    a.set_title(
        f"column-max dBZ at {step['time'][11:19]}Z and {len(src)} LMA sources ({len(L['flashes'])} flashes)"
    )
    a.set_aspect("equal")
    tj = b / man["tornadoes"]
    for t in load_json(tj):
        if t["overlaps_window"]:
            p = np.array(t["path"])
            a.plot((p[:, 2] - ORIGIN[1]) * 111.2 * ref, (p[:, 1] - ORIGIN[0]) * 111.2, "w-", lw=3)
            a.plot(
                (p[:, 2] - ORIGIN[1]) * 111.2 * ref,
                (p[:, 1] - ORIGIN[0]) * 111.2,
                "r-",
                lw=1.5,
                label=f"{t['rating']} tornado (survey)",
            )
    a.legend(loc="upper left")
    a = ax[1]
    a.hist(src[:, 3] / 1000.0, bins=40, color="k")
    a.set_xlabel("source altitude (km ASL)")
    a.set_ylabel("sources")
    a.set_title("LMA source heights")
    fg.savefig(out / "fig2_flashes.png", dpi=60, bbox_inches="tight")
    plt.close(fg)
    ns = L["sources_per_minute_in_box"]
    fl = np.array([f["t_start_s"] for f in L["flashes"]])
    num["lma"] = {
        "sources": L["n_sources"],
        "flashes": L["n_flashes"],
        "sources_per_minute_min_max": [min(ns), max(ns)],
        "flashes_per_minute": [int(((fl >= 60 * i) & (fl < 60 * i + 60)).sum()) for i in range(16)],
        "median_source_alt_km": round(float(np.median(src[:, 3])) / 1000.0, 2),
        "median_sources_per_flash": float(np.median([f["n_sources"] for f in L["flashes"]])),
    }


def station_figure(b: Path, man, out: Path, num: dict) -> None:
    st = {s["id"]: s for s in load_json(b / man["stations"])}
    ev = (
        load_json(b / "science_events.json")["events"]
        if (b / "science_events.json").exists()
        else []
    )
    gf = [e for e in ev if e["kind"] == "gust_front_passage"]
    fg, ax = plt.subplots(4, 2, figsize=(16, 13), sharex=False)
    pick = [x for x in ("StickNet_0102A", "StickNet_0217A") if x in st]
    for j, sid in enumerate(pick):
        s = st[sid]["series"]
        t = arr(s["t_s"])
        ax[0, j].plot(t, arr(s["temperature_c"]), "r")
        ax[0, j].set_ylabel("T (C)")
        ax[1, j].plot(t, arr(s["pressure_hpa"]), "b")
        ax[1, j].set_ylabel("p (hPa)")
        ax[2, j].plot(t, arr(s["wind_speed_ms"]), "k", label="mean")
        ax[2, j].plot(t, arr(s.get("wind_gust_ms", [])), "g", label="10 s max")
        ax[2, j].set_ylabel("wind (m/s)")
        ax[2, j].legend()
        ax[3, j].plot(t, arr(s["theta_e_k"]), "m")
        ax[3, j].set_ylabel("theta-e (K)")
        ax[0, j].set_title(sid)
        for e in gf:
            if e["station"] == sid:
                for r in range(4):
                    ax[r, j].axvline(e["t_s"], color="orange", ls="--")
        for r in range(4):
            ax[r, j].axvspan(0, 960, color="0.9", zorder=0)
    fg.savefig(out / "fig3_stations_outflow.png", dpi=60, bbox_inches="tight")
    plt.close(fg)
    # rain at the PIPS and the MRR
    fg, ax = plt.subplots(3, 1, figsize=(12, 9), sharex=True)
    for sid in [k for k in st if k.startswith("PIPS")]:
        s = st[sid]["series"]
        ax[0].plot(arr(s["t_s"]), arr(s["rain_rate_mmh"]), label=sid)
        ax[1].plot(arr(s["t_s"]), arr(s["dm_mm"]), label=sid)
        ax[2].plot(arr(s["t_s"]), arr(s["log10_nw"]), label=sid)
    ax[0].set_ylabel("rain rate (mm/h)")
    ax[1].set_ylabel("Dm (mm)")
    ax[2].set_ylabel("log10 Nw")
    ax[0].legend()
    for a in ax:
        a.axvspan(0, 960, color="0.9", zorder=0)
    fg.savefig(out / "fig3b_pips_dsd.png", dpi=60, bbox_inches="tight")
    plt.close(fg)
    num["gust_fronts"] = [
        {
            k: e["evidence"].get(k)
            for k in ("station", "t_s", "dT_2min_K", "dP_hPa", "wind_peak_ms", "wind_jump_ms")
        }
        for e in gf
    ]


def skewt(b: Path, man, out: Path, num: dict) -> None:
    S = load_json(b / man["soundings"])
    pick = [s for s in S if s["id"].startswith("UIUC_SONDE") and -400 <= s["launch_t_s"] <= 1100]
    pick = pick[:4] + [s for s in S if s["id"].startswith("UIUC_SONDE4")][-1:]
    n = len(pick)
    fg, ax = plt.subplots(1, n, figsize=(4.4 * n, 7))
    ax = np.atleast_1d(ax)
    skew = 35.0
    for a, s in zip(ax, pick, strict=True):
        rows = list(csv.DictReader((b / s["profile_csv"]).open()))
        p = arr([r["pressure_hpa"] or None for r in rows])
        T = arr([r["temperature_c"] or None for r in rows])
        Td = arr([r["dewpoint_c"] or None for r in rows])
        ok = np.isfinite(p) & np.isfinite(T)
        lp = np.log(p[ok] / 1000.0)
        a.plot(T[ok] - skew * lp, -lp, "r", label="T")
        ok2 = ok & np.isfinite(Td)
        a.plot(Td[ok2] - skew * np.log(p[ok2] / 1000.0), -np.log(p[ok2] / 1000.0), "g", label="Td")
        for name, col in (("surface-based", "k"), ("mixed-layer", "b")):
            pp = s["diagnostics"].get("parcel_paths", {}).get(name)
            if pp:
                pa, ta = np.array(pp["p_hpa"]), np.array(pp["t_c"])
                a.plot(
                    ta - skew * np.log(pa / 1000.0),
                    -np.log(pa / 1000.0),
                    col + "--",
                    lw=1,
                    label=name,
                )
        yt = [1000, 850, 700, 500, 400]
        a.set_yticks([-np.log(q / 1000.0) for q in yt], [str(q) for q in yt])
        a.set_ylim(-np.log(1.0), -np.log(0.38))
        a.set_xlim(-30, 40)
        d = s["diagnostics"]["parcels"]["surface-based"]
        a.set_title(
            f"{s['site']} {s['launch'][11:16]}Z\nSBCAPE {d.get('cape')} CIN {d.get('cin')}",
            fontsize=9,
        )
        a.legend(fontsize=7)
        a.grid(alpha=0.3)
    fg.savefig(out / "fig4_skewt.png", dpi=60, bbox_inches="tight")
    plt.close(fg)
    num["soundings"] = [
        {
            "id": s["id"],
            "launch": s["launch"],
            "relation": s["relation_to_window"],
            "sbcape": s["diagnostics"].get("parcels", {}).get("surface-based", {}).get("cape"),
            "sbcin": s["diagnostics"].get("parcels", {}).get("surface-based", {}).get("cin"),
            "mlcape": s["diagnostics"].get("parcels", {}).get("mixed-layer", {}).get("cape"),
            "freezing_asl_m": (s["diagnostics"].get("isotherms_asl_m") or {}).get("freezing_asl_m"),
            "top_asl_m": s.get("top_height_asl_m"),
            "shear_0_1km_ms": ((s["diagnostics"].get("shear") or {}).get("0-1km") or {}).get(
                "speed_ms"
            ),
            "shear_0_3km_ms": ((s["diagnostics"].get("shear") or {}).get("0-3km") or {}).get(
                "speed_ms"
            ),
            "pw_mm": (s["diagnostics"].get("moisture") or {}).get("precipitable_water_mm"),
        }
        for s in S
        if "diagnostics" in s and "parcels" in s["diagnostics"]
    ]


def tornado_panels(b: Path, man, out: Path, num: dict) -> None:
    tors = [t for t in load_json(b / man["tornadoes"]) if t["overlaps_window"]]
    if not tors:
        return
    t = tors[0]
    rt = load_json(b / man["rotation_tracks"])
    ref = float(np.cos(np.radians(ORIGIN[0])))
    steps = man["steps"]
    pick = [steps[i] for i in range(2, len(steps), 2)][:8]
    fg, ax = plt.subplots(2, 4, figsize=(22, 11))
    for a, s in zip(ax.ravel(), pick, strict=False):
        v = np.load(b / s["volume"])
        tt = (
            datetime.fromisoformat(s["time"].replace("Z", "+00:00"))
            - datetime.fromisoformat(man["window"]["start"].replace("Z", "+00:00"))
        ).total_seconds()
        pos = tornadoes.position_at(t, tt)
        cx, cy = 14.0, -47.0
        a.pcolormesh(X, X, v["dbz"][2], vmin=10, vmax=65, cmap="turbo", shading="nearest")
        rl = v["rotation_ll"]
        a.contour(
            X,
            X,
            np.where(np.isfinite(rl), rl, 0.0),
            levels=[0.01, 0.02, 0.03],
            colors=["w", "k", "m"],
            linewidths=[0.8, 1.2, 1.6],
        )
        ts = v["tds"].max(axis=0)
        yy, xx = np.nonzero(ts >= 1)
        a.plot(X[xx], X[yy], ".", color="cyan", ms=1.5)
        p = np.array(t["path"])
        a.plot((p[:, 2] - ORIGIN[1]) * 111.2 * ref, (p[:, 1] - ORIGIN[0]) * 111.2, "k-", lw=3)
        a.plot((p[:, 2] - ORIGIN[1]) * 111.2 * ref, (p[:, 1] - ORIGIN[0]) * 111.2, "w-", lw=1.5)
        if pos:
            a.plot(
                (pos[1] - ORIGIN[1]) * 111.2 * ref,
                (pos[0] - ORIGIN[0]) * 111.2,
                "w*",
                ms=15,
                mec="k",
            )
        for vt in rt["per_step"][s["index"]]["vortices"]:
            a.plot(vt["x_km"], vt["y_km"], "o", mfc="none", mec="yellow", ms=10, mew=2)
        for r in man["radars"]:
            a.plot(r["x_km"], r["y_km"], "k^", ms=8)
        a.set_xlim(cx - 28, cx + 28)
        a.set_ylim(cy - 24, cy + 24)
        a.set_aspect("equal")
        a.set_title(
            f"{s['time'][11:19]}Z dBZ 0.5 km; rotation 0.01/0.02/0.03 s-1; cyan: low-RHO flag; * survey; o vortex cand.",
            fontsize=8,
        )
    fg.savefig(out / "fig5_tornado_track.png", dpi=55, bbox_inches="tight")
    plt.close(fg)
    comp = rt.get("survey_comparison", {}).get(t["id"])
    num["tornado_vs_radar"] = (
        {
            k: comp.get(k)
            for k in (
                "n_steps_with_survey_position",
                "n_steps_detected",
                "distance_km_median",
                "distance_km_min",
                "distance_km_mean",
                "time_shift_s",
                "mean_distance_at_best_shift_km",
            )
        }
        if comp
        else None
    )


def cross_section(b: Path, man, out: Path, num: dict) -> None:
    tors = [t for t in load_json(b / man["tornadoes"]) if t["overlaps_window"]]
    if not tors:
        return
    t = tors[0]
    steps = man["steps"]
    s = steps[min(len(steps) - 1, 10)]
    v = np.load(b / s["volume"])
    t0 = datetime.fromisoformat(man["window"]["start"].replace("Z", "+00:00"))
    tt = (datetime.fromisoformat(s["time"].replace("Z", "+00:00")) - t0).total_seconds()
    pos = tornadoes.position_at(t, tt)
    ref = float(np.cos(np.radians(ORIGIN[0])))
    xk = (pos[1] - ORIGIN[1]) * 111.2 * ref
    yk = (pos[0] - ORIGIN[0]) * 111.2
    j = round((yk + 90) / 0.5)
    z = np.arange(41) * 0.25
    xs = slice(round((xk - 30 + 90) / 0.5), round((xk + 30 + 90) / 0.5))
    fg, ax = plt.subplots(4, 1, figsize=(13, 16), sharex=True)
    xx = X[xs]
    im = ax[0].pcolormesh(
        xx, z, v["dbz"][:, j, xs], vmin=0, vmax=65, cmap="turbo", shading="nearest"
    )
    plt.colorbar(im, ax=ax[0], label="dBZ")
    rho = v["rho"][:, j, xs].astype(float)
    im = ax[1].pcolormesh(xx, z, rho, vmin=0.5, vmax=1.0, cmap="viridis", shading="nearest")
    ax[1].contour(xx, z, v["tds"][:, j, xs], levels=[0.5, 1.5], colors=["w", "r"])
    plt.colorbar(im, ax=ax[1], label="RHO (white: flag 1, red: flag 2)")
    leg = hailconv.HID_LEGEND
    hid = v["hid"][:, j, xs].astype(float)
    hid[hid == 0] = np.nan
    im = ax[2].pcolormesh(xx, z, hid, vmin=0.5, vmax=11.5, cmap="tab20", shading="nearest")
    cb = plt.colorbar(im, ax=ax[2], ticks=range(1, 12))
    cb.ax.set_yticklabels([f"{k} {leg[k][0]}" for k in range(1, 12)], fontsize=7)
    ax[2].set_title("hydrometeor class (unified legend)")
    im = ax[3].pcolormesh(
        xx, z, v["zdr"][:, j, xs].astype(float), vmin=-1, vmax=5, cmap="viridis", shading="nearest"
    )
    plt.colorbar(im, ax=ax[3], label="ZDR (dB, bias corrected)")
    for a in ax:
        a.axvline(xk, color="w", ls="--")
        a.set_ylabel("km ASL")
    ax[0].set_title(
        f"west-east section at y = {yk:.1f} km through the surveyed tornado position at {s['time'][11:19]}Z"
    )
    fg.savefig(out / "fig6_cross_sections.png", dpi=55, bbox_inches="tight")
    plt.close(fg)
    sub = v["hid"][:, max(j - 10, 0) : j + 11, xs]
    num["hid_counts_in_section_band"] = {leg[k][0]: int((sub == k).sum()) for k in range(1, 12)}


def winds_figure(b: Path, man, out: Path, num: dict) -> None:
    steps = [s for s in man["steps"] if s.get("winds")]
    if not steps:
        return
    s = steps[len(steps) // 2]
    w = np.load(b / s["winds"])
    x = w["x_km"]
    k = 6
    fg, ax = plt.subplots(2, 4, figsize=(22, 10))
    sp = np.hypot(w["u"], w["v"])
    panels = [
        ("speed 1.5 km (m/s) + vectors", sp[k], (0, 45), "viridis"),
        ("storm-relative u (m/s)", w["u_sr"][k], (-30, 30), "RdBu_r"),
        ("w at 3.5 km (m/s)", w["w"][14], (-10, 10), "RdBu_r"),
        ("w at 6 km (m/s)", w["w"][24], (-10, 10), "RdBu_r"),
        (
            "vertical vorticity 1.5 km (1e-3 s-1)",
            w["vorticity"][k].astype(float) * 1000,
            (-10, 10),
            "RdBu_r",
        ),
        (
            "divergence 0.5 km (1e-3 s-1)",
            w["divergence"][2].astype(float) * 1000,
            (-10, 10),
            "RdBu_r",
        ),
        ("beam crossing angle (deg)", w["crossing_deg"][k], (0, 90), "viridis"),
        ("u,v error (m/s)", w["uv_error"][k].astype(float), (0, 6), "viridis"),
    ]
    for a, (t, f, lim, cm) in zip(ax.ravel(), panels, strict=True):
        im = a.pcolormesh(x, x, f, vmin=lim[0], vmax=lim[1], cmap=cm, shading="nearest")
        a.set_aspect("equal")
        a.set_title(t)
        plt.colorbar(im, ax=a, shrink=0.75)
    q = slice(None, None, 8)
    ax[0, 0].quiver(x[q], x[q], w["u"][k][q, q], w["v"][k][q, q], scale=600)
    fg.suptitle(f"multi-Doppler winds {s['time'][11:19]}Z (RETRIEVED, 1 km grid)")
    fg.savefig(out / "fig7_winds.png", dpi=55, bbox_inches="tight")
    plt.close(fg)
    # validation against the sondes launched in the window: wind profile at the sonde's position
    S = load_json(b / man["soundings"])
    t0 = datetime.fromisoformat(man["window"]["start"].replace("Z", "+00:00"))
    ref = float(np.cos(np.radians(ORIGIN[0])))
    res = []
    for sd in S:
        if sd["id"].startswith("UIUC") and 0 <= sd["launch_t_s"] <= 960 and sd.get("drift"):
            rows = list(csv.DictReader((b / sd["profile_csv"]).open()))
            h = arr([r["height_asl_m"] or None for r in rows])
            ws = arr([r["wind_speed_ms"] or None for r in rows])
            wd = arr([r["wind_dir_deg"] or None for r in rows])
            ts_ = arr([r["t_s"] or None for r in rows])
            la = arr([r["lat"] or None for r in rows])
            lo = arr([r["lon"] or None for r in rows])
            us, vs = -ws * np.sin(np.radians(wd)), -ws * np.cos(np.radians(wd))
            du, dv, n = [], [], 0
            for zq in np.arange(500.0, 4001.0, 250.0):
                i = np.nanargmin(np.abs(h - zq))
                if abs(h[i] - zq) > 100 or not np.isfinite(us[i]):
                    continue
                st = min(
                    steps,
                    key=lambda m: abs(
                        (
                            datetime.fromisoformat(m["time"].replace("Z", "+00:00")) - t0
                        ).total_seconds()
                        - ts_[i]
                    ),
                )
                ww = np.load(b / st["winds"])
                xi = int(np.argmin(np.abs(ww["x_km"] - (lo[i] - ORIGIN[1]) * 111.2 * ref)))
                yi = int(np.argmin(np.abs(ww["y_km"] - (la[i] - ORIGIN[0]) * 111.2)))
                kz = round(zq / 250.0)
                uu, vv = ww["u"][kz, yi, xi], ww["v"][kz, yi, xi]
                if np.isfinite(uu):
                    du.append(float(uu - us[i]))
                    dv.append(float(vv - vs[i]))
                    n += 1
            res.append(
                {
                    "sonde": sd["id"],
                    "n_levels_compared": n,
                    "mean_du": None if not n else round(float(np.mean(du)), 2),
                    "mean_dv": None if not n else round(float(np.mean(dv)), 2),
                    "rms_vector_diff_ms": None
                    if not n
                    else round(float(np.sqrt(np.mean(np.square(du) + np.square(dv)))), 2),
                }
            )
    num["winds_vs_sondes"] = res
    # vs surface stations: lowest valid retrieved level near the station
    st_all = load_json(b / man["stations"])
    diffs = []
    for s_ in st_all:
        if s_["id"].startswith("StickNet") or s_["id"].startswith("PIPS"):
            ser = s_["series"]
            if "wind_speed_ms" not in ser or "wind_dir_deg" not in ser:
                continue
            t = arr(ser["t_s"])
            for m in steps[::3]:
                tm = (datetime.fromisoformat(m["time"].replace("Z", "+00:00")) - t0).total_seconds()
                sel = np.abs(t - tm) <= 30
                if not sel.any():
                    continue
                wsp = np.nanmean(arr(ser["wind_speed_ms"])[sel])
                wdi = np.nanmean(arr(ser["wind_dir_deg"])[sel])
                if not (np.isfinite(wsp) and np.isfinite(wdi)):
                    continue
                ww = np.load(b / m["winds"])
                xi = int(np.argmin(np.abs(ww["x_km"] - s_["x_km"])))
                yi = int(np.argmin(np.abs(ww["y_km"] - s_["y_km"])))
                for kz in (1, 2, 3):
                    if np.isfinite(ww["u"][kz, yi, xi]):
                        diffs.append(
                            (
                                s_["id"],
                                tm,
                                float(np.hypot(ww["u"][kz, yi, xi], ww["v"][kz, yi, xi])),
                                float(wsp),
                                kz * 250,
                            )
                        )
                        break
    if diffs:
        a_ = np.array([d[2] for d in diffs])
        o_ = np.array([d[3] for d in diffs])
        num["winds_vs_surface_wind_speed"] = {
            "n": len(diffs),
            "mean_retrieved_ms": round(float(a_.mean()), 1),
            "mean_station_ms": round(float(o_.mean()), 1),
            "note": "lowest valid level (250 to 750 m ASL) vs the 2-10 m station wind: the retrieved wind is much stronger aloft than at the surface; not a like-for-like comparison",
        }


def radar_vs_pips(b: Path, man, out: Path, num: dict) -> None:
    st = [s for s in load_json(b / man["stations"]) if s["id"].startswith("PIPS")]
    t0 = datetime.fromisoformat(man["window"]["start"].replace("Z", "+00:00"))
    rows = []
    for s in st:
        ser = s["series"]
        if "dbz" not in ser:
            continue
        t = arr(ser["t_s"])
        d = arr(ser["dbz"])
        rr = arr(ser["rain_rate_mmh"])
        pairs = []
        for m in man["steps"]:
            tm = (datetime.fromisoformat(m["time"].replace("Z", "+00:00")) - t0).total_seconds()
            sel = np.abs(t - tm) <= 30
            if not sel.any() or not np.isfinite(d[sel]).any():
                continue
            v = np.load(b / m["volume"])
            xi = round((s["x_km"] + 90) / 0.5)
            yi = round((s["y_km"] + 90) / 0.5)
            col = v["dbz"][:, yi - 1 : yi + 2, xi - 1 : xi + 2]
            low = next((k for k in range(1, 8) if np.isfinite(col[k]).any()), None)
            if low is None:
                continue
            md = float(np.nanmean(col[low]))
            10 ** (md / 10)
            pairs.append(
                (
                    tm,
                    md,
                    float(np.nanmean(d[sel])),
                    float(np.nanmean(rr[sel])),
                    low * 0.25,
                    10 ** (md / 10),
                )
            )
        if pairs:
            a_ = np.array(pairs)
            # radar rain rate from Z = 300 R^1.4 (NEXRAD default)
            rr_radar = (a_[:, 5] / 300.0) ** (1 / 1.4)
            rows.append(
                {
                    "station": s["id"],
                    "n": len(pairs),
                    "lowest_valid_level_km_mean": round(float(a_[:, 4].mean()), 2),
                    "mean_dbz_radar": round(float(a_[:, 1].mean()), 1),
                    "mean_dbz_disdrometer": round(float(a_[:, 2].mean()), 1),
                    "bias_dB": round(float((a_[:, 1] - a_[:, 2]).mean()), 1),
                    "corr_dbz": None
                    if len(pairs) < 4
                    else round(float(np.corrcoef(a_[:, 1], a_[:, 2])[0, 1]), 2),
                    "mean_rain_disdrometer_mmh": round(float(a_[:, 3].mean()), 1),
                    "mean_rain_radar_Z300R1.4_mmh": round(float(rr_radar.mean()), 1),
                }
            )
    num["radar_vs_pips"] = rows


def qc_numbers(b: Path, man, num: dict) -> None:
    n = len(man["steps"])
    size = sum(s.get("volume_bytes", 0) for s in man["steps"])
    num["steps"] = n
    num["offsets_s_per_radar"] = {}
    for s in man["steps"]:
        for r, o in s["offsets_s"].items():
            num["offsets_s_per_radar"].setdefault(r, []).append(o)
    num["offsets_summary"] = {
        r: {
            "n_steps": len(v),
            "mean_abs_s": round(float(np.mean(np.abs(v))), 1),
            "max_abs_s": round(float(np.max(np.abs(v))), 1),
        }
        for r, v in num["offsets_s_per_radar"].items()
    }
    cov = [s["diagnostics"]["voxels_with_echo"] / (41 * 361 * 361) * 100 for s in man["steps"]]
    num["echo_voxel_coverage_pct"] = [
        round(min(cov), 1),
        round(float(np.mean(cov)), 1),
        round(max(cov), 1),
    ]
    num["mean_nradars_where_echo"] = round(
        float(np.mean([s["diagnostics"]["mean_nradars_where_echo"] for s in man["steps"]])), 2
    )
    num["volume_bytes_total"] = size


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--bundle", default="~/Downloads/xradar-exports/perils_movie/bundle")
    ap.add_argument("--out", required=True)
    ap.add_argument("--data", default="~/Downloads/MULTIDOPPLER")
    a = ap.parse_args(argv)
    b = Path(a.bundle).expanduser()
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    man = load_json(b / "manifest.json")
    num: dict = {"_data": str(Path(a.data).expanduser())}
    for fn in (qc_numbers,):
        fn(b, man, num)
    for fn in (
        merged_vs_single,
        flash_map,
        station_figure,
        skewt,
        tornado_panels,
        cross_section,
        winds_figure,
        radar_vs_pips,
    ):
        try:
            fn(b, man, out, num)
        except Exception as exc:
            import traceback

            traceback.print_exc()
            num.setdefault("errors", []).append(f"{fn.__name__}: {exc}")
    num.pop("offsets_s_per_radar", None)
    num.pop("_data")
    (out / "verification.json").write_text(json.dumps(num, indent=1, default=str))
    print(json.dumps(num, indent=1, default=str)[:6000])
    return 0


if __name__ == "__main__":
    sys.exit(main())

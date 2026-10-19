"""science_events.json: data-derived, time-stamped events with the numbers that support them.

Nothing here is invented: every event is computed from the bundle's own files and carries ``evidence`` (the
numbers), the ``class`` of the data it rests on (observed / retrieved / candidate) and a ``caption`` that
states only what the numbers show. Times are ``t_s`` seconds since the window start (negative = before it,
larger than the duration = after it; ``in_window`` says which). If a number cannot be computed from the
data of this window, the event is not written (the ``skipped`` list in the file says why).

Event kinds: ``gust_front_passage`` (station: temperature drop, pressure rise, wind jump), ``gust_front_speed``,
``cold_pool_sounding`` (post-storm vs pre-storm sounding at the same site), ``lma_rate``,
``lightning_near_tornado``, ``tornado_survey``, ``tornado_radar_rotation``, ``hail_signature`` (radar and station
evidence), ``line_motion``, ``updraft_maximum``, ``rear_inflow_candidate``, ``low_level_convergence``,
``low_level_vorticity_maximum``, ``rain_dsd`` (disdrometer drop size distribution and its relation to rain rate).
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import bundle
import numpy as np

UTC = timezone.utc


def _arr(vals) -> np.ndarray:
    return np.array([np.nan if v is None else v for v in vals], float)


def _movavg(a: np.ndarray, n: int) -> np.ndarray:
    if n <= 1:
        return a
    k = np.ones(n) / n
    ok = np.isfinite(a)
    num = np.convolve(np.where(ok, a, 0.0), k, mode="same")
    den = np.convolve(ok.astype(float), k, mode="same")
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0.5, num / den, np.nan)


def gust_front_passage(
    st: dict[str, Any], dt_min_k: float = 1.0, dp_min_hpa: float = 0.4, wind_jump_min: float = 3.0
) -> dict[str, Any] | None:
    """Passage of a gust front / outflow boundary at a fixed station from its own series.

    ``t`` of the passage = time of the largest 2-minute temperature fall, accepted if (a) the fall is at least
    ``dt_min_k`` K, and (b) at least one of: pressure rise of at least ``dp_min_hpa`` hPa within +-3 min, wind speed
    maximum 3 s / 10 s at least ``wind_jump_min`` m/s above the median of the 5 min before. Needs 10 s (or
    finer) data; numbers returned are the raw differences, not model values.
    """
    s = st["series"]
    t = _arr(s["t_s"])
    T = _arr(s.get("temperature_c", []))
    if T.size != t.size or not np.isfinite(T).sum() > 30 or st.get("moving"):
        return None
    dt = float(np.nanmedian(np.diff(t)))
    n = max(1, round(10.0 / dt))
    Ts = _movavg(T, n)
    # temperature change across 2 min (a sharp front) and across 4 min (a gradual one), the stronger relative
    # to its threshold (1.0 K / 2 min or 1.5 K / 4 min) is used
    best = None
    for half_s, thr, label in ((60.0, dt_min_k, "2min"), (120.0, 1.5 * dt_min_k, "4min")):
        h = max(1, round(half_s / dt))
        dd = np.full_like(Ts, np.nan)
        dd[h:-h] = Ts[2 * h :] - Ts[: -2 * h]
        if np.isfinite(dd).any() and np.nanmin(dd) <= -thr:
            score = -float(np.nanmin(dd)) / thr
            if best is None or score > best[0]:
                best = (score, dd, label)
    if best is None:
        return None
    _score, d2, window_label = best
    k = int(np.nanargmin(d2))
    # a sharp step makes a plateau of equal 2-minute differences: take the middle of the contiguous plateau (>= 95 % of the fall)
    lo = k
    while lo - 1 >= 0 and np.isfinite(d2[lo - 1]) and d2[lo - 1] <= 0.95 * d2[k]:
        lo -= 1
    hi = k
    while hi + 1 < d2.size and np.isfinite(d2[hi + 1]) and d2[hi + 1] <= 0.95 * d2[k]:
        hi += 1
    tp = float(0.5 * (t[lo] + t[hi]))
    out: dict[str, Any] = {
        "t_s": round(tp, 1),
        "dT_2min_K": round(float(d2[k]), 2),
        "dT_window": window_label,
    }
    # pressure
    P = _arr(s.get("pressure_hpa", np.full(t.size, np.nan)))
    if np.isfinite(P).sum() > 30:
        Ps = _movavg(P, n)
        w = (t >= tp - 180) & (t <= tp + 180)
        a, b = Ps[(t >= tp - 240) & (t <= tp - 60)], Ps[(t >= tp + 60) & (t <= tp + 240)]
        if np.isfinite(a).any() and np.isfinite(b).any():
            out["dP_hPa"] = round(float(np.nanmax(Ps[w]) - np.nanmean(a)), 2)
            out["P_rise_after_hPa"] = round(float(np.nanmean(b) - np.nanmean(a)), 2)
    # wind
    ws = _arr(s.get("wind_speed_ms", np.full(t.size, np.nan)))
    gu = _arr(s.get("wind_gust_ms", np.full(t.size, np.nan)))
    wspd = np.where(np.isfinite(gu), gu, ws)
    if np.isfinite(wspd).sum() > 30:
        pre = wspd[(t >= tp - 420) & (t <= tp - 90)]
        near = wspd[(t >= tp - 90) & (t <= tp + 150)]
        if np.isfinite(pre).any() and np.isfinite(near).any():
            out["wind_before_median_ms"] = round(float(np.nanmedian(pre)), 1)
            out["wind_peak_ms"] = round(float(np.nanmax(near)), 1)
            out["wind_jump_ms"] = round(float(np.nanmax(near) - np.nanmedian(pre)), 1)
        wd = _arr(s.get("wind_dir_deg", np.full(t.size, np.nan)))
        if np.isfinite(wd).sum() > 30:

            def circ_mean(x):
                x = x[np.isfinite(x)]
                return (
                    float(
                        (
                            np.degrees(
                                np.arctan2(
                                    np.mean(np.sin(np.radians(x))), np.mean(np.cos(np.radians(x)))
                                )
                            )
                            + 360
                        )
                        % 360
                    )
                    if x.size
                    else float("nan")
                )

            b = circ_mean(wd[(t >= tp - 300) & (t <= tp - 90)])
            a = circ_mean(wd[(t >= tp + 90) & (t <= tp + 300)])
            if math.isfinite(a) and math.isfinite(b):
                out["wind_dir_before_deg"], out["wind_dir_after_deg"] = round(b, 0), round(a, 0)
                out["wind_shift_deg"] = round(float((a - b + 540) % 360 - 180), 0)
    te = _arr(s.get("theta_e_k", np.full(t.size, np.nan)))
    if np.isfinite(te).sum() > 30:
        b = te[(t >= tp - 300) & (t <= tp - 90)]
        a = te[(t >= tp + 90) & (t <= tp + 300)]
        if np.isfinite(a).any() and np.isfinite(b).any():
            out["theta_e_change_K"] = round(float(np.nanmean(a) - np.nanmean(b)), 1)
    evid = ("dP_hPa" in out and out["dP_hPa"] >= dp_min_hpa) or (
        "wind_jump_ms" in out and out["wind_jump_ms"] >= wind_jump_min
    )
    if not evid:
        return None
    out.update(
        {
            "station": st["id"],
            "lat": st["lat"],
            "lon": st["lon"],
            "x_km": st.get("x_km"),
            "y_km": st.get("y_km"),
        }
    )
    return out


def build_all(c) -> None:
    out_dir: Path = c.out
    ev: list[dict[str, Any]] = []
    skipped: list[str] = []
    dur = c.duration

    def add(kind: str, t_s: float, caption: str, evidence: dict[str, Any], cls: str, **extra):
        e = {
            "id": f"{kind}_{len(ev):03d}",
            "kind": kind,
            "t_s": round(float(t_s), 1),
            "in_window": bool(0 <= t_s <= dur),
            "class": cls,
            "caption": caption,
            "evidence": evidence,
        }
        e.update(extra)
        ev.append(e)

    plans = [json.loads(p.read_text()) for p in sorted(c.work.glob("step_*.json"))]
    times = [
        (datetime.fromisoformat(m["time"].replace("Z", "+00:00")) - c.start).total_seconds()
        for m in plans
    ]
    motion = (
        json.loads((c.work / "motion.json").read_text())
        if (c.work / "motion.json").exists()
        else None
    )

    # ---------------------------------------------------------------- line motion
    tobac_p = out_dir / "context" / "tobac_track.json"
    tobac = json.loads(tobac_p.read_text()) if tobac_p.exists() else None
    if motion:
        evid = {
            k: motion[k]
            for k in (
                "u_ms",
                "v_ms",
                "speed_ms",
                "toward_deg",
                "correlation",
                "dt_s",
                "times",
                "method",
            )
        }
        cap = f"The echo pattern moves toward {motion['toward_deg']:.0f} deg (from {motion['from_deg']:.0f} deg) at {motion['speed_ms']:.1f} m/s."
        if tobac and tobac.get("centroid_motion"):
            tm = tobac["centroid_motion"]
            evid["tobac_centroid_motion"] = tm
            cap += f" The tobac track of the whole echo area (data owner's tracking) gives {tm['speed_ms']:.1f} m/s toward {tm['toward_deg']:.0f} deg."
        add("line_motion", 0.0, cap, evid, "retrieved")

    # ---------------------------------------------------------------- stations: gust fronts, rain DSD
    sj = out_dir / "stations" / "stations.json"
    stations = json.loads(sj.read_text()) if sj.exists() else []
    passes = []
    for st in stations:
        try:
            g = gust_front_passage(st)
        except Exception as exc:
            skipped.append(f"gust front {st['id']}: {exc}")
            continue
        if g:
            passes.append(g)
            parts = [
                f"temperature fell {abs(g['dT_2min_K']):.1f} K in {g.get('dT_window', '2min').replace('min', ' min')}"
            ]
            if "dP_hPa" in g:
                parts.append(f"pressure rose {g['dP_hPa']:.1f} hPa")
            if "wind_peak_ms" in g:
                parts.append(
                    f"wind peaked at {g['wind_peak_ms']:.0f} m/s (before: {g['wind_before_median_ms']:.0f})"
                )
            add(
                "gust_front_passage",
                g["t_s"],
                f"Outflow boundary at {st['id']}: " + ", ".join(parts) + ".",
                g,
                "observed",
                station=st["id"],
                lat=st["lat"],
                lon=st["lon"],
            )
    if len(passes) >= 3 and motion:
        ex, ey = (
            math.sin(math.radians(motion["toward_deg"])),
            math.cos(math.radians(motion["toward_deg"])),
        )
        pts = [
            (p["x_km"] * ex + p["y_km"] * ey, p["t_s"]) for p in passes if p.get("x_km") is not None
        ]
        if len(pts) >= 3:
            s_ = np.array([q[0] for q in pts])
            t_ = np.array([q[1] for q in pts])
            if np.ptp(s_) > 10 and np.ptp(t_) > 60:
                A = np.vstack([t_, np.ones_like(t_)]).T
                sl, ic = np.linalg.lstsq(A, s_, rcond=None)[0]
                res = s_ - (sl * t_ + ic)
                rms = float(np.sqrt(np.mean(res**2)))
                if rms <= 3.0 and 2.0 <= sl * 1000 <= 40.0:
                    add(
                        "gust_front_speed",
                        float(np.mean(t_)),
                        f"Passage times at {len(pts)} stations give an outflow-boundary speed of {sl * 1000:.1f} m/s along the pattern motion (rms residual {rms:.1f} km).",
                        {
                            "speed_ms": round(float(sl * 1000), 1),
                            "n_stations": len(pts),
                            "rms_residual_km": round(rms, 2),
                            "stations": [p["station"] for p in passes if p.get("x_km") is not None],
                        },
                        "observed",
                    )
                else:
                    skipped.append(
                        f"gust_front_speed: the {len(pts)} passage times do not line up on one boundary "
                        f"(fit {sl * 1000:.1f} m/s, rms residual {rms:.1f} km > 3 km): not written"
                    )
    else:
        skipped.append(
            f"gust_front_speed: only {len(passes)} stations with a clear passage in the data around the window (need 3)"
        )
    # rain / DSD per PIPS
    for st in stations:
        if (
            st["id"].startswith("PIPS")
            and "dm_mm" in st["series"]
            and "rain_rate_mmh" in st["series"]
        ):
            t = _arr(st["series"]["t_s"])
            m = (t >= 0) & (t <= dur)
            rr, dm, nw = (
                _arr(st["series"]["rain_rate_mmh"])[m],
                _arr(st["series"]["dm_mm"])[m],
                _arr(st["series"].get("log10_nw", np.full(t.size, np.nan)))[m],
            )
            ok = np.isfinite(rr) & np.isfinite(dm)
            if ok.sum() > 20:
                cc = (
                    float(np.corrcoef(rr[ok], dm[ok])[0, 1])
                    if np.std(rr[ok]) > 0 and np.std(dm[ok]) > 0
                    else None
                )
                third = ok.sum() // 3
                idx = np.nonzero(ok)[0]
                first, last = idx[:third], idx[-third:]
                add(
                    "rain_dsd",
                    float(np.mean(t[m])),
                    f"{st['id']}: rain {np.nanmin(rr[ok]):.0f}-{np.nanmax(rr[ok]):.0f} mm/h, Dm {np.nanmin(dm[ok]):.1f}-{np.nanmax(dm[ok]):.1f} mm in the window.",
                    {
                        "station": st["id"],
                        "rain_rate_mmh_min_max_mean": [
                            round(float(np.nanmin(rr[ok])), 1),
                            round(float(np.nanmax(rr[ok])), 1),
                            round(float(np.nanmean(rr[ok])), 1),
                        ],
                        "dm_mm_min_max_mean": [
                            round(float(np.nanmin(dm[ok])), 2),
                            round(float(np.nanmax(dm[ok])), 2),
                            round(float(np.nanmean(dm[ok])), 2),
                        ],
                        "log10_nw_mean": None
                        if not np.isfinite(nw).any()
                        else round(float(np.nanmean(nw)), 2),
                        "corr_dm_rain_rate": None if cc is None else round(cc, 2),
                        "dm_mean_first_third_mm": round(float(np.nanmean(dm[first])), 2),
                        "dm_mean_last_third_mm": round(float(np.nanmean(dm[last])), 2),
                        "rain_mean_first_third_mmh": round(float(np.nanmean(rr[first])), 1),
                        "rain_mean_last_third_mmh": round(float(np.nanmean(rr[last])), 1),
                        "accumulation_mm": st.get("rain_accum_window_mm"),
                    },
                    "observed",
                    station=st["id"],
                )
    # hail evidence at stations
    hj = out_dir / "stations" / "hail_events.json"
    hail_st = json.loads(hj.read_text()) if hj.exists() else []
    inwin = [h for h in hail_st if 0 <= h["t_s"] <= dur]
    for st in stations:
        if st["id"] == "RaDAPS_WXT" and "hail_intensity_hits_cm2h" in st["series"]:
            a = _arr(st["series"]["hail_intensity_hits_cm2h"])
            if np.nanmax(a) > 0:
                add(
                    "hail_signature",
                    float(_arr(st["series"]["t_s"])[int(np.nanargmax(a))]),
                    f"RaDAPS hail sensor registered hits (max {np.nanmax(a):.0f} hits/cm2/h).",
                    {"station": st["id"], "max_hits_cm2h": float(np.nanmax(a))},
                    "observed",
                    station=st["id"],
                )
    pips_dmax = {
        st["id"]: st.get("large_particles", {}).get("max_diameter_in_window_mm")
        for st in stations
        if st["id"].startswith("PIPS")
    }
    if not inwin:
        skipped.append(
            f"hail at disdrometers: no particle of 8 mm or more (hail_candidate) in the window; largest diameter seen per PIPS {pips_dmax}"
        )
    else:
        for h in inwin:
            add(
                "hail_signature",
                h["t_s"],
                f"{h['station']}: {h['n_large']} particles of 8 mm or more in 10 s (largest {h['max_d_mm']} mm).",
                h,
                "candidate",
                station=h["station"],
            )

    # ---------------------------------------------------------------- lightning
    lj = out_dir / "lma" / "flashes.json"
    L = None
    if lj.exists():
        L = json.loads(lj.read_text())
        fl = L["flashes"]
        if fl:
            starts = np.array([f["t_start_s"] for f in fl])
            nmin = math.ceil(dur / 60.0)
            per_min = [
                int(((starts >= 60 * k) & (starts < 60 * (k + 1))).sum()) for k in range(nmin)
            ]
            alts = np.array([f["centroid"][2] for f in fl])
            add(
                "lma_rate",
                dur / 2.0,
                f"LMA flashes (at least {L.get('min_flash_sources', 2)} sources) in the domain: {sum(per_min)} in {dur / 60:.0f} min, {min(per_min)} to {max(per_min)} per minute; sources per minute {min(L['sources_per_minute_in_box'])}-{max(L['sources_per_minute_in_box'])}.",
                {
                    "flashes_per_minute": per_min,
                    "sources_per_minute": L["sources_per_minute_in_box"],
                    "n_flashes": len(fl),
                    "n_sources": L["n_sources"],
                    "median_flash_centroid_altitude_km": round(float(np.median(alts)) / 1000, 1),
                    "params": L["params"],
                },
                "observed",
            )
    # ---------------------------------------------------------------- tornado: survey + radar
    tj = out_dir / "tornadoes" / "tornadoes.json"
    tors = json.loads(tj.read_text()) if tj.exists() else []
    rt = (
        json.loads((out_dir / "context" / "rotation_tracks.json").read_text())
        if (out_dir / "context" / "rotation_tracks.json").exists()
        else None
    )
    for t in tors:
        if not (t["overlaps_window"] and t.get("in_domain")):
            continue
        t0 = datetime.fromisoformat(t["start"].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(t["end"].replace("Z", "+00:00"))
        ts = (t0 - c.start).total_seconds()
        add(
            "tornado_survey",
            ts,
            f"{t['rating']} tornado (damage survey): begins {t['start'][11:19]} UTC at {t['begin']['lat']:.3f} N {abs(t['begin']['lon']):.3f} W, ends {t['end'][11:19]} UTC; length {t['length_km_from_endpoints']:.1f} km (endpoints), width {t['width_m'] or 'n/a'} m.",
            {
                k: t[k]
                for k in (
                    "id",
                    "rating",
                    "start",
                    "end",
                    "begin",
                    "end_point",
                    "width_m",
                    "length_km_from_endpoints",
                    "max_wind_mph",
                    "wind_note",
                    "path_kind",
                    "source",
                )
            },
            "observed (survey)",
            tornado=t["id"],
        )
        if L is not None and L["flashes"]:
            from tornadoes import position_at

            t_end_s = (t1 - c.start).total_seconds()
            near, per_min = [], np.zeros(math.ceil(dur / 60.0))
            for f in L["flashes"]:
                if not (ts <= f["t_start_s"] <= t_end_s):
                    continue
                pos = position_at(t, f["t_start_s"])
                if pos is None:
                    continue
                d = math.hypot(
                    (f["centroid"][0] - pos[0]) * 111.2,
                    (f["centroid"][1] - pos[1]) * 111.2 * math.cos(math.radians(pos[0])),
                )
                if d <= 15.0:
                    near.append(d)
                    per_min[min(int(f["t_start_s"] // 60), per_min.size - 1)] += 1
            life_min = max((t_end_s - ts) / 60.0, 1e-9)
            add(
                "lightning_near_tornado",
                ts,
                f"{len(near)} LMA flashes started within 15 km of the surveyed tornado position during its {life_min:.0f} min life ({len(near) / life_min:.1f} per minute).",
                {
                    "tornado": t["id"],
                    "n_flashes_within_15km": len(near),
                    "per_minute": [int(v) for v in per_min],
                    "median_distance_km": round(float(np.median(near)), 1) if near else None,
                    "note": "the position is the interpolated survey track; the flash position is the centroid of its LMA sources",
                },
                "observed",
                tornado=t["id"],
            )
        add(
            "tornado_survey",
            (t1 - c.start).total_seconds(),
            f"{t['rating']} tornado ends ({t['end'][11:19]} UTC).",
            {"id": t["id"]},
            "observed (survey)",
            tornado=t["id"],
        )
        if rt and t["id"] in rt.get("survey_comparison", {}):
            cm = rt["survey_comparison"][t["id"]]
            if cm["n_steps_detected"]:
                best = min((r for r in cm["rows"] if r["detected"]), key=lambda r: r["distance_km"])
                add(
                    "tornado_radar_rotation",
                    best["t_s"],
                    f"Radar-retrieved low-level rotation (candidate) within {best['distance_km']:.1f} km of the surveyed tornado position: azimuthal shear {best['peak_as']:.3f} s-1"
                    + (
                        f", rotational velocity {best['vrot_ms']:.0f} m/s"
                        if best.get("vrot_ms")
                        else ""
                    )
                    + f" ({best.get('radar')}). Detected at {cm['n_steps_detected']} of {cm['n_steps_with_survey_position']} steps.",
                    {
                        "best_row": best,
                        "n_steps_detected": cm["n_steps_detected"],
                        "n_steps": cm["n_steps_with_survey_position"],
                        "distance_km_median": cm.get("distance_km_median"),
                        "time_shift_s": cm.get("time_shift_s"),
                        "mean_distance_at_best_shift_km": cm.get("mean_distance_at_best_shift_km"),
                        "note": cm["note"],
                    },
                    "retrieved",
                    tornado=t["id"],
                )
            else:
                skipped.append(
                    f"tornado_radar_rotation {t['id']}: no rotation candidate (>= {cm['min_peak_as']} s-1) within {cm['search_km']} km of the surveyed track"
                )
        # debris flag near the track
        tds_rows = []
        for m, tt in zip(plans, times, strict=True):
            if not (ts - 120 <= tt <= (t1 - c.start).total_seconds() + 120):
                continue
            try:
                z = np.load(out_dir / m["volume"])
            except Exception:
                continue
            tds = z["tds"]
            from tornadoes import position_at

            pos = position_at(t, tt)
            if pos is None:
                continue
            x, y = c.xy_km(pos[0], pos[1])
            ii = round((float(x) + 90) / 0.5)
            jj = round((float(y) + 90) / 0.5)
            r = 10  # 5 km
            sub = tds[:, max(jj - r, 0) : jj + r + 1, max(ii - r, 0) : ii + r + 1]
            rho = z["rho"][:, max(jj - r, 0) : jj + r + 1, max(ii - r, 0) : ii + r + 1].astype(
                np.float32
            )
            dbz = z["dbz"][:, max(jj - r, 0) : jj + r + 1, max(ii - r, 0) : ii + r + 1]
            sel = sub >= 1
            tds_rows.append(
                {
                    "t_s": tt,
                    "voxels_flag_ge1": int(sel.sum()),
                    "voxels_flag2": int((sub == 2).sum()),
                    "min_rho_in_flag": None
                    if not sel.any()
                    else round(float(np.nanmin(rho[sel])), 2),
                    "max_dbz_in_flag": None
                    if not sel.any()
                    else round(float(np.nanmax(dbz[sel])), 1),
                }
            )
        if tds_rows:
            tot = sum(r["voxels_flag_ge1"] for r in tds_rows)
            add(
                "hail_signature",
                ts,
                f"Low-RHO / high-Z (debris-like or hail) voxels within 5 km of the surveyed track: {tot} voxel-steps ({sum(r['voxels_flag2'] for r in tds_rows)} also near a rotation maximum). Evidence only.",
                {
                    "tornado": t["id"],
                    "rows": tds_rows,
                    "criteria": "RHO < 0.8, Z >= 30 dBZ, below 2 km (flag 2: within 2 km of azimuthal shear >= 0.012 s-1)",
                    "caveat": "large drops, hail and X-band resonance also lower RHO",
                },
                "candidate",
                tornado=t["id"],
                subkind="tds_candidate",
            )

    # ---------------------------------------------------------------- hail and convection from the radar volumes
    best = None
    for m, tt in zip(plans, times, strict=True):
        d = m.get("diagnostics") or {}
        if d.get("mesh_max_mm") is not None and (best is None or d["mesh_max_mm"] > best[1]):
            best = (tt, d["mesh_max_mm"], d, m)
    if best:
        tt, mm, d, m = best
        add(
            "hail_signature",
            tt,
            f"Largest radar-estimated hail size in the window: MESH {mm:.0f} mm (POSH {d.get('posh_max_pct') or 0:.0f} %), VIL up to {d.get('vil_max_kgm2') or 0:.0f} kg/m2, hail / high-density graupel class in {d.get('hail_class_voxels', 0)} voxels.",
            {
                "mesh_max_mm": mm,
                "posh_max_pct": d.get("posh_max_pct"),
                "vil_max_kgm2": d.get("vil_max_kgm2"),
                "zdr_column_max_km": d.get("zdr_column_max_km"),
                "cmax_dbz": d.get("cmax_dbz"),
                "hail_class_voxels": d.get("hail_class_voxels"),
                "step": m["index"],
                "caveat": "MESH is an upper estimate of the largest stone (Witt 1998), uses a sounding about 100 min old and a grid that ends at 10 km; hail class: X band has no hail class",
            },
            "retrieved",
            subkind="mesh",
        )
    # ZDR column and convection time series
    zs = [
        (tt, (m.get("diagnostics") or {}).get("zdr_column_max_km"))
        for m, tt in zip(plans, times, strict=True)
    ]
    zs = [z for z in zs if z[1] is not None]
    if zs:
        tmax, vmax = max(zs, key=lambda z: z[1])
        add(
            "hail_signature",
            tmax,
            f"Deepest ZDR column (ZDR >= 1 dB above the freezing level): {vmax:.1f} km (an updraft / large-drop indicator).",
            {"zdr_column_max_km_series": [[round(a, 0), b] for a, b in zs]},
            "retrieved",
            subkind="zdr_column",
        )

    # ---------------------------------------------------------------- winds: updraft, rear inflow, convergence, vorticity
    wsteps = [(m, tt) for m, tt in zip(plans, times, strict=True) if m.get("winds")]
    if wsteps and motion:
        ex, ey = motion["u_ms"], motion["v_ms"]
        sp = math.hypot(ex, ey) or 1.0
        ex, ey = ex / sp, ey / sp
        best_w = best_rij = best_conv = best_vort = None
        for m, tt in wsteps:
            w = np.load(out_dir / m["winds"])
            x, y, z = w["x_km"], w["y_km"], w["z_km"]
            vol = np.load(out_dir / m["volume"])
            X, Y = np.meshgrid(x, y)
            # only cells where the analysis is well constrained and inside the domain (not at its edge), under echo:
            # error <= 3 m/s, beam crossing >= 45 deg, at least 3 radars, |x|,|y| <= 75 km, column max >= 30 dBZ
            cm_ = vol["cmax"][::2, ::2]
            rel = (
                (w["uv_error"].astype(np.float32) <= 3.0)
                & (w["crossing_deg"] >= 45.0)
                & (w["nradars"] >= 3)
                & ((np.abs(X) <= 75.0) & (np.abs(Y) <= 75.0) & (cm_ >= 30.0))[None]
            )
            u = np.where(rel, w["u"], np.nan)
            v = np.where(rel, w["v"], np.nan)
            ww = np.where(rel, w["w"], np.nan)
            lo = (z >= 1.0) & (z <= 8.0)
            if np.isfinite(ww[lo]).any():
                k = np.nanargmax(np.where(np.isfinite(ww), ww, -np.inf)[lo])
                kk, jj, ii = np.unravel_index(k, ww[lo].shape)
                val = float(ww[lo][kk, jj, ii])
                if best_w is None or val > best_w["w_ms"]:
                    best_w = {
                        "w_ms": round(val, 1),
                        "t_s": tt,
                        "z_km": round(float(z[lo][kk]), 2),
                        "x_km": float(x[ii]),
                        "y_km": float(y[jj]),
                        "step": m["index"],
                    }
            # rear inflow candidate: along-motion wind component at 1-4 km behind the convective line
            conv = vol["conv_strat"][::2, ::2]
            core = (cm_ >= 45) & (conv == 2)
            if core.sum() > 50:
                s_core = X[core] * ex + Y[core] * ey
                lead = float(np.percentile(s_core, 90))
                s_grid = X * ex + Y * ey
                behind = (s_grid <= lead - 5.0) & (s_grid >= lead - 40.0)
                ua = u * ex + v * ey
                zr = (z >= 1.0) & (z <= 4.0)
                cand = np.where(behind[None] & zr[:, None, None] & np.isfinite(ua), ua, -np.inf)
                if np.isfinite(cand).any() and cand.max() > -np.inf:
                    k = int(np.argmax(cand))
                    kk, jj, ii = np.unravel_index(k, cand.shape)
                    val = float(cand[kk, jj, ii])
                    rec = {
                        "along_motion_wind_ms": round(val, 1),
                        "storm_relative_ms": round(val - sp, 1),
                        "z_km": round(float(z[kk]), 2),
                        "x_km": float(x[ii]),
                        "y_km": float(y[jj]),
                        "distance_behind_leading_core_km": round(lead - float(s_grid[jj, ii]), 1),
                        "t_s": tt,
                        "step": m["index"],
                    }
                    if best_rij is None or val > best_rij["along_motion_wind_ms"]:
                        best_rij = rec
            # low-level convergence and vorticity
            low = z <= 1.0
            dv = np.where(rel, w["divergence"].astype(np.float32), np.nan)[low]
            if np.isfinite(dv).any():
                val = float(np.nanmin(dv))
                if best_conv is None or val < best_conv["divergence_s-1"]:
                    kk, jj, ii = np.unravel_index(
                        int(np.nanargmin(np.where(np.isfinite(dv), dv, np.inf))), dv.shape
                    )
                    best_conv = {
                        "divergence_s-1": round(val, 5),
                        "x_km": float(x[ii]),
                        "y_km": float(y[jj]),
                        "z_km": float(z[low][kk]),
                        "t_s": tt,
                        "step": m["index"],
                    }
            vo = np.where(rel, w["vorticity"].astype(np.float32), np.nan)[z <= 2.0]
            if np.isfinite(vo).any():
                val = float(np.nanmax(vo))
                if best_vort is None or val > best_vort["vorticity_s-1"]:
                    kk, jj, ii = np.unravel_index(
                        int(np.nanargmax(np.where(np.isfinite(vo), vo, -np.inf))), vo.shape
                    )
                    best_vort = {
                        "vorticity_s-1": round(val, 5),
                        "x_km": float(x[ii]),
                        "y_km": float(y[jj]),
                        "z_km": float(z[z <= 2.0][kk]),
                        "t_s": tt,
                        "step": m["index"],
                    }
        cav = (
            "RETRIEVED multi-Doppler winds on a 1 km grid; the variational solver did not fully converge; w is the least "
            "reliable component; only cells with u,v error <= 3 m/s, beam crossing >= 45 deg, >= 3 radars, inside |x|,|y| <= 75 km "
            "and under echo >= 30 dBZ are searched"
        )
        if best_w:
            add(
                "updraft_maximum",
                best_w["t_s"],
                f"Strongest retrieved updraft between 1 and 8 km: w = {best_w['w_ms']:.0f} m/s at {best_w['z_km']:.1f} km (x {best_w['x_km']:.0f}, y {best_w['y_km']:.0f} km).",
                {**best_w, "caveat": cav},
                "retrieved",
            )
        if best_rij:
            add(
                "rear_inflow_candidate",
                best_rij["t_s"],
                f"Strongest wind component toward the line motion at 1-4 km height, {best_rij['distance_behind_leading_core_km']:.0f} km behind the leading convective core: {best_rij['along_motion_wind_ms']:.0f} m/s ({best_rij['storm_relative_ms']:+.0f} m/s relative to the pattern motion) at {best_rij['z_km']:.1f} km. A candidate for rear inflow, not a proven jet.",
                {
                    **best_rij,
                    "caveat": cav
                    + "; 'behind' = along-motion coordinate 5 to 40 km behind the 90th percentile of the 45 dBZ convective cores",
                },
                "retrieved",
            )
        if best_conv:
            add(
                "low_level_convergence",
                best_conv["t_s"],
                f"Strongest low-level (below 1 km) convergence: {best_conv['divergence_s-1'] * 1000:.1f}e-3 s-1 at x {best_conv['x_km']:.0f}, y {best_conv['y_km']:.0f} km.",
                {**best_conv, "caveat": cav},
                "retrieved",
            )
        if best_vort:
            add(
                "low_level_vorticity_maximum",
                best_vort["t_s"],
                f"Strongest low-level (below 2 km) vertical vorticity of the 1 km wind analysis: {best_vort['vorticity_s-1'] * 1000:.1f}e-3 s-1 at x {best_vort['x_km']:.0f}, y {best_vort['y_km']:.0f} km (resolution 1 km: a tornado vortex is not resolved).",
                {**best_vort, "caveat": cav},
                "retrieved",
            )
    elif not wsteps:
        skipped.append("wind events: no winds in the bundle")

    # ---------------------------------------------------------------- cold pool from paired soundings
    sidx = out_dir / "soundings" / "index.json"
    if sidx.exists():
        S = json.loads(sidx.read_text())
        by_site: dict[str, list[dict[str, Any]]] = {}
        for s in S:
            if s["id"].startswith("UIUC_"):
                by_site.setdefault(s["site"], []).append(s)
        for site, lst in by_site.items():
            lst.sort(key=lambda s: s["launch"])
            pre = [s for s in lst if s["launch_t_s"] < -2400]
            post = [s for s in lst if s["launch_t_s"] > dur + 600]
            if not pre or not post:
                continue
            p0, p1 = pre[-1], post[0]
            try:
                import csv

                def prof(s):
                    rows = list(csv.DictReader((out_dir / s["profile_csv"]).open()))
                    z = np.array(
                        [float(r["height_asl_m"]) if r["height_asl_m"] else np.nan for r in rows]
                    )
                    T = np.array(
                        [float(r["temperature_c"]) if r["temperature_c"] else np.nan for r in rows]
                    )
                    Td = np.array(
                        [float(r["dewpoint_c"]) if r["dewpoint_c"] else np.nan for r in rows]
                    )
                    ok = np.isfinite(z) & np.isfinite(T)
                    return z[ok], T[ok], Td[ok]

                za, Ta, _Tda = prof(p0)
                zb, Tb, _Tdb = prof(p1)
                g = np.arange(100.0, 3001.0, 50.0) + max(za[0], zb[0])
                dTz = np.interp(g, zb, Tb) - np.interp(g, za, Ta)
                s0 = max(za[0], zb[0])
                depth = None
                cold = dTz <= -1.0
                if cold.any():
                    # depth of the contiguous cold layer from the surface: first height where the difference is above -1 K
                    k = int(np.argmax(~cold)) if (~cold).any() else len(g) - 1
                    depth = float(g[k] - s0) if (~cold).any() else None
                add(
                    "cold_pool_sounding",
                    p1["launch_t_s"],
                    f"{site}: sounding at {p1['launch'][11:16]} UTC vs {p0['launch'][11:16]} UTC (same site): surface-layer temperature {np.mean(dTz[:6]):+.1f} K"
                    + (f", the layer colder by 1 K or more is {depth:.0f} m deep" if depth else "")
                    + ".",
                    {
                        "site": site,
                        "pre": p0["id"],
                        "post": p1["id"],
                        "mean_dT_100_350m_K": round(float(np.mean(dTz[:6])), 2),
                        "mean_dT_100_1000m_K": round(float(np.mean(dTz[:19])), 2),
                        "cold_layer_depth_m_ge1K": None if depth is None else round(depth, 0),
                        "note": "the balloons drift (several km) and are 25-35 min and 2.3 h apart: the difference mixes the cold pool with the daily and synoptic change",
                    },
                    "observed",
                )
            except Exception as exc:
                skipped.append(f"cold pool {site}: {exc}")
    bundle.write_json(
        out_dir / "science_events.json",
        bundle.clean_nan(
            {
                "events": sorted(ev, key=lambda e: e["t_s"]),
                "skipped": skipped,
                "window_duration_s": dur,
                "note": "every number is computed from the files of this bundle; classes: observed / retrieved / candidate",
            }
        ),
        indent=1,
    )
    c.log(f"events: {len(ev)} events, {len(skipped)} skipped")

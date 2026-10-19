#!/usr/bin/env python
"""Build the PERiLS IOP2 data bundle (SPEC v1 plus additions) from the maintainer's data folder.

Run with the sidecar's Python (it needs the engine and radarx)::

    python/.venv/bin/python tools/perils_movie/build_bundle.py \
        --window 2022-03-31T01:04:00Z 2022-03-31T01:20:00Z \
        --out ~/Downloads/xradar-exports/perils_movie/bundle

Idempotent and resumable: a stage or a step whose output exists is skipped (``--force`` redoes it,
``--stages`` chooses stages). Source data are only read (nothing is written under ``--data``). No network.
Stages, in order: soundings, volumes (merged volume + rotation + hail + convection + winds per step),
lma, stations, tornadoes, context, profiles, events, manifest. See ``docs/PERILS_BUNDLE.md``.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "python"))
sys.path.insert(0, str(HERE))

import bundle
import context
import events
import lma
import merge
import pgrid
import profiles
import soundings
import stations
import tornadoes
import tracks
import volumes
import winds

UTC = timezone.utc
ALL_RADARS = [
    "COW1",
    "DOW7",
    "DOW8",
    "KBMX",
    "KGWX",
    "NOXP",
    "SMARTR2",
    "UAH",
]  # alphabetical: the *_src codes
STAGES = [
    "soundings",
    "volumes",
    "arc",
    "lma",
    "stations",
    "tornadoes",
    "couplet",
    "context",
    "profiles",
    "events",
    "manifest",
]
ORIGIN = (33.575864857, -88.54851899)  # lat, lon of the grids


def parse_time(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    if len(s) <= 5:  # HH:MM on the default day
        return datetime.fromisoformat(f"2022-03-31T{s}:00+00:00")
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


class Ctx:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.start, self.end = parse_time(a.window[0]), parse_time(a.window[1])
        self.out = Path(a.out).expanduser()
        self.data = Path(a.data).expanduser()
        self.work = self.out / "_work"
        self.proj = pgrid.projection(*ORIGIN)
        self.log = lambda *m: print(time.strftime("%H:%M:%S"), *m, flush=True)
        self.duration = (self.end - self.start).total_seconds()
        self.env: volumes.Env | None = None
        self.sondes_info: list[dict[str, Any]] = []

    def done(self, rel: str) -> bool:
        return (self.out / rel).exists() and not self.a.force

    def xy_km(self, lat, lon):
        return pgrid.xy_km(self.proj, lat, lon)


# ---------------------------------------------------------------------------
# soundings
# ---------------------------------------------------------------------------


def find_sonde_files(data: Path) -> list[tuple[str, Path]]:
    base = data / "soundings"
    out: list[tuple[str, Path]] = []
    for f in sorted(
        (base / "non-radar_QC_Illinois" / "20220330_IOP02").glob("SONDE*/sounding/L1/*.eol")
    ):
        out.append(("uiuc", f))
    for f in sorted((base / "UAH_sounding").glob("upperair.UAH_Sonde.2022033*")):
        if "SUMMARY" not in f.name:
            out.append(("uah", f))
    for f in sorted((base / "ULM").glob("upperair.ULM_sonde.2022033*.txt")):
        if "SHARPPY" not in f.name and "SUMMARY" not in f.name:
            out.append(("ulm", f))
    for f in sorted((base / "WYO_SOUNDING").glob("2022033*_*.csv")):
        out.append(("wyo", f))
    return [(k, f) for k, f in out if not f.name.startswith(".")]  # macOS "._" resource-fork stubs


def stage_soundings(c: Ctx) -> None:
    sdir = c.out / "soundings"
    idx_path = sdir / "index.json"
    lo, hi = c.start - timedelta(hours=4), c.end + timedelta(hours=1, minutes=30)
    index = []
    sondes: dict[str, soundings.Sonde] = {}
    for kind, f in find_sonde_files(c.data):
        try:
            s = {
                "uiuc": soundings.read_uiuc_eol,
                "uah": soundings.read_uah_ulm,
                "ulm": soundings.read_uah_ulm,
                "wyo": soundings.read_wyo,
            }[kind](f)
        except Exception as exc:  # unreadable file: say so, go on
            c.log(f"  sounding {f.name}: skipped ({str(exc)[:80]})")
            continue
        if not (lo <= s.launch <= hi):
            continue
        sondes[s.id] = s
    c.log(f"soundings: {len(sondes)} launches in [{lo:%H:%M}, {hi:%H:%M}] UTC")
    # environment profile for the hail products: the operational 00 UTC sounding nearest to the storm (full depth)
    full = [s for s in sondes.values() if s.id.startswith("WYO_BMX_20220331_0000")]
    if not full:
        full = [s for s in sondes.values() if s.id.startswith("WYO_") and s.id.endswith("_0000")]
    if full:
        h, t, fz, m20 = soundings.environment_profile(full[0])
        c.env = volumes.Env(
            h,
            t,
            fz,
            m20,
            f"{full[0].id}: operational radiosonde, nominal 00 UTC (balloon left about 23:15 UTC), 150 km east of the storm",
        )
    else:
        raise SystemExit("no full-depth sounding for the freezing level")
    if c.done("soundings/index.json") and not c.a.force:
        c.log("  index exists, skipping profiles")
        c.sondes_info = json.loads(idx_path.read_text())
        return
    for sid, s in sorted(sondes.items(), key=lambda kv: kv[1].launch):
        try:
            diag = soundings.diagnostics(s)
        except Exception as exc:
            c.log(f"  {sid}: diagnostics failed ({str(exc)[:100]})")
            diag = {"error": str(exc)[:200]}
        csv_name = f"{sid}.csv"
        (sdir).mkdir(parents=True, exist_ok=True)
        (sdir / csv_name).write_text(soundings.common_csv(s, c.start))
        rel = (
            "pre-storm"
            if s.launch < c.start - timedelta(minutes=30)
            else ("in-window" if s.launch <= c.end else "post-storm")
        )
        index.append(
            {
                "id": sid,
                "site": s.site,
                "launch": s.launch.isoformat().replace("+00:00", "Z"),
                "launch_t_s": round((s.launch - c.start).total_seconds(), 0),
                "lat": s.lat,
                "lon": s.lon,
                "x_km": [round(float(v), 2) for v in c.xy_km(s.lat, s.lon)],
                "profile_csv": f"soundings/{csv_name}",
                "drift": soundings.drift(s, c.start),
                "diagnostics": diag,
                "relation_to_window": rel,
                "source": s.source,
                "credit": s.credit,
                "class": "observed",
                "notes": s.notes,
                "top_height_asl_m": float(np.nanmax(s.z_asl_m))
                if np.isfinite(s.z_asl_m).any()
                else None,
            }
        )
    bundle.write_json(idx_path, bundle.clean_nan(index))
    c.sondes_info = index
    c.log(f"  wrote {len(index)} soundings")


# ---------------------------------------------------------------------------
# volumes + winds
# ---------------------------------------------------------------------------


def window_motion(c: Ctx) -> dict[str, Any]:
    p = c.work / "motion.json"
    if p.exists() and not c.a.force:
        return json.loads(p.read_text())
    root = c.data / "GRID"
    vols = merge.build_index(root, ["KGWX"], c.start, c.end, margin_s=400.0).get("KGWX", [])
    if len(vols) < 2:
        raise SystemExit("need two KGWX volumes around the window to estimate the motion")
    v0, v1 = vols[0], vols[-1]
    qc = pgrid.QCConfig()
    g0 = pgrid.load_grid(v0.path, "KGWX", qc)
    g1 = pgrid.load_grid(v1.path, "KGWX", qc)
    import hailconv

    m = tracks.estimate_motion(
        hailconv.column_max(g0.fields["dbz"]),
        hailconv.column_max(g1.fields["dbz"]),
        (v1.t_mid - v0.t_mid).total_seconds(),
        0.5,
    )
    m["from_volumes"] = [v0.path.name, v1.path.name]
    m["times"] = [v0.t_mid.isoformat(), v1.t_mid.isoformat()]
    bundle.write_json(p, bundle.clean_nan(m), indent=1)
    return m


def stage_volumes(c: Ctx) -> None:
    a = c.a
    root = c.data / "GRID"
    qc = pgrid.QCConfig.off() if a.no_qc else pgrid.QCConfig()
    index = merge.build_index(root, ALL_RADARS, c.start, c.end)
    plans = merge.plan_steps(index, c.start, c.end, a.step_s, a.tol_s)
    if a.max_steps:
        plans = plans[: a.max_steps]
    c.log(f"volumes: {len(plans)} steps, radars with volumes near the window: {sorted(index)}")
    motion = window_motion(c)
    c.log(
        f"  line / pattern motion {motion['speed_ms']:.1f} m/s toward {motion['toward_deg']:.0f} deg (correlation {motion['correlation']:.2f})"
    )
    assert c.env is not None
    zdr_cal: dict[str, Any] | None = None if a.no_zdr_cal else {}
    cache: dict[str, volumes.Prod] = {}
    last_use: dict[str, int] = {}
    for pl in plans:
        for v in pl.volumes.values():
            last_use[str(v.path)] = pl.index
    wdir = c.out / "winds"
    for pl in plans:
        vol_rel = f"volumes/step_{pl.index:04d}.npz"
        wind_rel = f"winds/step_{pl.index:04d}.npz"
        meta_p = c.work / f"step_{pl.index:04d}.json"
        need_vol = not (c.out / vol_rel).exists() or not meta_p.exists() or a.force
        need_wind = (not a.no_winds) and (not (c.out / wind_rel).exists() or a.force)
        if not (need_vol or need_wind):
            c.log(f"  step {pl.index:02d} exists")
            continue
        t0 = time.time()
        for v in pl.volumes.values():
            if str(v.path) not in cache:
                cache[str(v.path)] = volumes.process_volume(v, qc, c.env, c.proj, zdr_cal=zdr_cal)
        prods = {r: cache[str(v.path)] for r, v in pl.volumes.items()}
        meta: dict[str, Any] = (
            json.loads(meta_p.read_text()) if meta_p.exists() and not need_vol else {}
        )
        if need_vol:
            arrays, summ = volumes.build_step(pl, prods, ALL_RADARS, c.env, qc)
            size = volumes.save_step(
                c.out / vol_rel, arrays, pl.time.isoformat().replace("+00:00", "Z")
            )
            meta.update(
                {
                    "index": pl.index,
                    "time": pl.time.isoformat().replace("+00:00", "Z"),
                    "radars": sorted(pl.volumes),
                    "offsets_s": pl.offsets_s,
                    "files": {r: v.path.name for r, v in pl.volumes.items()},
                    "volume": vol_rel,
                    "volume_bytes": size,
                    "diagnostics": summ,
                    "cmax": None,
                }
            )
            np.save(c.work / f"cmax_{pl.index:04d}.npy", arrays["cmax"])
            del arrays
        if need_wind and len(prods) >= 2:
            rl = []
            for r, p in prods.items():
                if "vel" in p.grid.fields:
                    rl.append(
                        {
                            "name": r,
                            "vel": p.grid.fields["vel"],
                            "dbz": p.grid.fields["dbz"],
                            "x_m": p.xy_km[0] * 1000.0,
                            "y_m": p.xy_km[1] * 1000.0,
                            "alt_m": p.grid.alt_m,
                            "offset_s": pl.offsets_s[r],
                        }
                    )
            try:
                warr, wsum = winds.synthesize(
                    rl,
                    volumes.Z_M,
                    volumes.X_KM * 1000.0,
                    volumes.Y_KM * 1000.0,
                    c.env.freezing_m,
                    (motion["u_ms"], motion["v_ms"]),
                    max_iterations=a.wind_iterations,
                )
                wdir.mkdir(parents=True, exist_ok=True)
                np.savez_compressed(
                    c.out / wind_rel,
                    x_km=volumes.X_KM[::2],
                    y_km=volumes.Y_KM[::2],
                    z_km=volumes.Z_M / 1000.0,
                    time_iso=np.array(meta["time"]),
                    **{
                        k: (
                            v.astype(np.float16)
                            if k in ("vorticity", "divergence", "uv_error")
                            else v
                        )
                        for k, v in warr.items()
                    },
                )
                meta["winds"] = wind_rel
                meta["winds_summary"] = wsum
            except Exception as exc:
                c.log(f"  winds step {pl.index}: failed ({str(exc)[:150]})")
                meta["winds_error"] = str(exc)[:300]
        bundle.write_json(meta_p, bundle.clean_nan(meta), indent=1)
        # free volumes no later step needs
        for pth in [p for p, last in last_use.items() if last <= pl.index and p in cache]:
            del cache[pth]
        c.log(
            f"  step {pl.index:02d} {pl.time:%H:%M:%S} radars {sorted(pl.volumes)} done in {time.time() - t0:.0f}s"
        )
    if zdr_cal is not None:
        bundle.write_json(
            c.work / "zdr_cal.json",
            bundle.clean_nan({r: v["log"] for r, v in zdr_cal.items()}),
            indent=1,
        )


# ---------------------------------------------------------------------------
# lightning, stations, tornadoes, context, profiles
# ---------------------------------------------------------------------------


def stage_arc(c: Ctx) -> None:
    """ZDR arc and KDP-ZDR separation (radarx ``zdr_arc``, Wilson and Van Den Broeke 2021) per step, at 500 m.

    A SUPERCELL algorithm applied to a QLCS: the forward-flank-downdraft direction is set opposite to the pattern
    motion (pointing into the storm); the result is a list of candidates for the maintainer to judge, stored in
    ``manifest.steps[i].diagnostics.zdr_arc``. Size sorting of the leading edge is only a candidate here.
    """
    import radarx.retrieve as rxr
    import xarray as xr

    motion = window_motion(c)
    ffd = (motion["toward_deg"] + 180.0) % 360.0
    x = volumes.X_KM * 1000.0
    for p in sorted(c.work.glob("step_*.json")):
        m = json.loads(p.read_text())
        if "zdr_arc" in (m.get("diagnostics") or {}) and not c.a.force:
            continue
        v = np.load(c.out / m["volume"])
        k = 2  # 500 m above sea level
        ds = xr.Dataset(
            {
                "REF": (("y", "x"), v["dbz"][k]),
                "ZDR": (("y", "x"), v["zdr"][k].astype(np.float32)),
                "RHO": (("y", "x"), v["rho"][k].astype(np.float32)),
                "KDP": (("y", "x"), v["kdp"][k].astype(np.float32)),
            },
            coords={"y": x, "x": x},
        )
        try:
            out = rxr.zdr_arc(
                ds,
                zdr="ZDR",
                dbz="REF",
                rhohv="RHO",
                kdp="KDP",
                ffd_direction=ffd,
                storm_motion=motion["toward_deg"],
            )
        except Exception as exc:
            m.setdefault("diagnostics", {})["zdr_arc"] = {"error": str(exc)[:200]}
            bundle.write_json(p, bundle.clean_nan(m), indent=1)
            continue
        storms = []
        for i in range(out.sizes["storm"]):
            storms.append(
                {
                    "x_km": float(out["storm_x"].values[i]) / 1000.0,
                    "y_km": float(out["storm_y"].values[i]) / 1000.0,
                    "storm_area_km2": float(out["storm_area"].values[i]),
                    "arc_area_km2": float(out["arc_area"].values[i]),
                    "arc_mean_zdr_db": float(out["arc_mean_zdr"].values[i]),
                    "arc_top10_zdr_db": float(out["arc_top10_zdr"].values[i]),
                    "kdp_foot_area_km2": float(out["foot_area"].values[i]),
                    "separation_length_km": float(out["separation_length"].values[i]),
                    "separation_angle_deg": float(out["separation_angle"].values[i]),
                }
            )
        m.setdefault("diagnostics", {})["zdr_arc"] = {
            "level_km_asl": 0.5,
            "ffd_direction_deg": ffd,
            "note": "radarx zdr_arc is a supercell algorithm (Wilson and Van Den Broeke 2021); candidates only in a QLCS; ZDR is bias-corrected by an estimate",
            "storms": storms,
        }
        bundle.write_json(p, bundle.clean_nan(m), indent=1)
    c.log("arc: ZDR arc candidates written to the step diagnostics")


def stage_couplet(c: Ctx) -> None:
    """Couplet kinematics at the surveyed tornado per step and the KGWX native polar data around it.

    Writes ``context/vortex_kinematics.json`` and ``context/kgwx_polar_local_<time>.npz``. Needs the volumes and
    tornado stages. See ``couplet.py`` for the method and the resolution verdict.
    """
    import couplet

    out_p = c.out / "context" / "vortex_kinematics.json"
    if out_p.exists() and not c.a.force:
        return
    tj = c.out / "tornadoes" / "tornadoes.json"
    if not tj.exists():
        c.log("couplet: no tornado file")
        return
    tors = [t for t in json.loads(tj.read_text()) if t["overlaps_window"] and t.get("in_domain")]
    if not tors:
        return
    tor = tors[0]
    root = c.data / "GRID"
    z_km = volumes.Z_M / 1000.0
    rows = []
    for p in sorted(c.work.glob("step_*.json")):
        m = json.loads(p.read_text())
        t_s = (datetime.fromisoformat(m["time"].replace("Z", "+00:00")) - c.start).total_seconds()
        pos = tornadoes.position_at(tor, t_s)
        if pos is None:
            continue
        sx, sy = c.xy_km(pos[0], pos[1])
        survey = (float(sx), float(sy))
        # debris-signature indicator within 5 km of the surveyed position (merged volume): evidence only
        debris = None
        try:
            vol = np.load(c.out / m["volume"])
            ii = round((survey[0] + 90.0) / 0.5)
            jj = round((survey[1] + 90.0) / 0.5)
            sl = (slice(None), slice(max(jj - 10, 0), jj + 11), slice(max(ii - 10, 0), ii + 11))
            tds_ = vol["tds"][sl]
            rho_ = vol["rho"][sl].astype(np.float32)
            dbz_ = vol["dbz"][sl]
            flag = tds_ >= 1
            debris = {
                "voxels_flag1_or_2_within_5km": int(flag.sum()),
                "voxels_flag2_near_rotation": int((tds_ == 2).sum()),
                "min_rho_in_flag": None
                if not flag.any()
                else round(float(np.nanmin(rho_[flag])), 2),
                "max_dbz_in_flag": None
                if not flag.any()
                else round(float(np.nanmax(dbz_[flag])), 1),
                "min_rho_below_2km_any": None
                if not np.isfinite(rho_[:9]).any()
                else round(float(np.nanmin(rho_[:9])), 2),
                "criteria": "RHO < 0.8 coherent patch, Z >= 30 dBZ, below 2 km; flag 2 within 2 km of azimuthal shear >= 0.012 s-1",
                "class": "candidate / evidence only (large drops, hail and X-band effects also lower RHO)",
            }
        except Exception:
            debris = None
        per_radar = {}
        for radar, fn in m["files"].items():
            f = next((root / radar).glob(fn), None)
            if f is None:
                continue
            try:
                g = pgrid.load_grid(f, radar, pgrid.QCConfig())
            except Exception:
                continue
            if "vel" not in g.fields:
                continue
            rxy = c.xy_km(g.lat, g.lon)
            res = couplet.grid_couplet(
                g.fields["vel"],
                g.fields.get("roi"),
                volumes.X_KM,
                volumes.Y_KM,
                z_km,
                (float(rxy[0]), float(rxy[1])),
                survey,
            )
            if res:
                res["radar"] = radar
                res["offset_s"] = m["offsets_s"].get(radar)
                per_radar[radar] = res
        if not per_radar:
            rows.append(
                {
                    "index": m["index"],
                    "t_s": t_s,
                    "survey_xy_km": [round(survey[0], 2), round(survey[1], 2)],
                    "per_radar": {},
                    "best": None,
                    "debris_indicator": debris,
                }
            )
            continue
        # the radar with the finest effective resolution (ties: the larger velocity difference)
        best_r = min(
            per_radar,
            key=lambda r: (
                per_radar[r]["effective_resolution_m"],
                -per_radar[r]["couplet"]["dV_ms"],
            ),
        )
        b = per_radar[best_r]
        verdict = couplet.resolved_verdict(
            tor.get("width_m"), b["effective_resolution_m"], b["couplet"]["couplet_diameter_km"]
        )
        rows.append(
            {
                "index": m["index"],
                "t_s": t_s,
                "survey_xy_km": [round(survey[0], 2), round(survey[1], 2)],
                "per_radar": per_radar,
                "best_radar": best_r,
                "best": b,
                "resolution": verdict,
                "debris_indicator": debris,
            }
        )
        c.log(
            f"  couplet step {m['index']:02d}: {best_r} dV {b['couplet']['dV_ms']:.0f} m/s over {b['couplet']['couplet_diameter_km']:.1f} km, {b['couplet']['sense']}, eff. res {b['effective_resolution_m']:.0f} m"
        )
    # polar data of KGWX at native resolution around the surveyed position
    polar = []
    cf = c.data / "CFRAD" / "KGWX"
    site = None
    g0 = next(iter(sorted((root / "KGWX").glob("*.nc"))), None)
    if g0 is not None:
        import netCDF4

        with netCDF4.Dataset(g0) as ds:
            site = (
                float(np.ravel(ds["radar_latitude"][:])[0]),
                float(np.ravel(ds["radar_longitude"][:])[0]),
                float(np.ravel(ds["radar_altitude"][:])[0]),
            )
    if site and cf.exists():
        for f in sorted(cf.glob("KGWX2022033*_V06_retr.nc")):
            t0 = pgrid.parse_time(f)
            if t0 is None or not (
                c.start - timedelta(minutes=8) <= t0 <= c.end + timedelta(minutes=2)
            ):
                continue

            def target(t_abs: float, tor=tor):
                return tornadoes.position_at(tor, t_abs - c.start.timestamp())

            res = couplet.polar_local(f, site, target, c.proj, survey_width_m=tor.get("width_m"))
            if res:
                tag = t0.strftime("%H%M%S")
                np.savez_compressed(
                    c.out / "context" / f"kgwx_polar_local_{tag}.npz",
                    **res["arrays"],
                    sweeps=np.array(json.dumps(res["sweeps"])),
                    note=np.array(
                        "KGWX native polar gates (VEL, REF, RHO, ZDR) within 4 km of the surveyed tornado position at the sweep time; x_km, y_km in the grid frame; heights m ASL"
                    ),
                )
                polar.append(
                    {
                        "file": f.name,
                        "npz": f"context/kgwx_polar_local_{tag}.npz",
                        "sweeps": res["sweeps"],
                    }
                )
                c.log(f"  polar {f.name}: {len(res['sweeps'])} sweeps near the tornado")
    det = [r for r in rows if r.get("best")]
    summary: dict[str, Any] = {"n_steps": len(rows), "n_steps_with_couplet": len(det)}
    if det:
        vr = [r["best"]["couplet"]["vrot_ms"] for r in det]
        dd = [r["best"]["couplet"]["couplet_diameter_km"] for r in det]
        om = [r["best"]["couplet"]["omega_s-1"] for r in det]
        pr = [r["best"]["couplet"]["period_s"] for r in det if r["best"]["couplet"]["period_s"]]
        summary.update(
            {
                "vrot_ms_min_median_max": [min(vr), float(np.median(vr)), max(vr)],
                "couplet_diameter_km_min_median_max": [min(dd), float(np.median(dd)), max(dd)],
                "omega_s-1_median": float(np.median(om)),
                "period_s_median": float(np.median(pr)) if pr else None,
                "sense_counts": {
                    s: sum(1 for r in det if r["best"]["couplet"]["sense"] == s)
                    for s in ("cyclonic", "anticyclonic")
                },
                "best_radars": sorted({r["best_radar"] for r in det}),
                "effective_resolution_m_min": min(r["best"]["effective_resolution_m"] for r in det),
                "tornado_resolved_any_step": any(
                    r["resolution"].get("tornado_resolved") for r in det
                ),
            }
        )
    # what the survey implies for a tornado-scale vortex: an ESTIMATE from the EF-scale wind range and the damage width,
    # not a radar measurement (the renderer may use it for the spin of a drawn funnel, labelled as such)
    lo_mph, hi_mph = tornadoes.EF_WIND_MPH.get(tor["rating"], (None, None))
    survey_est = None
    if lo_mph and tor.get("width_m"):
        v_lo, v_hi = lo_mph * 0.44704, hi_mph * 0.44704
        d = float(tor["width_m"])
        survey_est = {
            "class": "ESTIMATE from the damage survey (EF-scale wind range and damage-path width), NOT a radar measurement",
            "wind_range_ms": [round(v_lo, 1), round(v_hi, 1)],
            "damage_width_m": d,
            "angular_velocity_s-1_range": [round(2 * v_lo / d, 3), round(2 * v_hi / d, 3)],
            "rotation_period_s_range": [round(math.pi * d / v_hi, 1), round(math.pi * d / v_lo, 1)],
            "assumptions": "solid-body core with the rated peak wind at the edge of the damage width: period = pi D / V; the real vortex structure, the radius of maximum wind and the translation are unknown; the rated wind is a damage-based range",
        }
    doc = {
        "class": "retrieved (the survey: observed)",
        "survey_scale_estimate": survey_est,
        "tornado": {
            k: tor.get(k)
            for k in ("id", "rating", "start", "end", "width_m", "path_kind", "begin", "end_point")
        },
        "method": "see tools/perils_movie/couplet.py: velocity sampled along the azimuthal line through the strongest shear centre within 3 km of the surveyed position, below 1 km ASL, of every radar of the step; Vrot = dV/2, omega = dV/d, period = 2 pi/omega (solid-body core assumption); sense from the order of the extremes",
        "assumptions": [
            f"beam width of the mobile radars taken as a nominal {couplet.NOMINAL_BEAM_WIDTH_DEG} degree (not in the grid files)",
            "the surveyed position is the interpolated straight line between the survey's begin and end points",
            "the couplet is that of the strongest shear within 3 km of the survey: a circulation wider than the tornado is the parent circulation, not the tornado",
        ],
        "per_step": rows,
        "summary": summary,
        "kgwx_polar": polar,
        "polar_data_available": "KGWX only (CfRadial volumes 01:05, 01:11, 01:18 UTC). The COW1 CfRadial files end at 23:20 UTC and DOW7, DOW8, NOXP, SMART-R 2 and UAH exist only as 500 m grids in the folder: no native-gate mobile-radar data exist for the tornado time, so no 50-100 m local grid can be made from real gates",
        "local_grid_note": "a finer grid would only be an interpolation of the 500 m data (no new information) and is not provided; the native KGWX gates around the position are in context/kgwx_polar_local_*.npz",
    }
    bundle.write_json(out_p, bundle.clean_nan(doc), indent=1)
    c.log(f"couplet: {summary}")


def stage_lma(c: Ctx) -> None:
    if c.done("lma/flashes.json"):
        return
    folder = c.data / "NSSL_LMA" / "syed41331583" / "datagPIuma" / "20220330"
    fs = lma.files_for_window(folder, c.start, c.end)
    if not fs:
        c.log("lma: no files for the window")
        return
    ds = lma.read_sources(fs)
    lat = ds["event_latitude"].values
    lon = ds["event_longitude"].values
    t = np.asarray(ds["event_time"].values).astype("datetime64[ns]").astype("int64") / 1e9
    sel = (
        (lat > 32.2)
        & (lat < 35.0)
        & (lon > -90.0)
        & (lon < -87.0)
        & (t >= c.start.timestamp() - 5)
        & (t <= c.end.timestamp() + 5)
    )
    sub = ds.isel(number_of_events=np.nonzero(sel)[0])
    clustered = lma.cluster(sub)
    info = lma.header_info(fs[len(fs) // 2])
    j = lma.to_flashes_json(
        clustered,
        c.start,
        c.end,
        (32.5, -89.6, 34.7, -87.5),
        station_count={
            "network": info.get("n_stations"),
            "active": info.get("n_active"),
            "active_ids": info.get("active"),
            "min_stations_per_source": lma.DEFAULT_PARAMS["min_stations"],
        },
    )
    j["files"] = [f.name for f in fs]
    j["sources_per_minute_in_box"] = lma.per_minute_counts(sub, c.start, c.end)
    j["stations"] = info.get("stations")
    j["credit"] = (
        "NALMA (NASA MSFC / UAH) and the NSSL deployable LMA, PERiLS 2022 (NCAR/EOL 610.006 v1.1)"
    )
    bundle.write_json(c.out / "lma" / "flashes.json", bundle.clean_nan(j))
    c.log(f"lma: {j['n_sources']} sources in {j['n_flashes']} flashes")


def stage_stations(c: Ctx) -> None:
    if c.done("stations/stations.json"):
        return
    stations.MARGIN_S = 600.0
    M = c.data
    sdir = c.out / "stations"
    recs: list[dict[str, Any]] = []
    hail_events: list[dict[str, Any]] = []
    for f in sorted(glob.glob(str(M / "PIPS_data/IOP2_033022/netcdf/parsivel_combined*_10s.nc"))):
        try:
            r, ev = stations.read_pips(f, c.start, c.end, sdir)
            recs.append(r)
            hail_events += ev
        except Exception as exc:
            c.log(f"  PIPS {Path(f).name}: {exc}")
    loc = stations.read_sticknet_locations(M / "ttu/config/IOP2_StickNet_Locations.csv")
    for f in sorted(glob.glob(str(M / "ttu/Sticknet_10s/IOP2_*.csv"))):
        sid = Path(f).name.split("_")[1]
        if sid not in loc:
            continue
        r = stations.read_sticknet(f, loc[sid], c.start, c.end)
        if r:
            recs.append(r)
    r = stations.read_wxt(
        M / "UAH_MAPNet_RaDAPS_Surface_610.017/radaps_20220331_wxt.dat",
        (33.595558, -88.987904, 87.0),
        c.start,
        c.end,
    )
    if r:
        recs.append(r)
    mesos = {}
    for p in ["DOW7", "DOW8", "SCOUT1", "SCOUT2", "SCOUT3"]:
        files = glob.glob(
            str(M / f"soundings/non-radar_QC_Illinois/20220330_IOP02/{p}/mesonet/L1/*.csv")
        )
        files = files or glob.glob(str(M / f"UIUC_FARM_SCOUTS/{p}/mesonet/L1/*.csv"))
        files = [f for f in files if not Path(f).name.startswith(".")]
        if not files:
            continue
        try:
            r = stations.read_mesonet(files[0], p, c.start, c.end)
        except Exception as exc:
            c.log(f"  mesonet {p}: {exc}")
            continue
        if r:
            recs.append(r)
            mesos[p] = (r["lat"], r["lon"])
    seen = set()
    for scout, _pid in (("SCOUT1", None), ("SCOUT2", None), ("SCOUT3", None)):
        for f in glob.glob(
            str(M / f"UIUC_FARM_SCOUTS/{scout}/disdrometer/20220331*.csv")
        ) + glob.glob(
            str(
                M
                / f"soundings/non-radar_QC_Illinois/20220330_IOP02/{scout}/disdrometer/20220331*.csv"
            )
        ):
            try:
                r = stations.read_farm_disdrometer(f, c.start, c.end, mesos.get(scout))
            except Exception as exc:
                c.log(f"  FARM disdrometer {Path(f).name}: {exc}")
                continue
            if r and r["id"] not in seen:
                seen.add(r["id"])
                r["name"] += f" (on {scout})"
                recs.append(r)
    for r in recs:  # position in the grid frame (km), accumulation
        if r.get("lat") is not None:
            x, y = c.xy_km(r["lat"], r["lon"])
            r["x_km"], r["y_km"] = round(float(x), 2), round(float(y), 2)
        rr = r["series"].get("rain_rate_mmh")
        if rr:
            r["rain_accum_window_mm"] = stations.hourly_accumulation_mm(r["series"]["t_s"], rr)
    bundle.write_json(sdir / "stations.json", bundle.clean_nan(recs))
    bundle.write_json(sdir / "hail_events.json", bundle.clean_nan(hail_events))
    c.log(f"stations: {len(recs)} stations, {len(hail_events)} hail candidate samples")


def stage_tornadoes(c: Ctx) -> None:
    if c.done("tornadoes/tornadoes.json"):
        return
    tab = Path(c.a.tables).expanduser()
    nsed, spc = tornadoes.read_tables(
        tab / "perils_tornadoes_nsed.csv", tab / "perils_spc_tornadoes.csv"
    )
    recs = tornadoes.build(nsed, spc, c.start, c.end, (30.5, -91.0, 35.5, -86.0))
    for r in recs:
        for key in ("begin", "end_point"):
            x, y = c.xy_km(r[key]["lat"], r[key]["lon"])
            r[key]["x_km"], r[key]["y_km"] = round(float(x), 2), round(float(y), 2)
        r["in_domain"] = any(
            abs(r[key]["x_km"]) <= 90.0 and abs(r[key]["y_km"]) <= 90.0
            for key in ("begin", "end_point")
        )
        r["distance_of_begin_from_grid_origin_km"] = round(
            float(np.hypot(r["begin"]["x_km"], r["begin"]["y_km"])), 1
        )
    bundle.write_json(c.out / "tornadoes" / "tornadoes.json", bundle.clean_nan(recs), indent=1)
    c.log(
        f"tornadoes: {len(recs)} records, {sum(1 for r in recs if r['overlaps_window'])} overlap the window"
    )


def stage_context(c: Ctx) -> None:
    cdir = c.out / "context"
    plans = []
    for p in sorted(c.work.glob("step_*.json")):
        plans.append(json.loads(p.read_text()))
    if not plans:
        c.log("context: run the volumes stage first")
        return
    # -- MRMS -------------------------------------------------------------------------------------------
    mdir = c.data / "GRID" / "MRMS"
    refs = context.mrms_files(mdir / "MergedRef", "MRMS_MergedBaseReflectivityQC_*.grib2")
    azs = context.mrms_files(mdir / "AzShear", "MRMS_MergedAzShear_0-2kmAGL_*.grib2")
    exe = context.wgrib2_path()
    mrms_done = 0
    if exe is None:
        c.log("context: wgrib2 not found, MRMS skipped")
    else:
        tmp = c.work / "wgrib2"
        for m in plans:
            t = datetime.fromisoformat(m["time"].replace("Z", "+00:00"))
            out_p = cdir / f"mrms_{m['index']:04d}.npz"
            if out_p.exists() and not c.a.force:
                m["mrms"] = f"context/{out_p.name}"
                continue
            fr = context.nearest(refs, t, 150.0)
            fa = context.nearest(azs, t, 150.0)
            if fr is None and fa is None:
                continue
            arrays: dict[str, Any] = {}
            info = {}
            for name, f in (("mergedref_dbz", fr), ("azshear_s-1", fa)):
                if f is None:
                    continue
                try:
                    r = context.read_mrms_crop(f[1], (-89.7, -87.4), (32.6, 34.5), tmp)
                except Exception as exc:
                    c.log(f"  MRMS {f[1].name}: {str(exc)[:100]}")
                    continue
                arrays["lat"], arrays["lon"] = r["lat"], r["lon"]
                arrays[name] = r["field"]
                info[name] = {
                    "file": f[1].name,
                    "valid": f[0].isoformat().replace("+00:00", "Z"),
                    "offset_s": (f[0] - t).total_seconds(),
                }
            if arrays:
                cdir.mkdir(parents=True, exist_ok=True)
                np.savez_compressed(
                    out_p, time_iso=np.array(m["time"]), info=np.array(json.dumps(info)), **arrays
                )
                m["mrms"] = f"context/{out_p.name}"
                m["mrms_info"] = info
                mrms_done += 1
        c.log(f"context: MRMS {mrms_done} files written")
    # -- surface objective analysis ---------------------------------------------------------------------------
    sj = c.out / "stations" / "stations.json"
    if sj.exists():
        st = json.loads(sj.read_text())
        gx = np.arange(-90.0, 90.001, 2.0)
        gy = gx.copy()
        for m in plans:
            out_p = cdir / f"surface_{m['index']:04d}.npz"
            if out_p.exists() and not c.a.force:
                m["surface"] = f"context/{out_p.name}"
                continue
            t_s = (
                datetime.fromisoformat(m["time"].replace("Z", "+00:00")) - c.start
            ).total_seconds()
            snap = context.station_snapshot(st, t_s)
            arrays = {}
            for var in (
                "temperature_c",
                "dewpoint_c",
                "theta_e_k",
                "pressure_hpa",
                "mixing_ratio_gkg",
            ):
                if var in snap and len(snap[var]) >= 3:
                    pts = snap[var]
                    xs, ys = c.xy_km(np.array([p[0] for p in pts]), np.array([p[1] for p in pts]))
                    arrays[var] = context.barnes(xs, ys, np.array([p[2] for p in pts]), gx, gy)
            if "wind_speed_ms" in snap and "wind_dir_deg" in snap:
                ws = {(p[0], p[1]): p[2] for p in snap["wind_speed_ms"]}
                wd = {(p[0], p[1]): p[2] for p in snap["wind_dir_deg"]}
                keys = [k for k in ws if k in wd]
                if len(keys) >= 3:
                    uv = np.array([context.wind_components(ws[k], wd[k]) for k in keys])
                    xs, ys = c.xy_km(np.array([k[0] for k in keys]), np.array([k[1] for k in keys]))
                    arrays["u_ms"] = context.barnes(xs, ys, uv[:, 0], gx, gy)
                    arrays["v_ms"] = context.barnes(xs, ys, uv[:, 1], gx, gy)
            if arrays:
                cdir.mkdir(parents=True, exist_ok=True)
                np.savez_compressed(
                    out_p,
                    time_iso=np.array(m["time"]),
                    x_km=gx,
                    y_km=gy,
                    n_stations=np.array(len(st)),
                    **arrays,
                )
                m["surface"] = f"context/{out_p.name}"
        c.log("context: surface analysis written")
    for m in plans:
        bundle.write_json(c.work / f"step_{m['index']:04d}.json", bundle.clean_nan(m), indent=1)
    # -- environment time series from the soundings and the surface network ------------------------------------
    if (c.out / "soundings" / "index.json").exists():
        sidx = json.loads((c.out / "soundings" / "index.json").read_text())
        env_rows = []
        for s in sidx:
            d = s.get("diagnostics") or {}
            par = d.get("parcels") or {}
            sh = d.get("shear") or {}
            srh = d.get("srh") or {}
            iso = d.get("isotherms_asl_m") or {}
            sfc = d.get("surface") or {}
            env_rows.append(
                {
                    "id": s["id"],
                    "site": s["site"],
                    "launch": s["launch"],
                    "launch_t_s": s["launch_t_s"],
                    "lat": s["lat"],
                    "lon": s["lon"],
                    "x_km": s.get("x_km"),
                    "relation_to_window": s["relation_to_window"],
                    "surface_t_c": sfc.get("temperature_c"),
                    "surface_td_c": sfc.get("dewpoint_c"),
                    "surface_p_hpa": sfc.get("pressure_hpa"),
                    "sbcape": (par.get("surface-based") or {}).get("cape"),
                    "sbcin": (par.get("surface-based") or {}).get("cin"),
                    "mlcape": (par.get("mixed-layer") or {}).get("cape"),
                    "mucape": (par.get("most-unstable") or {}).get("cape"),
                    "lcl_agl_m": (par.get("surface-based") or {}).get("lcl_agl_m"),
                    "lfc_agl_m": (par.get("surface-based") or {}).get("lfc_agl_m"),
                    "shear_0_1km_ms": (sh.get("0-1km") or {}).get("speed_ms") if sh else None,
                    "shear_0_3km_ms": (sh.get("0-3km") or {}).get("speed_ms") if sh else None,
                    "shear_0_6km_ms": (sh.get("0-6km") or {}).get("speed_ms") if sh else None,
                    "srh_0_1km_right_m2s2": ((srh.get("0-1km") or {}).get("right_mover"))
                    if srh
                    else None,
                    "srh_0_3km_right_m2s2": ((srh.get("0-3km") or {}).get("right_mover"))
                    if srh
                    else None,
                    "freezing_level_asl_m": iso.get("freezing_asl_m"),
                    "minus10_level_asl_m": iso.get("minus10_asl_m"),
                    "minus20_level_asl_m": iso.get("minus20_asl_m"),
                    "pw_mm": (d.get("moisture") or {}).get("precipitable_water_mm"),
                    "top_height_asl_m": s.get("top_height_asl_m"),
                }
            )
        sj = c.out / "stations" / "stations.json"
        sfc_series = None
        if sj.exists():
            st = [
                s
                for s in json.loads(sj.read_text())
                if not s.get("moving") and s["id"].startswith("StickNet")
            ]
            if st:
                grid_t = np.arange(-600.0, c.duration + 601.0, 60.0)
                sfc_series = {"t_s": grid_t.tolist(), "n_stations": len(st)}
                for var in (
                    "temperature_c",
                    "dewpoint_c",
                    "theta_e_k",
                    "pressure_hpa",
                    "wind_speed_ms",
                ):
                    cols = []
                    for s in st:
                        if var in s["series"]:
                            tt = np.array(s["series"]["t_s"], float)
                            vv = np.array(
                                [np.nan if v is None else v for v in s["series"][var]], float
                            )
                            cols.append(
                                [
                                    np.nanmean(vv[np.abs(tt - g) <= 30])
                                    if (np.abs(tt - g) <= 30).any()
                                    else np.nan
                                    for g in grid_t
                                ]
                            )
                    if cols:
                        arr = np.array(cols, float)
                        sfc_series[var + "_median"] = np.nanmedian(arr, axis=0).tolist()
                        sfc_series[var + "_p10"] = np.nanpercentile(arr, 10, axis=0).tolist()
                        sfc_series[var + "_p90"] = np.nanpercentile(arr, 90, axis=0).tolist()
        bundle.write_json(
            cdir / "environment.json",
            bundle.clean_nan(
                {
                    "class": "observed (radiosondes, StickNet); no model or reanalysis (see docs/PERILS_BUNDLE.md for why)",
                    "soundings": env_rows,
                    "surface_stickNet_median": sfc_series,
                    "note": "the soundings are at the launch sites (not all at the storm); several end below 6 km, then 0-6 km shear and the Bunkers motion are not computed; the surface series are the median and the 10th / 90th percentile of the 24 StickNet probes",
                }
            ),
            indent=1,
        )
    # -- tobac cell track (the data owner's feature tracking of KGWX echo) ------------------------------------
    tob = c.data / "TRAJECTORIES" / "tobac_Save_20220330" / "Track.nc"
    if tob.exists():
        tt = context.read_tobac_track(tob, c.start, c.end)
        if tt:
            bundle.write_json(cdir / "tobac_track.json", bundle.clean_nan(tt), indent=1)
            c.log(f"context: tobac track rows {len(tt['rows'])}")
    # -- rotation tracks + survey comparison ---------------------------------------------------------------
    times_s = [
        (datetime.fromisoformat(m["time"].replace("Z", "+00:00")) - c.start).total_seconds()
        for m in plans
    ]
    per_step = [m["diagnostics"].get("vortices", []) for m in plans]
    all_trs = tracks.link_vortices(per_step, times_s)
    trs = [
        t for t in all_trs if t["n_steps"] >= 3
    ]  # a candidate that lasts at least 3 steps (2 min)
    comparison = {}
    tj = c.out / "tornadoes" / "tornadoes.json"
    if tj.exists():
        for t in json.loads(tj.read_text()):
            if not (t["overlaps_window"] and t.get("in_domain")):
                continue
            survey = []
            for p in t["path"]:
                x, y = c.xy_km(p[1], p[2])
                survey.append((p[0], float(x), float(y)))
            comparison[t["id"]] = tracks.compare_with_survey(per_step, times_s, survey)
            comparison[t["id"]]["rating"] = t["rating"]
    mot = (
        json.loads((c.work / "motion.json").read_text())
        if (c.work / "motion.json").exists()
        else None
    )
    bundle.write_json(
        cdir / "rotation_tracks.json",
        bundle.clean_nan(
            {
                "class": "retrieved",
                "method": "connected regions of 0-2 km layer-maximum azimuthal shear >= 0.02 s-1 (rotation.py); centre = AS-weighted centroid; linked step to step within 5 km",
                "frame": "x_km east, y_km north of the grid origin; times t_s since the window start",
                "per_step": [
                    {"index": m["index"], "t_s": t, "vortices": v}
                    for m, t, v in zip(plans, times_s, per_step, strict=True)
                ],
                "tracks": trs,
                "n_candidate_tracks_all": len(all_trs),
                "tracks_note": "only tracks of at least 3 steps are listed; per_step has every candidate",
                "survey_comparison": comparison,
                "vortex_kinematics": (
                    json.loads((cdir / "vortex_kinematics.json").read_text())
                    if (cdir / "vortex_kinematics.json").exists()
                    else None
                ),
                "pattern_motion": mot,
                "uncertainty": "position: about one grid cell (0.5 km) to the radius of influence of the data (up to 2.5 km), plus the time offset of the radar volume (up to 150 s) times the vortex motion (up to 20 m/s = 3 km)",
            }
        ),
    )
    c.log(
        f"context: {len(trs)} vortex tracks; comparison: "
        + json.dumps(
            {
                k: {
                    kk: vv
                    for kk, vv in v.items()
                    if kk in ("n_steps_detected", "distance_km_median", "time_shift_s")
                }
                for k, v in comparison.items()
            }
        )
    )


def stage_profiles(c: Ctx) -> None:
    pdir = c.out / "profiles"
    M = c.data / "UAH_MRR"
    st, en = c.start, c.end
    mrr_files = sorted(glob.glob(str(M / "syed43240357" / "RaDAPS_MRR_2022033*.nc")))
    r = profiles.read_mrr(mrr_files, st, en)
    if r is not None and (not (pdir / "mrr_RaDAPS.npz").exists() or c.a.force):
        pdir.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            pdir / "mrr_RaDAPS.npz",
            **r,
            lat=33.595558,
            lon=-88.987904,
            note=np.array(
                "UAH RaDAPS Micro Rain Radar, heights above the radar; dbz = attenuation-corrected reflectivity (MRR_Capital_Z)"
            ),
        )
        c.log(f"profiles: MRR {r['t_s'].size} profiles")
    w = profiles.read_rwp(
        sorted(glob.glob(str(M / "syed43239670" / "RADAPS_915_20220330_05.nc"))), st, en
    )
    if w is not None and (not (pdir / "rwp_RaDAPS.npz").exists() or c.a.force):
        meta = w.pop("meta")
        np.savez_compressed(
            pdir / "rwp_RaDAPS.npz",
            **w,
            lat=meta["lat"],
            lon=meta["lon"],
            note=np.array(
                "UAH RaDAPS 915 MHz radar wind profiler, 5 min consensus winds, heights above ground"
            ),
        )
        c.log(f"profiles: RWP {w['t_s'].size} profiles")


def stage_events(c: Ctx) -> None:
    events.build_all(c)


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def stage_manifest(c: Ctx) -> None:
    plans = [json.loads(p.read_text()) for p in sorted(c.work.glob("step_*.json"))]
    sites: dict[str, dict[str, Any]] = {}
    for rn in ALL_RADARS:
        p = next(iter(sorted(glob.glob(str(c.data / "GRID" / rn / "*.nc")))), None)
        if p is None:
            continue
        import netCDF4

        with netCDF4.Dataset(p) as ds:
            lat = float(np.ravel(ds["radar_latitude"][:])[0])
            lon = float(np.ravel(ds["radar_longitude"][:])[0])
            alt = float(np.ravel(ds["radar_altitude"][:])[0])
        x, y = c.xy_km(lat, lon)
        sites[rn] = {
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "alt_m": round(alt, 1),
            "x_km": round(float(x), 2),
            "y_km": round(float(y), 2),
        }
    steps = []
    for m in plans:
        s = {
            "index": m["index"],
            "time": m["time"],
            "radars": m["radars"],
            "offsets_s": m["offsets_s"],
            "volume": m["volume"],
            "volume_bytes": m.get("volume_bytes"),
            "files": m.get("files"),
            "diagnostics": m.get("diagnostics"),
        }
        for k_src, k_dst in (
            ("mrms", "mrms"),
            ("surface", "surface"),
            ("winds", "winds"),
            ("winds_summary", "winds_summary"),
        ):
            if m.get(k_src):
                s[k_dst] = m[k_src]
        steps.append(s)
    out = c.out
    zdr = (
        json.loads((c.work / "zdr_cal.json").read_text())
        if (c.work / "zdr_cal.json").exists()
        else None
    )
    mot = (
        json.loads((c.work / "motion.json").read_text())
        if (c.work / "motion.json").exists()
        else None
    )
    man = {
        "title": "PERiLS IOP2 QLCS, 31 March 2022 UTC (30 March local evening): a real-data bundle",
        "case": "PERiLS 2022 IOP2, Mississippi / Alabama QLCS and the EF2 tornado of the NWS damage survey (event 1035605, 01:08 to 01:20 UTC, begin 33.1008 N 88.4691 W)",
        "spec_version": bundle.SPEC_VERSION,
        "window": {
            "start": c.start.isoformat().replace("+00:00", "Z"),
            "end": c.end.isoformat().replace("+00:00", "Z"),
            "duration_s": c.duration,
        },
        "grid": bundle.grid_block(*ORIGIN),
        "steps": steps,
        "radars": bundle.radar_list(sites),
        "lma": "lma/flashes.json" if (out / "lma/flashes.json").exists() else None,
        "stations": "stations/stations.json" if (out / "stations/stations.json").exists() else None,
        "soundings": "soundings/index.json" if (out / "soundings/index.json").exists() else None,
        "environment": "context/environment.json"
        if (out / "context/environment.json").exists()
        else None,
        "tornadoes": "tornadoes/tornadoes.json"
        if (out / "tornadoes/tornadoes.json").exists()
        else None,
        "rotation_tracks": "context/rotation_tracks.json"
        if (out / "context/rotation_tracks.json").exists()
        else None,
        "vortex_kinematics": "context/vortex_kinematics.json"
        if (out / "context/vortex_kinematics.json").exists()
        else None,
        "tobac_track": "context/tobac_track.json"
        if (out / "context/tobac_track.json").exists()
        else None,
        "kgwx_polar_local": sorted(
            str(p.relative_to(out)) for p in (out / "context").glob("kgwx_polar_local_*.npz")
        ),
        "science_events": "science_events.json" if (out / "science_events.json").exists() else None,
        "hail_events": "stations/hail_events.json"
        if (out / "stations/hail_events.json").exists()
        else None,
        "profiles": {
            k: f"profiles/{k}_RaDAPS.npz"
            for k in ("mrr", "rwp")
            if (out / f"profiles/{k}_RaDAPS.npz").exists()
        },
        "hid_legend": bundle.hid_legend(),
        "pattern_motion": mot,
        "qc": {
            "config": (pgrid.QCConfig.off() if c.a.no_qc else pgrid.QCConfig()).as_dict(),
            "zdr_calibration": {
                "applied": not c.a.no_zdr_cal,
                "reference_rain_zdr_db": pgrid.ZDR_RAIN_EXPECTED_DB,
                "log": zdr,
            },
        },
        "merge": {
            "step_s": c.a.step_s,
            "tolerance_s": c.a.tol_s,
            "dbz": "weighted mean in linear Z, weights band_weight / ROI^2 (X band 0.5)",
            "vel": "best radar (smallest ROI), vel_src gives it",
            "dual_pol": "best dual-pol radar, pol_src gives it",
            "band_weight": pgrid.BAND_WEIGHT,
        },
        "environment_profile_for_hail": None
        if c.env is None
        else {
            "source": c.env.source,
            "freezing_m_asl": c.env.freezing_m,
            "minus20_m_asl": c.env.minus20_m,
        },
        "sources": bundle.SOURCES,
        "deviations_from_spec_v1": [
            "extra fields in volume npz (vel_src, pol_src, rotation, tds, hid, 2-D diagnostics); SPEC fields unchanged",
            "z is height above mean sea level (0..10 km), not above each radar",
            "vel/zdr/kdp/rho are float32 like dbz",
            "winds/step_NNNN.npz are on a 1 km grid (x_km, y_km stored in the file), not the 500 m grid",
        ],
    }
    bundle.write_json(out / "manifest.json", bundle.clean_nan(man), indent=1)
    write_readme(c, man)
    c.log(f"manifest: {len(steps)} steps, {len(man['sources'])} sources")


def write_readme(c: Ctx, man: dict[str, Any]) -> None:
    out = c.out

    def ex(rel: str) -> str:
        return "[x]" if (out / rel).exists() else "[ ]"

    n_vol = len(list((out / "volumes").glob("step_*.npz"))) if (out / "volumes").exists() else 0
    n_wind = len(list((out / "winds").glob("step_*.npz"))) if (out / "winds").exists() else 0
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file() and "_work" not in f.parts)
    text = f"""# PERiLS IOP2 data bundle (SPEC v1.1) - status

Written by `tools/perils_movie/build_bundle.py` (branch `feat/perils-data`). `docs/PERILS_BUNDLE.md` in the
repository is the full description (method, deviations from SPEC v1, limits); this file is the progress board.

Window: **{man["window"]["start"]} to {man["window"]["end"]}** ({man["window"]["duration_s"]:.0f} s), steps every {c.a.step_s:g} s
({len(man["steps"])} steps, index 0 = window start). Bundle size {size / 1e6:.0f} MB.

- {ex("manifest.json")} manifest.json ({len(man["steps"])} steps)
- {ex("volumes/step_0000.npz")} volumes/step_NNNN.npz ({n_vol} files): merged dbz, vel, zdr, kdp, rho, nradars + rotation, tds, hid, 2-D diagnostics
- {ex("winds/step_0000.npz")} winds/step_NNNN.npz ({n_wind} files): u, v, w, vorticity, divergence (1 km grid)
- {ex("lma/flashes.json")} lma/flashes.json
- {ex("stations/stations.json")} stations/stations.json (+ *_dsd.npz, hail_events.json)
- {ex("soundings/index.json")} soundings/index.json (+ CSV per launch)
- {ex("tornadoes/tornadoes.json")} tornadoes/tornadoes.json
- {ex("context/rotation_tracks.json")} context/rotation_tracks.json (vortex candidates, tracks, survey comparison)
- {ex("context/vortex_kinematics.json")} context/vortex_kinematics.json (Vrot, couplet diameter, omega, sense, resolved / unresolved verdict per step), {ex("context/kgwx_polar_local_011153.npz")} context/kgwx_polar_local_*.npz (native KGWX gates around the tornado; no mobile-radar polar data exist for that time)
- {ex("context/mrms_0000.npz")} context/mrms_NNNN.npz, {ex("context/surface_0000.npz")} context/surface_NNNN.npz
- {ex("profiles/mrr_RaDAPS.npz")} profiles/mrr_RaDAPS.npz, {ex("profiles/rwp_RaDAPS.npz")} profiles/rwp_RaDAPS.npz
- {ex("science_events.json")} science_events.json (data-derived events with numbers)

Not available for this case (see the repository doc): model fields (WRF / MPAS output is not on disk, ERA5 covers
another region), lidar, GOES for the window, MRMS MESH.
"""
    (out / "README.md").write_text(text)


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--window",
        nargs=2,
        metavar=("START", "END"),
        default=["2022-03-31T01:04:00Z", "2022-03-31T01:20:00Z"],
        help="ISO times (UTC) or HH:MM of 31 March",
    )
    ap.add_argument(
        "--out",
        default="~/Downloads/xradar-exports/perils_movie/bundle",
        help="bundle folder (written)",
    )
    ap.add_argument(
        "--data", default="~/Downloads/MULTIDOPPLER", help="the data folder (read only)"
    )
    ap.add_argument(
        "--tables", default="~/aies_stage/perils", help="folder with the tornado tables (read only)"
    )
    ap.add_argument("--stages", nargs="+", choices=STAGES, default=STAGES)
    ap.add_argument("--step-s", type=float, default=60.0)
    ap.add_argument(
        "--tol-s",
        type=float,
        default=190.0,
        help="largest offset between a radar volume mid-time and a step (190 s = half the 6.2 min WSR-88D cycle)",
    )
    ap.add_argument("--force", action="store_true", help="redo outputs that exist")
    ap.add_argument("--max-steps", type=int, default=0, help="only the first N steps (debug)")
    ap.add_argument(
        "--no-qc",
        action="store_true",
        help="switch the QC gates off (impossible values still removed)",
    )
    ap.add_argument("--no-zdr-cal", action="store_true", help="do not correct ZDR biases")
    ap.add_argument("--no-winds", action="store_true", help="skip the multi-Doppler synthesis")
    ap.add_argument("--wind-iterations", type=int, default=300)
    a = ap.parse_args(argv)
    c = Ctx(a)
    data = c.data.resolve()
    out = c.out.resolve()
    if data in out.parents or out == data:
        ap.error("--out must not be inside the data folder")
    c.out.mkdir(parents=True, exist_ok=True)
    c.work.mkdir(parents=True, exist_ok=True)
    c.log(f"window {c.start:%Y-%m-%d %H:%M:%S} to {c.end:%H:%M:%S} UTC, out {c.out}")
    fns = {
        "soundings": stage_soundings,
        "volumes": stage_volumes,
        "arc": stage_arc,
        "couplet": stage_couplet,
        "lma": stage_lma,
        "stations": stage_stations,
        "tornadoes": stage_tornadoes,
        "context": stage_context,
        "profiles": stage_profiles,
        "events": stage_events,
        "manifest": stage_manifest,
    }
    # the soundings stage always runs (the environment profile is needed by the volumes)
    stages = list(a.stages)
    if any(s in stages for s in ("volumes", "manifest")) and "soundings" not in stages:
        stages.insert(0, "soundings")
    failed = []
    for s in STAGES:
        if s not in stages:
            continue
        t0 = time.time()
        try:
            fns[s](c)
        except SystemExit:
            raise
        except Exception:
            failed.append(s)
            c.log(f"STAGE {s} FAILED:\n{traceback.format_exc()}")
        c.log(f"stage {s} took {time.time() - t0:.0f}s")
    size = sum(f.stat().st_size for f in c.out.rglob("*") if f.is_file())
    files = sum(1 for f in c.out.rglob("*") if f.is_file())
    print(
        f"\nsummary: {files} files, {size / 1e6:.0f} MB in {c.out}; failed stages: {failed or 'none'}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

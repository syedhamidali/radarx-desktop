#!/usr/bin/env python
"""Before / after checks of the reflectivity merge v2 (read only): numbers in ``verification_refl.json`` and figures.

    python/.venv/bin/python tools/perils_movie/verify_refl.py --bundle ~/Downloads/xradar-exports/perils_movie/bundle --out /tmp/refl_verify

* radar-to-radar: for every pair of radars at the steps 01:08, 01:11, 01:14, 01:17, 01:20, the median and robust scatter of the
  difference in the cells both see (0.5 to 3 km, both above 15 dBZ, both with a radius of influence <= 3 km), as supplied
  (before: the field the v1 merge used, QC only, no advection) and after (cleaned, attenuation-corrected, calibrated, advected;
  flagged cells left out);
* each radar and the merged v1 / v2 field against the S-band radar (KGWX as supplied) and against MRMS merged base reflectivity;
* the merged fields against the PIPS disdrometer reflectivity (Rayleigh Z from the owner's QC'd DSD) at the stations, and the owner's own
  KGWX-at-PIPS series;
* side-by-side plan views and vertical cross-sections.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "python"))
sys.path.insert(0, str(HERE))

import bundle
import pgrid
import refl
import reflcheck
import tornadoes

STEPS = (4, 7, 10, 13, 16)
X = np.arange(-90.0, 90.001, 0.5)
Z_KM = np.arange(41) * 0.25
ORIGIN = (33.575864857, -88.54851899)
RADARS = ["COW1", "DOW7", "KGWX", "NOXP", "SMARTR2", "UAH"]


def load_json(p: Path):
    return json.loads(p.read_text())


def regrid_mrms(npz, proj):
    m = np.load(npz)
    gx, gy = np.meshgrid(X, X)
    lat, lon = pgrid.latlon(proj, gx, gy)
    la, lo = m["lat"], m["lon"]
    ii = np.clip(np.round((lat - la[0]) / (la[1] - la[0])).astype(int), 0, la.size - 1)
    jj = np.clip(np.round((lon - lo[0]) / (lo[1] - lo[0])).astype(int), 0, lo.size - 1)
    inside = (lat >= la.min()) & (lat <= la.max()) & (lon >= lo.min()) & (lon <= lo.max())
    f = m["mergedref_dbz"][ii, jj]
    f = np.where(inside, f, np.nan)
    return f


def lowest_level(f: np.ndarray, lo_km=0.25, hi_km=2.5):
    """First valid value from the ground up between ``lo_km`` and ``hi_km`` and the mean height it comes from."""
    k = np.nonzero((lo_km <= Z_KM) & (hi_km >= Z_KM))[0]
    d = f[k]
    ok = np.isfinite(d)
    first = np.argmax(ok, axis=0)
    out = np.take_along_axis(d, first[None], axis=0)[0]
    out[~ok.any(axis=0)] = np.nan
    lev = Z_KM[k][first][ok.any(axis=0)]
    return out, float(lev.mean()) if lev.size else float("nan")


def column_at_level(dbz: np.ndarray, lo_km=0.25, hi_km=1.5):
    k = np.nonzero((lo_km <= Z_KM) & (hi_km >= Z_KM))[0]
    d = dbz[k]
    first = np.argmax(np.isfinite(d), axis=0)
    out = np.take_along_axis(d, first[None], axis=0)[0]
    out[~np.isfinite(d).any(axis=0)] = np.nan
    return out


def smooth2d(f: np.ndarray, n: int = 5) -> np.ndarray:
    """NaN-aware n x n mean (2.5 km for n = 5 on the 500 m grid), NaN where less than 60 % of the window has data."""
    from scipy import ndimage

    ok = np.isfinite(f).astype(np.float32)
    num = ndimage.uniform_filter(np.where(ok > 0, f, 0.0).astype(np.float32), n, mode="constant")
    den = ndimage.uniform_filter(ok, n, mode="constant")
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den >= 0.6, num / np.maximum(den, 1e-6), np.nan).astype(np.float32)


def stats2d(a, ref, min_dbz=15.0, stride=2):
    ok = np.isfinite(a) & np.isfinite(ref) & (a > min_dbz) & (ref > min_dbz)
    ok[:, :] &= True
    d = (a - ref)[ok][:: stride * stride]
    if d.size < 100:
        return {"n": int(d.size)}
    return {
        "n": int(ok.sum()),
        "mean_db": round(float(d.mean()), 2),
        "median_db": round(float(np.median(d)), 2),
        "rms_db": round(float(np.sqrt((d**2).mean())), 2),
        "std_db": round(float(d.std()), 2),
        "corr": round(float(np.corrcoef(a[ok], ref[ok])[0, 1]), 3),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--bundle", default="~/Downloads/xradar-exports/perils_movie/bundle")
    ap.add_argument("--out", required=True)
    ap.add_argument("--data", default="~/Downloads/MULTIDOPPLER")
    ap.add_argument(
        "--into-bundle",
        action="store_true",
        help="also write context/reflectivity_v2_verification.json into the bundle and point manifest_v2.json at it",
    )
    a = ap.parse_args(argv)
    b = Path(a.bundle).expanduser()
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    root = Path(a.data).expanduser() / "GRID"
    man1 = load_json(b / "manifest.json")
    man2 = load_json(b / "manifest_v2.json")
    calib = man2["reflectivity_v2"]["calibration"]
    proj = pgrid.projection(*ORIGIN)
    mot_d = man1["pattern_motion"]
    mot = (mot_d["u_ms"], mot_d["v_ms"])
    qc = pgrid.QCConfig()
    sw = refl.Switches()
    sw_noadv = refl.Switches(advection=False)
    pair_noadv: dict[str, list] = {}
    num: dict = {"steps": list(STEPS), "calibration": calib}
    pair_before: dict[str, list] = {}
    pair_after: dict[str, list] = {}
    vs_kgwx_before: dict[str, list] = {}
    vs_kgwx_after: dict[str, list] = {}
    merged_rows = []
    per_step_fields: dict[int, dict] = {}
    for si in STEPS:
        s = man1["steps"][si]
        files = {r: next((root / r).glob(fn)) for r, fn in s["files"].items()}
        before = {}
        after = {}
        after_noadv = {}
        prep = {}
        for r, f in files.items():
            p = refl.prepare_radar(f, r, qc, X, X, Z_KM, proj, s["offsets_s"][r], mot, sw, calib)
            prep[r] = p
            # "before" is what the v1 merge used (UAH: REF, not CREF)
            g = pgrid.load_grid(f, r, qc)
            before[r] = g.fields["dbz"]
            d = p.dbz.copy()
            d[(p.flags & 3) > 0] = (
                np.nan
            )  # flagged (cap / low KDP coverage) cells are not "after" data
            after[r] = d
            p_na = refl.prepare_radar(
                f, r, qc, X, X, Z_KM, proj, s["offsets_s"][r], mot, sw_noadv, calib
            )
            d_na = p_na.dbz.copy()
            d_na[(p_na.flags & 3) > 0] = np.nan
            after_noadv[r] = d_na
        for r1, r2 in itertools.combinations(RADARS, 2):
            st = reflcheck.pair_stats(
                after_noadv[r1],
                after_noadv[r2],
                Z_KM,
                X,
                X,
                prep[r1].xy_km,
                prep[r1].roi,
                prep[r2].roi,
                roi_max_m=3000.0,
            )
            pair_noadv.setdefault(f"{r1}-{r2}", []).append(st)
        per_step_fields[si] = {"prep": prep, "before": before, "after": after}
        for r1, r2 in itertools.combinations(RADARS, 2):
            for store, fields in ((pair_before, before), (pair_after, after)):
                st = reflcheck.pair_stats(
                    fields[r1],
                    fields[r2],
                    Z_KM,
                    X,
                    X,
                    prep[r1].xy_km,
                    prep[r1].roi,
                    prep[r2].roi,
                    roi_max_m=3000.0,
                )
                store.setdefault(f"{r1}-{r2}", []).append(st)
        for r in RADARS:
            if r == "KGWX":
                continue
            for store, fields in ((vs_kgwx_before, before), (vs_kgwx_after, after)):
                st = reflcheck.pair_stats(
                    fields[r],
                    fields["KGWX"],
                    Z_KM,
                    X,
                    X,
                    prep[r].xy_km,
                    prep[r].roi,
                    prep["KGWX"].roi,
                    roi_max_m=3000.0,
                )
                store.setdefault(r, []).append(st)
        # merged v1, v2 vs KGWX, MRMS
        v1 = np.load(b / man1["steps"][si]["volume"])["dbz"]
        v2 = np.load(b / man2["steps"][si]["volume"])["dbz"]
        mr = (
            regrid_mrms(b / man1["steps"][si]["mrms"], proj)
            if man1["steps"][si].get("mrms")
            else None
        )
        row = {"step": si, "time": s["time"]}
        kg = before["KGWX"]
        for lab, fld in (("v1", v1), ("v2", v2)):
            row[f"{lab}_vs_KGWX"] = reflcheck.vs_reference(fld, kg, Z_KM)
            if mr is not None:
                row[f"{lab}_vs_MRMS"] = stats2d(column_at_level(fld), mr)
                row[f"{lab}_vs_MRMS_2p5km"] = stats2d(smooth2d(column_at_level(fld)), smooth2d(mr))
        for r in RADARS:
            if mr is not None:
                row[f"{r}_after_vs_MRMS"] = stats2d(column_at_level(after[r]), mr)
                row[f"{r}_before_vs_MRMS"] = stats2d(column_at_level(before[r]), mr)
        if mr is not None:
            row["KGWX_vs_MRMS"] = stats2d(column_at_level(kg), mr)
            row["KGWX_vs_MRMS_2p5km"] = stats2d(smooth2d(column_at_level(kg)), smooth2d(mr))
        merged_rows.append(row)
        print("step", si, "done", flush=True)

    def med_pairs(store):
        res = {}
        for k, rows in store.items():
            med = [r["median_db"] for r in rows if "median_db" in r]
            sg = [r["robust_sigma_db"] for r in rows if "robust_sigma_db" in r]
            n = sum(r.get("n", 0) for r in rows)
            cr = [r["corr"] for r in rows if r.get("corr") is not None]
            res[k] = {
                "median_bias_db": round(float(np.median(med)), 2) if med else None,
                "robust_sigma_db": round(float(np.median(sg)), 2) if sg else None,
                "corr": round(float(np.median(cr)), 2) if cr else None,
                "n_cells": n,
            }
        return res

    num["pairs_before"] = med_pairs(pair_before)
    num["pairs_after"] = med_pairs(pair_after)
    num["pairs_after_without_advection"] = med_pairs(pair_noadv)
    num["vs_KGWX_before"] = med_pairs(vs_kgwx_before)
    num["vs_KGWX_after"] = med_pairs(vs_kgwx_after)

    def by_range(store):
        res = {}
        for r, rows in store.items():
            agg = {}
            for st in rows:
                for k, v in st.get("by_range_km", {}).items():
                    agg.setdefault(k, []).append(v["median_db"])
            res[r] = {k: round(float(np.median(v)), 2) for k, v in agg.items()}
        return res

    num["vs_KGWX_by_range_before"] = by_range(vs_kgwx_before)
    num["vs_KGWX_by_range_after"] = by_range(vs_kgwx_after)

    def agg_rows(key):
        rows = [r[key] for r in merged_rows if key in r and "median_db" in r[key]]
        if not rows:
            return None
        return {
            "median_bias_db": round(float(np.median([r["median_db"] for r in rows])), 2),
            "mean_bias_db": round(float(np.mean([r["mean_db"] for r in rows])), 2),
            "rms_db": round(float(np.mean([r["rms_db"] for r in rows])), 2),
            "std_db": round(float(np.mean([r["std_db"] for r in rows])), 2),
            "corr": round(float(np.mean([r["corr"] for r in rows])), 3),
        }

    num["merged_vs_KGWX"] = {
        k: {
            kk: round(float(np.median([r[k][kk] for r in merged_rows])), 2)
            for kk in ("median_db", "robust_sigma_db", "rms_db", "corr")
        }
        for k in ("v1_vs_KGWX", "v2_vs_KGWX")
    }
    num["merged_vs_MRMS"] = {
        k: agg_rows(k)
        for k in (
            "v1_vs_MRMS",
            "v2_vs_MRMS",
            "KGWX_vs_MRMS",
            "v1_vs_MRMS_2p5km",
            "v2_vs_MRMS_2p5km",
            "KGWX_vs_MRMS_2p5km",
        )
    }
    num["radar_vs_MRMS"] = {
        r: {"before": agg_rows(f"{r}_before_vs_MRMS"), "after": agg_rows(f"{r}_after_vs_MRMS")}
        for r in RADARS
    }
    num["per_step_merged"] = merged_rows

    # ---- PIPS ------------------------------------------------------------------------------------------
    from datetime import datetime, timedelta

    import netCDF4

    pips_rows = []
    st = [s for s in load_json(b / man1["stations"]) if s["id"].startswith("PIPS")]
    t_start = datetime.fromisoformat(man1["window"]["start"].replace("Z", "+00:00")).replace(
        tzinfo=None
    )
    for s_ in st:
        f = next(
            iter(
                (Path(a.data).expanduser() / "PIPS_data/IOP2_033022/netcdf").glob(
                    f"parsivel_combined*_{s_['id']}_10s.nc"
                )
            )
        )
        with netCDF4.Dataset(f) as ds:
            t0 = datetime.strptime(ds.getncattr("starting_time"), "%Y%m%d%H%M%S")
            tt = np.asarray(ds["time"][:], float)
            ts = np.array(
                [(t0 + timedelta(seconds=float(x)) - t_start).total_seconds() for x in tt]
            )
            zr = np.ma.filled(ds["reflectivity_derived_qc"][:], np.nan)
            kg = np.ma.filled(ds["KGWX_at_PIPS"][:], np.nan)
            names = [str(x) for x in ds["fields_KGWX"][:]]
            kref = kg[:, names.index("REF")]
            heights = np.ma.filled(ds["KGWX_beam_height_at_PIPS"][:], np.nan)
        m = (ts >= 0) & (ts <= 960) & np.isfinite(zr) & (zr > 15)
        rec = {
            "station": s_["id"],
            "x_km": s_["x_km"],
            "y_km": s_["y_km"],
            "disdrometer_dbz_mean": round(float(np.mean(zr[m])), 1),
            "owner_KGWX_at_PIPS_mean": round(float(np.nanmean(kref[m])), 1),
            "owner_KGWX_minus_disdrometer": round(float(np.nanmean(kref[m] - zr[m])), 1),
            "owner_KGWX_beam_height_m": round(float(np.nanmean(heights[m])), 0),
        }
        for lab, man in (("v1", man1), ("v2", man2)):
            vals, refs = [], []
            for mstep in man["steps"]:
                tm = (
                    datetime.fromisoformat(mstep["time"].replace("Z", "+00:00")).replace(
                        tzinfo=None
                    )
                    - t_start
                ).total_seconds()
                sel = (np.abs(ts - tm) <= 30) & np.isfinite(zr)
                if not sel.any():
                    continue
                v = np.load(b / mstep["volume"])["dbz"]
                xi, yi = round((s_["x_km"] + 90) / 0.5), round((s_["y_km"] + 90) / 0.5)
                col = v[:, yi - 1 : yi + 2, xi - 1 : xi + 2]
                k = next((k for k in range(1, 8) if np.isfinite(col[k]).any()), None)
                if k is None:
                    continue
                vals.append(float(np.nanmean(col[k])))
                refs.append(float(np.nanmean(zr[sel])))
            if vals:
                v_, r_ = np.array(vals), np.array(refs)
                rec[f"{lab}_mean_dbz"] = round(float(v_.mean()), 1)
                rec[f"{lab}_bias_db"] = round(float((v_ - r_).mean()), 1)
                rec[f"{lab}_corr"] = (
                    round(float(np.corrcoef(v_, r_)[0, 1]), 2) if len(v_) > 3 else None
                )
                rec[f"{lab}_n"] = len(v_)
        pips_rows.append(rec)
    num["pips"] = pips_rows

    # ---- figures ---------------------------------------------------------------------------------
    tor = next(
        t for t in load_json(b / man1["tornadoes"]) if t["overlaps_window"] and t.get("in_domain")
    )
    ref_c = float(np.cos(np.radians(ORIGIN[0])))

    def pos_at(tsec):
        p = tornadoes.position_at(tor, tsec)
        return ((p[1] - ORIGIN[1]) * 111.2 * ref_c, (p[0] - ORIGIN[0]) * 111.2) if p else None

    cols1 = ["KGWX", "NOXP", "SMARTR2", "COW1", "DOW7"]
    p_ = np.array(tor["path"])
    path_x = (p_[:, 2] - ORIGIN[1]) * 111.2 * ref_c
    path_y = (p_[:, 1] - ORIGIN[0]) * 111.2
    for si in STEPS:
        pf = per_step_fields[si]
        pos = pos_at(si * 60.0)
        hhmm = man1["steps"][si]["time"][11:16].replace(":", "")
        v1f = np.load(b / man1["steps"][si]["volume"])
        v2f = np.load(b / man2["steps"][si]["volume"])
        panels = []
        for r in cols1:
            f, lev = lowest_level(pf["prep"][r].dbz)
            panels.append(
                (f"{r} (corrected, lowest level, mean {lev:.1f} km ASL)", f, (10, 65), "turbo")
            )
        f, lev = lowest_level(pf["prep"]["UAH"].dbz)
        panels.append(("UAH CREF (as supplied, excluded)", f, (10, 65), "turbo"))
        f1, _ = lowest_level(v1f["dbz"])
        f2, _ = lowest_level(v2f["dbz"])
        panels.append(("merged v1", f1, (10, 65), "turbo"))
        panels.append(("merged v2", f2, (10, 65), "turbo"))
        panels.append(("merged v2 - v1 (dB)", f2 - f1, (-12, 12), "RdBu_r"))
        fv, _ = lowest_level(v2f["vel"].astype(np.float32))
        panels.append(("merged Doppler velocity (best radar)", fv, (-30, 30), "RdBu_r"))
        fg, ax = plt.subplots(2, 5, figsize=(22, 9.6))
        for a_, (name, fld, (lo, hi), cm) in zip(ax.ravel(), panels, strict=True):
            a_.pcolormesh(X, X, fld, vmin=lo, vmax=hi, cmap=cm, shading="nearest")
            a_.set_xlim(pos[0] - 16, pos[0] + 16)
            a_.set_ylim(pos[1] - 13, pos[1] + 13)
            a_.set_aspect("equal")
            a_.plot(path_x, path_y, "k-", lw=1)
            a_.plot(*pos, "w*", ms=13, mec="k")
            a_.set_title(name, fontsize=9)
            a_.tick_params(labelsize=7)
        fg.suptitle(
            f"{hhmm}Z: reflectivity (dBZ) at each radar's lowest valid level (0.25 to 2.5 km ASL), merged v1 and v2; "
            "star = surveyed tornado position at this time, line = its surveyed path",
            fontsize=11,
        )
        fg.savefig(out / f"refl_plan_{hhmm}.png", dpi=75, bbox_inches="tight")
        plt.close(fg)

    # cross-sections through the surveyed tornado position (west-east and south-north)
    for si in (7, 10, 13):
        pos = pos_at(si * 60.0)
        hhmm = man1["steps"][si]["time"][11:16].replace(":", "")
        v1f = np.load(b / man1["steps"][si]["volume"])
        v2f = np.load(b / man2["steps"][si]["volume"])
        j = round((pos[1] + 90) / 0.5)
        i0 = round((pos[0] + 90) / 0.5)
        xs = slice(i0 - 40, i0 + 41)
        zsl = slice(0, 25)
        fg, ax = plt.subplots(5, 1, figsize=(10, 15), sharex=True)
        rows = [
            ("merged v1 dBZ", v1f["dbz"][zsl, j, xs], (0, 65), "turbo"),
            ("merged v2 dBZ", v2f["dbz"][zsl, j, xs], (0, 65), "turbo"),
            ("v2 - v1 (dB)", v2f["dbz"][zsl, j, xs] - v1f["dbz"][zsl, j, xs], (-12, 12), "RdBu_r"),
            ("v2 dbz_unc (dB, 1 sigma)", v2f["dbz_unc"][zsl, j, xs].astype(float), (0, 8), "magma"),
            ("v2 radars per voxel", v2f["nradars"][zsl, j, xs].astype(float), (0, 5), "viridis"),
        ]
        for a_, (t, f, (lo, hi), cm) in zip(ax, rows, strict=True):
            im = a_.pcolormesh(X[xs], Z_KM[zsl], f, vmin=lo, vmax=hi, cmap=cm, shading="nearest")
            plt.colorbar(im, ax=a_)
            a_.set_title(t, fontsize=10)
            a_.axvline(pos[0], color="w", ls="--")
            a_.set_ylabel("km ASL")
        ax[-1].set_xlabel("x east of the grid origin, km")
        fg.suptitle(
            f"{hhmm}Z west-east cross-section at y = {pos[1]:.1f} km through the surveyed tornado position (dashed)",
            fontsize=11,
        )
        fg.savefig(out / f"refl_xsec_{hhmm}.png", dpi=60, bbox_inches="tight")
        plt.close(fg)

    # pair bias matrices
    fg, ax = plt.subplots(1, 2, figsize=(13, 5.5))
    for a_, (lab, d) in zip(
        ax, (("before", num["pairs_before"]), ("after", num["pairs_after"])), strict=True
    ):
        M = np.full((len(RADARS), len(RADARS)), np.nan)
        for (r1, r2), v in ((k_.split("-"), v) for k_, v in d.items()):
            i, j2 = RADARS.index(r1), RADARS.index(r2)
            if v["median_bias_db"] is not None:
                M[i, j2] = v["median_bias_db"]
                M[j2, i] = -v["median_bias_db"]
        im = a_.imshow(M, vmin=-20, vmax=20, cmap="RdBu_r")
        a_.set_xticks(range(len(RADARS)), RADARS, rotation=45)
        a_.set_yticks(range(len(RADARS)), RADARS)
        for i in range(len(RADARS)):
            for j2 in range(len(RADARS)):
                if np.isfinite(M[i, j2]):
                    a_.text(j2, i, f"{M[i, j2]:.1f}", ha="center", va="center", fontsize=8)
        a_.set_title(f"median bias (row - column), dB, {lab}")
    plt.colorbar(im, ax=ax)
    fg.savefig(out / "refl_pair_bias.png", dpi=55, bbox_inches="tight")
    plt.close(fg)
    (out / "verification_refl.json").write_text(json.dumps(num, indent=1, default=str))
    if a.into_bundle:
        rel = "context/reflectivity_v2_verification.json"
        bundle.write_json(
            b / rel, bundle.clean_nan(json.loads(json.dumps(num, default=str))), indent=1
        )
        man2["reflectivity_v2"]["verification"] = {
            "file": rel,
            "script": "tools/perils_movie/verify_refl.py",
            "steps": [man1["steps"][i]["time"] for i in STEPS],
            "note": "radar pairs and radar-vs-KGWX: cells 0.5 to 3 km, both above 15 dBZ, ROI <= 3 km; 'after' leaves out voxels flagged at the attenuation cap or without KDP coverage",
        }
        bundle.write_json(b / "manifest_v2.json", bundle.clean_nan(man2), indent=1)
    print(
        json.dumps(
            {
                k: num[k]
                for k in (
                    "pairs_before",
                    "pairs_after",
                    "vs_KGWX_before",
                    "vs_KGWX_after",
                    "merged_vs_KGWX",
                    "merged_vs_MRMS",
                    "radar_vs_MRMS",
                    "pips",
                )
            },
            indent=1,
            default=str,
        )[:12000]
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

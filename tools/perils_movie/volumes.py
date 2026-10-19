"""The merged 3-D volume of one time step and everything derived from it (volumes/step_NNNN.npz).

``process_volume`` loads one radar grid (QC, heights moved to above sea level), runs the per-radar parts
(hydrometeor class, azimuthal shear, rotational velocity); ``build_step`` merges the radars of a step
(``merge.Merger``) and computes the diagnostics on the merged volume; ``save_step`` writes the npz.

Arrays in the npz (z above sea level 0..10 km every 250 m, y, x -90..90 km every 500 m):

===============  ======================  ===========================================================
name             shape / dtype           meaning
===============  ======================  ===========================================================
dbz              (nz, ny, nx) float32    merged reflectivity (dBZ), NaN = no echo / no data  [SPEC]
vel              (nz, ny, nx) float32    radial velocity (m/s) of the best radar (``vel_src``) [SPEC]
zdr, kdp, rho    (nz, ny, nx) float32    dual-pol fields of the best dual-pol radar (``pol_src``)
nradars          (nz, ny, nx) uint8      number of radars with valid reflectivity in the voxel  [SPEC]
vel_src, pol_src (nz, ny, nx) uint8      1-based index into manifest ``radars`` (0 = none)
rotation         (nz, ny, nx) float16    azimuthal shear (s-1), largest over radars, cyclonic > 0
tds              (nz, ny, nx) uint8      debris signature candidate: 1 low RHO + high Z, 2 near a vortex
hid              (nz, ny, nx) uint8      hydrometeor class, legend in the manifest (hailconv.HID_LEGEND)
2-D (ny, nx)     float32 / uint8         rotation_ll, vrot_ll, cmax, zdr_column_km, echotop18/30/40_km,
                                         conv_strat (0/1 strat/2 conv), vil, shi, mesh_mm, posh_pct,
                                         bwer (0/1), top_truncated (0/1)
time_iso         scalar str              step time
===============  ======================  ===========================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import hailconv
import merge
import numpy as np
import pgrid
import rotation

NX = NY = 361
NZ = 41
X_KM = np.arange(-90.0, 90.001, 0.5)
Y_KM = X_KM.copy()
Z_M = np.arange(NZ) * pgrid.Z_STEP_M  # metres above sea level


@dataclass
class Env:
    """Environment used by the hail and ZDR-column products: a temperature profile and its isotherms."""

    height_asl_m: np.ndarray
    temperature_k: np.ndarray
    freezing_m: float
    minus20_m: float
    source: str

    def dataset(self):
        return hailconv.temperature_dataset(self.height_asl_m, self.temperature_k)


@dataclass
class Prod:
    """Per-volume products (computed once, used by every step that takes this volume)."""

    radar: str
    grid: pgrid.RadarGrid
    hid: np.ndarray | None
    shear: np.ndarray | None  # (nz, ny, nx) azimuthal shear, ROI-gated
    vrot_ll: np.ndarray | None  # (ny, nx) largest cyclonic Vrot below 2 km
    xy_km: tuple[float, float]


def process_volume(
    ref: merge.VolumeRef,
    qc: pgrid.QCConfig,
    env: Env,
    proj,
    with_rotation: bool = True,
    zdr_cal: dict[str, Any] | None = None,
) -> Prod:
    """One radar volume: QC, optional ZDR bias correction, class, shear.

    ``zdr_cal`` (None = no correction) is a dict ``radar -> {"last": offset or None, "log": [...]}`` that
    persists between volumes: a volume without enough rain samples uses the last offset estimated for
    that radar, and without any estimate its ZDR is dropped (not trusted); all recorded in the log.
    """
    g = pgrid.load_grid(ref.path, ref.radar, qc)
    band = pgrid.RADAR_INFO.get(ref.radar, {}).get("band", "C")
    if zdr_cal is not None and "zdr" in g.fields:
        rec = zdr_cal.setdefault(ref.radar, {"last": None, "log": []})
        off, n = pgrid.estimate_zdr_offset(g.fields, Z_M)
        how = "estimated"
        if off is None:
            off, how = rec["last"], ("previous estimate" if rec["last"] is not None else "dropped")
        if off is None:
            g.fields.pop("zdr", None)
        else:
            g.fields["zdr"] = g.fields["zdr"] - np.float32(off)
            rec["last"] = off
        rec["log"].append(
            {
                "file": ref.path.name,
                "offset_db": None if off is None else round(float(off), 2),
                "samples": n,
                "how": how,
            }
        )
    hid = None
    if all(k in g.fields for k in ("zdr", "rho")):
        hid = hailconv.unified_hid(g.fields, Z_M, X_KM * 1000.0, Y_KM * 1000.0, env.dataset(), band)
    rxy = pgrid.xy_km(proj, g.lat, g.lon)
    rxy = (float(rxy[0]), float(rxy[1]))
    shear = vr = None
    if with_rotation and "vel" in g.fields:
        shear = rotation.azimuthal_shear(g.fields["vel"], X_KM, Y_KM, rxy)
        roi = g.fields.get("roi")
        if roi is not None:
            shear[roi > rotation.ROI_MAX_M] = np.nan
        low = np.nonzero(Z_M / 1000.0 <= 2.0 + 1e-6)[0]
        best = None
        for k in low:
            v = rotation.vrot(g.fields["vel"][k], X_KM, Y_KM, rxy)
            if roi is not None:
                v[roi[k] > rotation.ROI_MAX_M] = np.nan
            best = v if best is None else np.fmax(best, v)
        vr = best
    return Prod(ref.radar, g, hid, shear, vr, rxy)


def build_step(
    plan: merge.StepPlan,
    prods: dict[str, Prod],
    radar_names: list[str],
    env: Env,
    qc: pgrid.QCConfig,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    """Merge the radars of ``plan`` and compute all diagnostics. Returns (npz arrays, summary dict)."""
    shape = (NZ, NY, NX)
    m = merge.Merger(shape, radar_names)
    used: dict[str, Any] = {}
    for radar in plan.volumes:
        p = prods[radar]
        used[radar] = m.add(radar, p.grid, p.hid, p.shear)
    out = m.result(qc.min_dbz)
    dbz = out["dbz"]
    z_km = Z_M / 1000.0

    # -- rotation ------------------------------------------------------------------------------------
    as3 = out.pop("as_max")
    as_src = out.pop("as_src")
    as_ll = rotation.layer_max(as3, z_km, 0.0, 2.0)
    vr_ll = None
    for radar in plan.volumes:
        v = prods[radar].vrot_ll
        if v is not None:
            vr_ll = v if vr_ll is None else np.fmax(vr_ll, v)
    # which radar gave the 0-2 km maximum (for the vortex records)
    src_ll = np.zeros(as_ll.shape, np.uint8)
    if np.isfinite(as3).any():
        sel = z_km <= 2.0 + 1e-6
        sub = np.where(np.isfinite(as3[sel]), as3[sel], -np.inf)
        kmax = np.argmax(sub, axis=0)
        src_ll = np.take_along_axis(as_src[sel], kmax[None], axis=0)[0]
    out["rotation"] = as3.astype(np.float16)
    out["rotation_ll"] = as_ll
    out["vrot_ll"] = vr_ll if vr_ll is not None else np.full(as_ll.shape, np.nan, np.float32)
    out["tds"] = rotation.tds_flag(dbz, out["rho"], as_ll, X_KM, Y_KM, z_km)

    # -- hail and convection ---------------------------------------------------------------------------
    hf = hailconv.hail_fields(dbz, Z_M, X_KM * 1000.0, Y_KM * 1000.0, env.freezing_m, env.minus20_m)
    out.update(hf)
    tops = hailconv.echo_tops(dbz, Z_M, X_KM * 1000.0, Y_KM * 1000.0)
    out.update(tops)
    zc = hailconv.zdr_column_km(
        out["zdr"], out["rho"], dbz, Z_M, X_KM * 1000.0, Y_KM * 1000.0, env.freezing_m, min_dbz=30.0
    )
    out["zdr_column_km"] = zc
    cm = hailconv.column_max(dbz)
    out["cmax"] = cm
    k2 = int(np.argmin(np.abs(Z_M - 2000.0)))
    out["conv_strat"] = hailconv.steiner_houze_yuter(dbz[k2], 0.5)
    out["bwer"] = hailconv.bwer(dbz, z_km, 0.5)
    ttop = (
        np.isfinite(dbz[-3:]).any(axis=0).astype(np.uint8)
    )  # echo reaches the grid top: tops are truncated
    out["top_truncated"] = ttop

    # -- summary -------------------------------------------------------------------------------------
    vortices = rotation.detect_vortices(
        as_ll, X_KM, Y_KM, rotation.AS_THRESHOLD, 2, out["vrot_ll"], src_ll, radar_names
    )
    nvox = float(np.isfinite(dbz).sum())
    hid = out["hid"]
    conv = out["conv_strat"]
    summary = {
        "radars_used": used,
        "voxels_with_echo": int(nvox),
        "mean_nradars_where_echo": float(out["nradars"][np.isfinite(dbz)].mean()) if nvox else None,
        "cmax_dbz": _nm(np.nanmax(cm) if np.isfinite(cm).any() else np.nan),
        "echotop18_max_km": _nm(
            np.nanmax(tops["echotop18_km"]) if np.isfinite(tops["echotop18_km"]).any() else np.nan
        ),
        "echotop30_max_km": _nm(
            np.nanmax(tops["echotop30_km"]) if np.isfinite(tops["echotop30_km"]).any() else np.nan
        ),
        "echotop40_max_km": _nm(
            np.nanmax(tops["echotop40_km"]) if np.isfinite(tops["echotop40_km"]).any() else np.nan
        ),
        "zdr_column_max_km": _nm(np.nanmax(zc) if np.isfinite(zc).any() else np.nan),
        "zdr_column_area_km2": float(np.isfinite(zc).sum() * 0.25),
        "convective_area_km2": float((conv == 2).sum() * 0.25),
        "stratiform_area_km2": float((conv == 1).sum() * 0.25),
        "vil_max_kgm2": _nm(np.nanmax(hf["vil"]) if np.isfinite(hf["vil"]).any() else np.nan),
        "mesh_max_mm": _nm(
            np.nanmax(hf["mesh_mm"]) if np.isfinite(hf["mesh_mm"]).any() else np.nan
        ),
        "posh_max_pct": _nm(
            np.nanmax(hf["posh_pct"]) if np.isfinite(hf["posh_pct"]).any() else np.nan
        ),
        "bwer_candidate_cells": int(out["bwer"].sum()),
        "hail_class_voxels": int(((hid == 10) | (hid == 9)).sum()),
        "tds_flag1_voxels": int((out["tds"] == 1).sum()),
        "tds_flag2_voxels": int((out["tds"] == 2).sum()),
        "rotation_ll_max_s-1": _nm(np.nanmax(as_ll) if np.isfinite(as_ll).any() else np.nan, 4),
        "vrot_ll_max_ms": _nm(
            np.nanmax(out["vrot_ll"]) if np.isfinite(out["vrot_ll"]).any() else np.nan
        ),
        "vortices": [v.as_dict() for v in vortices[:12]],
        "top_truncated_columns": int(ttop.sum()),
        "shear_line_regions": int(getattr(rotation.detect_vortices, "last_n_ribbons", 0)),
        "vortex_threshold_s-1": rotation.AS_THRESHOLD,
    }
    return out, summary


def _nm(x: float, d: int = 1):
    return None if not np.isfinite(x) else round(float(x), d)


def save_step(path: str | Path, arrays: dict[str, np.ndarray], time_iso: str) -> int:
    """Write the npz (compressed) and return its size in bytes. The SPEC v1 names come first."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.npz")
    spec = ["dbz", "vel", "zdr", "kdp", "rho", "nradars"]
    ordered = {k: arrays[k] for k in spec if k in arrays}
    ordered.update({k: v for k, v in arrays.items() if k not in ordered})
    for k in (
        "zdr",
        "kdp",
        "rho",
    ):  # float16 (3 digits): the precision of these fields is far below that
        if k in ordered:
            ordered[k] = ordered[k].astype(np.float16)
    ordered["time_iso"] = np.array(time_iso)
    np.savez_compressed(tmp, **ordered)
    tmp.replace(path)
    return path.stat().st_size

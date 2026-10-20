"""Reflectivity merge v2: clean, attenuation-correct, calibrate to the S-band radar, advect, quality-weight and blend.

Every step is a switch (``Switches``) and is logged; nothing is tuned to make the picture look better: the
calibration offsets are estimated from overlap statistics against the S-band radar (KGWX) and applied only if they are
stable across steps. See ``docs/PERILS_BUNDLE.md`` ("Reflectivity merge v2") for the numbers before and after.

Per radar (``prepare_radar``):

1. load the grid (QC of ``pgrid``); UAH uses its owner-corrected ``CREF`` and SMART-R 2 its ``DBZC``;
2. **clean** (``clean_echo``): ground clutter (|V| < 1 m/s, RHO < 0.9, below 1.5 km, below 45 dBZ), non-meteorological
   ZDR (< -2 or > 7 dB below 30 dBZ), low NCP (< 0.3 below 25 dBZ), isolated single voxels below 30 dBZ;
3. **attenuation** (``atten.py``, KDP based) for radars whose reflectivity the owner did not correct;
4. **calibration**: a constant offset (dB) per radar against the S-band radar, if stable (``estimate_calibration``);
5. **advection** to the step time along the line motion when the volume is more than ``adv_min_s`` away.

Merge (``merge_refl``): weights ``w = q_band * q_cal / ROI^2 * q_range * q_beam * q_att * q_ncp`` and a blend in linear Z over the
radars within ``core_db`` (6 dB) of the strongest reliable one (a C or X band value more than ``core_db`` above a reliable
S-band value in the same voxel is a suspect and is left out; reliable: unflagged, attenuation correction <= 6 dB, not
below the lowest beam by more than about 0.5 km; independent of the resolution weights; this keeps echo cores from being smeared by smoother or
attenuated radars and fills holes from the next best), plus a per-voxel uncertainty ``dbz_unc`` (dB), a quality
(0 to 100), the best radar, the mean attenuation correction and the number of radars.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import atten
import numpy as np
import pgrid
import winds
from scipy import ndimage

BAND_RELIABILITY = {"S": 1.0, "C": 0.85, "X": 0.6}
#: radars whose supplied reflectivity is already corrected by the owner (checked against the S-band radar)
OWNER_CORRECTED = {"UAH": "CREF", "SMARTR2": "DBZC"}
LOWEST_ELEV_DEG = 0.5  # assumed lowest tilt of every radar (not in the grids)
EARTH_4_3_KM = 4.0 / 3.0 * 6371.0


@dataclass
class Switches:
    clean: bool = True
    attenuation: bool = True
    calibration: bool = True
    advection: bool = True
    adv_min_s: float = 60.0
    core_blend: bool = True
    core_db: float = 6.0
    use_owner_corrected: bool = True  # UAH CREF, SMART-R 2 DBZC
    min_dbz: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Prepared:
    radar: str
    band: str
    dbz: np.ndarray  # final (corrected, calibrated, advected)
    dbz_raw: np.ndarray  # as loaded (QC only)
    roi: np.ndarray
    corr_db: np.ndarray  # attenuation correction included in ``dbz`` (advected like it)
    flags: np.ndarray  # bit 1 at cap, bit 2 low KDP coverage
    ncp: np.ndarray | None
    xy_km: tuple[float, float]
    alt_m: float
    offset_s: float
    calib_db: float = 0.0
    calib_unc_db: float = 3.0
    excluded: bool = False  # left out of the reflectivity blend (non-linear response)
    log: dict[str, Any] = field(default_factory=dict)


def clean_echo(f: dict[str, np.ndarray], z_km: np.ndarray) -> tuple[np.ndarray, dict[str, int]]:
    """Cleaned reflectivity and the counts removed per criterion."""
    dbz = f["dbz"].copy()
    counts: dict[str, int] = {}
    vel, rho, zdr, ncp = f.get("vel"), f.get("rho"), f.get("zdr"), f.get("ncp")
    low = (z_km <= 1.5)[:, None, None]
    if vel is not None and rho is not None:
        bad = (
            np.isfinite(dbz)
            & np.isfinite(vel)
            & np.isfinite(rho)
            & (np.abs(vel) < 1.0)
            & (rho < 0.9)
            & low
            & (dbz < 45.0)
        )
        counts["ground_clutter"] = int(bad.sum())
        dbz[bad] = np.nan
    if zdr is not None:
        bad = np.isfinite(dbz) & np.isfinite(zdr) & ((zdr < -2.0) | (zdr > 7.0)) & (dbz < 30.0)
        counts["zdr_nonmet"] = int(bad.sum())
        dbz[bad] = np.nan
    if ncp is not None:
        bad = np.isfinite(dbz) & np.isfinite(ncp) & (ncp < 0.3) & (dbz < 25.0)
        counts["low_ncp"] = int(bad.sum())
        dbz[bad] = np.nan
    ok = np.isfinite(dbz).astype(np.float32)
    nb = ndimage.uniform_filter(ok, size=(1, 3, 3), mode="constant") * 9.0 - ok
    bad = np.isfinite(dbz) & (nb <= 2.0) & (dbz < 30.0)
    counts["isolated"] = int(bad.sum())
    dbz[bad] = np.nan
    counts["echo_before"] = int(np.isfinite(f["dbz"]).sum())
    counts["echo_after"] = int(np.isfinite(dbz).sum())
    return dbz, counts


def _shift(a: np.ndarray, off_s: float, motion: tuple[float, float]) -> np.ndarray:
    return winds.advect_field(a, -motion[0] * off_s, -motion[1] * off_s)


def prepare_radar(
    path,
    radar: str,
    qc: pgrid.QCConfig,
    x_km: np.ndarray,
    y_km: np.ndarray,
    z_km: np.ndarray,
    proj,
    offset_s: float,
    motion: tuple[float, float],
    sw: Switches,
    calib: dict[str, Any] | None = None,
    raw_only: bool = False,
) -> Prepared:
    """Load and prepare one radar volume. ``raw_only`` stops before calibration (for the calibration estimate)."""
    band = pgrid.RADAR_INFO.get(radar, {}).get("band", "C")
    ref_var = OWNER_CORRECTED.get(radar) if sw.use_owner_corrected else None
    if radar == "SMARTR2":
        ref_var = None  # pgrid already takes DBZC for SMART-R 2
    g = pgrid.load_grid(path, radar, qc, ref_var=ref_var)
    rxy = pgrid.xy_km(proj, g.lat, g.lon)
    rxy = (float(rxy[0]), float(rxy[1]))
    log: dict[str, Any] = {"ref_var": ref_var or ("DBZC" if radar == "SMARTR2" else "REF")}
    raw = g.fields["dbz"].copy()
    dbz = raw
    if sw.clean:
        dbz, counts = clean_echo(g.fields, z_km)
        log["clean"] = counts
    corr = np.zeros(dbz.shape, np.float32)
    flags = np.zeros(dbz.shape, np.uint8)
    owner = radar in OWNER_CORRECTED and sw.use_owner_corrected
    if sw.attenuation and not owner:
        res = atten.attenuation_correction(
            dbz, g.fields.get("kdp"), g.fields.get("rho"), x_km, y_km, z_km, rxy, band
        )
        dbz, corr, flags = res["dbz"], res["corr_db"], res["flags"]
        log["attenuation"] = res["stats"]
        log["attenuation"]["applied"] = res["applied"]
    elif owner:
        log["attenuation"] = {
            "applied": False,
            "reason": "reflectivity supplied corrected by the owner",
        }
    else:
        log["attenuation"] = {"applied": False, "reason": "switched off"}
    roi = g.fields.get("roi", np.full(dbz.shape, 2000.0, np.float32))
    ncp = g.fields.get("ncp")
    cal_db, cal_unc = 0.0, 3.0
    if not raw_only and sw.calibration and calib and radar in calib and calib[radar].get("applied"):
        cal_db = float(calib[radar]["offset_db"])
        cal_unc = float(calib[radar]["uncertainty_db"])
        dbz = np.where(np.isfinite(dbz), dbz - cal_db, np.nan).astype(np.float32)
    elif radar == "KGWX":
        cal_unc = 1.5
    if sw.advection and abs(offset_s) > sw.adv_min_s:
        dbz = _shift(dbz, offset_s, motion)
        corr = _shift(corr, offset_s, motion)
        fl = _shift(flags.astype(np.float32), offset_s, motion)
        flags = np.where(np.isfinite(fl), (fl > 0.5).astype(np.uint8), 0).astype(np.uint8)
        roi = _shift(roi, offset_s, motion)
        if ncp is not None:
            ncp = _shift(ncp, offset_s, motion)
        raw = _shift(raw, offset_s, motion)
        log["advected_s"] = round(offset_s, 1)
    corr = np.nan_to_num(corr)
    excluded = bool(
        not raw_only and sw.calibration and calib and calib.get(radar, {}).get("exclude_from_blend")
    )
    if excluded:
        log["excluded_from_blend"] = calib[radar].get("reason")
    return Prepared(
        radar,
        band,
        dbz,
        raw,
        roi,
        corr,
        flags,
        ncp,
        rxy,
        g.alt_m,
        offset_s,
        cal_db,
        cal_unc,
        excluded,
        log,
    )


# ---------------------------------------------------------------------------
# calibration against the S-band reference
# ---------------------------------------------------------------------------


def _homogeneous(ref: np.ndarray, max_std_db: float = 3.0) -> np.ndarray:
    """Cells where the reference is locally uniform (5 x 5 standard deviation below ``max_std_db``)."""
    ok = np.isfinite(ref)
    v = np.where(ok, ref, 0.0).astype(np.float32)
    n = ndimage.uniform_filter(ok.astype(np.float32), size=(1, 5, 5), mode="constant")
    m1 = ndimage.uniform_filter(v, size=(1, 5, 5), mode="constant")
    m2 = ndimage.uniform_filter(v * v, size=(1, 5, 5), mode="constant")
    with np.errstate(invalid="ignore", divide="ignore"):
        var = np.where(n > 0.9, m2 / np.maximum(n, 1e-9) - (m1 / np.maximum(n, 1e-9)) ** 2, np.inf)
    return np.sqrt(np.maximum(var, 0.0)) <= max_std_db


def offset_vs_reference(
    p: Prepared,
    ref_dbz: np.ndarray,
    ref_roi: np.ndarray,
    x_km: np.ndarray,
    y_km: np.ndarray,
    z_km: np.ndarray,
    max_range_km: float = 40.0,
    max_corr_db: float = 4.0,
    min_dbz: float = 20.0,
    zlo: float = 1.0,
    zhi: float = 3.0,
) -> dict[str, Any]:
    """Median of (radar - reference) in cells where the comparison is clean: low attenuation, near the radar,
    1 to 3 km, both above ``min_dbz``, both with a radius of influence <= 3 km, locally uniform reference."""
    zs = (z_km >= zlo) & (z_km <= zhi)
    X, Y = np.meshgrid(x_km, y_km)
    R = np.hypot(X - p.xy_km[0], Y - p.xy_km[1])
    a = p.dbz[zs] + 0.0
    # the offset is estimated on the corrected field BEFORE any calibration offset
    b = ref_dbz[zs]
    ok = (
        np.isfinite(a)
        & np.isfinite(b)
        & (a > min_dbz)
        & (b > min_dbz)
        & (R[None] <= max_range_km)
        & (p.corr_db[zs] <= max_corr_db)
        & ((p.flags[zs] & 3) == 0)
        & (p.roi[zs] <= 3000.0)
        & (ref_roi[zs] <= 3000.0)
        & _homogeneous(ref_dbz)[zs]
    )
    d = (a - b)[ok]
    out: dict[str, Any] = {"n": int(d.size)}
    if d.size < 500:
        return out
    out["median_db"] = float(np.median(d))
    out["robust_sigma_db"] = float((np.percentile(d, 84) - np.percentile(d, 16)) / 2.0)
    bins = {}
    bb = b[ok]
    for lo, hi in ((20, 30), (30, 40), (40, 70)):
        m = (bb >= lo) & (bb < hi)
        if m.sum() > 200:
            bins[f"ref {lo}-{hi} dBZ"] = round(float(np.median(d[m])), 2)
    out["median_by_reference_level"] = bins
    return out


def estimate_calibration(
    per_step: dict[str, list[dict[str, Any]]],
    min_steps: int = 4,
    max_step_std_db: float = 1.5,
    min_abs_db: float = 0.3,
    nonlinear_db: float = 4.0,
) -> dict[str, Any]:
    """Constant offset per radar from the per-step medians; applied only if stable.

    ``per_step[radar]`` = list of ``offset_vs_reference`` results. The offset is the median over the steps of the
    per-step medians; the uncertainty is ``max(1 dB, std over steps)`` combined with 1.5 dB for the reference itself
    (KGWX is 2 to 4 dB below the disdrometers: the reference is a convention, not the truth).
    """
    out: dict[str, Any] = {}
    for radar, rows in per_step.items():
        vals = [r["median_db"] for r in rows if "median_db" in r]
        rec: dict[str, Any] = {
            "per_step_median_db": [round(v, 2) for v in vals],
            "n_cells": int(sum(r.get("n", 0) for r in rows)),
        }
        if len(vals) < min_steps:
            rec.update(
                {
                    "applied": False,
                    "offset_db": None,
                    "reason": f"only {len(vals)} steps with enough overlap",
                }
            )
        else:
            off = float(np.median(vals))
            sd = float(np.std(vals))
            rec.update({"offset_db": round(off, 2), "std_over_steps_db": round(sd, 2)})
            if sd > max_step_std_db:
                rec.update(
                    {
                        "applied": False,
                        "reason": f"not stable over the steps (std {sd:.1f} dB > {max_step_std_db} dB)",
                    }
                )
            elif abs(off) < min_abs_db:
                rec.update({"applied": False, "reason": "offset below 0.3 dB: not applied"})
            else:
                rec.update(
                    {"applied": True, "uncertainty_db": round(math.hypot(max(1.0, sd), 1.5), 2)}
                )
        levels: dict[str, list[float]] = {}
        for r in rows:
            for k, v in (r.get("median_by_reference_level") or {}).items():
                levels.setdefault(k, []).append(v)
        bylev = {k: round(float(np.median(v)), 2) for k, v in levels.items()}
        rec["by_reference_level"] = bylev
        # a constant offset is only meaningful if the difference does not depend on the reflectivity: 20-30 and
        # 30-40 dBZ bins differing by more than 4 dB mean a compressed / non-linear response (UAH): not usable
        lo, hi = bylev.get("ref 20-30 dBZ"), bylev.get("ref 30-40 dBZ")
        rec["exclude_from_blend"] = False
        if lo is not None and hi is not None and abs(lo - hi) > nonlinear_db:
            rec["exclude_from_blend"] = True
            rec["applied"] = False
            rec["reason"] = (
                f"the offset depends on the reflectivity ({lo:+.1f} dB at 20-30 dBZ, {hi:+.1f} dB at 30-40 dBZ): "
                "a non-linear response, not a calibration constant; excluded from the reflectivity blend"
            )
        out[radar] = rec
    return out


# ---------------------------------------------------------------------------
# the merge
# ---------------------------------------------------------------------------


def weights_for(
    p: Prepared, x_km: np.ndarray, y_km: np.ndarray, z_km: np.ndarray
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Quality weights (nz, ny, nx) of one radar and the sigma (dB) of each voxel."""
    X, Y = np.meshgrid(x_km, y_km)
    R = np.hypot(X - p.xy_km[0], Y - p.xy_km[1])  # ground range km
    roi_km = np.maximum(p.roi / 1000.0, 0.5)
    q_roi = 1.0 / roi_km**2
    q_range = (1.0 / (1.0 + (R / 60.0) ** 2))[None]
    h_beam = R * math.sin(math.radians(LOWEST_ELEV_DEG)) + R**2 / (
        2.0 * EARTH_4_3_KM
    )  # km above the radar
    h_vox = (z_km - p.alt_m / 1000.0)[:, None, None]
    below = np.maximum(h_beam[None] - h_vox, 0.0)  # voxel under the lowest beam: filled from above
    q_beam = 1.0 / (1.0 + (below / 0.5) ** 2)
    q_att = 1.0 / (1.0 + (p.corr_db / 4.0) ** 2)
    q_att = np.where((p.flags & 1) > 0, q_att * 0.1, q_att)
    q_att = np.where((p.flags & 2) > 0, q_att * 0.5, q_att)
    q_ncp = np.where(np.isfinite(p.ncp) & (p.ncp < 0.5), 0.6, 1.0) if p.ncp is not None else 1.0
    q_cal = 1.0 if p.calib_unc_db <= 2.5 else 0.6
    w = (BAND_RELIABILITY.get(p.band, 0.8) * q_cal) * q_roi * q_range * q_beam * q_att * q_ncp
    w = np.where(np.isfinite(p.dbz), w, 0.0).astype(np.float32)
    sigma2 = p.calib_unc_db**2 + (atten.UNCERTAINTY_FRACTION * p.corr_db) ** 2 + (0.4 * roi_km) ** 2
    return w, {"sigma2": sigma2.astype(np.float32), "q_beam": q_beam.astype(np.float32)}


#: a voxel can anchor the echo core only if its attenuation correction is at most this (dB), it is not flagged, and
#: the lowest assumed beam is not more than about 0.5 km above it
RELIABLE_MAX_CORR_DB = 6.0
RELIABLE_MIN_Q_BEAM = 0.25


def is_reliable(p: Prepared, q_beam: np.ndarray) -> np.ndarray:
    """Voxels of one prepared radar that may anchor the strongest reliable core (see ``merge_refl``)."""
    ok = (p.flags & 3) == 0
    ok &= p.corr_db <= RELIABLE_MAX_CORR_DB
    ok &= q_beam >= RELIABLE_MIN_Q_BEAM
    if p.ncp is not None:
        ok &= ~(np.isfinite(p.ncp) & (p.ncp < 0.3))
    return ok & (not p.excluded)


def merge_refl(
    preps: list[Prepared],
    radar_names: list[str],
    x_km: np.ndarray,
    y_km: np.ndarray,
    z_km: np.ndarray,
    sw: Switches,
) -> dict[str, np.ndarray]:
    """Blend the prepared radars. Returns dbz, dbz_unc, quality, dbz_src, atten_corr_db, nradars."""
    nz, ny, nx = len(z_km), len(y_km), len(x_km)
    preps = [p for p in preps if not p.excluded]
    n = len(preps)
    D = np.full((n, nz, ny, nx), np.nan, np.float32)
    W = np.zeros((n, nz, ny, nx), np.float32)
    S2 = np.zeros((n, nz, ny, nx), np.float32)
    C = np.zeros((n, nz, ny, nx), np.float32)
    R = np.zeros((n, nz, ny, nx), bool)
    for i, p in enumerate(preps):
        D[i] = p.dbz
        w, extra = weights_for(p, x_km, y_km, z_km)
        W[i] = w
        S2[i] = extra["sigma2"]
        C[i] = p.corr_db
        R[i] = is_reliable(p, extra["q_beam"])
    valid = np.isfinite(D) & (W > 0)
    if sw.core_blend:
        # which radar may anchor the core depends on the quality of its measurement (not on its resolution, which only
        # sets the averaging weight): a close radar behind a heavy-rain path must not pull the core down
        # The S-band radar does not attenuate and does not resonate on hail: a C or X band radar that reads more than
        # core_db above it in the same voxel is a suspect (clutter, hail, calibration), not the core. Without a reliable
        # S-band value in the voxel there is no such check.
        is_s = np.array([p.band == "S" for p in preps])[:, None, None, None]
        s_ref = np.where(valid & R & is_s, D, -np.inf).max(axis=0)
        high = valid & ~is_s & np.isfinite(s_ref)[None] & ((s_ref + sw.core_db)[None] < D)
        valid_c = valid & ~high
        reliable = valid_c & R
        top = np.where(reliable, D, -np.inf).max(axis=0)
        cand = valid_c & ((top - sw.core_db)[None] <= D)
        # a radar below the core threshold is kept only when no reliable radar exists in the voxel
        cand = np.where(np.isfinite(top)[None], cand, valid_c)
        cand = np.where(cand.any(axis=0)[None], cand, valid)
    else:
        cand = valid
    Wc = np.where(cand, W, 0.0)
    zlin = np.where(cand, np.power(10.0, D / 10.0), 0.0)
    sw_ = Wc.sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        zm = (Wc * zlin).sum(axis=0) / sw_
        dbz = np.where(sw_ > 0, 10.0 * np.log10(np.maximum(zm, 1e-12)), np.nan).astype(np.float32)
        # spread among the candidate radars (weighted standard deviation in dB)
        dd = np.where(cand, D, 0.0)
        mean_db = (Wc * dd).sum(axis=0) / sw_
        spread2 = (Wc * (dd - mean_db[None]) ** 2).sum(axis=0) / sw_
        sig2 = (Wc * S2).sum(axis=0) / sw_ + np.where(cand.sum(axis=0) > 1, spread2, 0.0)
        corr_mean = (Wc * C).sum(axis=0) / sw_
    nrad = cand.sum(axis=0).astype(np.uint8)
    unc = np.sqrt(np.where(np.isfinite(sig2), sig2, 0.0)).astype(np.float32)
    quality = np.clip(100.0 * np.exp(-unc / 4.0), 0, 100).astype(np.uint8)
    src = (np.argmax(Wc, axis=0) + 1).astype(np.uint8)
    names_idx = np.array([radar_names.index(p.radar) + 1 for p in preps], np.uint8)
    src = names_idx[src - 1]
    gone = ~np.isfinite(dbz)
    if sw.min_dbz is not None:
        low = np.isfinite(dbz) & (dbz < sw.min_dbz)
        dbz[low] = np.nan
        gone = ~np.isfinite(dbz)
    unc[gone] = np.nan
    quality[gone] = 0
    src[gone] = 0
    nrad[gone] = 0
    corr_mean = np.where(gone, np.nan, corr_mean)
    return {
        "dbz": dbz,
        "dbz_unc": unc.astype(np.float16),
        "quality": quality,
        "dbz_src": src,
        "atten_corr_db": corr_mean.astype(np.float16),
        "nradars": nrad,
    }


# ---------------------------------------------------------------------------
# the v2 volume: v1 arrays with the new reflectivity and everything that depends on it recomputed
# ---------------------------------------------------------------------------


def rederive(
    v1: dict[str, np.ndarray], m2: dict[str, np.ndarray], env
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    """The v2 npz arrays: v1 arrays, the new ``dbz`` and ``nradars``, the quality arrays, and the dbz-dependent products
    (echo tops, VIL, SHI / MESH / POSH, ZDR column, convective / stratiform, BWER, column maximum, debris flag)
    recomputed from the new reflectivity. Velocity, dual-pol fields and the hydrometeor class of voxels that have no
    echo in v2 are removed (a voxel without echo has none)."""
    import hailconv
    import rotation
    import volumes

    arrays = {k: np.asarray(v) for k, v in v1.items() if k != "time_iso"}
    dbz = m2["dbz"]
    arrays["dbz"] = dbz
    arrays["nradars"] = m2["nradars"]
    for k in ("dbz_unc", "quality", "dbz_src", "atten_corr_db"):
        arrays[k] = m2[k]
    gone = ~np.isfinite(dbz)
    for k in ("vel", "zdr", "kdp", "rho"):
        a = arrays[k].astype(np.float32)
        a[gone] = np.nan
        arrays[k] = a
    for k in ("vel_src", "pol_src", "hid"):
        arrays[k] = np.where(gone, 0, arrays[k]).astype(np.uint8)
    z_km = volumes.Z_M / 1000.0
    xm, ym = volumes.X_KM * 1000.0, volumes.Y_KM * 1000.0
    hf = hailconv.hail_fields(dbz, volumes.Z_M, xm, ym, env.freezing_m, env.minus20_m)
    arrays.update(hf)
    tops = hailconv.echo_tops(dbz, volumes.Z_M, xm, ym)
    arrays.update(tops)
    zc = hailconv.zdr_column_km(
        arrays["zdr"], arrays["rho"], dbz, volumes.Z_M, xm, ym, env.freezing_m, min_dbz=30.0
    )
    arrays["zdr_column_km"] = zc
    arrays["cmax"] = hailconv.column_max(dbz)
    k2 = int(np.argmin(np.abs(volumes.Z_M - 2000.0)))
    arrays["conv_strat"] = hailconv.steiner_houze_yuter(dbz[k2], 0.5)
    arrays["bwer"] = hailconv.bwer(dbz, z_km, 0.5)
    arrays["top_truncated"] = np.isfinite(dbz[-3:]).any(axis=0).astype(np.uint8)
    arrays["tds"] = rotation.tds_flag(
        dbz, arrays["rho"], arrays["rotation_ll"], volumes.X_KM, volumes.Y_KM, z_km
    )

    def mx(a, d=1):
        return None if not np.isfinite(a).any() else round(float(np.nanmax(a)), d)

    echo = np.isfinite(dbz)
    summ = {
        "voxels_with_echo": int(echo.sum()),
        "mean_nradars_where_echo": float(arrays["nradars"][echo].mean()) if echo.any() else None,
        "cmax_dbz": mx(arrays["cmax"]),
        "echotop18_max_km": mx(tops["echotop18_km"]),
        "echotop30_max_km": mx(tops["echotop30_km"]),
        "echotop40_max_km": mx(tops["echotop40_km"]),
        "zdr_column_max_km": mx(zc),
        "convective_area_km2": float((arrays["conv_strat"] == 2).sum() * 0.25),
        "stratiform_area_km2": float((arrays["conv_strat"] == 1).sum() * 0.25),
        "vil_max_kgm2": mx(hf["vil"]),
        "mesh_max_mm": mx(hf["mesh_mm"]),
        "posh_max_pct": mx(hf["posh_pct"]),
        "bwer_candidate_cells": int(arrays["bwer"].sum()),
        "hail_class_voxels": int(((arrays["hid"] == 10) | (arrays["hid"] == 9)).sum()),
        "tds_flag1_voxels": int((arrays["tds"] == 1).sum()),
        "tds_flag2_voxels": int((arrays["tds"] == 2).sum()),
        "dbz_unc_median_db": float(np.nanmedian(arrays["dbz_unc"].astype(np.float32)))
        if echo.any()
        else None,
        "quality_median": float(np.median(arrays["quality"][echo])) if echo.any() else None,
    }
    return arrays, summ

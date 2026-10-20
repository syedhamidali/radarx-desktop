"""Rain attenuation correction of reflectivity from KDP on the Cartesian radar grids (C and X band, small at S band).

Method (``attenuation_correction``): the specific (one-way) attenuation in rain is proportional to the specific
differential phase, ``A = alpha * KDP`` (dB/km, KDP in deg/km), the relation of Bringi et al. (1990) and
Carey et al. (2000) at C band and of Park et al. (2005) and Matrosov (2005) at X band. The two-way path-integrated
attenuation to a voxel is ``PIA = 2 * alpha * integral(KDP dr)`` along the radar beam; the corrected reflectivity is
``Z + PIA``. The coefficients are ``alpha`` = 0.015 (S), 0.08 (C), 0.28 (X) dB/km per deg/km: values quoted from
the literature as remembered, NOT checked against the papers, and they depend on temperature and drop shape (X band
varies by +-30 %): the uncertainty of the correction is taken as 30 % of its size.

What it does on the grid: for every height level the KDP field of the radar is resampled to polar coordinates around the
radar (720 azimuths, 0.5 km range steps), negative KDP and KDP above 20 deg/km are limited, KDP is integrated along
the range, and the result is resampled back to the cells. **Limits**, all recorded:

* the integration is along the horizontal ray at the height of the cell (a real beam climbs: at 50 km a 0.5 degree beam is
  0.5 km higher at the cell than at the radar): fine for the 0.5 to 3 km layer, wrong for cells above the melting layer
  (ice does not attenuate: the integral is limited to cells below ``z_max_km``, default 3.5 km, and held constant above);
* gaps in the KDP (no echo, QC-rejected gates) count as zero attenuation: the share of echo gates along the ray with a
  valid KDP is the ``coverage``; where it is below 0.5 the correction is flagged unreliable (C and X band only: the
  S-band correction is capped at 1 dB, so a missing KDP there is not worth a flag);
* the correction is capped (``CAP_DB``: 1, 6, 12 dB at S, C, X); cells at the cap are flagged (C and X band);
* KDP derived from noisy PHIDP at X band is itself noisy; heavy rain beyond the cap (more than about 12 dB at X band)
  cannot be recovered: the voxel is flagged and down-weighted in the merge, not corrected.

No correction is made for a radar without KDP (SMART-R 2, whose KDP is unusable) or whose supplied reflectivity is already
corrected by its owner and verified against the S-band radar (see ``docs/PERILS_BUNDLE.md``).
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy import ndimage

#: one-way specific attenuation per KDP, dB/km per deg/km
ALPHA = {"S": 0.015, "C": 0.08, "X": 0.28}
CAP_DB = {"S": 1.0, "C": 6.0, "X": 12.0}
KDP_MAX = 20.0
#: the low-KDP-coverage flag is only raised where the cap is above this (C and X band)
LOW_COVERAGE_MIN_CAP_DB = 2.0
UNCERTAINTY_FRACTION = 0.30


def _polar_axes(max_range_km: float, dr_km: float, n_az: int) -> tuple[np.ndarray, np.ndarray]:
    r = np.arange(0.0, max_range_km + dr_km / 2.0, dr_km)
    a = np.arange(n_az) * (2.0 * np.pi / n_az)
    return r, a


def pia_two_way_level(
    kdp2d: np.ndarray,
    echo2d: np.ndarray,
    x_km: np.ndarray,
    y_km: np.ndarray,
    radar_xy_km: tuple[float, float],
    alpha: float,
    dr_km: float = 0.5,
    n_az: int = 720,
    max_range_km: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Two-way path-integrated attenuation (dB) and the KDP coverage along the ray, both on the grid (ny, nx)."""
    dx = float(x_km[1] - x_km[0])
    if max_range_km is None:
        max_range_km = float(
            np.hypot(
                max(abs(x_km[0] - radar_xy_km[0]), abs(x_km[-1] - radar_xy_km[0])),
                max(abs(y_km[0] - radar_xy_km[1]), abs(y_km[-1] - radar_xy_km[1])),
            )
            + 1.0
        )
    r, a = _polar_axes(max_range_km, dr_km, n_az)
    A, Rr = np.meshgrid(a, r, indexing="ij")
    px = radar_xy_km[0] + Rr * np.sin(A)
    py = radar_xy_km[1] + Rr * np.cos(A)
    ci = (px - x_km[0]) / dx
    cj = (py - y_km[0]) / dx
    ok = np.isfinite(kdp2d)
    k0 = np.where(ok, np.clip(kdp2d, 0.0, KDP_MAX), 0.0)
    kp = ndimage.map_coordinates(k0, [cj, ci], order=1, mode="constant", cval=0.0)
    vk = (
        ndimage.map_coordinates(ok.astype(np.float32), [cj, ci], order=1, mode="constant", cval=0.0)
        > 0.5
    )
    ec = (
        ndimage.map_coordinates(
            echo2d.astype(np.float32), [cj, ci], order=1, mode="constant", cval=0.0
        )
        > 0.5
    )
    # trapezoid integral along range (km * deg/km = deg), PIA = 2 alpha * integral
    integ = np.concatenate(
        [np.zeros((n_az, 1)), np.cumsum(0.5 * (kp[:, 1:] + kp[:, :-1]) * dr_km, axis=1)], axis=1
    )
    pia_polar = 2.0 * alpha * integ
    n_echo = np.cumsum(ec, axis=1)
    n_valid = np.cumsum(ec & vk, axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        cov_polar = np.where(n_echo > 0, n_valid / np.maximum(n_echo, 1), 1.0)
    # back to the grid cells
    X, Y = np.meshgrid(x_km, y_km)
    gx, gy = X - radar_xy_km[0], Y - radar_xy_km[1]
    rr = np.hypot(gx, gy)
    az = np.mod(np.arctan2(gx, gy), 2.0 * np.pi)
    ia = az / (2.0 * np.pi / n_az)
    ir = rr / dr_km
    pia = ndimage.map_coordinates(pia_polar, [ia, ir], order=1, mode="grid-wrap")
    cov = ndimage.map_coordinates(cov_polar, [ia, ir], order=1, mode="grid-wrap")
    return pia.astype(np.float32), cov.astype(np.float32)


def attenuation_correction(
    dbz: np.ndarray,
    kdp: np.ndarray | None,
    rho: np.ndarray | None,
    x_km: np.ndarray,
    y_km: np.ndarray,
    z_km: np.ndarray,
    radar_xy_km: tuple[float, float],
    band: str,
    z_max_km: float = 3.5,
    cap_db: float | None = None,
    alpha: float | None = None,
) -> dict[str, Any]:
    """Corrected reflectivity and the diagnostics of the correction for one radar volume ``(nz, ny, nx)``.

    Returns ``{"dbz": corrected, "corr_db": correction added, "flags": uint8 (bit 1: at the cap, bit 2: KDP
    coverage below 0.5), "applied": bool, "stats": {...}}``. Without KDP nothing is changed (``applied`` False).
    """
    alpha = ALPHA[band] if alpha is None else alpha
    cap = CAP_DB[band] if cap_db is None else cap_db
    if kdp is None or not np.isfinite(kdp).any():
        return {
            "dbz": dbz,
            "corr_db": np.zeros(dbz.shape, np.float32),
            "flags": np.zeros(dbz.shape, np.uint8),
            "applied": False,
            "stats": {"reason": "no usable KDP"},
        }
    k = kdp
    if rho is not None:
        k = np.where(
            np.isfinite(rho) & (rho < 0.9), np.nan, kdp
        )  # non-meteorological / mixed: not rain KDP
    corr = np.zeros(dbz.shape, np.float32)
    cov_all = np.ones(dbz.shape, np.float32)
    top = int(np.searchsorted(z_km, z_max_km, side="right"))
    for kk in range(min(top, dbz.shape[0])):
        echo = np.isfinite(dbz[kk])
        if not echo.any():
            continue
        pia, cov = pia_two_way_level(k[kk], echo, x_km, y_km, radar_xy_km, alpha)
        corr[kk] = pia
        cov_all[kk] = cov
    if top < dbz.shape[0]:  # above the limit: keep the correction of the highest corrected level
        corr[top:] = corr[top - 1]
        cov_all[top:] = cov_all[top - 1]
    flags = np.zeros(dbz.shape, np.uint8)
    if cap > LOW_COVERAGE_MIN_CAP_DB:
        # at S band the correction is capped at 1 dB: reaching the cap, or a missing KDP, leaves an error of the order
        # of the calibration uncertainty and must not down-weight the S-band reference (KGWX hit the cap in 5 % of its
        # echo, in the storm core, and a whole sector had no valid KDP)
        flags[corr >= cap] = 1
        flags[cov_all < 0.5] |= 2
    corr = np.minimum(corr, cap)
    ok = np.isfinite(dbz)
    out = np.where(ok, dbz + corr, np.nan).astype(np.float32)
    vals = corr[ok]
    stats = {
        "band": band,
        "alpha_dB_per_km_per_degkm_oneway": alpha,
        "cap_db": cap,
        "echo_voxels": int(ok.sum()),
        "correction_db_median": round(float(np.median(vals)), 2) if vals.size else None,
        "correction_db_p90": round(float(np.percentile(vals, 90)), 2) if vals.size else None,
        "correction_db_max": round(float(vals.max()), 2) if vals.size else None,
        "fraction_corrected_over_1db": round(float((vals > 1.0).mean()), 3) if vals.size else None,
        "fraction_at_cap": round(float(((flags & 1) > 0)[ok].mean()), 4) if vals.size else None,
        "fraction_low_kdp_coverage": round(float(((flags & 2) > 0)[ok].mean()), 4)
        if vals.size
        else None,
        "uncertainty_fraction_of_correction": UNCERTAINTY_FRACTION,
    }
    return {"dbz": out, "corr_db": corr, "flags": flags, "applied": True, "stats": stats}

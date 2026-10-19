"""Rotation evidence from gridded Doppler velocity: azimuthal shear, rotational velocity, vortex
detection and a debris (TDS) candidate flag. All of it is RETRIEVED from radar, not observed.

Method (``docs/PERILS_BUNDLE.md`` has the limits)
-------------------------------------------------
* **Azimuthal shear** (s-1): for every radar and every level a local linear least-squares plane is fitted
  to the radial velocity ``Vr(x, y)`` in a square window (default 2.5 km x 2.5 km, NaN aware, at least
  60 % of the points), as the LLSD of Smith and Elmore (2004) / Mahalik et al. (2019) does on polar
  data. The gradient is projected on the azimuthal direction of that radar, ``AS = (dVr/dx, dVr/dy) .
  (Y, -X) / R`` with ``(X, Y)`` the offset from the radar and ``R`` its length. Positive is cyclonic
  (Vr rising with azimuth: inbound to the left, outbound to the right as seen from the radar). For a
  solid-body rotation AS equals half the vertical vorticity, whatever radar sees it.
* **Rotational velocity** (m s-1): ``Vrot = (Vr(p + s e) - Vr(p - s e)) / 2`` across an azimuthal
  baseline of 2 s = 1, 2 or 3 km (largest of the three), the gate-to-gate couplet detector. It is the
  rotational velocity if the baseline is about the vortex diameter and underestimates smaller vortices.
* **Merging radars**: per voxel the largest AS of the radars that see it between 3 km and 80 km
  with a radius of influence of 2.5 km or less. The radar that gave it is recorded.
* **Vortex detection**: connected regions of the 0 to 2 km layer-maximum AS above a threshold, centre =
  AS weighted centroid, with the peak AS, the area, the largest Vrot, and an uncertainty of the position
  (grid spacing, radius of influence, time offset of the radar times the motion of the vortex).
* **Debris signature (TDS) candidate**: correlation coefficient below 0.8 with reflectivity above 30 dBZ
  below 2 km, within 2 km of a vortex. Flag 1 = low RHO + high Z only; flag 2 = also near a vortex.
  Evidence only: large drops, hail and gridding of noisy data also lower RHO, and X band
  (non-Rayleigh scattering) more so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import ndimage

AS_THRESHOLD = (
    0.012  # s-1, vortex candidates (the NWS TPA uses 0.006 on 0.5 deg tilts for any rotation)
)
AS_STRONG = 0.02  # s-1, "strong" candidates (tornado-scale shear at the 500 m grid)
MIN_RANGE_KM = 3.0
MAX_RANGE_KM = 80.0
ROI_MAX_M = 2500.0


def _kernel(offsets_x: np.ndarray, offsets_y: np.ndarray, power: tuple[int, int]) -> np.ndarray:
    k = (offsets_x[None, :] ** power[0]) * (offsets_y[:, None] ** power[1])
    return k[None, :, :].astype(np.float64)


def local_plane_gradient(
    v: np.ndarray, dx_m: float, dy_m: float, half: int = 2, min_frac: float = 0.6
) -> tuple[np.ndarray, np.ndarray]:
    """``(dV/dx, dV/dy)`` (per metre) by a local linear least-squares plane in a (2 half + 1)^2 window.

    ``v`` is ``(nz, ny, nx)`` (or 2-D) with NaN for missing points; the fit uses the valid points only
    and needs at least ``min_frac`` of the window; NaN where it cannot.
    """
    squeeze = v.ndim == 2
    a = v[None] if squeeze else v
    m = np.isfinite(a).astype(np.float64)
    vv = np.where(np.isfinite(a), a, 0.0).astype(np.float64)
    idx = np.arange(-half, half + 1, dtype=np.float64)
    ox, oy = idx * dx_m, idx * dy_m
    ones = np.ones((1, 2 * half + 1, 2 * half + 1))

    def corr(arr: np.ndarray, kern: np.ndarray) -> np.ndarray:
        return ndimage.correlate(arr, kern, mode="constant", cval=0.0)

    kx, ky = _kernel(ox, oy, (1, 0)), _kernel(ox, oy, (0, 1))
    S0 = corr(m, ones)
    Sx, Sy = corr(m, kx), corr(m, ky)
    Sxx, Sxy, Syy = (
        corr(m, _kernel(ox, oy, (2, 0))),
        corr(m, _kernel(ox, oy, (1, 1))),
        corr(m, _kernel(ox, oy, (0, 2))),
    )
    SV, SxV, SyV = corr(vv, ones), corr(vv, kx), corr(vv, ky)
    # Cramer's rule for b (dV/dx) and c (dV/dy) of V = a + b x + c y
    det = S0 * (Sxx * Syy - Sxy * Sxy) - Sx * (Sx * Syy - Sxy * Sy) + Sy * (Sx * Sxy - Sxx * Sy)
    det_b = S0 * (SxV * Syy - Sxy * SyV) - SV * (Sx * Syy - Sxy * Sy) + Sy * (Sx * SyV - SxV * Sy)
    det_c = S0 * (Sxx * SyV - SxV * Sxy) - Sx * (Sx * SyV - SxV * Sy) + SV * (Sx * Sxy - Sxx * Sy)
    n = (2 * half + 1) ** 2
    ok = (min_frac * n <= S0) & (np.abs(det) > 1e-9 * np.maximum(1.0, np.abs(S0 * Sxx * Syy)))
    with np.errstate(invalid="ignore", divide="ignore"):
        gx = np.where(ok, det_b / det, np.nan).astype(np.float32)
        gy = np.where(ok, det_c / det, np.nan).astype(np.float32)
    return (gx[0], gy[0]) if squeeze else (gx, gy)


def azimuthal_shear(
    vel: np.ndarray,
    x_km: np.ndarray,
    y_km: np.ndarray,
    radar_xy_km: tuple[float, float],
    half: int = 2,
    min_frac: float = 0.6,
    r_min_km: float = MIN_RANGE_KM,
    r_max_km: float = MAX_RANGE_KM,
) -> np.ndarray:
    """Azimuthal shear (s-1, cyclonic positive) of the radial velocity ``vel`` (nz, ny, nx) of one radar."""
    dx = float(np.median(np.diff(x_km))) * 1000.0
    dy = float(np.median(np.diff(y_km))) * 1000.0
    gx, gy = local_plane_gradient(vel, dx, dy, half, min_frac)
    X = (x_km[None, :] - radar_xy_km[0]) * 1000.0
    Y = (y_km[:, None] - radar_xy_km[1]) * 1000.0
    R = np.hypot(X, Y)
    with np.errstate(invalid="ignore", divide="ignore"):
        px, py = Y / R, -X / R  # azimuthal (clockwise) unit vector
        as_ = gx * px[None] + gy * py[None]
    far = (r_min_km * 1000.0 > R) | (r_max_km * 1000.0 < R)
    as_[:, far] = np.nan
    return as_.astype(np.float32)


def vrot(
    vel2d: np.ndarray,
    x_km: np.ndarray,
    y_km: np.ndarray,
    radar_xy_km: tuple[float, float],
    baselines_km: tuple[float, ...] = (1.0, 2.0, 3.0),
) -> np.ndarray:
    """Largest cyclonic ``(Vr(p + s e) - Vr(p - s e)) / 2`` over the baselines (m s-1) for one level."""
    dx = float(np.median(np.diff(x_km)))
    ny, nx = vel2d.shape
    jj, ii = np.meshgrid(
        np.arange(ny, dtype=np.float64), np.arange(nx, dtype=np.float64), indexing="ij"
    )
    X = x_km[None, :] - radar_xy_km[0]
    Y = y_km[:, None] - radar_xy_km[1]
    R = np.hypot(X, Y)
    with np.errstate(invalid="ignore", divide="ignore"):
        ex, ey = Y / R, -X / R
    best = np.full(vel2d.shape, np.nan, np.float32)
    v = np.where(np.isfinite(vel2d), vel2d, np.nan)
    # map_coordinates cannot take NaN: interpolate value and validity separately
    vz = np.where(np.isfinite(v), v, 0.0)
    mk = np.isfinite(v).astype(np.float64)

    def sample(oi: np.ndarray, oj: np.ndarray) -> np.ndarray:
        c = [jj + oj, ii + oi]
        val = ndimage.map_coordinates(vz, c, order=1, mode="nearest")
        w = ndimage.map_coordinates(mk, c, order=1, mode="nearest")
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(w > 0.99, val / np.maximum(w, 1e-9), np.nan)

    for L in baselines_km:
        s = (L / 2.0) / dx  # half baseline in grid cells
        a = sample(ex * s, ey * s)
        b = sample(-ex * s, -ey * s)
        d = (a - b) / 2.0
        d = np.where(np.isfinite(d), d, np.nan)
        best = np.fmax(best, d.astype(np.float32))
    best[(R < MIN_RANGE_KM) | (R > MAX_RANGE_KM)] = np.nan
    return best


@dataclass
class Vortex:
    x_km: float
    y_km: float
    peak_as: float
    mean_as: float
    area_km2: float
    vrot_ms: float | None
    z_km: float | None
    pos_unc_km: float
    radar: str | None
    n_cells: int
    centroid_x_km: float = float("nan")
    centroid_y_km: float = float("nan")
    length_km: float = float("nan")
    aspect: float = float("nan")

    def as_dict(self) -> dict[str, Any]:
        return {
            k: (None if (isinstance(v, float) and not math.isfinite(v)) else v)
            for k, v in self.__dict__.items()
        }


def layer_max(
    field3d: np.ndarray, z_km: np.ndarray, lo: float = 0.0, hi: float = 2.0
) -> np.ndarray:
    sel = (z_km >= lo - 1e-6) & (z_km <= hi + 1e-6)
    with np.errstate(all="ignore"):
        out = np.nanmax(np.where(np.isfinite(field3d[sel]), field3d[sel], -np.inf), axis=0)
    out = np.where(np.isfinite(out), out, np.nan)
    return out.astype(np.float32)


def detect_vortices(
    as_ll: np.ndarray,
    x_km: np.ndarray,
    y_km: np.ndarray,
    threshold: float = AS_THRESHOLD,
    min_cells: int = 2,
    vrot_ll: np.ndarray | None = None,
    src_ll: np.ndarray | None = None,
    radar_names: list[str] | None = None,
    pos_unc_km: float = 0.75,
    z_of_max: np.ndarray | None = None,
    max_length_km: float = 12.0,
    max_aspect: float = 4.0,
) -> list[Vortex]:
    """Connected regions of ``as_ll`` >= ``threshold`` (4-neighbour) as ``Vortex`` records, strongest first.

    The position is the cell of the largest shear (``x_km``, ``y_km``); the shear-weighted centroid, the
    length (4 sigma of the cells along their long axis) and the aspect ratio are recorded too. Regions longer
    than ``max_length_km`` or with an aspect ratio above ``max_aspect`` are shear lines (ribbons, for example
    seams of the gridding along a radar's axes or a gust-front convergence line) and are not returned as
    vortices; their number is in ``detect_vortices.last_n_ribbons``.
    """
    dx = float(np.median(np.diff(x_km)))
    a = np.where(np.isfinite(as_ll), as_ll, 0.0)
    lab, n = ndimage.label(a >= threshold)
    n_ribbons = 0
    out: list[Vortex] = []
    for i in range(1, n + 1):
        m = lab == i
        cnt = int(m.sum())
        if cnt < min_cells:
            continue
        w = a[m]
        jj, ii = np.nonzero(m)
        # shape: long axis from the covariance of the cells (4 sigma ~ full length); ribbons are shear lines
        if cnt >= 3:
            cov = np.cov(np.vstack([x_km[ii], y_km[jj]]))
            ev = np.sort(np.linalg.eigvalsh(cov))[::-1]
            length = float(4.0 * math.sqrt(max(ev[0], 0.0)) + dx)
            aspect = float(math.sqrt(ev[0] / max(ev[1], 1e-6)))
        else:
            length, aspect = dx * cnt, 1.0
        if length > max_length_km or aspect > max_aspect:
            n_ribbons += 1
            continue
        cx = float(np.sum(x_km[ii] * w) / np.sum(w))
        cy = float(np.sum(y_km[jj] * w) / np.sum(w))
        k = int(np.argmax(w))
        vr = None
        if vrot_ll is not None:
            vals = vrot_ll[m]
            vals = vals[np.isfinite(vals)]
            vr = float(np.max(vals)) if vals.size else None
        radar = None
        if src_ll is not None and radar_names:
            s = int(src_ll[jj[k], ii[k]])
            if 1 <= s <= len(radar_names):
                radar = radar_names[s - 1]
        zk = float(z_of_max[jj[k], ii[k]]) if z_of_max is not None else None
        out.append(
            Vortex(
                float(x_km[ii[k]]),
                float(y_km[jj[k]]),
                float(w[k]),
                float(np.mean(w)),
                cnt * dx * dx,
                vr,
                zk,
                pos_unc_km,
                radar,
                cnt,
                cx,
                cy,
                length,
                aspect,
            )
        )
    out.sort(key=lambda v: -v.peak_as)
    detect_vortices.last_n_ribbons = n_ribbons  # type: ignore[attr-defined]
    return out


def tds_flag(
    dbz: np.ndarray,
    rho: np.ndarray,
    as_ll: np.ndarray,
    x_km: np.ndarray,
    y_km: np.ndarray,
    z_km: np.ndarray,
    near_km: float = 2.0,
    rho_max: float = 0.8,
    dbz_min: float = 30.0,
    z_max_km: float = 2.0,
    as_thr: float = AS_THRESHOLD,
) -> np.ndarray:
    """uint8 (nz, ny, nx): 1 = RHO < ``rho_max`` and Z >= ``dbz_min`` below ``z_max_km`` and the mean RHO of the
    3 x 3 neighbours (valid ones) below ``rho_max + 0.08`` (a coherent low-RHO patch, not single noisy gates
    of an X-band radar); 2 = the same within ``near_km`` of a region of azimuthal shear >= ``as_thr``.
    Evidence only."""
    flag = np.zeros(dbz.shape, np.uint8)
    lowz = z_km <= z_max_km + 1e-6
    cand = np.isfinite(dbz) & np.isfinite(rho) & (rho < rho_max) & (dbz >= dbz_min)
    cand[~lowz] = False
    if cand.any():
        ok = np.isfinite(rho).astype(np.float32)
        filled = np.where(np.isfinite(rho), rho, 0.0).astype(np.float32)
        num = ndimage.uniform_filter(filled, size=(1, 3, 3), mode="constant") * 9.0
        den = ndimage.uniform_filter(ok, size=(1, 3, 3), mode="constant") * 9.0
        with np.errstate(invalid="ignore", divide="ignore"):
            mean3 = np.where(den >= 5.0, num / den, np.nan)
        cand &= mean3 < rho_max + 0.08
    flag[cand] = 1
    dx = float(np.median(np.diff(x_km)))
    near = ndimage.binary_dilation(
        np.where(np.isfinite(as_ll), as_ll, 0.0) >= as_thr,
        structure=np.ones((3, 3), bool),
        iterations=max(1, round(near_km / dx)),
    )
    flag[cand & near[None]] = 2
    return flag

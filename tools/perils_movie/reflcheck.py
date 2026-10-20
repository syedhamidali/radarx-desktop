"""Radar-to-radar and radar-to-reference reflectivity statistics on the common grid (the "matchable" check).

``pair_stats`` compares two radars' reflectivity in the cells both see: same grid cell, heights 0.5 to 3 km above
sea level, both above ``min_dbz``, both with a radius of influence <= ``roi_max_m`` (similar resolution). It returns the
number of cells, the median and mean difference ``a - b`` (dB), the scatter (standard deviation of the difference and
its robust version from the 16th to 84th percentiles), the correlation, and the median difference in range bins
and height bins. ``vs_reference`` does the same for a field against an arbitrary reference on the same grid
(for example the S-band radar) without the ROI condition.
"""

from __future__ import annotations

import itertools
from typing import Any

import numpy as np

RANGE_BINS_KM = (0, 20, 40, 60, 90)
HEIGHT_BINS_KM = (0.5, 1.0, 1.5, 2.0, 3.0)


def _summ(d: np.ndarray, a: np.ndarray, b: np.ndarray) -> dict[str, Any]:
    if d.size < 50:
        return {"n": int(d.size)}
    p16, p84 = np.percentile(d, [16, 84])
    return {
        "n": int(d.size),
        "median_db": round(float(np.median(d)), 2),
        "mean_db": round(float(d.mean()), 2),
        "std_db": round(float(d.std()), 2),
        "robust_sigma_db": round(float((p84 - p16) / 2.0), 2),
        "rms_db": round(float(np.sqrt((d**2).mean())), 2),
        "corr": round(float(np.corrcoef(a, b)[0, 1]), 3) if d.size > 3 else None,
    }


def pair_stats(
    a: np.ndarray,
    b: np.ndarray,
    z_km: np.ndarray,
    x_km: np.ndarray,
    y_km: np.ndarray,
    radar_a_xy: tuple[float, float],
    roi_a: np.ndarray | None = None,
    roi_b: np.ndarray | None = None,
    min_dbz: float = 15.0,
    zlo: float = 0.5,
    zhi: float = 3.0,
    roi_max_m: float = 2000.0,
    stride: int = 2,
) -> dict[str, Any]:
    """Statistics of ``a - b`` (dBZ, (nz, ny, nx)) in the cells both see. Range bins are the range from radar a."""
    zs = (z_km >= zlo) & (z_km <= zhi)
    A = a[zs][:, ::stride, ::stride]
    B = b[zs][:, ::stride, ::stride]
    ok = np.isfinite(A) & np.isfinite(B) & (min_dbz < A) & (min_dbz < B)
    if roi_a is not None:
        ok &= roi_a[zs][:, ::stride, ::stride] <= roi_max_m
    if roi_b is not None:
        ok &= roi_b[zs][:, ::stride, ::stride] <= roi_max_m
    X, Y = np.meshgrid(x_km[::stride], y_km[::stride])
    R = np.broadcast_to(np.hypot(X - radar_a_xy[0], Y - radar_a_xy[1])[None], ok.shape)
    Z = np.broadcast_to(z_km[zs][:, None, None], ok.shape)
    d = (A - B)[ok]
    out = _summ(d, A[ok], B[ok])
    out["by_range_km"] = {}
    for lo, hi in itertools.pairwise(RANGE_BINS_KM):
        m = ok & (lo <= R) & (hi > R)
        if m.sum() >= 50:
            out["by_range_km"][f"{lo}-{hi}"] = {
                "n": int(m.sum()),
                "median_db": round(float(np.median((A - B)[m])), 2),
            }
    out["by_height_km"] = {}
    for lo, hi in itertools.pairwise(HEIGHT_BINS_KM):
        m = ok & (lo <= Z) & (hi > Z)
        if m.sum() >= 50:
            out["by_height_km"][f"{lo}-{hi}"] = {
                "n": int(m.sum()),
                "median_db": round(float(np.median((A - B)[m])), 2),
            }
    return out


def vs_reference(
    a: np.ndarray,
    ref: np.ndarray,
    z_km: np.ndarray,
    min_dbz: float = 15.0,
    zlo: float = 0.5,
    zhi: float = 3.0,
    stride: int = 2,
) -> dict[str, Any]:
    zs = (z_km >= zlo) & (z_km <= zhi)
    A = a[zs][:, ::stride, ::stride]
    B = ref[zs][:, ::stride, ::stride]
    ok = np.isfinite(A) & np.isfinite(B) & (min_dbz < A) & (min_dbz < B)
    return _summ((A - B)[ok], A[ok], B[ok])

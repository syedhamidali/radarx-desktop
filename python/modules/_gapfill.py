"""Gap filling / smoothing for cleaned polar fields (shared by the QC modules).

After cleaning, small patches inside real echo are left empty (a few noisy gates in an otherwise
good region). Normalised Gaussian convolution fills them from their clean neighbours:

    value = sum(w * v * valid) / sum(w * valid)     (only where enough of the window is valid)

The *coverage* test (fraction of the window that is clean) keeps large noisy regions empty instead
of inventing data there, and gates that had no data originally are never filled.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage


def covers_full_circle(azimuth_deg: np.ndarray) -> bool:
    az = np.sort(np.asarray(azimuth_deg, dtype=float))
    if az.size < 3:
        return False
    step = float(np.median(np.diff(az)))
    return (az[-1] - az[0]) > 360.0 - 2.0 * step


def window_sigmas(
    range_m: np.ndarray, azimuth_deg: np.ndarray, radius_km: float, at_range_m: float
) -> tuple[float, float]:
    """Gaussian sigmas ``(rays, gates)`` that span about *radius_km* at *at_range_m*."""
    dr = float(np.median(np.diff(range_m))) if range_m.size > 1 else 250.0
    daz = float(np.median(np.abs(np.diff(np.sort(azimuth_deg))))) if azimuth_deg.size > 1 else 1.0
    arc = max(at_range_m, 1000.0) * np.deg2rad(max(daz, 1e-3))  # metres per ray at that range
    radius_m = radius_km * 1000.0
    sigma_rng = float(np.clip(radius_m / dr / 2.0, 0.7, 40.0))
    sigma_az = float(np.clip(radius_m / arc / 2.0, 0.5, 12.0))
    return sigma_az, sigma_rng


def normalized_gaussian(
    values: np.ndarray, valid: np.ndarray, sigma: tuple[float, float], wrap_azimuth: bool
) -> tuple[np.ndarray, np.ndarray]:
    """``(smoothed, coverage)``: NaN-aware Gaussian mean and the clean fraction of the window."""
    mode = ("wrap" if wrap_azimuth else "constant", "constant")
    v = np.where(valid, np.nan_to_num(values), 0.0)
    w = valid.astype(np.float64)
    num = ndimage.gaussian_filter(v, sigma, mode=mode, truncate=3.0)
    den = ndimage.gaussian_filter(w, sigma, mode=mode, truncate=3.0)
    full = ndimage.gaussian_filter(np.ones_like(w), sigma, mode=mode, truncate=3.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        smoothed = np.where(den > 1e-9, num / den, np.nan)
        coverage = den / np.maximum(full, 1e-12)
    return smoothed, coverage


def fill_gaps(
    values: np.ndarray,
    keep: np.ndarray,
    candidates: np.ndarray,
    *,
    sigma: tuple[float, float],
    min_coverage: float,
    wrap_azimuth: bool,
) -> tuple[np.ndarray, np.ndarray]:
    """Fill *candidates* (removed gates that had data) from the *keep* gates around them.

    Returns ``(filled_values, filled_mask)``; ``filled_values`` is NaN outside ``filled_mask``.
    """
    smoothed, coverage = normalized_gaussian(values, keep, sigma, wrap_azimuth)
    ok = candidates & ~keep & np.isfinite(smoothed) & (coverage >= min_coverage)
    return np.where(ok, smoothed, np.nan), ok


def smooth_clean(
    values: np.ndarray, keep: np.ndarray, *, sigma: tuple[float, float], wrap_azimuth: bool
) -> np.ndarray:
    """Light NaN-aware Gaussian smoothing of the clean gates only (other gates stay NaN)."""
    smoothed, _ = normalized_gaussian(values, keep, sigma, wrap_azimuth)
    return np.where(keep, smoothed, np.nan)

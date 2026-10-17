"""Shared helpers for the built-in modules (underscore files are skipped by discovery).

Modules are loaded by file path, so they pull this file in with ``importlib``
relative to their own ``__file__`` (see ``_load_utils`` in each module). User
modules that want to stay single-file and copy-pasteable should duplicate the
few helpers they need instead.
"""

from __future__ import annotations

import numpy as np
import xarray as xr

MAX_POLYGON_POINTS = 100


def polar_to_xy(az_deg: np.ndarray, range_m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Azimuth (deg clockwise from north) + range (m) -> (x east, y north) in metres."""
    az = np.radians(np.asarray(az_deg, dtype=np.float64))
    r = np.asarray(range_m, dtype=np.float64)
    return r * np.sin(az), r * np.cos(az)


def azimuth_spacing_deg(azimuth: np.ndarray) -> float:
    """Median ray spacing in degrees, robust to unsorted rays and the 0/360 wrap."""
    az = np.asarray(azimuth, dtype=np.float64)
    if az.size < 2:
        return 1.0
    d = np.abs(np.diff(az))
    d = np.minimum(d, 360.0 - d)
    d = d[d > 1e-6]
    return float(np.median(d)) if d.size else 1.0


def covers_full_circle(azimuth: np.ndarray) -> bool:
    """True if the rays span (almost) 360 degrees, so first and last ray are neighbours."""
    az = np.asarray(azimuth, dtype=np.float64)
    if az.size < 3:
        return False
    spacing = azimuth_spacing_deg(az)
    wrapped = np.sort(np.mod(az, 360.0))
    gaps = np.diff(np.concatenate([wrapped, [wrapped[0] + 360.0]]))
    return bool(gaps.max() <= 2.5 * spacing)


def circular_mean_deg(az_deg: np.ndarray, weights: np.ndarray | None = None) -> float:
    """(Weighted) circular mean of angles in degrees, result in [0, 360)."""
    a = np.radians(np.asarray(az_deg, dtype=np.float64))
    w = np.ones_like(a) if weights is None else np.asarray(weights, dtype=np.float64)
    return float(np.degrees(np.arctan2((w * np.sin(a)).sum(), (w * np.cos(a)).sum())) % 360.0)


def order_polygon(
    az_deg: np.ndarray,
    range_m: np.ndarray,
    max_points: int = MAX_POLYGON_POINTS,
    *,
    hull: bool = False,
) -> list[list[float]]:
    """Order unordered boundary points into a ring and return ``[[az_deg, range_m], ...]``.

    Points are sorted by angle about their centroid in x/y space (not az/range), which
    gives a sane outline for blob-like shapes and survives the 0/360 wrap. With
    ``hull=True`` the convex hull is used instead, which is the better choice for
    crescents/strips whose centroid lies outside the shape. Output is capped at
    ``max_points`` and may be empty/short for degenerate input (< 3 points).
    """
    x, y = polar_to_xy(az_deg, range_m)
    pts = np.unique(np.round(np.column_stack([x, y]), 1), axis=0)
    if len(pts) < 3:
        return []
    order = None
    if hull:
        try:
            from scipy.spatial import ConvexHull

            order = ConvexHull(pts).vertices
        except Exception:  # QhullError location differs between scipy versions
            order = None
    if order is None:
        c = pts.mean(axis=0)
        order = np.argsort(np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0]))
    pts = pts[order]
    if len(pts) > max_points:
        pts = pts[np.linspace(0, len(pts) - 1, max_points).astype(int)]
    az = np.mod(np.degrees(np.arctan2(pts[:, 0], pts[:, 1])), 360.0)
    rng = np.hypot(pts[:, 0], pts[:, 1])
    return [[round(float(a), 3), round(float(r), 1)] for a, r in zip(az, rng, strict=True)]


def sweep_array(ctx, name: str) -> xr.DataArray:
    """Field as a 2-D ``(azimuth, range)`` DataArray with explicit coordinates."""
    da = ctx.field(name)
    az, rng = ctx.polar_coords()
    if da.ndim != 2 or da.shape != (az.size, rng.size):
        raise ValueError(f"'{name}' has shape {da.shape}, expected ({az.size}, {rng.size})")
    return xr.DataArray(
        np.asarray(da.values, dtype=np.float64),
        dims=("azimuth", "range"),
        coords={"azimuth": az, "range": rng},
        name=name,
    )


def dbz_color(dbz: float) -> str:
    """Hex colour ramp for storm-cell outlines (yellow -> magenta with intensity)."""
    for limit, colour in (
        (40, "#facc15"),
        (45, "#fb923c"),
        (50, "#f97316"),
        (55, "#ef4444"),
        (60, "#dc2626"),
    ):
        if dbz < limit:
            return colour
    return "#c026d3"

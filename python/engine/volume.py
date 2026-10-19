"""3D volume grid extraction and cross-section from radar DataTree.

Converts polar radar sweeps into regular Cartesian grids for 3D visualisation.

Instead of building a Delaunay triangulation of every gate (millions of scattered
points, tens of seconds), each *output* grid point is mapped back into radar
coordinates (slant range, azimuth, elevation) and interpolated directly from the
sweeps: bilinear in (azimuth, range) inside each sweep, linear in elevation between
the two bracketing sweeps.  Beam geometry uses the standard 4/3 effective-Earth-radius
model, so heights and ground ranges include earth curvature and beam refraction.

All heights ``z`` are metres above the radar (the radar altitude is not added).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import xarray as xr

from engine import rhi
from engine.reader import sorted_sweep_names

logger = logging.getLogger(__name__)

NODATA: float = -9999.0

#: Effective Earth radius (4/3 model), metres.
EARTH_RADIUS_M: float = 6_371_000.0
EFFECTIVE_RADIUS_M: float = EARTH_RADIUS_M * 4.0 / 3.0

# Elevation slack (degrees) above the top / below the bottom sweep that is still
# attributed to that sweep (roughly half a beamwidth).
_ELEV_TOL_DEG = 0.5
# Sweeps closer than this in elevation are treated as the same tilt (split cuts).
_ELEV_MERGE_DEG = 0.05
# Minimum summed interpolation weight of valid samples for an output cell to be valid.
_MIN_VALID_WEIGHT = 0.5
# Output points processed per chunk (bounds peak temporary memory to ~100 MB).
_CHUNK_POINTS = 1_000_000

SweepLoader = Callable[[int], xr.Dataset]


# ----------------------------------------------------------------------
# Geometry (4/3 earth model)
# ----------------------------------------------------------------------


def beam_height_and_ground_range(
    range_m: np.ndarray | float,
    elevation_deg: np.ndarray | float,
    re: float = EFFECTIVE_RADIUS_M,
) -> tuple[np.ndarray, np.ndarray]:
    """Height above the radar and great-circle ground distance of a beam point.

    ``h = sqrt(r^2 + Re^2 + 2 r Re sin(el)) - Re`` and
    ``s = Re * asin(r cos(el) / (Re + h))`` (Doviak & Zrnic 1993, eq. 2.28b/c).
    """
    r = np.asarray(range_m, dtype=np.float64)
    el = np.deg2rad(np.asarray(elevation_deg, dtype=np.float64))
    h = np.sqrt(r * r + re * re + 2.0 * r * re * np.sin(el)) - re
    s = re * np.arcsin(np.clip(r * np.cos(el) / (re + h), -1.0, 1.0))
    return h, s


def slant_range_and_height_from_ground(
    ground_m: np.ndarray | float,
    elevation_deg: np.ndarray | float,
    re: float = EFFECTIVE_RADIUS_M,
) -> tuple[np.ndarray, np.ndarray]:
    """Slant range and height of the beam at a given ground distance and elevation.

    Closed-form inverse of :func:`beam_height_and_ground_range` for a fixed elevation:
    ``r = Re sin(a) / cos(a + el)`` with ``a = s / Re``; NaN where the beam never reaches
    that ground distance.
    """
    alpha = np.asarray(ground_m, dtype=np.float64) / re
    el = np.deg2rad(np.asarray(elevation_deg, dtype=np.float64))
    denom = np.cos(alpha + el)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(denom > 1e-9, re * np.sin(alpha) / denom, np.nan)
        h = (re + r * np.sin(el)) / np.cos(alpha) - re
    return r, h


def _inverse_beam_geometry(
    ground_m: np.ndarray, height_m: np.ndarray, re: float = EFFECTIVE_RADIUS_M
) -> tuple[np.ndarray, np.ndarray]:
    """Slant range (m) and elevation angle (deg) of the point at (ground distance, height)."""
    alpha = ground_m / re
    rh = re + height_m
    dx = rh * np.sin(alpha)
    dz = rh * np.cos(alpha) - re
    return np.hypot(dx, dz), np.degrees(np.arctan2(dz, dx))


def _polar_to_cartesian(
    azimuth: np.ndarray,
    range_m: np.ndarray,
    elevation: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Convert polar coordinates to Cartesian (x, y, z) in metres (4/3 earth model).

    Parameters
    ----------
    azimuth : 1-D array of azimuth angles in degrees (length n_az)
    range_m : 1-D array of slant range gates in metres (length n_range)
    elevation : scalar elevation angle in degrees

    Returns
    -------
    x, y, z : 2-D arrays of shape (n_az, n_range); z is height above the radar.
    """
    h, s = beam_height_and_ground_range(range_m, elevation)
    az_rad = np.deg2rad(np.asarray(azimuth, dtype=np.float64))
    x = np.sin(az_rad)[:, None] * s[None, :]
    y = np.cos(az_rad)[:, None] * s[None, :]
    z = np.broadcast_to(h[None, :], x.shape).copy()
    return x, y, z


def _get_sweep_groups(dt: xr.DataTree) -> list[str]:
    """Return sweep group names from a DataTree in numeric order."""
    return sorted_sweep_names(dt)


# ----------------------------------------------------------------------
# Sweep preparation
# ----------------------------------------------------------------------


@dataclass
class _Sweep:
    elevation: float
    az: np.ndarray  # padded, strictly increasing azimuths (deg), length n_az + 2
    rng: np.ndarray  # strictly increasing slant ranges (m)
    vals: np.ndarray  # (n_az + 2, n_rng) float32, 0 where invalid
    wts: np.ndarray  # (n_az + 2, n_rng) float32, 1 where valid else 0
    gap_max: float  # max azimuth gap (deg) across which we still interpolate
    n_valid: int


def _first_present(ds: xr.Dataset, name: str) -> np.ndarray | None:
    if name in ds.variables:
        return np.asarray(ds.variables[name].values)
    return None


def _sweep_elevation_deg(ds: xr.Dataset) -> float | None:
    """Mean elevation of a sweep, or None for non-PPI sweeps (e.g. RHI) / missing metadata."""
    el = _first_present(ds, "elevation")
    if el is not None:
        el = el[np.isfinite(el)]
        if el.size:
            if float(el.max() - el.min()) > 3.0:
                return None  # elevation varies within the sweep (RHI): not a cone
            return float(el.mean())
    if "sweep_fixed_angle" in ds.attrs:
        try:
            v = float(ds.attrs["sweep_fixed_angle"])
        except (TypeError, ValueError):
            v = math.nan
        if math.isfinite(v):
            return v
    fixed = _first_present(ds, "sweep_fixed_angle")
    if fixed is not None and fixed.size and np.isfinite(fixed.flat[0]):
        return float(fixed.flat[0])
    return None


def _prepare_sweep(ds: xr.Dataset, variable: str) -> _Sweep | None:
    if variable not in ds.data_vars:
        return None
    da = ds[variable]
    if da.ndim != 2:
        return None
    elevation = _sweep_elevation_deg(ds)
    if elevation is None:
        logger.debug("Skipping sweep without usable elevation")
        return None
    az = _first_present(ds, "azimuth")
    rng = _first_present(ds, "range")
    if az is None or rng is None or az.ndim != 1 or rng.ndim != 1:
        return None

    # Orient the data as (azimuth, range) whatever the stored dimension order
    dims = da.dims
    values = np.asarray(da.values)
    if "azimuth" in dims and "range" in dims and dims.index("azimuth") > dims.index("range"):
        values = values.T
    if values.shape != (az.size, rng.size):
        return None

    az = az.astype(np.float64) % 360.0
    rng = rng.astype(np.float64)

    # Sort and de-duplicate rays/gates so np.interp sees strictly increasing axes
    ray_ok = np.isfinite(az)
    az_u, first = np.unique(az[ray_ok], return_index=True)
    if az_u.size < 2:
        return None
    values = values[np.flatnonzero(ray_ok)[first]]
    gate_ok = np.isfinite(rng)
    if not gate_ok.all():
        rng, values = rng[gate_ok], values[:, gate_ok]
    if rng.size < 2:
        return None
    if np.any(np.diff(rng) <= 0):
        rng, first_g = np.unique(rng, return_index=True)
        values = values[:, first_g]
        if rng.size < 2:
            return None

    # Wrap-around padding: one ray from each end so interpolation works across north
    az_p = np.concatenate(([az_u[-1] - 360.0], az_u, [az_u[0] + 360.0]))
    median_gap = float(np.median(np.diff(np.concatenate((az_u, [az_u[0] + 360.0])))))
    gap_max = max(2.5 * median_gap, 0.5)

    valid = np.isfinite(values)
    vals = np.where(valid, values, 0.0).astype(np.float32)
    vals_p = np.concatenate((vals[-1:], vals, vals[:1]), axis=0)
    wts = valid.astype(np.float32)
    wts_p = np.concatenate((wts[-1:], wts, wts[:1]), axis=0)
    return _Sweep(
        elevation=elevation,
        az=az_p,
        rng=rng,
        vals=np.ascontiguousarray(vals_p),
        wts=np.ascontiguousarray(wts_p),
        gap_max=gap_max,
        n_valid=int(valid.sum()),
    )


def _collect_sweeps(
    dt: xr.DataTree, variable: str, sweep_loader: SweepLoader | None
) -> list[_Sweep]:
    """Prepare every usable sweep, sorted by elevation, one sweep per distinct tilt."""
    prepared: list[_Sweep] = []
    for idx, name in enumerate(_get_sweep_groups(dt)):
        try:
            ds = sweep_loader(idx) if sweep_loader is not None else dt[name].to_dataset()
        except (KeyError, ValueError, IndexError) as exc:
            logger.debug("Skipping %s: %s", name, exc)
            continue
        if rhi.is_rhi(ds):
            logger.debug(
                "Skipping %s: range-height (RHI) scans cannot be gridded as PPI sweeps", name
            )
            continue
        sweep = _prepare_sweep(ds, variable)
        if sweep is not None:
            prepared.append(sweep)
        elif not ds.data_vars and "_sweep_variables" in ds.attrs:
            logger.warning(
                "%s is a lazy ARCO placeholder; pass sweep_loader=reader.get_sweep_dataset "
                "to include it",
                name,
            )

    prepared.sort(key=lambda s: s.elevation)
    # Split-cut tilts (same elevation, different PRF) both carrying the variable: keep
    # the one with more valid gates.
    unique: list[_Sweep] = []
    for sweep in prepared:
        if unique and sweep.elevation - unique[-1].elevation < _ELEV_MERGE_DEG:
            if sweep.n_valid > unique[-1].n_valid:
                unique[-1] = sweep
        else:
            unique.append(sweep)
    return unique


# ----------------------------------------------------------------------
# Sampling
# ----------------------------------------------------------------------


def _elevation_weights(theta: np.ndarray, elevs: list[float], k: int) -> np.ndarray:
    """Linear 'hat' weight of sweep *k* at beam elevations *theta* (all sweeps sum to 1)."""
    e = elevs[k]
    lo = elevs[k - 1] if k > 0 else None
    hi = elevs[k + 1] if k + 1 < len(elevs) else None
    xs = [
        lo if lo is not None else e - _ELEV_TOL_DEG,
        e,
        hi if hi is not None else e + _ELEV_TOL_DEG,
    ]
    ys = [0.0 if lo is not None else 1.0, 1.0, 0.0 if hi is not None else 1.0]
    return np.interp(theta, xs, ys, left=0.0, right=0.0)


def _sample_polar(
    sweeps: list[_Sweep],
    ground_m: np.ndarray,
    azimuth_deg: np.ndarray,
    height_m: np.ndarray,
) -> np.ndarray:
    """Interpolate the sweeps at 1-D arrays of (ground distance, azimuth, height) points.

    Returns float32 values, NaN where no sweep covers the point.
    """
    n = ground_m.size
    out = np.full(n, np.nan, dtype=np.float32)
    if n == 0 or not sweeps:
        return out
    from scipy.ndimage import map_coordinates  # deferred: ~150 ms at import

    rng_pt, theta = _inverse_beam_geometry(ground_m, height_m)
    az_pt = azimuth_deg % 360.0
    elevs = [s.elevation for s in sweeps]

    num = np.zeros(n, dtype=np.float32)
    den = np.zeros(n, dtype=np.float32)

    for k, sw in enumerate(sweeps):
        w = _elevation_weights(theta, elevs, k)
        sel = np.flatnonzero(w > 0.0)
        if sel.size == 0:
            continue
        r = rng_pt[sel]
        in_range = (r >= sw.rng[0]) & (r <= sw.rng[-1])
        if not in_range.any():
            continue
        sel = sel[in_range]
        r = r[in_range]

        # Fractional indices into the padded (az, range) arrays
        az_frac = np.interp(az_pt[sel], sw.az, np.arange(sw.az.size, dtype=np.float64))
        lo_i = np.minimum(az_frac.astype(np.intp), sw.az.size - 2)
        gap_ok = (sw.az[lo_i + 1] - sw.az[lo_i]) <= sw.gap_max
        if not gap_ok.any():
            continue
        sel, r, az_frac = sel[gap_ok], r[gap_ok], az_frac[gap_ok]
        rng_frac = np.interp(r, sw.rng, np.arange(sw.rng.size, dtype=np.float64))

        coords = np.stack((az_frac, rng_frac))
        v = map_coordinates(sw.vals, coords, order=1, mode="nearest", prefilter=False)
        wv = map_coordinates(sw.wts, coords, order=1, mode="nearest", prefilter=False)

        # Normalised convolution: average over the valid neighbours only, and require
        # that valid neighbours carry at least half of the interpolation weight.
        ok = wv >= _MIN_VALID_WEIGHT
        if not ok.any():
            continue
        sel = sel[ok]
        wk = w[sel].astype(np.float32)
        num[sel] += wk * (v[ok] / wv[ok])
        den[sel] += wk

    good = den >= _MIN_VALID_WEIGHT
    out[good] = num[good] / den[good]
    return out


def _sample_cartesian(
    sweeps: list[_Sweep], x: np.ndarray, y: np.ndarray, z: np.ndarray
) -> np.ndarray:
    """Sample at Cartesian points (metres from the radar, z = height above radar)."""
    ground = np.hypot(x, y)
    azimuth = np.degrees(np.arctan2(x, y))  # clockwise from north
    return _sample_polar(sweeps, ground, azimuth, z)


def _value_range(grid: np.ndarray) -> tuple[float, float]:
    finite = grid[np.isfinite(grid)]
    if finite.size == 0:
        return 0.0, 0.0
    return float(finite.min()), float(finite.max())


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def extract_volume(
    dt: xr.DataTree,
    variable: str,
    box: dict[str, float],
    resolution: int = 100,
    z_max: float = 15000.0,
    *,
    sweep_loader: SweepLoader | None = None,
) -> dict[str, Any]:
    """Extract a 3D Cartesian volume grid from all sweeps of a DataTree.

    Parameters
    ----------
    dt : xr.DataTree
        Radar DataTree opened by xradar.
    variable : str
        Name of the data variable (e.g. ``"DBZH"``).
    box : dict
        Bounding box in metres from radar:
        ``{x_min, x_max, y_min, y_max}``.
    resolution : int
        Number of grid points per spatial axis (default 100).
    z_max : float
        Maximum height in metres above the radar (default 15 000).
    sweep_loader : callable, optional
        ``sweep_loader(i) -> xr.Dataset`` returning the full dataset of sweep *i*
        (use ``RadarReader.get_sweep_dataset``).  Required for lazily-loaded ARCO
        trees, whose placeholder sweeps carry no data inside *dt* itself.

    Returns
    -------
    dict with keys:
        data : np.ndarray — flat float32 array (x-major order), NODATA where empty
        nx, ny, nz : int — grid dimensions
        x_min, x_max, y_min, y_max, z_min, z_max : float — bounds
        vmin, vmax : float — data value range
    """
    x_min = float(box.get("x_min", -50000))
    x_max = float(box.get("x_max", 50000))
    y_min = float(box.get("y_min", -50000))
    y_max = float(box.get("y_max", 50000))
    z_min = 0.0
    z_max = float(z_max)

    nx = ny = nz = int(resolution)
    if nx < 2:
        raise ValueError("resolution must be at least 2")

    if not _get_sweep_groups(dt):
        raise ValueError("No sweep groups found in DataTree")

    sweeps = _collect_sweeps(dt, variable, sweep_loader)

    result: dict[str, Any] = {
        "nx": nx,
        "ny": ny,
        "nz": nz,
        "x_min": x_min,
        "x_max": x_max,
        "y_min": y_min,
        "y_max": y_max,
        "z_min": z_min,
        "z_max": z_max,
    }
    if not sweeps:
        result.update(data=np.full(nx * ny * nz, NODATA, dtype=np.float32), vmin=0.0, vmax=0.0)
        return result

    logger.info("Volume sampling: %d sweeps -> (%d, %d, %d) grid", len(sweeps), nx, ny, nz)

    gx = np.linspace(x_min, x_max, nx)
    gy = np.linspace(y_min, y_max, ny)
    gz = np.linspace(z_min, z_max, nz)

    grid = np.empty((nx, ny, nz), dtype=np.float32)
    rows = max(1, _CHUNK_POINTS // (ny * nz))  # x-slices per chunk
    for i0 in range(0, nx, rows):
        i1 = min(nx, i0 + rows)
        xx = gx[i0:i1, None]
        # Ground distance / azimuth only depend on (x, y); height only on z.
        ground2 = np.hypot(xx, gy[None, :])  # (rows, ny)
        az2 = np.degrees(np.arctan2(xx, gy[None, :]))
        shape = (i1 - i0, ny, nz)
        ground = np.broadcast_to(ground2[:, :, None], shape).ravel()
        azimuth = np.broadcast_to(az2[:, :, None], shape).ravel()
        height = np.broadcast_to(gz[None, None, :], shape).ravel()
        grid[i0:i1] = _sample_polar(sweeps, ground, azimuth, height).reshape(shape)

    vmin, vmax = _value_range(grid)
    np.nan_to_num(grid, copy=False, nan=NODATA)
    result.update(data=grid.ravel(order="C"), vmin=vmin, vmax=vmax)
    return result


def extract_cross_section_3d(
    dt: xr.DataTree,
    variable: str,
    start: dict[str, float],
    end: dict[str, float],
    width: float = 2000.0,
    n_horizontal: int = 200,
    n_vertical: int = 100,
    z_max: float = 15000.0,
    *,
    sweep_loader: SweepLoader | None = None,
) -> dict[str, Any]:
    """Extract a vertical curtain (cross-section) along a line in Cartesian space.

    Each output cell is the mean of the valid samples taken across the corridor's
    width (perpendicular to the line), so ``width`` smooths the section rather than
    selecting scattered gates.

    Parameters
    ----------
    dt : xr.DataTree
        Radar DataTree.
    variable : str
        Data variable name.
    start, end : dict
        ``{x, y}`` coordinates in metres from radar.
    width : float
        Perpendicular averaging width in metres (default 2000).
    n_horizontal : int
        Number of grid points along the horizontal line (default 200).
    n_vertical : int
        Number of grid points in the vertical (default 100).
    z_max : float
        Maximum height in metres above the radar (default 15 000).
    sweep_loader : callable, optional
        See :func:`extract_volume`.

    Returns
    -------
    dict with keys:
        data : np.ndarray — flat float32 array (distance-major)
        n_dist, n_z : int — 2D grid dimensions
        dist_min, dist_max, z_min, z_max : float
        vmin, vmax : float
    """
    sx, sy = float(start["x"]), float(start["y"])
    ex, ey = float(end["x"]), float(end["y"])

    dx = ex - sx
    dy = ey - sy
    line_len = math.hypot(dx, dy)
    if line_len < 1.0:
        raise ValueError("Start and end points are too close together")

    # Unit vectors: along line and perpendicular
    ux, uy = dx / line_len, dy / line_len
    px, py = -uy, ux

    half_w = max(float(width), 0.0) / 2.0
    z_min_val = 0.0
    z_max = float(z_max)
    n_dist = int(n_horizontal)
    n_z = int(n_vertical)

    if not _get_sweep_groups(dt):
        raise ValueError("No sweep groups found in DataTree")
    sweeps = _collect_sweeps(dt, variable, sweep_loader)

    result: dict[str, Any] = {
        "n_dist": n_dist,
        "n_z": n_z,
        "dist_min": 0.0,
        "dist_max": float(line_len),
        "z_min": z_min_val,
        "z_max": z_max,
    }
    if not sweeps:
        result.update(data=np.full(n_dist * n_z, NODATA, dtype=np.float32), vmin=0.0, vmax=0.0)
        return result

    logger.info("Cross-section 3D: %d sweeps -> (%d, %d) grid", len(sweeps), n_dist, n_z)

    gd = np.linspace(0.0, line_len, n_dist)
    gz = np.linspace(z_min_val, z_max, n_z)

    # Perpendicular sample offsets, about one per output column spacing (max 9)
    spacing = line_len / max(n_dist - 1, 1)
    n_perp = int(np.clip(round(2.0 * half_w / max(spacing, 1.0)) + 1, 1, 9)) if half_w > 0 else 1
    offsets = np.linspace(-half_w, half_w, n_perp) if n_perp > 1 else np.zeros(1)

    shape = (n_dist, n_z)
    acc = np.zeros(shape, dtype=np.float64)
    cnt = np.zeros(shape, dtype=np.float64)
    height = np.broadcast_to(gz[None, :], shape).ravel()
    for off in offsets:
        x = sx + gd * ux + off * px
        y = sy + gd * uy + off * py
        ground2 = np.hypot(x, y)
        az2 = np.degrees(np.arctan2(x, y))
        vals = _sample_polar(
            sweeps,
            np.broadcast_to(ground2[:, None], shape).ravel(),
            np.broadcast_to(az2[:, None], shape).ravel(),
            height,
        ).reshape(shape)
        ok = np.isfinite(vals)
        acc[ok] += vals[ok]
        cnt[ok] += 1.0

    grid = np.full(shape, np.nan, dtype=np.float32)
    has = cnt > 0
    grid[has] = (acc[has] / cnt[has]).astype(np.float32)

    vmin, vmax = _value_range(grid)
    np.nan_to_num(grid, copy=False, nan=NODATA)
    result.update(data=grid.ravel(order="C"), vmin=vmin, vmax=vmax)
    return result

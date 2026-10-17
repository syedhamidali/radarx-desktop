"""Radar data processing pipeline with cross-section and profile extraction."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import numpy as np
import xarray as xr

from engine.reader import sorted_sweep_names
from engine.volume import slant_range_and_height_from_ground

logger = logging.getLogger(__name__)

# Type alias for the progress callback: (percent: int, message: str) -> None
ProgressCallback = Callable[[int, str], None]


def _noop_progress(percent: int, message: str) -> None:
    """Default progress callback that does nothing."""


class RadarProcessor:
    """Apply processing steps to radar data.

    Phase 1 provides stub implementations that pass data through unchanged
    while logging what *would* be done. Each method accepts an optional
    progress callback so the server can relay status to the frontend.
    """

    def process(
        self,
        datatree: xr.DataTree,
        pipeline_config: dict[str, Any],
        progress: ProgressCallback = _noop_progress,
    ) -> xr.DataTree:
        """Run the configured pipeline on *datatree* and return the result.

        Parameters
        ----------
        datatree : xr.DataTree
            The radar datatree to process.
        pipeline_config : dict
            Keys are processing step names; values are booleans or config
            dicts.  Recognised keys (Phase 1):
            - ``despeckle`` (bool)
            - ``dealias`` (bool)
            - ``gridding`` (str — ``"none"``, ``"grid"``, etc.)
        progress : ProgressCallback
            Called with ``(percent, message)`` as processing advances.

        Returns
        -------
        xr.DataTree
            Processed datatree (in Phase 1, returned unmodified).
        """
        steps = self._plan_steps(pipeline_config)
        total = len(steps) if steps else 1

        progress(0, "Starting processing pipeline")

        for idx, (name, config) in enumerate(steps):
            pct = int(((idx + 1) / total) * 100)
            progress(pct, f"Running {name}...")
            datatree = self._run_step(name, datatree, config)

        progress(100, "Processing complete")
        return datatree

    # ------------------------------------------------------------------
    # Individual processing steps (Phase 1 stubs)
    # ------------------------------------------------------------------

    def despeckle(
        self,
        data: xr.DataTree,
        progress: ProgressCallback = _noop_progress,
    ) -> xr.DataTree:
        """Remove speckle noise from radar data.

        Phase 1: stub — returns data unchanged.
        """
        logger.info(
            "[stub] despeckle called — would apply speckle filter to %d sweeps",
            len([n for n in data.children if n.startswith("sweep_")]),
        )
        progress(100, "Despeckle complete (stub)")
        return data

    def dealias_velocity(
        self,
        data: xr.DataTree,
        progress: ProgressCallback = _noop_progress,
    ) -> xr.DataTree:
        """Dealias radial velocity data.

        Phase 1: stub — returns data unchanged.
        """
        logger.info(
            "[stub] dealias_velocity called — would dealias velocity in %d sweeps",
            len([n for n in data.children if n.startswith("sweep_")]),
        )
        progress(100, "Velocity dealiasing complete (stub)")
        return data

    def grid_data(
        self,
        data: xr.DataTree,
        method: str = "none",
        progress: ProgressCallback = _noop_progress,
    ) -> xr.DataTree:
        """Grid polar radar data to a Cartesian grid.

        Phase 1: stub — returns data unchanged.

        Parameters
        ----------
        method : str
            Gridding method.  ``"none"`` skips gridding.
        """
        if method == "none":
            logger.info("[stub] grid_data called with method='none' — skipping")
            progress(100, "Gridding skipped")
            return data

        logger.info(
            "[stub] grid_data called with method='%s' — would grid %d sweeps",
            method,
            len([n for n in data.children if n.startswith("sweep_")]),
        )
        progress(100, f"Gridding ({method}) complete (stub)")
        return data

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _plan_steps(self, config: dict[str, Any]) -> list[tuple[str, Any]]:
        """Determine which steps to run and in what order."""
        steps: list[tuple[str, Any]] = []
        if config.get("despeckle"):
            steps.append(("despeckle", True))
        if config.get("dealias"):
            steps.append(("dealias", True))
        gridding = config.get("gridding", "none")
        if gridding and gridding != "none":
            steps.append(("gridding", gridding))
        return steps

    def _run_step(
        self,
        name: str,
        datatree: xr.DataTree,
        config: Any,
    ) -> xr.DataTree:
        """Dispatch a single processing step by *name*."""
        if name == "despeckle":
            return self.despeckle(datatree)
        if name == "dealias":
            return self.dealias_velocity(datatree)
        if name == "gridding":
            method = config if isinstance(config, str) else "none"
            return self.grid_data(datatree, method=method)

        logger.warning("Unknown processing step '%s' — skipping", name)
        return datatree


# ======================================================================
# Cross-section and vertical profile extraction
# ======================================================================

SweepLoader = Callable[[int], xr.Dataset]


def _get_sweep_data(
    datatree: xr.DataTree,
    sweep_idx: int,
    variable: str,
    sweep_loader: SweepLoader | None = None,
    sweep_names: list[str] | None = None,
) -> tuple[xr.DataArray | None, float, np.ndarray, np.ndarray]:
    """Extract a variable from a sweep, returning (data, elev_deg, azimuth, range).

    The returned DataArray is ordered (azimuth, range).  Returns (None, 0, [], []) if
    the variable is not in the sweep.  ``sweep_loader(i)`` (e.g.
    ``RadarReader.get_sweep_dataset``) supplies lazily-loaded sweeps.
    """
    empty = (None, 0.0, np.array([]), np.array([]))
    if sweep_names is None:
        sweep_names = sorted_sweep_names(datatree)
    if sweep_idx >= len(sweep_names):
        return empty

    if sweep_loader is not None:
        ds = sweep_loader(sweep_idx)
    else:
        ds = datatree[sweep_names[sweep_idx]].to_dataset()
    if variable not in ds.data_vars:
        return empty

    da = ds[variable]
    if "azimuth" in da.dims and "range" in da.dims and da.dims != ("azimuth", "range"):
        da = da.transpose("azimuth", "range")

    # Elevation
    elev = 0.0
    if "sweep_fixed_angle" in ds.attrs:
        elev = float(ds.attrs["sweep_fixed_angle"])
    elif "sweep_fixed_angle" in ds.variables:
        elev = float(ds.variables["sweep_fixed_angle"].values.flat[0])
    elif "elevation" in ds.variables:
        elev = float(np.nanmean(ds.variables["elevation"].values))

    azimuth = (
        da.coords["azimuth"].values
        if "azimuth" in da.coords
        else np.arange(da.shape[0], dtype=np.float64)
    )
    range_m = (
        da.coords["range"].values
        if "range" in da.coords
        else np.arange(da.shape[-1], dtype=np.float64)
    )

    return da, elev, azimuth.astype(np.float64), range_m.astype(np.float64)


def _sample_sweep(
    values_2d: np.ndarray,
    sweep_az: np.ndarray,
    sweep_rng: np.ndarray,
    az_pts: np.ndarray,
    rng_pts: np.ndarray,
) -> np.ndarray:
    """Vectorised lookup: nearest ray (circular azimuth), linear interpolation in range.

    NaN where the slant range lies outside the sweep's range coverage (or either
    neighbouring gate is NaN).
    """
    az_pts = np.asarray(az_pts, dtype=np.float64)
    rng_pts = np.asarray(rng_pts, dtype=np.float64)
    out = np.full(rng_pts.shape, np.nan, dtype=np.float64)
    if sweep_az.size == 0 or sweep_rng.size == 0:
        return out

    # Nearest azimuth on the circle, via the sorted rays' neighbours of each target
    order = np.argsort(sweep_az)
    az_sorted = sweep_az[order] % 360.0
    resort = np.argsort(az_sorted)
    az_sorted, order = az_sorted[resort], order[resort]
    pos = np.searchsorted(az_sorted, az_pts % 360.0)
    left = (pos - 1) % az_sorted.size
    right = pos % az_sorted.size
    d_left = np.abs((az_sorted[left] - az_pts + 180.0) % 360.0 - 180.0)
    d_right = np.abs((az_sorted[right] - az_pts + 180.0) % 360.0 - 180.0)
    az_idx = order[np.where(d_left <= d_right, left, right)]

    # Range: linear between the two bracketing gates
    rng = sweep_rng
    if rng.size > 1 and np.any(np.diff(rng) < 0):
        ro = np.argsort(rng)
        rng, values_2d = rng[ro], values_2d[:, ro]
    in_cov = (rng_pts >= rng[0]) & (rng_pts <= rng[-1])
    if not in_cov.any():
        return out
    if rng.size == 1:
        out[in_cov] = values_2d[az_idx[in_cov], 0]
        return out
    r = rng_pts[in_cov]
    hi = np.clip(np.searchsorted(rng, r), 1, rng.size - 1)
    lo = hi - 1
    r0, r1 = rng[lo], rng[hi]
    span = r1 - r0
    frac = np.divide(r - r0, span, out=np.zeros_like(r), where=span != 0)
    rows = az_idx[in_cov]
    v0 = values_2d[rows, lo].astype(np.float64)
    v1 = values_2d[rows, hi].astype(np.float64)
    out[in_cov] = v0 + frac * (v1 - v0)
    return out


def _nan_to_none(arr: np.ndarray) -> list[Any]:
    """Nested list with NaN/Inf as None (JSON.parse rejects NaN)."""
    obj = np.asarray(arr, dtype=np.float64).astype(object)
    obj[~np.isfinite(np.asarray(arr, dtype=np.float64))] = None
    return obj.tolist()


def _local_xy(
    lat: np.ndarray | float, lon: np.ndarray | float, radar_lat: float, radar_lon: float
) -> tuple[np.ndarray, np.ndarray]:
    """Approximate local (x east, y north) metres of lat/lon relative to the radar."""
    dx = (np.asarray(lon) - radar_lon) * 111320.0 * np.cos(np.radians(radar_lat))
    dy = (np.asarray(lat) - radar_lat) * 110540.0
    return dx, dy


def extract_cross_section(
    datatree: xr.DataTree,
    start_point: tuple[float, float],
    end_point: tuple[float, float],
    variable: str,
    *,
    n_points: int = 200,
    sweep_loader: SweepLoader | None = None,
) -> dict[str, Any]:
    """Extract an RHI-like cross-section between two geographic points.

    The cross-section interpolates across all sweeps to produce a
    distance-vs-height slice.  Heights and slant ranges use the 4/3 effective
    Earth radius beam model (the sample point's *ground* distance from the radar is
    converted to the slant range at which each sweep's beam reaches it).

    Parameters
    ----------
    datatree : xr.DataTree
        Radar DataTree.
    start_point : tuple[float, float]
        Start (latitude, longitude) in degrees.
    end_point : tuple[float, float]
        End (latitude, longitude) in degrees.
    variable : str
        Variable name (e.g. ``"DBZH"``).
    n_points : int
        Number of horizontal sample points.
    sweep_loader : callable, optional
        ``sweep_loader(i) -> xr.Dataset`` for lazily-loaded (ARCO) sweeps.

    Returns
    -------
    dict
        Keys: ``distance_km`` (list), ``height_km`` (list of lists per
        sweep), ``values`` (list of lists per sweep), ``variable`` (str),
        ``units`` (str), ``n_sweeps`` (int).
    """
    sweep_names = sorted_sweep_names(datatree)
    radar_lat, radar_lon = _get_radar_location(datatree)

    lat1, lon1 = start_point
    lat2, lon2 = end_point

    # Sample points along the line (approximate: lat/lon as local offsets from the radar)
    lats = np.linspace(lat1, lat2, n_points)
    lons = np.linspace(lon1, lon2, n_points)
    dx, dy = _local_xy(lats, lons, radar_lat, radar_lon)

    ground_dist = np.hypot(dx, dy)
    sample_azimuth = np.degrees(np.arctan2(dx, dy)) % 360.0

    # Distance along the cross-section line
    total_dist = float(np.hypot(dx[-1] - dx[0], dy[-1] - dy[0]))
    distance_km = np.linspace(0, total_dist / 1000.0, n_points)

    all_heights: list[list[float]] = []
    all_values: list[list[float | None]] = []
    units = ""

    for si in range(len(sweep_names)):
        da, elev_deg, sweep_az, sweep_rng = _get_sweep_data(
            datatree, si, variable, sweep_loader, sweep_names
        )
        if da is None:
            continue

        if not units and "units" in da.attrs:
            units = str(da.attrs["units"])

        slant, height = slant_range_and_height_from_ground(ground_dist, elev_deg)
        values = _sample_sweep(np.asarray(da.values), sweep_az, sweep_rng, sample_azimuth, slant)

        all_heights.append(_nan_to_none(height / 1000.0))
        all_values.append(_nan_to_none(values))

    return {
        "distance_km": distance_km.tolist(),
        "height_km": all_heights,
        "values": all_values,
        "variable": variable,
        "units": units,
        "n_sweeps": len(all_heights),
    }


def extract_vertical_profile(
    datatree: xr.DataTree,
    lat: float,
    lon: float,
    variable: str,
    *,
    sweep_loader: SweepLoader | None = None,
) -> dict[str, Any]:
    """Extract a vertical profile (column) at a geographic point.

    Interpolates the variable at the nearest ray for each sweep (linear in range),
    producing a profile of value vs. height.

    Parameters
    ----------
    datatree : xr.DataTree
        Radar DataTree.
    lat, lon : float
        Geographic coordinates of the point.
    variable : str
        Variable name.
    sweep_loader : callable, optional
        ``sweep_loader(i) -> xr.Dataset`` for lazily-loaded (ARCO) sweeps.

    Returns
    -------
    dict
        Keys: ``heights_km`` (list), ``values`` (list),
        ``elevations_deg`` (list), ``variable`` (str), ``units`` (str).
    """
    sweep_names = sorted_sweep_names(datatree)
    radar_lat, radar_lon = _get_radar_location(datatree)

    dx, dy = _local_xy(lat, lon, radar_lat, radar_lon)
    ground = np.array([float(np.hypot(dx, dy))])
    point_az = np.array([float(np.degrees(np.arctan2(dx, dy)) % 360.0)])

    heights: list[float | None] = []
    values: list[float | None] = []
    elevations: list[float] = []
    units = ""

    for si in range(len(sweep_names)):
        da, elev_deg, sweep_az, sweep_rng = _get_sweep_data(
            datatree, si, variable, sweep_loader, sweep_names
        )
        if da is None:
            continue

        if not units and "units" in da.attrs:
            units = str(da.attrs["units"])

        slant, h = slant_range_and_height_from_ground(ground, elev_deg)
        val = _sample_sweep(np.asarray(da.values), sweep_az, sweep_rng, point_az, slant)

        heights.append(_nan_to_none(h / 1000.0)[0])
        values.append(_nan_to_none(val)[0])
        elevations.append(elev_deg)

    return {
        "heights_km": heights,
        "values": values,
        "elevations_deg": elevations,
        "variable": variable,
        "units": units,
    }


def _get_radar_location(datatree: xr.DataTree) -> tuple[float, float]:
    """Extract radar latitude/longitude from a DataTree.

    Checks root attributes and first sweep for common coordinate names.
    Returns (0.0, 0.0) if not found.
    """
    # Check root attributes
    try:
        root_ds = datatree.to_dataset()
        for lat_key in ("latitude", "origin_latitude", "siteLat"):
            if lat_key in root_ds:
                lat = float(np.asarray(root_ds[lat_key]).flat[0])
                for lon_key in ("longitude", "origin_longitude", "siteLon"):
                    if lon_key in root_ds:
                        lon = float(np.asarray(root_ds[lon_key]).flat[0])
                        return lat, lon
            if lat_key in root_ds.attrs:
                lat = float(root_ds.attrs[lat_key])
                for lon_key in ("longitude", "origin_longitude", "siteLon"):
                    if lon_key in root_ds.attrs:
                        lon = float(root_ds.attrs[lon_key])
                        return lat, lon
    except Exception:
        pass

    # Check first sweep
    sweep_names = sorted_sweep_names(datatree)
    if sweep_names:
        try:
            ds = datatree[sweep_names[0]].to_dataset()
            for lat_key in ("latitude", "origin_latitude", "siteLat"):
                if lat_key in ds:
                    lat = float(np.asarray(ds[lat_key]).flat[0])
                    for lon_key in ("longitude", "origin_longitude", "siteLon"):
                        if lon_key in ds:
                            lon = float(np.asarray(ds[lon_key]).flat[0])
                            return lat, lon
        except Exception:
            pass

    logger.warning("Could not determine radar location; defaulting to (0, 0)")
    return 0.0, 0.0

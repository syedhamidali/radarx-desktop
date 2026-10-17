"""Radar data statistics and quality-control metrics.

Provides histogram computation, area-averaged profiles, cell statistics
(max reflectivity, VIL, echo top), and QC metrics per variable/sweep.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import xarray as xr

from engine.reader import sorted_sweep_names

logger = logging.getLogger(__name__)


_REFLECTIVITY_ALIASES = ("DBZH", "DBZ", "reflectivity", "Z")


def _is_reflectivity_name(name: str) -> bool:
    low = name.lower()
    return name in _REFLECTIVITY_ALIASES or low.startswith(("dbz", "refl", "zh", "corrected_refl"))


def _sweep_elevation(ds: xr.Dataset) -> float:
    """Best-effort fixed elevation angle (degrees) of a sweep dataset."""
    if "sweep_fixed_angle" in ds.attrs:
        return float(ds.attrs["sweep_fixed_angle"])
    if "sweep_fixed_angle" in ds:
        return float(ds["sweep_fixed_angle"].values)
    if "elevation" in ds:
        return float(np.nanmean(ds["elevation"].values))
    return 0.0


# ---------------------------------------------------------------------------
# Histograms
# ---------------------------------------------------------------------------


def compute_histogram(
    data: xr.DataArray,
    *,
    n_bins: int = 50,
    range_min: float | None = None,
    range_max: float | None = None,
) -> dict[str, Any]:
    """Compute a histogram for a radar variable.

    Parameters
    ----------
    data : xr.DataArray
        2D array (azimuth x range) of the variable.
    n_bins : int
        Number of histogram bins.
    range_min, range_max : float, optional
        Explicit bin limits. If *None*, derived from data.

    Returns
    -------
    dict
        Keys: ``bin_edges`` (list), ``counts`` (list), ``variable``
        (str), ``units`` (str), ``stats`` (dict with mean, std, min,
        max, median, count_valid).
    """
    vals = data.values.ravel()
    valid = vals[np.isfinite(vals)]

    if len(valid) == 0:
        return {
            "bin_edges": [],
            "counts": [],
            "variable": str(data.name) if data.name else "unknown",
            "units": str(data.attrs.get("units", "")),
            "stats": {
                "mean": None,
                "std": None,
                "min": None,
                "max": None,
                "median": None,
                "count_valid": 0,
            },
        }

    lo = range_min if range_min is not None else float(np.min(valid))
    hi = range_max if range_max is not None else float(np.max(valid))

    counts, bin_edges = np.histogram(valid, bins=n_bins, range=(lo, hi))

    return {
        "bin_edges": bin_edges.tolist(),
        "counts": counts.tolist(),
        "variable": str(data.name) if data.name else "unknown",
        "units": str(data.attrs.get("units", "")),
        "stats": {
            "mean": float(np.mean(valid)),
            "std": float(np.std(valid)),
            "min": float(np.min(valid)),
            "max": float(np.max(valid)),
            "median": float(np.median(valid)),
            "count_valid": len(valid),
        },
    }


# ---------------------------------------------------------------------------
# Area-averaged vertical profile
# ---------------------------------------------------------------------------


def area_averaged_profile(
    datatree: xr.DataTree,
    variable: str,
) -> dict[str, Any]:
    """Compute an area-averaged vertical profile across all sweeps.

    For each sweep, computes the mean of the variable. The result is a
    profile of mean values vs. elevation angle.

    Parameters
    ----------
    datatree : xr.DataTree
        Radar DataTree.
    variable : str
        Variable name.

    Returns
    -------
    dict
        Keys: ``elevations`` (list[float]), ``means`` (list[float]),
        ``stds`` (list[float]), ``counts`` (list[int]),
        ``variable`` (str).
    """
    sweep_names = sorted_sweep_names(datatree)
    elevations: list[float] = []
    means: list[float] = []
    stds: list[float] = []
    counts: list[int] = []

    for sname in sweep_names:
        ds = datatree[sname].to_dataset()
        if variable not in ds.data_vars:
            continue

        da = ds[variable]
        vals = da.values.ravel()
        valid = vals[np.isfinite(vals)]

        elevations.append(_sweep_elevation(ds))
        if len(valid) > 0:
            means.append(float(np.mean(valid)))
            stds.append(float(np.std(valid)))
            counts.append(len(valid))
        else:
            means.append(float("nan"))
            stds.append(float("nan"))
            counts.append(0)

    return {
        "elevations": elevations,
        "means": means,
        "stds": stds,
        "counts": counts,
        "variable": variable,
    }


# ---------------------------------------------------------------------------
# Cell statistics
# ---------------------------------------------------------------------------


def compute_cell_stats(
    datatree: xr.DataTree,
    dbz_variable: str = "DBZH",
    *,
    echo_top_thresh_dbz: float = 18.0,
) -> dict[str, Any]:
    """Compute storm cell statistics from a radar volume.

    Parameters
    ----------
    datatree : xr.DataTree
        Radar DataTree.
    dbz_variable : str
        Name of the reflectivity variable.
    echo_top_thresh_dbz : float
        Reflectivity threshold (dBZ) for echo-top height calculation.

    Returns
    -------
    dict
        Keys: ``max_reflectivity`` (float), ``vil_estimate`` (float,
        kg/m^2), ``echo_top_km`` (float), ``n_sweeps`` (int).
    """
    sweep_names = sorted_sweep_names(datatree)

    # Collect per-sweep max reflectivity and elevation info
    max_dbz = -999.0
    vil_sum = 0.0
    echo_top_km = 0.0
    n_sweeps = 0

    # Simple VIL approximation: VIL = sum over layers of
    #   3.44e-6 * Z_linear^(4/7) * dh
    # where dh is the layer thickness in metres.

    prev_height_m: float | None = None

    for sname in sweep_names:
        ds = datatree[sname].to_dataset()

        # Find reflectivity variable
        dbz_da = None
        # Only honour the requested name if it is a reflectivity field; asking
        # for cell stats while a velocity / ZDR field is selected must not
        # report that field's maximum as "max_reflectivity".
        candidates = list(_REFLECTIVITY_ALIASES)
        if _is_reflectivity_name(dbz_variable):
            candidates.insert(0, dbz_variable)
        for alias in candidates:
            if alias in ds.data_vars:
                dbz_da = ds[alias]
                break
        if dbz_da is None:
            continue

        vals = dbz_da.values
        valid = vals[np.isfinite(vals)]
        if len(valid) == 0:
            continue

        n_sweeps += 1
        sweep_max = float(np.max(valid))
        max_dbz = max(max_dbz, sweep_max)

        # Get elevation angle for height estimation
        elev_deg = _sweep_elevation(ds)

        # Approximate beam height at mid-range
        if "range" in ds.coords:
            mid_range_m = float(np.median(ds.coords["range"].values))
        else:
            mid_range_m = 100_000.0  # 100 km default

        # Standard atmosphere beam height: h = r * sin(elev) + r^2 / (2 * Re_eff)
        re_eff = 8493_000.0  # 4/3 earth radius in metres
        elev_rad = np.radians(elev_deg)
        height_m = mid_range_m * np.sin(elev_rad) + (mid_range_m**2) / (2.0 * re_eff)

        # Echo top: if any gate exceeds threshold at this elevation
        if sweep_max >= echo_top_thresh_dbz:
            echo_top_km = max(echo_top_km, height_m / 1000.0)

        # VIL contribution from this layer
        z_linear_mean = np.mean(10.0 ** (valid / 10.0))
        if prev_height_m is not None:
            dh = abs(height_m - prev_height_m)
            vil_sum += 3.44e-6 * (z_linear_mean ** (4.0 / 7.0)) * dh

        prev_height_m = height_m

    return {
        "max_reflectivity": float(max_dbz) if max_dbz > -999.0 else None,
        "vil_estimate": round(float(vil_sum), 2),
        "echo_top_km": round(echo_top_km, 2),
        "n_sweeps": n_sweeps,
    }


# ---------------------------------------------------------------------------
# QC statistics
# ---------------------------------------------------------------------------


def compute_qc_stats(
    data: xr.DataArray,
    *,
    noise_floor: float | None = None,
) -> dict[str, Any]:
    """Compute quality-control statistics for a radar variable.

    Parameters
    ----------
    data : xr.DataArray
        2D array (azimuth x range) of the variable.
    noise_floor : float, optional
        Noise threshold. If given, counts gates below this value.

    Returns
    -------
    dict
        Keys: ``total_gates`` (int), ``missing_gates`` (int),
        ``pct_missing`` (float), ``below_noise_gates`` (int),
        ``pct_below_noise`` (float), ``variable`` (str).
    """
    vals = data.values.ravel()
    total = len(vals)
    missing = int(np.sum(~np.isfinite(vals)))
    pct_missing = (missing / total * 100.0) if total > 0 else 0.0

    below_noise = 0
    pct_below_noise = 0.0
    if noise_floor is not None:
        finite = vals[np.isfinite(vals)]
        below_noise = int(np.sum(finite < noise_floor))
        pct_below_noise = (below_noise / total * 100.0) if total > 0 else 0.0

    return {
        "total_gates": total,
        "missing_gates": missing,
        "pct_missing": round(pct_missing, 2),
        "below_noise_gates": below_noise,
        "pct_below_noise": round(pct_below_noise, 2),
        "variable": str(data.name) if data.name else "unknown",
    }


# ---------------------------------------------------------------------------
# Dispatcher for WebSocket handler
# ---------------------------------------------------------------------------


def get_statistics(
    datatree: xr.DataTree,
    variable: str,
    sweep: int,
    stat_type: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch a statistics request.

    Parameters
    ----------
    datatree : xr.DataTree
        Radar DataTree.
    variable : str
        Variable name.
    sweep : int
        Sweep index.
    stat_type : str
        One of ``"histogram"``, ``"profile"``, ``"cell_stats"``, ``"qc"``.
    params : dict, optional
        Additional parameters passed to the stat function.

    Returns
    -------
    dict
        Statistics result.
    """
    if params is None:
        params = {}

    if stat_type == "profile":
        return area_averaged_profile(datatree, variable)

    if stat_type == "cell_stats":
        return compute_cell_stats(datatree, dbz_variable=variable, **params)

    # For histogram and qc, we need sweep-level data
    sweep_names = sorted_sweep_names(datatree)
    if sweep >= len(sweep_names):
        raise IndexError(f"Sweep {sweep} out of range (0..{len(sweep_names) - 1})")

    ds = datatree[sweep_names[sweep]].to_dataset()
    if variable not in ds.data_vars:
        available = sorted(str(v) for v in ds.data_vars if ds[v].ndim == 2)
        raise KeyError(f"Variable '{variable}' not found. Available: {available}")

    da = ds[variable]

    if stat_type == "histogram":
        return compute_histogram(da, **params)
    elif stat_type == "qc":
        return compute_qc_stats(da, **params)
    else:
        raise ValueError(f"Unknown stat_type: {stat_type}. Use: histogram, profile, cell_stats, qc")

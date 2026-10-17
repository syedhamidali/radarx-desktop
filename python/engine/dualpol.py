"""Dual-polarization analysis utilities.

Provides scatter data, histogram data, and region statistics for
the dual-pol analysis dashboard. All functions return JSON-serializable dicts.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import xarray as xr

from engine.reader import sorted_sweep_names

logger = logging.getLogger(__name__)

# Canonical variable name aliases (same as in retrievals.py)
_VAR_ALIASES: dict[str, list[str]] = {
    "DBZH": ["DBZH", "DBZ", "reflectivity", "Z", "corrected_reflectivity_horizontal"],
    "ZDR": ["ZDR", "differential_reflectivity", "Zdr"],
    "PHIDP": ["PHIDP", "differential_phase", "PhiDP", "UPHIDP"],
    "KDP": ["KDP", "specific_differential_phase"],
    "RHOHV": ["RHOHV", "cross_correlation_ratio", "RhoHV", "CCORH"],
    "VRADH": ["VRADH", "velocity", "V", "radial_velocity"],
}


def _find_var(ds: xr.Dataset, name: str) -> xr.DataArray | None:
    """Find a variable in a dataset using canonical aliases."""
    aliases = _VAR_ALIASES.get(name, [name])
    for alias in aliases:
        if alias in ds.data_vars:
            return ds[alias]
    # Fall back to exact name
    if name in ds.data_vars:
        return ds[name]
    return None


def _get_sweep_ds(datatree: xr.DataTree, sweep: int) -> xr.Dataset:
    """Extract a sweep Dataset from a DataTree."""
    sweep_nodes = sorted_sweep_names(datatree)
    if sweep >= len(sweep_nodes):
        raise IndexError(f"Sweep {sweep} out of range (0..{len(sweep_nodes) - 1})")
    return datatree[sweep_nodes[sweep]].to_dataset()


def scatter_data(
    datatree: xr.DataTree,
    var1: str,
    var2: str,
    sweep: int = 0,
    subsample: int = 5000,
    color_var: str | None = None,
) -> dict[str, Any]:
    """Return paired arrays for a scatter plot of two variables.

    Parameters
    ----------
    datatree : xr.DataTree
        Radar DataTree.
    var1, var2 : str
        Variable names for X and Y axes.
    sweep : int
        Sweep index.
    subsample : int
        Maximum number of points to return.
    color_var : str, optional
        Third variable for coloring points.

    Returns
    -------
    dict
        Keys: var1, var2, x, y, color (optional), units1, units2,
        n_total, n_valid, n_returned.
    """
    ds = _get_sweep_ds(datatree, sweep)

    da1 = _find_var(ds, var1)
    da2 = _find_var(ds, var2)
    if da1 is None:
        raise ValueError(f"Variable '{var1}' not found in sweep {sweep}")
    if da2 is None:
        raise ValueError(f"Variable '{var2}' not found in sweep {sweep}")

    v1 = da1.values.ravel()
    v2 = da2.values.ravel()

    # Mask where both are valid
    valid = np.isfinite(v1) & np.isfinite(v2)

    color_vals = None
    color_units = ""
    if color_var:
        da_c = _find_var(ds, color_var)
        if da_c is not None:
            vc = da_c.values.ravel()
            valid &= np.isfinite(vc)
            color_vals = vc
            color_units = str(da_c.attrs.get("units", ""))

    idx_valid = np.flatnonzero(valid)
    n_valid = len(idx_valid)

    # Subsample if needed
    if n_valid > subsample:
        rng = np.random.default_rng(42)
        idx_sample = rng.choice(idx_valid, size=subsample, replace=False)
        idx_sample.sort()
    else:
        idx_sample = idx_valid

    result: dict[str, Any] = {
        "var1": var1,
        "var2": var2,
        "x": v1[idx_sample].astype(np.float64).tolist(),
        "y": v2[idx_sample].astype(np.float64).tolist(),
        "units1": str(da1.attrs.get("units", "")),
        "units2": str(da2.attrs.get("units", "")),
        "n_total": len(v1),
        "n_valid": int(n_valid),
        "n_returned": len(idx_sample),
    }

    if color_vals is not None:
        result["color"] = color_vals[idx_sample].astype(np.float64).tolist()
        result["color_var"] = color_var
        result["color_units"] = color_units

    return result


def histogram_data(
    datatree: xr.DataTree,
    variable: str,
    sweep: int = 0,
    bins: int = 50,
) -> dict[str, Any]:
    """Compute histogram data for a variable.

    Parameters
    ----------
    datatree : xr.DataTree
        Radar DataTree.
    variable : str
        Variable name.
    sweep : int
        Sweep index.
    bins : int
        Number of bins.

    Returns
    -------
    dict
        Keys: variable, bin_edges, counts, stats (mean, median, std,
        min, max, skewness), units, n_valid.
    """
    ds = _get_sweep_ds(datatree, sweep)
    da = _find_var(ds, variable)
    if da is None:
        raise ValueError(f"Variable '{variable}' not found in sweep {sweep}")

    vals = da.values.ravel().astype(np.float64)
    valid = vals[np.isfinite(vals)]

    if len(valid) == 0:
        return {
            "variable": variable,
            "bin_edges": [],
            "counts": [],
            "stats": {},
            "units": str(da.attrs.get("units", "")),
            "n_valid": 0,
        }

    counts, bin_edges = np.histogram(valid, bins=int(bins))

    mean = float(np.mean(valid))
    std = float(np.std(valid))
    median = float(np.median(valid))
    vmin = float(np.min(valid))
    vmax = float(np.max(valid))

    # Skewness: Fisher's definition
    skewness = float(np.mean(((valid - mean) / std) ** 3)) if std > 0 and len(valid) > 2 else 0.0

    # Gaussian fit: use mean and std
    bin_centers = 0.5 * (bin_edges[:-1] + bin_edges[1:])
    bin_width = float(bin_edges[1] - bin_edges[0])
    if std > 0:
        gaussian = (
            len(valid)
            * bin_width
            / (std * np.sqrt(2 * np.pi))
            * np.exp(-0.5 * ((bin_centers - mean) / std) ** 2)
        )
    else:
        # Constant field: a Gaussian fit is undefined (and inf/NaN is not valid JSON)
        gaussian = np.zeros_like(bin_centers)

    return {
        "variable": variable,
        "bin_edges": bin_edges.tolist(),
        "counts": counts.tolist(),
        "gaussian_fit": gaussian.tolist(),
        "stats": {
            "mean": mean,
            "median": median,
            "std": std,
            "min": vmin,
            "max": vmax,
            "skewness": skewness,
        },
        "units": str(da.attrs.get("units", "")),
        "n_valid": len(valid),
    }


def region_stats(
    datatree: xr.DataTree,
    variable: str,
    sweep: int = 0,
    az_min: float = 0.0,
    az_max: float = 360.0,
    range_min: float = 0.0,
    range_max: float = 300000.0,
) -> dict[str, Any]:
    """Compute statistics for a polar region.

    Parameters
    ----------
    datatree : xr.DataTree
        Radar DataTree.
    variable : str
        Variable name.
    sweep : int
        Sweep index.
    az_min, az_max : float
        Azimuth range in degrees.
    range_min, range_max : float
        Range limits in metres.

    Returns
    -------
    dict
        Keys: variable, stats, n_valid, n_total, units.
    """
    ds = _get_sweep_ds(datatree, sweep)
    da = _find_var(ds, variable)
    if da is None:
        raise ValueError(f"Variable '{variable}' not found in sweep {sweep}")

    # Get coordinate arrays
    az = (
        da.coords["azimuth"].values
        if "azimuth" in da.coords
        else np.arange(da.shape[0], dtype=np.float64)
    )
    rng = (
        da.coords["range"].values
        if "range" in da.coords
        else np.arange(da.shape[-1], dtype=np.float64)
    )

    # Build mask for region.  Azimuth limits are normalised to [0, 360) so that
    # e.g. (-10, 10) or (350, 370) select the sector through north; a span of
    # 360 degrees or more selects every ray.
    az_arr = np.asarray(az, dtype=np.float64)
    if az_max - az_min >= 360.0:
        az_mask = np.ones(az_arr.shape, dtype=bool)
    else:
        lo = az_min % 360.0
        hi = az_max % 360.0
        az_mod = np.mod(az_arr, 360.0)
        # lo > hi means the sector wraps through north (e.g. 350-10 degrees)
        wraps = lo > hi
        az_mask = ((az_mod >= lo) | (az_mod <= hi)) if wraps else ((az_mod >= lo) & (az_mod <= hi))

    rng_mask = (rng >= range_min) & (rng <= range_max)

    # Apply mask: az along first axis, range along second.  Convert to float64
    # only after dropping non-finite gates (avoids a full-region copy).
    region = da.values[np.ix_(az_mask, rng_mask)].ravel()
    n_total = region.size
    valid = region[np.isfinite(region)].astype(np.float64, copy=False)

    if len(valid) == 0:
        return {
            "variable": variable,
            "stats": {},
            "n_valid": 0,
            "n_total": n_total,
            "units": str(da.attrs.get("units", "")),
        }

    mean = float(np.mean(valid))
    std = float(np.std(valid))

    return {
        "variable": variable,
        "stats": {
            "mean": mean,
            "median": float(np.median(valid)),
            "std": std,
            "min": float(np.min(valid)),
            "max": float(np.max(valid)),
            "skewness": float(np.mean(((valid - mean) / max(std, 1e-9)) ** 3)),
            "count": len(valid),
        },
        "n_valid": len(valid),
        "n_total": n_total,
        "units": str(da.attrs.get("units", "")),
    }

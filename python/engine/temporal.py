"""Temporal analysis tools for multi-file radar comparisons.

Provides time series extraction, element-wise differencing,
accumulation, and linear trend computation across multiple
radar files.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import numpy as np
import xarray as xr

from engine.reader import sorted_sweep_names

logger = logging.getLogger(__name__)

NODATA = -9999.0


def compute_time_series(
    readers: dict[str, Any],
    variable: str,
    sweep: int,
    point_az: float,
    point_range: float,
) -> dict[str, Any]:
    """Extract the value at a fixed (azimuth, range) point across multiple files.

    Parameters
    ----------
    readers : dict[str, RadarReader]
        Mapping of file_id -> RadarReader instances.
    variable : str
        Variable name (e.g. ``"DBZH"``).
    sweep : int
        Sweep index to query.
    point_az : float
        Azimuth angle in degrees.
    point_range : float
        Range distance in metres.

    Returns
    -------
    dict
        ``{file_ids: [...], values: [...], timestamps: [...], variable, sweep,
          azimuth, range_m}``
    """
    rows: list[tuple[str, float | None, str | None]] = []

    for fid, reader in readers.items():
        try:
            da = reader.get_sweep_data(sweep, variable)
            rows.append((fid, _sample_point(da, point_az, point_range), _extract_timestamp(reader)))
        except Exception as exc:
            logger.debug("Skipping file %s in time series: %s", fid, exc)
            rows.append((fid, None, None))

    rows = _sort_chronologically(rows)
    file_ids = [r[0] for r in rows]
    values = [r[1] for r in rows]
    timestamps = [r[2] for r in rows]

    return {
        "file_ids": file_ids,
        "values": values,
        "timestamps": timestamps,
        "variable": variable,
        "sweep": sweep,
        "azimuth": point_az,
        "range_m": point_range,
    }


def compute_difference(
    reader1: Any,
    reader2: Any,
    variable: str,
    sweep: int,
) -> dict[str, Any]:
    """Compute element-wise difference (reader2 - reader1) for a sweep.

    Parameters
    ----------
    reader1, reader2 : RadarReader
        Two open RadarReader instances.
    variable : str
        Radar moment variable name.
    sweep : int
        Sweep index.

    Returns
    -------
    dict
        ``{diff: list[list[float]], n_az: int, n_range: int, vmin: float,
          vmax: float, variable, sweep}``
    """
    da1 = reader1.get_sweep_data(sweep, variable)
    da2 = reader2.get_sweep_data(sweep, variable)

    v1 = np.asarray(da1.values, dtype=np.float32)
    v2 = np.asarray(da2.values, dtype=np.float32)

    # Handle shape mismatch by trimming to smallest common shape
    min_az = min(v1.shape[0], v2.shape[0])
    min_rng = min(v1.shape[1], v2.shape[1])

    # float32 is plenty for radar moments; inf-inf etc. become NaN, which we mask below
    with np.errstate(invalid="ignore"):
        diff = v2[:min_az, :min_rng] - v1[:min_az, :min_rng]
    finite = np.isfinite(diff)
    if finite.any():
        valid = diff[finite]
        vmin = float(valid.min())
        vmax = float(valid.max())
    else:
        vmin, vmax = -1.0, 1.0

    # Make symmetric about zero for diverging colormaps
    abs_max = max(abs(vmin), abs(vmax))

    # Get coordinate arrays for the client
    az1 = (
        da1.coords["azimuth"].values.astype(np.float32)
        if "azimuth" in da1.coords
        else np.arange(min_az, dtype=np.float32)
    )
    rng1 = (
        da1.coords["range"].values.astype(np.float32)
        if "range" in da1.coords
        else np.arange(min_rng, dtype=np.float32)
    )
    az1 = az1[:min_az]
    rng1 = rng1[:min_rng]

    diff_f32 = np.where(finite, diff, np.float32(NODATA)).astype(np.float32, copy=False)

    return {
        "diff_values": diff_f32.tobytes(),
        "azimuth": az1.tobytes(),
        "range": rng1.tobytes(),
        "n_az": min_az,
        "n_range": min_rng,
        "vmin": -abs_max,
        "vmax": abs_max,
        "max_range": float(rng1.max()) if len(rng1) > 0 else 1.0,
        "variable": variable,
        "sweep": sweep,
        "azimuth_bytes": az1.nbytes,
        "range_bytes": rng1.nbytes,
        "diff_bytes": diff_f32.nbytes,
    }


def compute_accumulation(
    readers: dict[str, Any],
    variable: str,
    sweep: int,
    method: Literal["sum", "mean", "max"] = "sum",
) -> dict[str, Any]:
    """Accumulate a variable across multiple files.

    Useful for rainfall accumulation (sum), average reflectivity (mean),
    or max composite (max).

    Parameters
    ----------
    readers : dict[str, RadarReader]
    variable : str
    sweep : int
    method : {"sum", "mean", "max"}

    Returns
    -------
    dict
        ``{values: list[list[float]], n_az, n_range, vmin, vmax, n_files,
          method, variable, sweep}``
    """
    if method not in ("sum", "mean", "max"):
        raise ValueError(f"Unknown accumulation method: {method}")

    # Running accumulators instead of stacking every file (memory is O(one sweep),
    # not O(n_files x sweep)); NaN means "no echo" and is skipped.
    acc: np.ndarray | None = None
    cnt: np.ndarray | None = None
    n_files = 0
    errors: list[str] = []

    for fid, reader in readers.items():
        try:
            arr = np.asarray(reader.get_sweep_data(sweep, variable).values)
        except Exception as exc:
            logger.debug("Skipping file %s in accumulation: %s", fid, exc)
            errors.append(f"{fid}: {exc}")
            continue
        if arr.ndim != 2:
            errors.append(f"{fid}: expected a 2-D sweep, got {arr.ndim}-D")
            continue

        if acc is None:
            acc = np.full(arr.shape, np.nan if method == "max" else 0.0, dtype=np.float64)
            cnt = np.zeros(arr.shape, dtype=np.int32)
        else:
            # Align to the smallest common shape
            min_az = min(acc.shape[0], arr.shape[0])
            min_rng = min(acc.shape[1], arr.shape[1])
            acc, cnt = acc[:min_az, :min_rng], cnt[:min_az, :min_rng]  # type: ignore[index]
        part = arr[: acc.shape[0], : acc.shape[1]]
        ok = np.isfinite(part)
        if method == "max":
            np.fmax(acc, np.where(ok, part, np.nan) if not ok.all() else part, out=acc)
        else:
            acc += np.where(ok, part, 0.0)
        cnt += ok  # type: ignore[operator]
        n_files += 1

    if acc is None or cnt is None:
        detail = f" ({'; '.join(errors[:3])})" if errors else ""
        raise ValueError(f"No valid data found for accumulation{detail}")

    if method == "mean":
        with np.errstate(invalid="ignore", divide="ignore"):
            result = np.where(cnt > 0, acc / np.maximum(cnt, 1), np.nan)
    else:
        # "sum" keeps the historical nansum behaviour (0 where nothing echoed);
        # "max" is NaN there.
        result = acc

    finite = np.isfinite(result)
    if finite.any():
        valid = result[finite]
        vmin, vmax = float(valid.min()), float(valid.max())
    else:
        vmin, vmax = 0.0, 1.0

    return {
        "values": _nan_to_none_list(result),
        "n_az": result.shape[0],
        "n_range": result.shape[1],
        "vmin": vmin,
        "vmax": vmax,
        "n_files": n_files,
        "method": method,
        "variable": variable,
        "sweep": sweep,
    }


def compute_trend(
    readers: dict[str, Any],
    variable: str,
    sweep: int,
    point_az: float,
    point_range: float,
) -> dict[str, Any]:
    """Compute a linear trend (slope) at a fixed point across files.

    Parameters
    ----------
    readers : dict[str, RadarReader]
    variable, sweep, point_az, point_range : as in compute_time_series

    Returns
    -------
    dict
        ``{slope, intercept, r_squared, n_points, values, file_ids}``
    """
    ts = compute_time_series(readers, variable, sweep, point_az, point_range)

    valid_idx = [i for i, v in enumerate(ts["values"]) if v is not None and np.isfinite(v)]

    if len(valid_idx) < 2:
        return {
            "slope": None,
            "intercept": None,
            "r_squared": None,
            "n_points": len(valid_idx),
            "values": ts["values"],
            "file_ids": ts["file_ids"],
            "timestamps": ts["timestamps"],
            "variable": variable,
            "sweep": sweep,
            "azimuth": point_az,
            "range_m": point_range,
        }

    x = np.array(valid_idx, dtype=np.float64)
    y = np.array([ts["values"][i] for i in valid_idx], dtype=np.float64)

    # Linear regression: y = slope * x + intercept
    n = len(x)
    sx = np.sum(x)
    sy = np.sum(y)
    sxx = np.sum(x * x)
    sxy = np.sum(x * y)

    denom = n * sxx - sx * sx
    if abs(denom) < 1e-15:
        slope = 0.0
        intercept = float(np.mean(y))
        r_squared = 0.0
    else:
        slope = float((n * sxy - sx * sy) / denom)
        intercept = float((sy - slope * sx) / n)

        # R-squared
        y_pred = slope * x + intercept
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r_squared = float(1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0

    return {
        "slope": slope,
        "intercept": intercept,
        "r_squared": r_squared,
        "n_points": n,
        "values": ts["values"],
        "file_ids": ts["file_ids"],
        "timestamps": ts["timestamps"],
        "variable": variable,
        "sweep": sweep,
        "azimuth": point_az,
        "range_m": point_range,
    }


# ── Internal helpers ──────────────────────────────────────────────────────────


def _nan_to_none_list(arr: np.ndarray) -> list[Any]:
    """Nested list with NaN/Inf as None (JSON.parse rejects NaN)."""
    a = np.asarray(arr, dtype=np.float64)
    obj = a.astype(object)
    obj[~np.isfinite(a)] = None
    return obj.tolist()


def _parse_timestamp(ts: str | None) -> np.datetime64 | None:
    if ts is None:
        return None
    try:
        return np.datetime64(ts.strip().replace("Z", ""), "ns")
    except (ValueError, TypeError):
        return None


def _sort_chronologically(
    rows: list[tuple[str, float | None, str | None]],
) -> list[tuple[str, float | None, str | None]]:
    """Order (file_id, value, timestamp) rows by time; undated rows keep their order at the end.

    If *no* timestamp can be parsed the original (file-open) order is kept.
    """
    parsed = [_parse_timestamp(r[2]) for r in rows]
    if all(p is None for p in parsed):
        return rows
    dated = sorted((i for i, p in enumerate(parsed) if p is not None), key=lambda i: (parsed[i], i))
    undated = [i for i, p in enumerate(parsed) if p is None]
    return [rows[i] for i in dated + undated]


def _sample_point(da: xr.DataArray, az_deg: float, range_m: float) -> float | None:
    """Sample the nearest-neighbour value from a DataArray at (azimuth, range).

    Azimuth distance is circular (359.9 is next to 0.1) and a point farther than
    ~one gate beyond the sweep's range coverage, or an azimuth gap wider than twice
    the ray spacing, yields ``None`` instead of the nearest edge gate.
    """
    if "azimuth" not in da.coords or "range" not in da.coords:
        return None

    az = np.asarray(da.coords["azimuth"].values, dtype=np.float64)
    rng = np.asarray(da.coords["range"].values, dtype=np.float64)
    if az.size == 0 or rng.size == 0:
        return None

    az_dist = np.abs((az - az_deg + 180.0) % 360.0 - 180.0)
    az_idx = int(np.argmin(az_dist))
    rng_idx = int(np.argmin(np.abs(rng - range_m)))

    if rng.size > 1:
        gate = float(np.median(np.abs(np.diff(rng))))
        if abs(rng[rng_idx] - range_m) > max(gate, 1e-9):
            return None
    if az.size > 1:
        spacing = float(np.median(np.abs(np.diff(np.sort(az)))))
        if az_dist[az_idx] > max(2.0 * spacing, 1.0):
            return None

    if da.dims == ("azimuth", "range"):
        val = float(da.values[az_idx, rng_idx])
    else:
        val = float(da.isel(azimuth=az_idx, range=rng_idx).values)
    return val if np.isfinite(val) else None


def _extract_timestamp(reader: Any) -> str | None:
    """Try to pull a timestamp string from a reader's datatree attributes."""
    dtree = reader.datatree
    if dtree is None:
        return None

    try:
        root_ds = dtree.to_dataset()
        attrs = root_ds.attrs

        # Common attribute names for radar file timestamps
        for key in (
            "time_coverage_start",
            "time",
            "date",
            "timestamp",
            "scan_time",
            "start_datetime",
            "start_time",
        ):
            if key in attrs:
                return str(attrs[key])

        # Check for a time coordinate in the first sweep
        names = sorted_sweep_names(dtree)
        if names:
            ds = dtree[names[0]].to_dataset()
            if "time" in ds.coords:
                t = ds.coords["time"].values
                if t.ndim > 0 and t.size > 0:
                    return str(t.flat[0])
                return str(t)
    except Exception:
        pass

    return None

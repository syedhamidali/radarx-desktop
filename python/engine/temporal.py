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


# NEXRAD (and some other decoders) fill "no echo" with a real number (-33 dBZ) instead of NaN.
_NO_ECHO_DBZ = -32.0
_REFLECTIVITY_HINTS = ("dbz", "refl", "power")


def _mask_no_echo(values: np.ndarray, variable: str) -> np.ndarray:
    """NaN-out the no-echo fill of reflectivity-like variables so it is not treated as data."""
    if not any(h in variable.lower() for h in _REFLECTIVITY_HINTS):
        return values
    return np.where(values <= _NO_ECHO_DBZ, np.float32(np.nan), values)


#: name fragments of logarithmic (decibel) moments, used when a variable has no ``units``
_DB_NAME_HINTS = ("dbz", "dbt", "dbm", "zdr", "snr", "refl", "power")


def _is_decibel(da: xr.DataArray, variable: str) -> bool:
    """True if the variable is in decibels (dBZ, dB, dBm): its units, else its name."""
    units = str(da.attrs.get("units", "")).strip().lower()
    if units:
        return units.startswith("db") or units.startswith("decibel")
    return any(h in variable.lower() for h in _DB_NAME_HINTS)


def _polar_axes(da: xr.DataArray) -> tuple[np.ndarray, np.ndarray] | None:
    """``(azimuth, range)`` coordinates of a 2-D ``(azimuth, range)`` sweep, else ``None``."""
    if da.dims != ("azimuth", "range") or "azimuth" not in da.coords or "range" not in da.coords:
        return None
    az = np.asarray(da.coords["azimuth"].values, dtype=np.float64)
    rng = np.asarray(da.coords["range"].values, dtype=np.float64)
    if az.size == 0 or rng.size == 0 or not (np.isfinite(az).all() and np.isfinite(rng).all()):
        return None
    return az, rng


def _grid_axes(
    values: np.ndarray, axes: tuple[np.ndarray, np.ndarray] | None
) -> tuple[np.ndarray, np.ndarray] | None:
    """*axes* if they describe the shape of *values*, else ``None``."""
    ok = axes is not None and values.shape == (axes[0].size, axes[1].size)
    return axes if ok else None


def _ray_step(az: np.ndarray) -> float:
    """Typical angle between neighbouring rays (deg); 1 for a single ray."""
    if az.size < 2:
        return 1.0
    s = np.sort(np.mod(az, 360.0))
    gaps = np.diff(np.concatenate([s, [s[0] + 360.0]]))
    gaps = gaps[gaps > 1e-9]
    return float(np.median(gaps)) if gaps.size else 1.0


def _gate_step(rng: np.ndarray) -> float:
    """Typical gate spacing (m); 1 for a single gate."""
    if rng.size < 2:
        return 1.0
    d = np.abs(np.diff(np.sort(rng)))
    d = d[d > 1e-9]
    return float(np.median(d)) if d.size else 1.0


def _on_grid(
    values: np.ndarray,
    src: tuple[np.ndarray, np.ndarray],
    ref: tuple[np.ndarray, np.ndarray],
) -> np.ndarray:
    """*values* (on the ``src`` rays and gates) on the ``ref`` rays and gates.

    Two scans never have the same ray angles, and may have different numbers of rays or gates
    (0.5 and 1 degree, 250 and 1000 m). Index ``i`` of one is then not the ray ``i`` of the
    other. Volumes whose grids agree to within half a step where they overlap are used as they
    are (nothing is resampled); otherwise every ``ref`` ray and gate takes the nearest ray
    (circular) and gate of the source, and is missing where the source has no ray within 1.5
    steps or no gate within half a step.
    """
    az_s, rng_s = src
    az_r, rng_r = ref
    # Grids that agree where they overlap (the same scan strategy, one maybe a sector or shorter):
    # the caller trims to the common part, nothing is resampled.
    m_az, m_rng = min(az_s.size, az_r.size), min(rng_s.size, rng_r.size)
    d_az = np.abs((az_s[:m_az] - az_r[:m_az] + 180.0) % 360.0 - 180.0)
    if d_az.max() <= 0.5 * _ray_step(az_r) and np.abs(
        rng_s[:m_rng] - rng_r[:m_rng]
    ).max() <= 0.5 * _gate_step(rng_r):
        return values

    order = np.argsort(np.mod(az_s, 360.0))
    a = np.mod(az_s, 360.0)[order]
    q = np.mod(az_r, 360.0)
    hi = np.searchsorted(a, q) % a.size
    lo = (hi - 1) % a.size
    d_lo = np.abs((q - a[lo] + 180.0) % 360.0 - 180.0)
    d_hi = np.abs((q - a[hi] + 180.0) % 360.0 - 180.0)
    use_hi = d_hi < d_lo
    ray = order[np.where(use_hi, hi, lo)]
    ray_ok = np.where(use_hi, d_hi, d_lo) <= 1.5 * _ray_step(az_s)

    go = np.argsort(rng_s)
    g = rng_s[go]
    pos = np.clip(np.searchsorted(g, rng_r), 1, max(g.size - 1, 1)) if g.size > 1 else 0
    if g.size > 1:
        left = np.abs(rng_r - g[pos - 1]) <= np.abs(g[pos] - rng_r)
        gate = go[np.where(left, pos - 1, pos)]
    else:
        gate = np.zeros(rng_r.size, dtype=np.intp)
    gate_ok = np.abs(rng_s[gate] - rng_r) <= 0.5 * _gate_step(rng_s) * (1 + 1e-9)

    out = np.full((az_r.size, rng_r.size), np.nan, dtype=np.float32)
    sel = values[np.ix_(ray, gate)].astype(np.float32, copy=False)
    out[np.ix_(ray_ok, gate_ok)] = sel[np.ix_(ray_ok, gate_ok)]
    return out


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

    v1 = _mask_no_echo(np.asarray(da1.values, dtype=np.float32), variable)
    v2 = _mask_no_echo(np.asarray(da2.values, dtype=np.float32), variable)

    # The second scan is put on the rays and gates of the first (a no-op when they agree).
    # Without coordinates the arrays are trimmed to the smallest common shape instead.
    axes1, axes2 = _polar_axes(da1), _polar_axes(da2)
    if (
        axes1 is not None
        and axes2 is not None
        and v1.shape == (axes1[0].size, axes1[1].size)
        and v2.shape == (axes2[0].size, axes2[1].size)
    ):
        v2 = _on_grid(v2, axes2, axes1)
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

    Useful for adding up a linear quantity such as a rain rate or depth (sum), average
    reflectivity (mean), or max composite (max).

    Decibel variables (units dBZ / dB / dBm, or a reflectivity-like name when there are no units)
    are averaged in linear units: ``10 log10(mean(10^(x/10)))``, so 20 and 40 dBZ average to
    37.0 dBZ (Z = 100 and 10000 mm6 m-3), not 30. Summing them is refused (``ValueError``): a sum
    of logarithms is not a physical quantity. Rainfall totals come from :mod:`engine.hydro`.

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
          method, averaged_in, variable, sweep}``; ``averaged_in`` is ``"linear"`` when a
          decibel variable was averaged as powers, else ``"native"``.
    """
    if method not in ("sum", "mean", "max"):
        raise ValueError(f"Unknown accumulation method: {method}")

    # Running accumulators instead of stacking every file (memory is O(one sweep),
    # not O(n_files x sweep)); NaN means "no echo" and is skipped.
    acc: np.ndarray | None = None
    cnt: np.ndarray | None = None
    ref_axes: tuple[np.ndarray, np.ndarray] | None = None
    n_files = 0
    errors: list[str] = []
    decibel = False

    for fid, reader in readers.items():
        try:
            da = reader.get_sweep_data(sweep, variable)
            arr = np.asarray(da.values)
        except Exception as exc:
            logger.debug("Skipping file %s in accumulation: %s", fid, exc)
            errors.append(f"{fid}: {exc}")
            continue
        if arr.ndim != 2:
            errors.append(f"{fid}: expected a 2-D sweep, got {arr.ndim}-D")
            continue

        axes = _polar_axes(da)
        if acc is None:
            decibel = _is_decibel(da, variable)
            if decibel and method == "sum":
                raise ValueError(
                    f"Summing {variable} is not meaningful: it is in decibels (a logarithm), and "
                    "a sum of dBZ is neither a rainfall nor a reflectivity. Use 'mean' (averaged "
                    "in linear units) or 'max'; for a rainfall total use the Rainfall (QPE) tool."
                )
            acc = np.full(arr.shape, np.nan if method == "max" else 0.0, dtype=np.float64)
            cnt = np.zeros(arr.shape, dtype=np.int32)
            ref_axes = _grid_axes(arr, axes)
        elif ref_axes is not None and _grid_axes(arr, axes) is not None:
            # Put the scan on the rays and gates of the first one (a no-op when they agree)
            arr = _on_grid(arr, axes, ref_axes)  # type: ignore[arg-type]
        else:
            # Without coordinates: align to the smallest common shape
            min_az = min(acc.shape[0], arr.shape[0])
            min_rng = min(acc.shape[1], arr.shape[1])
            acc, cnt = acc[:min_az, :min_rng], cnt[:min_az, :min_rng]  # type: ignore[index]
        part = arr[: acc.shape[0], : acc.shape[1]]
        if decibel and method == "mean":  # average powers (Z in mm6 m-3), not logarithms
            part = np.power(10.0, np.asarray(part, dtype=np.float64) / 10.0)
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
            if decibel:
                result = 10.0 * np.log10(result)
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
        # "linear": a decibel variable averaged as powers (Z in mm6 m-3) and converted back
        "averaged_in": "linear" if decibel and method == "mean" else "native",
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

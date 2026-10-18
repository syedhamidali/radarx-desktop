"""Unified storm-cell identification, tracking and statistics.

One API, two interchangeable backends:

* **radarx** (preferred when installed, ``radarx.retrieve.identify_cells`` /
  ``track_cells`` / ``cell_statistics``): multi-threshold features + watershed
  segmentation (tobac style), overlap tracking with storm-motion advection
  (PyFLEXTRKR style) including split / merge detection, and per-cell field
  statistics.  radarx works on regular Cartesian grids, so polar sweeps are resampled
  onto a radar-centred x/y grid (default 0.5 km) for labelling; the labels are mapped back
  to the native polar gates, so area / peak / mean / outline are computed on the original
  data, exactly as in the fallback.
* **numpy** (always available; :mod:`engine.tracking`): 8-connected components on the
  polar grid with 0/360 seam merging and greedy nearest-neighbour tracking.

``backend="auto"`` (the default) uses radarx when it provides the three functions and falls
back to numpy otherwise or when radarx raises.  Every result records the backend that was
actually used (``CellScan.backend`` / ``TrackResult.backend``) so callers can report it.

Public API
----------
``identify(data, ...) -> CellScan``
    Cells of one sweep (a ``list[CellInfo]`` subclass that also remembers the polar label
    image, geometry and time so tracking can use the real cell masks).
``track(scans, ...) -> TrackResult``
    Tracks over a time series of ``CellScan`` (or plain ``list[CellInfo]``): motion vector,
    speed, lifetime, split / merge flags (radarx backend).
``track_pair(prev, new, ...)``
    Two-scan form with the legacy track ids used by the ``track_cells`` WebSocket message.
``cell_rows(cells)`` / ``arrow_coords(cell)``
    Statistics table rows and motion-arrow geometry shared by the plug-in modules.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import xarray as xr

from engine.tracking import (
    CellInfo,
    CellTrack,
    cells_from_labels,
    extrapolate_cell,
    identify_cells_labelled,
    match_nearest,
    polar_geometry,
)

logger = logging.getLogger(__name__)

BACKENDS = ("auto", "radarx", "numpy")
DEFAULT_GRID_KM = 0.5
_MAX_GRID_HALF_CELLS = 500  # identification grid never exceeds ~1001 x 1001 cells
_EPOCH = np.datetime64("2000-01-01T00:00:00", "ns")

# ---------------------------------------------------------------------------
# radarx availability
# ---------------------------------------------------------------------------

_RX_CACHE: dict[str, Any] = {}


def radarx_retrieve() -> Any | None:
    """``radarx.retrieve`` if it provides the cell functions, else ``None`` (cached)."""
    if "mod" not in _RX_CACHE:
        mod = None
        try:
            import radarx.retrieve as rt

            if all(hasattr(rt, n) for n in ("identify_cells", "track_cells", "cell_statistics")):
                mod = rt
        except Exception as exc:  # ImportError, or a broken optional dependency
            logger.debug("radarx unavailable: %s", exc)
        _RX_CACHE["mod"] = mod
    return _RX_CACHE["mod"]


def radarx_version() -> str:
    try:
        import radarx

        return str(getattr(radarx, "__version__", "?"))
    except Exception:
        return "?"


def resolve_backend(requested: str = "auto") -> str:
    """``"radarx"`` or ``"numpy"``: what a request for *requested* will actually run."""
    if requested not in BACKENDS:
        raise ValueError(f"backend must be one of {BACKENDS}, not {requested!r}")
    if requested == "numpy":
        return "numpy"
    return "radarx" if radarx_retrieve() is not None else "numpy"


def backend_label(backend: str) -> str:
    """Human-readable backend name for result text, e.g. ``"radarx 0.5.1"``."""
    return f"radarx {radarx_version()}" if backend == "radarx" else "numpy/scipy (built-in)"


# ---------------------------------------------------------------------------
# containers
# ---------------------------------------------------------------------------


class CellScan(list):
    """Cells of one scan (a ``list[CellInfo]``) plus what tracking needs to reuse them."""

    def __init__(
        self,
        cells: Sequence[CellInfo] = (),
        *,
        backend: str = "numpy",
        labels: np.ndarray | None = None,
        azimuth: np.ndarray | None = None,
        range_m: np.ndarray | None = None,
        time_s: float | None = None,
        thresholds: Sequence[float] = (),
        note: str = "",
    ) -> None:
        super().__init__(cells)
        self.backend = backend
        self.labels = labels  # int32 (azimuth, range); value k <-> self[k - 1]
        self.azimuth = azimuth
        self.range_m = range_m
        self.time_s = time_s
        self.thresholds = tuple(thresholds)
        self.note = note

    @property
    def backend_label(self) -> str:
        return backend_label(self.backend)


class TrackResult(list):
    """List of :class:`~engine.tracking.CellTrack` plus how they were obtained."""

    def __init__(
        self,
        tracks: Sequence[CellTrack] = (),
        *,
        backend: str = "numpy",
        motion: tuple[float, float] | None = None,
        times_s: Sequence[float] = (),
        note: str = "",
    ) -> None:
        super().__init__(tracks)
        self.backend = backend
        self.motion = motion  # storm motion (u east, v north, m/s) used to advect cells
        self.times_s = list(times_s)
        self.note = note

    @property
    def backend_label(self) -> str:
        return backend_label(self.backend)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def site_lat_lon(datatree: Any) -> tuple[float | None, float | None]:
    """Radar position from a DataTree root (variables, coords or attrs), else ``(None, None)``."""
    try:
        root = datatree.root if hasattr(datatree, "root") else datatree
        out = []
        for key in ("latitude", "longitude"):
            val = None
            if key in root.variables:
                val = np.asarray(root[key].values).ravel()[0]
            elif key in root.attrs:
                val = root.attrs[key]
            out.append(float(val) if val is not None and math.isfinite(float(val)) else None)
        return out[0], out[1]
    except Exception:
        return None, None


def scan_time_seconds(data: xr.DataArray) -> float | None:
    """Earliest ray time of a sweep as seconds since the Unix epoch, if the array has one."""
    try:
        t = data.coords["time"].values
        if not np.issubdtype(t.dtype, np.datetime64):
            return None
        t = t[~np.isnat(t)]
        if t.size == 0:
            return None
        return float((t.min() - np.datetime64(0, "s")) / np.timedelta64(1, "s"))
    except Exception:
        return None


def sweep_elevation_deg(data: xr.DataArray) -> float | None:
    """Fixed elevation of a sweep from its ``elevation`` coordinate, if present."""
    try:
        el = np.asarray(data.coords["elevation"].values, dtype=np.float64)
        el = el[np.isfinite(el)]
        return float(np.median(el)) if el.size else None
    except Exception:
        return None


def dataset_elevation_deg(ds: xr.Dataset) -> float | None:
    """Sweep elevation (deg) from ``sweep_fixed_angle`` or the ``elevation`` coordinate."""
    try:
        if "sweep_fixed_angle" in ds.variables:
            val = float(np.asarray(ds["sweep_fixed_angle"].values).ravel()[0])
            if math.isfinite(val):
                return val
        el = np.asarray(ds["elevation"].values, dtype=np.float64)
        el = el[np.isfinite(el)]
        return float(np.median(el)) if el.size else None
    except Exception:
        return None


def _xy_m(cell: CellInfo) -> tuple[float, float]:
    az = math.radians(cell.centroid_az)
    return cell.centroid_range * math.sin(az), cell.centroid_range * math.cos(az)


def options_from_params(params: dict[str, Any]) -> dict[str, Any]:
    """Validated optional engine settings from a WebSocket ``params`` object.

    Recognised keys (all optional): ``backend`` ("auto" | "radarx" | "numpy"), ``thresholds``
    (ascending list of dBZ), ``grid_spacing_km`` and ``smooth`` (radarx backend).
    Raises ``ValueError`` with a user-facing message for bad values.
    """
    backend = params.get("backend", "auto")
    if backend not in BACKENDS:
        raise ValueError(f"'backend' must be one of {list(BACKENDS)}")
    out: dict[str, Any] = {"backend": backend}
    thr = params.get("thresholds")
    if thr is not None:
        if (
            not isinstance(thr, (list, tuple))
            or not thr
            or not all(isinstance(t, (int, float)) and math.isfinite(t) for t in thr)
        ):
            raise ValueError("'thresholds' must be a non-empty list of numbers")
        out["thresholds"] = [float(t) for t in thr]
    for key in ("grid_spacing_km", "smooth"):
        val = params.get(key)
        if val is not None:
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not val > 0:
                raise ValueError(f"'{key}' must be a positive number")
            out[key] = float(val)
    return out


def identify_sweep(
    datatree: Any,
    data: xr.DataArray,
    threshold_dbz: float = 35.0,
    min_size_km2: float = 10.0,
    **options: Any,
) -> CellScan:
    """:func:`identify` with the radar position, elevation and scan time taken from the file."""
    lat, lon = site_lat_lon(datatree)
    return identify(
        data,
        threshold_dbz,
        min_size_km2,
        lat=lat,
        lon=lon,
        elevation_deg=sweep_elevation_deg(data),
        time_s=scan_time_seconds(data),
        **options,
    )


def uv_to_speed_dir(u: float, v: float) -> tuple[float, float]:
    """Speed (m/s) and meteorological *from* direction (deg) of an eastward/northward vector."""
    speed = math.hypot(u, v)
    if speed == 0.0:
        return 0.0, 0.0
    return speed, (math.degrees(math.atan2(u, v)) + 180.0) % 360.0


def _to_time(seconds: np.ndarray) -> np.ndarray:
    return _EPOCH + (np.asarray(seconds, dtype=np.float64) * 1e9).astype("timedelta64[ns]")


# ---------------------------------------------------------------------------
# polar <-> Cartesian grid mapping
# ---------------------------------------------------------------------------


def _nearest_ray(src_az: np.ndarray, query_az: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Index of the closest ray in *src_az* for each query azimuth (circular) and the gap (deg)."""
    src = np.mod(np.asarray(src_az, dtype=np.float64), 360.0)
    order = np.argsort(src)
    s = src[order]
    q = np.mod(np.asarray(query_az, dtype=np.float64), 360.0)
    hi = np.searchsorted(s, q) % s.size
    lo = (hi - 1) % s.size
    d_lo = np.abs((q - s[lo] + 180.0) % 360.0 - 180.0)
    d_hi = np.abs((q - s[hi] + 180.0) % 360.0 - 180.0)
    pick_hi = d_hi < d_lo
    return order[np.where(pick_hi, hi, lo)], np.where(pick_hi, d_hi, d_lo)


def _ray_spacing(az: np.ndarray) -> float:
    if az.size < 2:
        return 1.0
    d = np.abs(np.diff(np.mod(az, 360.0)))
    d = np.minimum(d, 360.0 - d)
    d = d[d > 1e-6]
    return float(np.median(d)) if d.size else 1.0


def _gate_spacing(rng: np.ndarray) -> float:
    return float(np.median(np.abs(np.diff(rng)))) if rng.size > 1 else 250.0


@dataclass
class _GridMap:
    """Nearest-neighbour map between a regular x/y grid (m, radar centred) and a polar sweep."""

    x: np.ndarray
    y: np.ndarray
    ray: np.ndarray  # (ny, nx) ray index of every grid cell
    gate: np.ndarray  # (ny, nx) gate index
    ok: np.ndarray  # (ny, nx) grid cell lies inside the sweep's coverage

    @classmethod
    def build(
        cls, x: np.ndarray, y: np.ndarray, azimuth: np.ndarray, range_m: np.ndarray
    ) -> _GridMap:
        xx, yy = np.meshgrid(x, y)
        r = np.hypot(xx, yy)
        bearing = np.degrees(np.arctan2(xx, yy))
        ray, gap = _nearest_ray(azimuth, bearing.ravel())
        ray = ray.reshape(r.shape)
        gap = gap.reshape(r.shape)
        order = np.argsort(range_m)
        rs = range_m[order]
        pos = np.clip(np.searchsorted(rs, r), 1, max(rs.size - 1, 1)) if rs.size > 1 else 0
        if rs.size > 1:
            left = np.abs(r - rs[pos - 1]) <= np.abs(rs[pos] - r)
            gate = order[np.where(left, pos - 1, pos)]
        else:
            gate = np.zeros(r.shape, dtype=np.intp)
        dr = _gate_spacing(range_m)
        ok = (r >= rs[0] - dr) & (r <= rs[-1] + dr) & (gap <= 1.5 * _ray_spacing(azimuth))
        return cls(np.asarray(x), np.asarray(y), ray, gate, ok)

    def to_grid(self, polar: np.ndarray, fill: float = np.nan) -> np.ndarray:
        out = np.full(self.ok.shape, fill, dtype=np.float64)
        out[self.ok] = polar[self.ray[self.ok], self.gate[self.ok]]
        return out

    def labels_to_grid(self, polar_labels: np.ndarray) -> np.ndarray:
        out = np.zeros(self.ok.shape, dtype=np.int32)
        out[self.ok] = polar_labels[self.ray[self.ok], self.gate[self.ok]]
        return out


def _polar_to_grid_index(
    azimuth: np.ndarray, range_m: np.ndarray, x0: float, y0: float, dx: float, dy: float, shape
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Grid ``(iy, ix, inside)`` of every polar gate on the grid ``(x0 + i dx, y0 + j dy)``."""
    az = np.radians(azimuth)[:, None]
    gx = range_m[None, :] * np.sin(az)
    gy = range_m[None, :] * np.cos(az)
    ix = np.rint((gx - x0) / dx).astype(np.int64)
    iy = np.rint((gy - y0) / dy).astype(np.int64)
    inside = (ix >= 0) & (ix < shape[1]) & (iy >= 0) & (iy < shape[0])
    return np.clip(iy, 0, shape[0] - 1), np.clip(ix, 0, shape[1] - 1), inside


def _auto_spacing_m(rmax_m: float, requested_km: float | None) -> float:
    if requested_km is not None and requested_km > 0:
        return float(requested_km) * 1000.0
    return max(DEFAULT_GRID_KM * 1000.0, math.ceil(rmax_m / _MAX_GRID_HALF_CELLS / 100.0) * 100.0)


# ---------------------------------------------------------------------------
# identification
# ---------------------------------------------------------------------------


def identify(
    data: xr.DataArray,
    threshold_dbz: float = 35.0,
    min_size_km2: float = 10.0,
    *,
    thresholds: Sequence[float] | None = None,
    backend: str = "auto",
    lat: float | None = None,
    lon: float | None = None,
    elevation_deg: float | None = None,
    time_s: float | None = None,
    grid_spacing_km: float | None = None,
    smooth: float | None = None,
    extra_fields: dict[str, np.ndarray] | None = None,
) -> CellScan:
    """Identify storm cells in one polar reflectivity sweep.

    Parameters
    ----------
    data : xr.DataArray
        2-D ``(azimuth, range)`` reflectivity (dBZ) with ``azimuth`` / ``range`` coordinates.
    threshold_dbz, min_size_km2 :
        Gates at or above the threshold form a cell; cells smaller than *min_size_km2* are dropped.
        Regions that touch across the 0/360 seam of a full-circle sweep are one cell.
    thresholds :
        Optional ascending list of reflectivity thresholds (radarx backend only): cells are
        split where they hold several separate cores at a higher threshold, then extend down to
        the lowest threshold (tobac).  Default: ``[threshold_dbz]``, i.e. plain connected
        components.  The numpy backend uses the lowest value only.
    backend : {"auto", "radarx", "numpy"}
    lat, lon, elevation_deg, time_s :
        Radar position (adds cell lat/lon), sweep elevation (adds an echo-top proxy) and scan
        time (seconds since epoch; used by :func:`track`).
    grid_spacing_km, smooth :
        radarx backend: spacing of the Cartesian labelling grid (default 0.5 km, larger for
        very long ranges) and Gaussian smoothing (grid cells) applied before labelling.
    extra_fields :
        ``{name: polar array}`` (same shape as *data*) summarised inside every cell as
        ``cell.extra["<name>_max"]`` / ``["<name>_mean"]`` (e.g. VIL).
    """
    thr = sorted({float(t) for t in thresholds}) if thresholds else [float(threshold_dbz)]
    kind = resolve_backend(backend)
    note = ""
    if backend == "radarx" and kind != "radarx":
        note = "radarx is not installed; used the built-in numpy backend"
    scan: CellScan | None = None
    if kind == "radarx":
        try:
            scan = _identify_radarx(
                data,
                thr,
                min_size_km2,
                lat,
                lon,
                elevation_deg,
                grid_spacing_km,
                smooth,
                extra_fields,
            )
        except Exception as exc:
            logger.warning("radarx cell identification failed (%s); using numpy backend", exc)
            note = f"radarx failed ({type(exc).__name__}: {exc}); used the built-in numpy backend"
    if scan is None:
        if len(thr) > 1:
            note = (note + "; " if note else "") + "numpy backend ignores extra thresholds"
        cells, labels = identify_cells_labelled(
            data, thr[0], min_size_km2, elevation_deg=elevation_deg, lat=lat, lon=lon
        )
        _, azimuth, range_m = polar_geometry(data)
        scan = CellScan(
            cells, backend="numpy", labels=labels, azimuth=azimuth, range_m=range_m, thresholds=thr
        )
        if extra_fields:
            _extra_stats_polar(scan, extra_fields)
    scan.time_s = time_s
    scan.note = note
    return scan


def _identify_radarx(
    data: xr.DataArray,
    thr: list[float],
    min_size_km2: float,
    lat: float | None,
    lon: float | None,
    elevation_deg: float | None,
    grid_spacing_km: float | None,
    smooth: float | None,
    extra_fields: dict[str, np.ndarray] | None,
) -> CellScan:
    rt = radarx_retrieve()
    assert rt is not None
    values, azimuth, range_m = polar_geometry(data)
    if values.size == 0:
        return CellScan(backend="radarx", thresholds=thr)

    spacing = _auto_spacing_m(float(np.max(np.abs(range_m))), grid_spacing_km)
    n = math.ceil(float(np.max(np.abs(range_m))) / spacing)
    axis = (np.arange(-n, n + 1) * spacing).astype(np.float64)
    gmap = _GridMap.build(axis, axis, azimuth, range_m)
    field = gmap.to_grid(values.astype(np.float64))

    da = xr.DataArray(
        field[None],
        dims=("time", "y", "x"),
        coords={"time": _to_time(np.array([0.0])), "y": axis, "x": axis},
    )
    res = rt.identify_cells(da, thresholds=thr, min_area=float(min_size_km2), smooth=smooth)
    grid_labels = np.asarray(res["CELL_LABEL"].values[0], dtype=np.int32)
    if grid_labels.max(initial=0) == 0:
        return CellScan(
            backend="radarx",
            azimuth=azimuth,
            range_m=range_m,
            labels=np.zeros(values.shape, np.int32),
            thresholds=thr,
        )

    # radarx labels live on the Cartesian grid; hand them back to the native polar gates
    iy, ix, inside = _polar_to_grid_index(
        azimuth, range_m, axis[0], axis[0], spacing, spacing, grid_labels.shape
    )
    polar_labels = np.where(inside, grid_labels[iy, ix], 0).astype(np.int32)
    with np.errstate(invalid="ignore"):
        polar_labels[~(values >= thr[0])] = 0  # gates below the segmentation threshold
    cells, ranked = cells_from_labels(
        values,
        polar_labels,
        azimuth,
        range_m,
        min_size_km2,
        elevation_deg=elevation_deg,
        lat=lat,
        lon=lon,
    )
    scan = CellScan(
        cells, backend="radarx", labels=ranked, azimuth=azimuth, range_m=range_m, thresholds=thr
    )
    if extra_fields and cells:
        orig = _original_labels(ranked, polar_labels, len(cells))
        try:
            _extra_stats_radarx(rt, scan, gmap, axis, grid_labels, orig, extra_fields)
        except Exception as exc:
            logger.debug("radarx cell_statistics failed (%s); computing on the polar grid", exc)
            _extra_stats_polar(scan, extra_fields)
    return scan


def _original_labels(ranked: np.ndarray, original: np.ndarray, n: int) -> np.ndarray:
    """``out[k]`` = label in *original* of the region ranked ``k + 1`` (``n`` entries)."""
    flat = ranked.ravel()
    nz = np.flatnonzero(flat)
    uniq, first = np.unique(flat[nz], return_index=True)
    out = np.zeros(n, dtype=np.int32)
    out[uniq - 1] = original.ravel()[nz[first]]
    return out


def _extra_stats_radarx(
    rt: Any,
    scan: CellScan,
    gmap: _GridMap,
    axis: np.ndarray,
    grid_labels: np.ndarray,
    orig: np.ndarray,
    extra_fields: dict[str, np.ndarray],
) -> None:
    """Per-cell max / mean of extra polar fields via ``radarx.retrieve.cell_statistics``."""
    coords = {"time": _to_time(np.array([0.0])), "y": axis, "x": axis}
    ds = xr.Dataset(
        {
            name: (("time", "y", "x"), gmap.to_grid(np.asarray(arr, dtype=np.float64))[None])
            for name, arr in extra_fields.items()
        },
        coords=coords,
    )
    labels = xr.DataArray(grid_labels[None], dims=("time", "y", "x"), coords=coords)
    stats = rt.cell_statistics(labels, ds, stats=("max", "mean"))
    for name in extra_fields:
        mx = np.asarray(stats[f"{name}_max"].values)[:, 0]
        mean = np.asarray(stats[f"{name}_mean"].values)[:, 0]
        for k, cell in enumerate(scan):
            lab = int(orig[k])
            if 1 <= lab <= mx.size and math.isfinite(float(mx[lab - 1])):
                cell.extra[f"{name}_max"] = float(mx[lab - 1])
                cell.extra[f"{name}_mean"] = float(mean[lab - 1])


def _extra_stats_polar(scan: CellScan, extra_fields: dict[str, np.ndarray]) -> None:
    """Per-cell max / mean of extra polar fields on the native gates."""
    if scan.labels is None or not len(scan):
        return
    lab = scan.labels.ravel()
    sel = lab > 0
    for name, arr in extra_fields.items():
        v = np.asarray(arr, dtype=np.float64).ravel()
        ok = sel & np.isfinite(v)
        n = len(scan) + 1
        cnt = np.bincount(lab[ok], minlength=n)
        tot = np.bincount(lab[ok], weights=v[ok], minlength=n)
        mx = np.full(n, -np.inf)
        np.maximum.at(mx, lab[ok], v[ok])
        for k, cell in enumerate(scan, start=1):
            if cnt[k] > 0:
                cell.extra[f"{name}_max"] = float(mx[k])
                cell.extra[f"{name}_mean"] = float(tot[k] / cnt[k])


# ---------------------------------------------------------------------------
# tracking
# ---------------------------------------------------------------------------


def _scan_times(
    scans: Sequence[Sequence[CellInfo]],
    times_s: Sequence[float] | None,
    dt_seconds: float | Sequence[float],
) -> list[float]:
    n = len(scans)
    if times_s is not None:
        if len(times_s) != n:
            raise ValueError("times_s needs one value per scan")
        return [float(t) for t in times_s]
    embedded = [getattr(s, "time_s", None) for s in scans]
    if n and all(t is not None for t in embedded):
        return [float(t) for t in embedded]  # type: ignore[arg-type]
    dts = np.atleast_1d(np.asarray(dt_seconds, dtype=np.float64))
    if dts.size == 1:
        dts = np.repeat(dts, max(n - 1, 0))
    if dts.size != max(n - 1, 0):
        raise ValueError("dt_seconds needs one value or one per interval")
    return [0.0, *np.cumsum(dts).tolist()][:n]


def track(
    scans: Sequence[Sequence[CellInfo]],
    *,
    times_s: Sequence[float] | None = None,
    dt_seconds: float | Sequence[float] = 300.0,
    max_distance_km: float = 30.0,
    min_overlap: float = 0.3,
    motion: tuple[float, float] | None = None,
    backend: str = "auto",
    grid_spacing_km: float | None = None,
    track_id_style: str = "seq",
) -> TrackResult:
    """Track cells through a time series of scans.

    Parameters
    ----------
    scans :
        Chronological list of :class:`CellScan` (preferred: the real cell masks are used) or
        plain ``list[CellInfo]`` (cell outlines are rasterised instead).
    times_s, dt_seconds :
        Scan times in seconds (default: the ``time_s`` of each ``CellScan``), or a fixed /
        per-interval step used when times are unknown.
    max_distance_km :
        Largest centroid distance for linking cells that do not overlap (radarx) or for
        nearest-neighbour matching (numpy).
    min_overlap :
        radarx: overlap area, as a fraction of either cell, needed to link two cells.
    motion :
        Optional storm motion ``(u east, v north)`` in m/s used to advect cells before the
        overlap is counted.  Default: estimated from a first, unadvected pass (radarx).
    backend : {"auto", "radarx", "numpy"}

    Returns
    -------
    TrackResult
        One :class:`CellTrack` per cell lineage: ``cell_history``, ``speed_mps``,
        ``direction_deg`` (met. *from* direction), ``u_mps``/``v_mps``, ``lifetime``
        (scans), ``split_from`` / ``merged_into`` (radarx only) and ``status``.
    """
    scans = list(scans)
    n = len(scans)
    if n == 0:
        return TrackResult(backend=resolve_backend(backend))
    times = _scan_times(scans, times_s, dt_seconds)
    dts = np.diff(np.asarray(times, dtype=np.float64))
    kind = resolve_backend(backend)
    note = ""
    if backend == "radarx" and kind != "radarx":
        note = "radarx is not installed; used the built-in numpy backend"
    bad_dt = bool(n > 1 and not np.all(dts > 0))
    if kind == "radarx" and bad_dt:
        kind = "numpy"
        note = "non-positive scan interval; used the built-in numpy backend (no motion)"

    keys = split_from = merged_into = used_motion = None
    if kind == "radarx" and n > 1 and any(len(s) for s in scans):
        try:
            keys, split_from, merged_into, used_motion = _link_radarx(
                scans, times, dts, max_distance_km, min_overlap, motion, grid_spacing_km
            )
        except Exception as exc:
            logger.warning("radarx tracking failed (%s); using numpy backend", exc)
            note = f"radarx failed ({type(exc).__name__}: {exc}); used the built-in numpy backend"
    if keys is None:
        kind = "numpy" if (kind != "radarx" or n > 1) else kind
        keys, split_from, merged_into = _link_nearest(scans, max_distance_km)
        used_motion = None
    assert split_from is not None and merged_into is not None
    tracks = _build_tracks(scans, keys, split_from, merged_into, times, track_id_style, bad_dt)
    return TrackResult(tracks, backend=kind, motion=used_motion, times_s=times, note=note)


def track_pair(
    prev: Sequence[CellInfo],
    new: Sequence[CellInfo],
    *,
    max_distance_km: float = 30.0,
    dt_seconds: float = 300.0,
    min_overlap: float = 0.3,
    backend: str = "auto",
    motion: tuple[float, float] | None = None,
    grid_spacing_km: float | None = None,
) -> TrackResult:
    """Two-scan tracking with the track ids of the ``track_cells`` message.

    Matched cells are ``T<prev id digits>``, a previous cell that found no successor keeps
    its id, new cells are ``TN01``, ``TN02``... (position in *new*).
    """
    return track(
        [prev, new],
        times_s=[0.0, float(dt_seconds)] if _valid_dt(dt_seconds) else [0.0, 0.0],
        max_distance_km=max_distance_km,
        min_overlap=min_overlap,
        motion=motion,
        backend=backend,
        grid_spacing_km=grid_spacing_km,
        track_id_style="pair",
    )


def _valid_dt(dt: float) -> bool:
    return bool(dt is not None and math.isfinite(dt) and dt > 0)


def _link_nearest(
    scans: list[Sequence[CellInfo]], max_distance_km: float
) -> tuple[list[list[int]], dict[int, int], dict[int, int]]:
    """Greedy nearest-neighbour linking of consecutive scans (numpy backend)."""
    next_key = 0
    keys: list[list[int]] = []
    first = list(range(len(scans[0])))
    next_key = len(first)
    keys.append(first)
    for k in range(1, len(scans)):
        match = match_nearest(list(scans[k - 1]), list(scans[k]), max_distance_km)
        inverse = {j: i for i, j in match.items()}
        row = []
        for j in range(len(scans[k])):
            if j in inverse:
                row.append(keys[k - 1][inverse[j]])
            else:
                row.append(next_key)
                next_key += 1
        keys.append(row)
    return keys, {}, {}


def _common_grid(
    scans: list[Sequence[CellInfo]], requested_km: float | None
) -> tuple[np.ndarray, np.ndarray, float]:
    """Radar-centred x/y axes covering every cell of every scan (m), and their spacing."""
    xs: list[float] = []
    ys: list[float] = []
    for s in scans:
        for c in s:
            cx, cy = _xy_m(c)
            xs.append(cx)
            ys.append(cy)
            for az, rng in c.boundary_points:
                a = math.radians(az)
                xs.append(rng * math.sin(a))
                ys.append(rng * math.cos(a))
    pad = 10_000.0
    x0, x1 = min(xs) - pad, max(xs) + pad
    y0, y1 = min(ys) - pad, max(ys) + pad
    sp = _auto_spacing_m(max(abs(x0), abs(x1), abs(y0), abs(y1)), requested_km)
    while ((x1 - x0) / sp + 1) * ((y1 - y0) / sp + 1) > 1.6e6:
        sp *= 1.5
    x = np.arange(math.floor(x0 / sp), math.ceil(x1 / sp) + 1) * sp
    y = np.arange(math.floor(y0 / sp), math.ceil(y1 / sp) + 1) * sp
    return x.astype(np.float64), y.astype(np.float64), sp


def _rasterise_scan(scan: Sequence[CellInfo], x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Label image (ny, nx) of a scan's cells on the grid: value ``k`` <-> ``scan[k - 1]``."""
    ny, nx = y.size, x.size
    dx, dy = float(x[1] - x[0]), float(y[1] - y[0])
    out = np.zeros((ny, nx), dtype=np.int32)
    labels = getattr(scan, "labels", None)
    az = getattr(scan, "azimuth", None)
    rng = getattr(scan, "range_m", None)
    if labels is not None and az is not None and rng is not None and labels.max(initial=0) > 0:
        out = _GridMap.build(x, y, az, rng).labels_to_grid(labels)
    else:
        from matplotlib.path import Path

        xx, yy = np.meshgrid(x, y)
        for k, c in enumerate(scan, start=1):
            if len(c.boundary_points) >= 3:
                pts = np.array(
                    [
                        [r * math.sin(math.radians(a)), r * math.cos(math.radians(a))]
                        for a, r in c.boundary_points
                    ]
                )
                bx0, bx1 = pts[:, 0].min() - dx, pts[:, 0].max() + dx
                by0, by1 = pts[:, 1].min() - dy, pts[:, 1].max() + dy
                sel = (xx >= bx0) & (xx <= bx1) & (yy >= by0) & (yy <= by1)
                inside = Path(pts).contains_points(np.column_stack([xx[sel], yy[sel]]))
                tgt = np.zeros(out.shape, dtype=bool)
                tgt[sel] = inside
                out[tgt & (out == 0)] = k
    # every cell owns at least its centroid pixel (tiny cells / failed outlines)
    present = set(np.unique(out).tolist())
    for k, c in enumerate(scan, start=1):
        if k not in present:
            cx, cy = _xy_m(c)
            iy = int(np.clip(round((cy - y[0]) / dy), 0, ny - 1))
            ix = int(np.clip(round((cx - x[0]) / dx), 0, nx - 1))
            if out[iy, ix] == 0:
                out[iy, ix] = k
    return out


def _link_radarx(
    scans: list[Sequence[CellInfo]],
    times: list[float],
    dts: np.ndarray,
    max_distance_km: float,
    min_overlap: float,
    motion: tuple[float, float] | None,
    grid_spacing_km: float | None,
) -> tuple[list[list[int]], dict[int, int], dict[int, int], tuple[float, float] | None]:
    rt = radarx_retrieve()
    assert rt is not None
    x, y, _ = _common_grid(scans, grid_spacing_km)
    stack = np.stack([_rasterise_scan(s, x, y) for s in scans])
    cells = xr.DataArray(
        stack,
        dims=("time", "y", "x"),
        coords={"time": _to_time(np.asarray(times)), "y": y, "x": x},
        name="CELL_LABEL",
    )
    max_speed = max_distance_km * 1000.0 / float(np.max(dts))

    def run(mot: tuple[float, float] | None) -> xr.Dataset:
        return rt.track_cells(
            cells, motion=mot, min_overlap=min_overlap, max_speed=max_speed, dt=dts
        )

    used = motion
    out = run(motion)
    if motion is None:
        # Storm motion = median step velocity of cells that kept their size and neither split
        # nor merged (a centroid jump between a parent and its child is not motion).
        u = np.asarray(out["cell_u"].values, dtype=np.float64)
        v = np.asarray(out["cell_v"].values, dtype=np.float64)
        area = np.asarray(out["cell_area"].values, dtype=np.float64)
        plain = (np.asarray(out["cell_split_from"].values) == 0) & (
            np.asarray(out["cell_merged_into"].values) == 0
        )
        with np.errstate(invalid="ignore", divide="ignore"):
            ratio = area[:, 1:] / area[:, :-1]
        steady = np.zeros(u.shape, dtype=bool)
        steady[:, 1:] = (ratio > 0.5) & (ratio < 2.0)
        ok = np.isfinite(u) & np.isfinite(v) & steady & plain[:, None]
        if ok.any() and float(np.hypot(np.median(u[ok]), np.median(v[ok]))) >= 1.0:
            used = (float(np.median(u[ok])), float(np.median(v[ok])))
            out = run(used)  # second pass: advect with the estimated storm motion
    cell_id = np.asarray(out["CELL_ID"].values)
    keys: list[list[int]] = []
    fresh = 10**6
    for t, scan in enumerate(scans):
        flat_lab = stack[t].ravel()
        flat_id = cell_id[t].ravel()
        nz = np.flatnonzero(flat_lab)
        uniq, first = np.unique(flat_lab[nz], return_index=True)
        rep = dict(zip(uniq.tolist(), nz[first].tolist(), strict=True))
        row = []
        for k in range(1, len(scan) + 1):
            tid = int(flat_id[rep[k]]) if k in rep else 0
            if tid <= 0:
                tid = fresh
                fresh += 1
            row.append(tid)
        keys.append(row)
    cell_ax = np.asarray(out["cell"].values)
    split = np.asarray(out["cell_split_from"].values)
    merge = np.asarray(out["cell_merged_into"].values)
    split_from = {int(c): int(s) for c, s in zip(cell_ax, split, strict=True) if s > 0}
    merged_into = {int(c): int(m) for c, m in zip(cell_ax, merge, strict=True) if m > 0}
    return keys, split_from, merged_into, used


def _build_tracks(
    scans: list[Sequence[CellInfo]],
    keys: list[list[int]],
    split_from: dict[int, int],
    merged_into: dict[int, int],
    times: list[float],
    id_style: str,
    bad_dt: bool,
) -> list[CellTrack]:
    n = len(scans)
    order: list[int] = []
    members: dict[int, list[tuple[int, int]]] = {}
    for t, row in enumerate(keys):
        for j, key in enumerate(row):
            if key not in members:
                members[key] = []
                order.append(key)
            members[key].append((t, j))

    def name(key: int, idx: int) -> str:
        t0, j0 = members[key][0]
        if id_style == "pair":
            return f"T{scans[0][j0].id[1:]}" if t0 == 0 else f"TN{j0 + 1:02d}"
        return f"T{idx + 1:03d}"

    ids = {key: name(key, i) for i, key in enumerate(order)}
    tracks: list[CellTrack] = []
    for key in order:
        mem = members[key]
        hist = [scans[t][j] for t, j in mem]
        u = v = 0.0
        if len(mem) >= 2 and not bad_dt:
            (xa, ya), (xb, yb) = _xy_m(hist[0]), _xy_m(hist[-1])
            span = times[mem[-1][0]] - times[mem[0][0]]
            if span > 0:
                u, v = (xb - xa) / span, (yb - ya) / span
        speed, from_deg = uv_to_speed_dir(u, v)
        last_t = mem[-1][0]
        status = "ended" if last_t < n - 1 else ("continued" if len(mem) > 1 else "new")
        tr = CellTrack(
            track_id=ids[key],
            cell_history=hist,
            speed_mps=speed,
            direction_deg=from_deg,
            u_mps=u,
            v_mps=v,
            lifetime=len(mem),
            split_from=ids.get(split_from[key]) if key in split_from else None,
            merged_into=ids.get(merged_into[key]) if key in merged_into else None,
            status=status,
        )
        for i, cell in enumerate(hist, start=1):
            cell.track_id = tr.track_id
            cell.lifetime = i
            if len(mem) >= 2 and not bad_dt:
                cell.speed_mps, cell.direction_deg = speed, from_deg
        tracks.append(tr)
    return tracks


# ---------------------------------------------------------------------------
# statistics table / overlay helpers (shared by the plug-in modules)
# ---------------------------------------------------------------------------

# (row key, column label, unit, digits) for the optional columns, in display order
OPTIONAL_COLUMNS = [
    ("lat", "Lat", "°", 3),
    ("lon", "Lon", "°", 3),
    ("echo_top_proxy_km", "Echo top (sweep)", "km", 1),
    ("echo_top_km", "Echo top", "km", 1),
    ("vil_max_kgm2", "VIL max", "kg/m²", 1),
    ("vil_mean_kgm2", "VIL mean", "kg/m²", 1),
    ("speed_ms", "Speed", "m/s", 1),
    ("direction_deg", "From", "°", 0),
    ("lifetime", "Scans", "", 0),
    ("track_id", "Track", "", None),
]

_EXTRA_ROWS = {
    "vil_max": "vil_max_kgm2",
    "vil_mean": "vil_mean_kgm2",
    "echo_top_max": "echo_top_km",
}


def cell_rows(cells: Sequence[CellInfo]) -> list[dict[str, Any]]:
    """Statistics-table rows: the original columns plus whatever each cell knows."""
    rows = []
    for c in cells:
        row: dict[str, Any] = {
            "id": c.id,
            "centroid_az": round(c.centroid_az % 360.0, 1),
            "centroid_range_km": round(c.centroid_range / 1000.0, 1),
            "area_km2": round(c.area_km2, 1),
            "max_dbz": round(c.max_dbz, 1),
            "mean_dbz": round(c.mean_dbz, 1),
        }
        if c.lat is not None and c.lon is not None:
            row["lat"], row["lon"] = round(c.lat, 4), round(c.lon, 4)
        if c.echo_top_proxy_km is not None:
            row["echo_top_proxy_km"] = round(c.echo_top_proxy_km, 2)
        for src, dst in _EXTRA_ROWS.items():
            if src in c.extra:
                row[dst] = round(c.extra[src], 2)
        if c.speed_mps is not None:
            row["speed_ms"] = round(c.speed_mps, 1)
            row["direction_deg"] = round(c.direction_deg or 0.0)
        if c.lifetime is not None:
            row["lifetime"] = int(c.lifetime)
        if c.track_id is not None:
            row["track_id"] = c.track_id
        rows.append(row)
    return rows


def table_columns(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Column specs for :meth:`engine.plugins.Result.table` matching :func:`cell_rows`."""
    cols: list[dict[str, Any]] = [
        {"key": "id", "label": "Cell"},
        {"key": "centroid_az", "label": "Azimuth", "unit": "°", "digits": 1},
        {"key": "centroid_range_km", "label": "Range", "unit": "km", "digits": 1},
        {"key": "area_km2", "label": "Area", "unit": "km²", "digits": 1},
        {"key": "max_dbz", "label": "Max", "unit": "dBZ", "digits": 1},
        {"key": "mean_dbz", "label": "Mean", "unit": "dBZ", "digits": 1},
    ]
    for key, label, unit, digits in OPTIONAL_COLUMNS:
        if any(key in r for r in rows):
            col: dict[str, Any] = {"key": key, "label": label}
            if unit:
                col["unit"] = unit
            if digits is not None:
                col["digits"] = digits
            cols.append(col)
    return cols


def set_motion(cells: Sequence[CellInfo], speed_mps: float, from_deg: float) -> None:
    """Give every cell the same motion (manual storm motion, or a tracked mean)."""
    for c in cells:
        c.speed_mps, c.direction_deg = float(speed_mps), float(from_deg)


def arrow_coords(cell: CellInfo, minutes: float = 15.0) -> list[list[float]] | None:
    """Polar polyline ``[[az, range_m], ...]`` of a motion arrow with a head, or ``None``.

    The arrow starts at the cell centroid and ends where the cell would be after *minutes*
    at its speed/direction; the last four points draw the two barbs of the head.
    """
    if not cell.speed_mps or cell.speed_mps <= 0 or cell.direction_deg is None:
        return None
    lead = minutes * 60.0
    tail = (cell.centroid_az % 360.0, cell.centroid_range)
    head = extrapolate_cell(cell, cell.speed_mps, cell.direction_deg, lead)
    ax, ay = _xy_m(cell)
    hx, hy = head[1] * math.sin(math.radians(head[0])), head[1] * math.cos(math.radians(head[0]))
    length = math.hypot(hx - ax, hy - ay)
    if length < 1.0:
        return None
    ux, uy = (hx - ax) / length, (hy - ay) / length
    barb = 0.25 * length
    pts_xy = [(hx, hy)]
    for sign in (1.0, -1.0):
        ang = math.radians(25.0) * sign
        bx = -(ux * math.cos(ang) - uy * math.sin(ang))
        by = -(ux * math.sin(ang) + uy * math.cos(ang))
        pts_xy.append((hx + bx * barb, hy + by * barb))

    def polar(px: float, py: float) -> list[float]:
        return [round(math.degrees(math.atan2(px, py)) % 360.0, 3), round(math.hypot(px, py), 1)]

    h, b1, b2 = pts_xy
    return [
        [round(tail[0], 3), round(tail[1], 1)],
        polar(*h),
        polar(*b1),
        polar(*h),
        polar(*b2),
    ]

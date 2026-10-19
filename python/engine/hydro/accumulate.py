"""Rainfall accumulation from a series of radar volumes, with advection-corrected time stepping.

``accumulate`` turns any number of volumes of one or several radars into

* the rainfall accumulation over a period, in mm, on a common grid,
* its uncertainty, a time-valid fraction, the maximum rate and the quality fields,
* optionally the basin-average rainfall series (see :mod:`engine.hydro.basins`).

How the time integral is done (and why)
---------------------------------------
A volume gives the instantaneous rain rate at its scan time. Rain is the integral of the rate over
time, so between two scans the rate must be *interpolated*, not held, and not just averaged: a
storm that moves 20 km between two scans would otherwise be smeared (a hole in the middle, two
ghosts at the ends). Per radar, between consecutive scans ``A`` (time ``t_A``) and ``B``
(``t_B``) the rate at a time ``t`` is the advection-corrected blend of
``radarx.retrieve.interpolate_time``
(Gal-Chen 1982 frame of reference; semi-Lagrangian departure points; storm motion from FFT
cross-correlation of the two fields, Rinehart & Garvey 1978): A is advected forward by ``t - t_A``,
B backward by ``t_B - t`` and the two are blended with the weights ``1 - f`` and ``f``,
``f = (t - t_A)/(t_B - t_A)``. The composite of the radars is formed at every time node, and
``A = sum_k 0.5 (R_{k-1} + R_k) (t_k - t_{k-1}) / 3600`` (mm, trapezoid rule, ``R`` in mm/h, ``t``
in s) over the nodes ``t_k``, which always contain every scan time so the kinks of the
piecewise interpolant are not cut. A constant rate ``R`` over ``T`` hours gives exactly ``R T``.

A gap between two scans longer than ``max_gap_s`` is **not** bridged: the cell is missing for that
time (``valid_fraction`` < 1) rather than silently filled. The accumulation is the sum over the time
that *was* observed; compare it with ``valid_fraction`` before using it.
"""

from __future__ import annotations

import contextlib
import itertools
import math
import time as _time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import xarray as xr

from engine import provenance as prov
from engine.hydro import grid as hgrid
from engine.hydro import qpe as hqpe

ProgressFn = Callable[[int, str], None]

#: lowest beams that serve the best-beam composite of one radar
DEFAULT_N_SWEEPS = 4  # with a DEM (beam blockage); 2 without
DEFAULT_MAX_ELEV_DEG = 6.0
SECONDS = np.timedelta64(1, "s")


# ---------------------------------------------------------------------------
# Reading volumes
# ---------------------------------------------------------------------------


@dataclass
class ScanMeta:
    reader: Any
    name: str
    latitude: float
    longitude: float
    altitude_m: float
    time: np.datetime64
    path: str | None = None

    @property
    def key(self) -> tuple[float, float]:
        return (round(self.latitude, 3), round(self.longitude, 3))


class TreeReader:
    """A reader-like view of an in-memory ``xarray.DataTree`` (for modules and notebooks)."""

    def __init__(self, tree: xr.DataTree, path: str | None = None) -> None:
        from engine.reader import sorted_sweep_names

        self.datatree = tree
        self._path = path
        self._names = sorted_sweep_names(tree)

    def get_sweep_dataset(self, sweep: int, *, mask: bool = True, only: Any = None) -> xr.Dataset:
        from engine.reader import mask_below_threshold

        ds = self.datatree[self._names[sweep]].to_dataset()
        return mask_below_threshold(ds, only) if mask else ds


def _time_of(ds: Any) -> np.datetime64:
    if "time" in ds.variables:
        t = np.ravel(ds["time"].values)
        if np.issubdtype(t.dtype, np.datetime64):
            t = t.astype("datetime64[ns]")
            t = t[~np.isnat(t)]
            if t.size:
                t0 = t.min()
                return t0 + (t - t0).astype(np.int64).mean().astype("timedelta64[ns]")
    return np.datetime64("NaT", "ns")


def scan_meta(reader: Any, index: int = 0) -> ScanMeta:
    """Site, name and time (mean ray time of the lowest sweep) of an opened volume."""
    from engine import mosaic

    tree = getattr(reader, "datatree", None)
    if tree is None:
        raise hqpe.QpeError("a volume is not open any more")
    ds0 = reader.get_sweep_dataset(0, mask=False)
    try:
        lat, lon, alt = mosaic._site(tree, ds0)
    except mosaic.MosaicError as exc:
        raise hqpe.QpeError(str(exc)) from exc
    t = _time_of(ds0)
    if np.isnat(t):
        attrs = tree.root.to_dataset()
        for key in ("time_coverage_start",):
            if key in attrs:
                with contextlib.suppress(ValueError):
                    t = np.datetime64(str(attrs[key].values).rstrip("Z"), "ns")
    return ScanMeta(
        reader=reader,
        name=mosaic._instrument_name(tree, reader, index),
        latitude=lat,
        longitude=lon,
        altitude_m=alt,
        time=t,
        path=getattr(reader, "_path", None),
    )


def _roles() -> tuple[str, ...]:
    return ("DBZH", "ZDR", "PHIDP", "RHOHV", "KDP")


def select_sweeps(
    reader: Any, n_sweeps: int = DEFAULT_N_SWEEPS, max_elev_deg: float = DEFAULT_MAX_ELEV_DEG
) -> list[int]:
    """Indices of the lowest sweeps with reflectivity, one per elevation, polarimetric first."""
    from engine import varmap
    from engine.reader import sorted_sweep_names

    names = sorted_sweep_names(reader.datatree)
    cand: list[tuple[float, int, int]] = []  # (elevation, richness, index)
    for i in range(len(names)):
        ds = reader.get_sweep_dataset(i, mask=False)
        if "azimuth" not in ds.coords or "range" not in ds.coords:
            continue
        mode = str(np.asarray(ds["sweep_mode"].values).ravel()[0]) if "sweep_mode" in ds else ""
        if "rhi" in mode.lower() or "vertical" in mode.lower():
            continue
        if varmap.resolve(ds, "DBZH") is None:
            continue
        el = varmap.sweep_fixed_angle(ds)
        if el is None or el > max_elev_deg:
            continue
        rich = sum(1 for r in ("ZDR", "PHIDP", "RHOHV") if varmap.resolve(ds, r) is not None)
        cand.append((float(el), rich, i))
    cand.sort(key=lambda c: (c[0], -c[1], c[2]))
    chosen: list[tuple[float, int, int]] = []
    for el, rich, i in cand:
        same = [c for c in chosen if abs(c[0] - el) < 0.3]
        if same:
            if rich > same[0][1] and same[0][1] == 0:
                chosen[chosen.index(same[0])] = (el, rich, i)
            continue
        chosen.append((el, rich, i))
        if len(chosen) >= n_sweeps:
            break
    return [c[2] for c in chosen]


def sweep_dataset(reader: Any, index: int) -> xr.Dataset:
    """The sweep with canonical variable names and only the moments QPE needs."""
    from engine import varmap

    raw = reader.get_sweep_dataset(index, mask=False)
    needed = []
    for role in _roles():
        m = varmap.resolve(raw, role)
        if m is not None:
            needed.append(m.variable)
    ds = reader.get_sweep_dataset(index, mask=True, only=needed)
    keep = [v for v in needed if v in ds.data_vars]
    extra = [
        v
        for v in ("sweep_fixed_angle", "sweep_mode", "radar_beam_width_h")
        if v in ds.variables and v not in keep
    ]
    ds = ds[keep + extra]
    return varmap.standardize_dataset(ds)


def radar_field(
    scan: ScanMeta,
    cfg: hqpe.QpeConfig,
    grid: hgrid.QpeGrid,
    geom: hgrid.RadarGeometry,
    *,
    n_sweeps: int = DEFAULT_N_SWEEPS,
    max_elev_deg: float = DEFAULT_MAX_ELEV_DEG,
    max_range_km: float = 230.0,
    block_max: float | None = None,
) -> tuple[hgrid.RadarField, list[hqpe.SweepQpe]]:
    """Best-beam rain rate of one volume on the grid."""
    idxs = select_sweeps(scan.reader, n_sweeps, max_elev_deg)
    if not idxs:
        raise hqpe.QpeError(
            f"{scan.name}: no sweep with reflectivity below {max_elev_deg:g} deg elevation"
        )
    site = {"latitude": scan.latitude, "longitude": scan.longitude, "altitude": scan.altitude_m}
    sweeps: list[hqpe.SweepQpe] = []
    for i in idxs:
        ds = sweep_dataset(scan.reader, i)
        sq = hqpe.estimate_sweep(ds, cfg, site=site)
        sweeps.append(sq)
    t = scan.time
    first = sweeps[0].ray_time
    if first is not None and np.isnat(t):
        t = first.astype("datetime64[ns]").min()
    rf = hgrid.merge_sweeps(
        scan.name,
        site,
        t,
        sweeps,
        geom,
        grid.shape,
        grid.cell_m,
        max_range_km * 1000.0,
        block_max=cfg.block_max if block_max is None else block_max,
    )
    rf.meta = {
        "methods": sorted({s.meta["method"] for s in sweeps}),
        "relation": sweeps[0].meta["relation"],
        "notes": sorted({n for s in sweeps for n in s.meta["notes"]}),
        "gate_counts": sweeps[0].meta["gate_counts"],
        "sweep_indices": idxs,
        "path": scan.path,
    }
    return rf, sweeps


# ---------------------------------------------------------------------------
# Time interpolation between two grids of one radar
# ---------------------------------------------------------------------------


def _pseudo_dbz(rate: np.ndarray) -> np.ndarray:
    """Reflectivity-like field from a rate (Z = 300 R^1.4) for the motion estimate only."""
    with np.errstate(divide="ignore", invalid="ignore"):
        z = 10.0 * np.log10(300.0 * np.power(np.maximum(rate, 0.0), 1.4))
    return np.where(np.isfinite(rate), np.where(rate > 0.01, z, -10.0), np.nan).astype(np.float32)


class PairInterpolator:
    """Advection-corrected interpolation of the rate between two consecutive grids."""

    def __init__(
        self,
        a: hgrid.RadarField,
        b: hgrid.RadarField,
        grid: hgrid.QpeGrid,
        advect: bool = True,
        tile_m: float | None = None,
    ) -> None:
        self.a, self.b, self.grid = a, b, grid
        self.t0, self.t1 = a.time, b.time
        self.dt = float((self.t1 - self.t0) / SECONDS)
        self.motion: xr.Dataset | None = None
        self.u = self.v = float("nan")
        self.method = "linear"
        self._xa = self._xb = None
        if advect and self.dt > 0:
            self._advect_setup(tile_m)

    def _ds(self, f: hgrid.RadarField, t: np.datetime64) -> xr.Dataset:
        return xr.Dataset(
            {"RAIN_RATE": (("y", "x"), f.rate)},
            coords={"x": self.grid.x, "y": self.grid.y, "time": t},
        )

    def _advect_setup(self, tile_m: float | None) -> None:
        try:
            import radarx.retrieve as rt
        except ImportError:
            return
        import warnings

        pa = self._ds(self.a, self.t0)
        pb = self._ds(self.b, self.t1)
        pa["DBZH"] = (("y", "x"), _pseudo_dbz(self.a.rate))
        pb["DBZH"] = (("y", "x"), _pseudo_dbz(self.b.rate))
        kw: dict[str, Any] = {}
        if tile_m:
            kw["tile"] = float(tile_m)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                mot = rt.estimate_motion(pa, pb, "DBZH", **kw)
        except Exception:
            return
        u, v = float(np.nanmean(mot["u"].values)), float(np.nanmean(mot["v"].values))
        if not (math.isfinite(u) and math.isfinite(v)):
            return
        self.motion = mot
        self.u, self.v = u, v
        self.method = "advection"
        self._xa, self._xb = pa[["RAIN_RATE"]], pb[["RAIN_RATE"]]

    def at(self, t: np.datetime64) -> np.ndarray:
        """Rate (mm/h) at ``t`` with ``t0 <= t <= t1``."""
        f = float((t - self.t0) / SECONDS) / self.dt if self.dt > 0 else 0.0
        if f <= 0.0:
            return self.a.rate
        if f >= 1.0:
            return self.b.rate
        if self.motion is not None:
            import warnings

            import radarx.retrieve as rt

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                out = rt.interpolate_time(
                    self._xa, self._xb, np.array([t], dtype="datetime64[ns]"), motion=self.motion
                )
            return np.asarray(out["RAIN_RATE"].isel(time=0).values, dtype=np.float32)
        ra, rb = self.a.rate, self.b.rate
        fa, fb = np.isfinite(ra), np.isfinite(rb)
        out = np.where(fa & fb, (1 - f) * ra + f * rb, np.where(fa, ra, rb))
        return out.astype(np.float32)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class QpeResult:
    """An accumulation (or instantaneous rate) with everything needed to judge and save it."""

    grid: hgrid.QpeGrid
    start: np.datetime64
    end: np.datetime64
    accumulation: np.ndarray  # mm
    accumulation_sigma: np.ndarray  # mm, 1-sigma, errors fully correlated in time
    rate_max: np.ndarray  # mm/h
    rate_last: np.ndarray  # mm/h at the last node
    valid_fraction: np.ndarray  # fraction of the period with data
    quality: np.ndarray  # time-mean quality index
    beam_height_agl_m: np.ndarray  # time-mean
    range_km: np.ndarray  # nearest serving radar, time mean
    flags: np.ndarray  # OR of the cell flags over time
    n_radars: np.ndarray  # max number of radars with an estimate
    nodes: list[np.datetime64]
    radars: list[dict[str, Any]]
    config: dict[str, Any]
    options: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    timing: dict[str, float] = field(default_factory=dict)
    basin_series: Any = None  # engine.hydro.basins.BasinSeries or None
    adjusted: dict[str, Any] | None = None  # filled by engine.hydro.gauges
    adjustment_factor: np.ndarray | None = None
    accumulation_adjusted: np.ndarray | None = None
    source_files: list[str] = field(default_factory=list)
    exceedance: list[dict[str, Any]] | None = None  # basins.exceedance(...) when a table was given
    validation: dict[str, Any] | None = None  # gauges.adjust(...) report when gauges were given

    # -- basic numbers ---------------------------------------------------------------------
    @property
    def duration_s(self) -> float:
        return float((self.end - self.start) / SECONDS)

    def stats(self) -> dict[str, Any]:
        a = self.accumulation
        fin = np.isfinite(a)
        instant = self.duration_s <= 0
        base = self.rate_last if instant else a  # what "wet" and the uncertainty refer to
        wet = np.isfinite(base) & (base >= 0.1)
        big = np.isfinite(base) & (base >= 1.0) & np.isfinite(self.accumulation_sigma)
        area = self.grid.cell_areas_km2()
        out = {
            "cells": int(a.size),
            "cells_with_data": int(fin.sum()),
            "area_km2": float(area[fin].sum()),
            "max_mm": float(np.nanmax(a)) if fin.any() else None,
            "mean_mm": float(np.average(a[fin], weights=area[fin])) if fin.any() else None,
            "wet_area_km2": float(area[wet].sum()),
            "max_rate_mm_h": float(np.nanmax(self.rate_max)) if fin.any() else None,
            "mean_valid_fraction": float(np.nanmean(self.valid_fraction[fin]))
            if fin.any()
            else None,
            # median relative 1-sigma where there is at least 1 mm (1 mm/h for an instant)
            "uncertainty_median_rel": float(np.median(self.accumulation_sigma[big] / base[big]))
            if big.any()
            else None,
        }
        return out

    # -- provenance ------------------------------------------------------------------------
    def provenance(self, adjusted: bool = False) -> dict[str, Any]:
        """The provenance record attached to every output (see ``docs/HYDROLOGY.md``).

        ``type`` is ``RETRIEVED`` for an estimate from radar and ``ADJUSTED`` when a gauge
        adjustment was applied; it is never ``OBSERVED``.
        """
        base = {
            "type": "RETRIEVED",
            "quantity": "accumulated precipitation (liquid-water equivalent)",
            "units": "mm",
            "method": self.config.get("method_text") or self.config.get("method"),
            "parameters": {k: v for k, v in self.config.items() if k != "method_text"},
            "options": self.options,
            "period": [prov.iso_utc(self.start), prov.iso_utc(self.end)],
            "uncertainty": {
                "kind": "1-sigma, mm",
                "model": (
                    "DSD variability of the relation (radarx relation_scatter) + calibration and "
                    "KDP measurement noise, errors taken as fully correlated in time. Does not "
                    "include vertical profile of reflectivity, bright band, overshoot or "
                    "advection error; see the quality fields."
                ),
            },
            "inputs": prov.source_names(self.source_files),
            "software": prov.software_string(),
            "created": prov.utc_stamp(),
        }
        if adjusted and self.adjusted:
            return {
                **base,
                "type": "ADJUSTED",
                "adjusted_from": "RETRIEVED",
                "adjustment": self.adjusted,
            }
        return base

    def to_dict(self) -> dict[str, Any]:
        d = {
            "start": prov.iso_utc(self.start),
            "end": prov.iso_utc(self.end),
            "duration_s": self.duration_s,
            "stats": self.stats(),
            "grid": {
                "ny": self.grid.shape[0],
                "nx": self.grid.shape[1],
                "resolution_m": self.grid.cell_m,
                "bbox": list(self.grid.bbox),
                "origin": list(self.grid.origin),
            },
            "radars": self.radars,
            "nodes": len(self.nodes),
            "notes": self.notes,
            "timing": self.timing,
            "provenance": self.provenance(adjusted=self.accumulation_adjusted is not None),
        }
        if self.adjusted:
            d["adjustment"] = self.adjusted
        if self.basin_series is not None:
            d["basins"] = self.basin_series.summary()
        if self.exceedance is not None:
            d["exceedance"] = self.exceedance
        if self.validation is not None:
            d["gauge_validation"] = {k: v for k, v in self.validation.items() if k != "adjustment"}
        return d

    # -- convenience ---------------------------------------------------------------------------
    def save(self, out: Any, *, field: str | None = None) -> list[Any]:
        """Write each path of ``out`` by extension (see :func:`engine.hydro.products.save`)."""
        from engine.hydro import products

        return products.save(self, out, field=field)

    def plot(self, field: str | None = None, **kwargs: Any) -> Any:
        """A matplotlib figure of a field (default: the adjusted accumulation if there is one)."""
        from engine.hydro import products

        return products.figure(self, field or products.default_field(self), **kwargs)

    def to_dataset(self) -> Any:
        """The CF NetCDF content as an ``xarray.Dataset``."""
        from engine.hydro import products

        return products.to_dataset(self)


# ---------------------------------------------------------------------------
# The accumulation loop
# ---------------------------------------------------------------------------


def _as_time(value: Any) -> np.datetime64 | None:
    if value is None:
        return None
    if isinstance(value, np.datetime64):
        return value.astype("datetime64[ns]")
    text = str(value).strip().rstrip("Z")
    try:
        return np.datetime64(text, "ns")
    except ValueError as exc:
        raise hqpe.QpeError(
            f"cannot read the time {value!r}; use ISO 8601 (2021-12-11T02:00:00)"
        ) from exc


def accumulate(
    readers: Sequence[Any],
    config: hqpe.QpeConfig | dict | None = None,
    *,
    resolution_m: float = 1000.0,
    extent: tuple[float, float, float, float] | None = None,
    max_range_km: float = 230.0,
    start: Any = None,
    end: Any = None,
    composite: str = "weighted",
    advect: bool = True,
    tile_km: float | None = None,
    step_s: float = 60.0,
    max_gap_s: float = 1200.0,
    hold_edges_s: float = 0.0,
    n_sweeps: int | None = None,
    max_elev_deg: float = DEFAULT_MAX_ELEV_DEG,
    basins: Any = None,
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> QpeResult:
    """Accumulate rainfall from opened volumes (readers) of one or several radars.

    Parameters
    ----------
    readers : sequence
        Opened volumes (``engine.reader.RadarReader`` or anything with ``datatree`` and
        ``get_sweep_dataset``). With one volume per radar and no ``start``/``end`` the result is
        an instantaneous composite: the accumulation is 0 mm over 0 s, read ``result.rate_last``.
    config : QpeConfig or dict
        Estimator options (relation, band, freezing level ...), see
        :class:`engine.hydro.qpe.QpeConfig`.
    resolution_m, extent, max_range_km
        The grid (cell size, west/south/east/north box in degrees, default: all radars).
    start, end
        Period (ISO 8601 UTC). Default: first to last scan. Time outside the scans is not
        extrapolated: it counts as missing (``valid_fraction``).
    composite : {"weighted", "best"}
        How radars are combined, see :func:`engine.hydro.grid.composite`.
    advect : bool
        Advection-corrected interpolation between scans (else linear in time).
    step_s : float
        Spacing of the integration nodes; every scan time is a node as well.
    max_gap_s : float
        Longest gap between two scans of a radar that is bridged by interpolation.
    hold_edges_s : float
        Hold the first (last) scan of a radar for up to this many seconds before (after) it, so a
        period that starts or ends on the hour is covered although the scans start 20 s later.
        A stated assumption (the rate stays constant for those seconds); 0 = off, the time is
        missing.
    basins : engine.hydro.basins.BasinSet, optional
        Collect the basin-average series while accumulating.
    """
    cfg = config if isinstance(config, hqpe.QpeConfig) else hqpe.QpeConfig.from_dict(config)  # type: ignore[arg-type]
    cfg.validate()
    if n_sweeps is None:  # a second beam only matters where the lowest one is blocked
        n_sweeps = DEFAULT_N_SWEEPS if cfg.dem is not None else 2
    t_all = _time.perf_counter()
    rep = progress or (lambda p, m: None)
    if step_s < 1:
        raise hqpe.QpeError("step_s must be at least 1 second")
    if not 250.0 <= resolution_m <= 5000.0:
        raise hqpe.QpeError("resolution_m must be between 250 and 5000 metres")
    if not 10.0 <= max_range_km <= 600.0:
        raise hqpe.QpeError("max_range_km must be between 10 and 600 km")
    if max_gap_s <= 0 or hold_edges_s < 0:
        raise hqpe.QpeError("max_gap_s must be positive and hold_edges_s must not be negative")
    if composite not in ("weighted", "best"):
        raise hqpe.QpeError("composite must be 'weighted' or 'best'")
    if not readers:
        raise hqpe.QpeError("no radar volumes given")

    rep(2, "Reading volume metadata")
    scans = [scan_meta(r, i) for i, r in enumerate(readers)]
    bad = [s.name for s in scans if np.isnat(s.time)]
    if bad:
        raise hqpe.QpeError(
            f"volume(s) without a time: {', '.join(bad)}. An accumulation needs scan times."
        )
    sites = {s.key: s for s in scans}
    box = extent or hgrid.bbox_for_radars(
        [{"latitude": s.latitude, "longitude": s.longitude} for s in sites.values()], max_range_km
    )
    try:
        grid = hgrid.make_grid(tuple(box), resolution_m)
    except Exception as exc:  # engine.mosaic.MosaicError: grid too large / box outside the map
        if type(exc).__name__ != "MosaicError":
            raise
        raise hqpe.QpeError(str(exc)) from exc
    geoms = {
        k: hgrid.RadarGeometry.build(grid, s.latitude, s.longitude, s.altitude_m)
        for k, s in sites.items()
    }
    names = _unique_names([sites[k].name for k in sites])
    name_of = dict(zip(sites, names, strict=True))

    # ---- one RadarField per volume -------------------------------------------------------
    t_read = _time.perf_counter()
    series: dict[tuple, list[hgrid.RadarField]] = {k: [] for k in sites}
    notes: list[str] = []
    ordered = sorted(scans, key=lambda s: s.time)
    n = len(ordered)
    seen_times: set[tuple] = set()
    for j, sc in enumerate(ordered):
        if cancel and cancel():
            raise hqpe.QpeError("cancelled")
        ident = (sc.key, str(sc.time))
        if ident in seen_times:
            notes.append(f"{sc.name}: duplicate volume at {prov.iso_utc(sc.time)} skipped")
            continue
        seen_times.add(ident)
        rep(5 + int(55 * j / max(n, 1)), f"QPE {sc.name} {j + 1}/{n}")
        rf, _ = radar_field(
            sc,
            cfg,
            grid,
            geoms[sc.key],
            n_sweeps=n_sweeps,
            max_elev_deg=max_elev_deg,
            max_range_km=max_range_km,
        )
        rf.name = name_of[sc.key]
        series[sc.key].append(rf)
    t_read = _time.perf_counter() - t_read

    first = min(f.time for lst in series.values() for f in lst)
    last = max(f.time for lst in series.values() for f in lst)
    for lst in series.values():
        lst.sort(key=lambda f: f.time)
    # One volume per radar and no period given: an instantaneous composite (rate_last), taken at
    # the first scan time; the scans of different radars may differ by minutes.
    snapshot = start is None and end is None and all(len(lst) == 1 for lst in series.values())
    if snapshot:
        t_start = t_end = first
        spread = float((last - first) / SECONDS)
        if spread > 60:
            notes.append(
                f"The radars were scanned up to {spread / 60:.0f} min apart; the composite does "
                "not advect them to a common time (give start= and end= with several volumes)"
            )
    else:
        t_start = _as_time(start) or first
        t_end = _as_time(end) or last
        if t_end < t_start:
            raise hqpe.QpeError("the end of the period is before its start")

    # ---- time nodes ------------------------------------------------------------------------
    nodes_ns = {t_start, t_end}
    k = 0
    while True:
        tk = t_start + np.timedelta64(round(k * step_s * 1e9), "ns")
        if tk >= t_end:
            break
        nodes_ns.add(tk)
        k += 1
    for lst in series.values():
        for f in lst:
            if t_start <= f.time <= t_end:
                nodes_ns.add(f.time)
    nodes = sorted(nodes_ns)

    # ---- state ---------------------------------------------------------------------------------
    shape = grid.shape
    acc = np.zeros(shape, dtype=np.float64)
    sig_num = np.zeros(shape, dtype=np.float64)
    vtime = np.zeros(shape, dtype=np.float64)
    qsum = np.zeros(shape, dtype=np.float64)
    hsum = np.zeros(shape, dtype=np.float64)
    rsum = np.zeros(shape, dtype=np.float64)
    rmax = np.full(shape, np.nan, dtype=np.float32)
    flags_or = np.zeros(shape, dtype=np.uint8)
    nrad = np.zeros(shape, dtype=np.uint8)
    prev_rate = None
    prev_t = None
    prev_sig = None
    last_rate = np.full(shape, np.nan, dtype=np.float32)
    last_comp = None
    total_s = max(float((t_end - t_start) / SECONDS), 0.0)

    interps: dict[tuple, tuple[int, PairInterpolator | None]] = {}
    motion_log: dict[tuple, list[tuple[float, float, str]]] = {k: [] for k in series}

    def radar_rate(key: tuple, t: np.datetime64) -> tuple[np.ndarray, hgrid.RadarField] | None:
        lst = series[key]
        times = [f.time for f in lst]
        if snapshot:
            return lst[0].rate, lst[0]
        if t < times[0]:
            return (lst[0].rate, lst[0]) if (times[0] - t) / SECONDS <= hold_edges_s else None
        if t > times[-1]:
            return (lst[-1].rate, lst[-1]) if (t - times[-1]) / SECONDS <= hold_edges_s else None
        # bracketing pair
        i = max(0, min(len(lst) - 2, int(np.searchsorted(times, t, side="right")) - 1))
        if len(lst) == 1:
            return (lst[0].rate, lst[0]) if t == times[0] else None
        a, b = lst[i], lst[i + 1]
        gap = float((b.time - a.time) / SECONDS)
        if t == a.time:
            return a.rate, a
        if t == b.time:
            return b.rate, b
        if gap > max_gap_s:
            return None
        cached = interps.get(key)
        if cached is None or cached[0] != i:
            it = PairInterpolator(
                a, b, grid, advect=advect, tile_m=tile_km * 1000.0 if tile_km else None
            )
            interps[key] = (i, it)
            motion_log[key].append((it.u, it.v, it.method))
        else:
            it = cached[1]
        near = a if (t - a.time) <= (b.time - t) else b
        return it.at(t), near

    rep(62, "Integrating over time")
    t_loop = _time.perf_counter()
    collector = basins.collector(grid) if basins is not None else None
    n_nodes = len(nodes)
    for kk, t in enumerate(nodes):
        if cancel and cancel():
            raise hqpe.QpeError("cancelled")
        rates, refs = [], []
        for key in series:
            got = radar_rate(key, t)
            if got is not None:
                rates.append(got[0])
                refs.append(got[1])
        if refs:
            comp = hgrid.composite(refs, rates, mode=composite)
            rate = comp.rate
            sig = comp.sigma
            flags_or |= comp.flags
            nrad = np.maximum(nrad, comp.n_radars)
        else:
            rate = np.full(shape, np.nan, dtype=np.float32)
            sig = rate
            comp = None
        if collector is not None:
            collector.add(t, rate)
        with np.errstate(invalid="ignore"):
            rmax = np.fmax(rmax, rate)
        last_rate = rate
        last_comp = comp
        if prev_rate is not None:
            dt = float((t - prev_t) / SECONDS)
            both = np.isfinite(rate) & np.isfinite(prev_rate)
            if dt > 0:
                ra = np.where(both, 0.5 * (rate + prev_rate), 0.0)
                acc += ra * dt / 3600.0
                sa = np.nan_to_num(prev_sig, nan=0.0) * np.nan_to_num(prev_rate, nan=0.0)
                sb = np.nan_to_num(sig, nan=0.0) * np.nan_to_num(rate, nan=0.0)
                sig_num += np.where(both, 0.5 * (sa + sb), 0.0) * dt / 3600.0
                vtime += np.where(both, dt, 0.0)
                if comp is not None:
                    qsum += np.where(both, np.nan_to_num(comp.quality) * dt, 0.0)
                    hsum += np.where(both, np.nan_to_num(comp.height_agl_m) * dt, 0.0)
                    rsum += np.where(both, np.nan_to_num(comp.range_km) * dt, 0.0)
        prev_rate, prev_t, prev_sig = rate, t, sig
        if kk % 8 == 0:
            rep(62 + int(30 * kk / max(n_nodes, 1)), f"Integrating {kk + 1}/{n_nodes}")
    t_loop = _time.perf_counter() - t_loop

    # ---- finalise --------------------------------------------------------------------------
    with np.errstate(invalid="ignore", divide="ignore"):
        vf = np.where(total_s > 0, vtime / total_s, np.nan)
        have = vtime > 0
        covered_ever = np.isfinite(rmax)
        accumulation = np.where(have, acc, np.where(covered_ever & (total_s == 0), 0.0, np.nan))
        sigma_mm = np.where(have, sig_num, np.nan)
        qmean = np.where(have, qsum / np.maximum(vtime, 1e-9), np.nan)
        hmean = np.where(have, hsum / np.maximum(vtime, 1e-9), np.nan)
        rkm = np.where(have, rsum / np.maximum(vtime, 1e-9), np.nan)
    if total_s == 0:
        vf = np.where(covered_ever, 1.0, np.nan)
        notes.append("A single time: the accumulation is 0 mm over 0 s; read rate_last instead.")
        if last_comp is not None:  # the instant itself has no time integral: show its beam fields
            qmean, hmean, rkm = last_comp.quality, last_comp.height_agl_m, last_comp.range_km
            sigma_mm = np.where(
                np.isfinite(last_rate),
                np.nan_to_num(last_comp.sigma) * np.nan_to_num(last_rate),
                np.nan,
            )
            notes.append(
                "accumulation_sigma of a single time is the 1-sigma of the rate (mm/h), not mm."
            )
    for key, lst in series.items():
        gaps = [float((b.time - a.time) / SECONDS) for a, b in itertools.pairwise(lst)]
        long_gaps = [g for g in gaps if g > max_gap_s]
        if long_gaps:
            notes.append(
                f"{name_of[key]}: {len(long_gaps)} gap(s) longer than {max_gap_s / 60:.0f} min "
                f"(largest {max(long_gaps) / 60:.0f} min) not bridged: those times are missing"
            )
    fell_back = sum(1 for v in motion_log.values() for m in v if m[2] != "advection")
    if advect and fell_back:
        notes.append(
            f"{fell_back} scan pair(s) had no usable storm motion: interpolated linearly in time"
        )
    if start is not None and _as_time(start) < first:
        notes.append(
            "The period starts before the first scan: "
            + (
                f"up to {hold_edges_s:g} s held at the first rate, the rest"
                if hold_edges_s
                else "that time"
            )
            + " is missing, not zero."
        )
    if end is not None and _as_time(end) > last:
        notes.append(
            "The period ends after the last scan: "
            + (
                f"up to {hold_edges_s:g} s held at the last rate, the rest"
                if hold_edges_s
                else "that time"
            )
            + " is missing, not zero."
        )

    radars_info = []
    for key, lst in series.items():
        sc = sites[key]
        us = [m for m in motion_log[key] if m[2] == "advection"]
        radars_info.append(
            {
                "name": name_of[key],
                "latitude": sc.latitude,
                "longitude": sc.longitude,
                "altitude_m": sc.altitude_m,
                "scans": len(lst),
                "first": prov.iso_utc(lst[0].time),
                "last": prov.iso_utc(lst[-1].time),
                "motion_mean_ms": (
                    [float(np.mean([m[0] for m in us])), float(np.mean([m[1] for m in us]))]
                    if us
                    else None
                ),
                "methods": sorted({m for f in lst for m in f.meta.get("methods", [])}),
            }
        )
    sample = next(iter(series.values()))[0]
    config_out = {
        "method": cfg.method,
        "method_text": sample.meta.get("relation"),
        "methods_used": sorted(
            {m for lst in series.values() for f in lst for m in f.meta["methods"]}
        ),
        "relation": cfg.relation
        or (hqpe.DEFAULT_SNOW if cfg.precip == "snow" else hqpe.DEFAULT_ZR),
        "zr": cfg.zr().text(),
        "precip": cfg.precip,
        "band": cfg.band,
        "z_cap_dbz": cfg.z_cap,
        "min_dbz": cfg.min_dbz,
        "hail_policy": cfg.hail_policy,
        "freezing_level_m": cfg.freezing_level_m,
        "attenuation": cfg.attenuation,
        "sigma_z_db": cfg.sigma_z_db,
        "sigma_zdr_db": cfg.sigma_zdr_db,
        "dem": cfg.dem is not None,
    }
    notes.extend(sample.meta.get("notes", []))
    result = QpeResult(
        grid=grid,
        start=t_start,
        end=t_end,
        accumulation=accumulation.astype(np.float32),
        accumulation_sigma=sigma_mm.astype(np.float32),
        rate_max=rmax,
        rate_last=last_rate.astype(np.float32),
        valid_fraction=vf.astype(np.float32),
        quality=qmean.astype(np.float32),
        beam_height_agl_m=hmean.astype(np.float32),
        range_km=rkm.astype(np.float32),
        flags=flags_or,
        n_radars=nrad,
        nodes=nodes,
        radars=radars_info,
        config=config_out,
        options={
            "resolution_m": resolution_m,
            "composite": composite,
            "advect": advect,
            "tile_km": tile_km,
            "step_s": step_s,
            "max_gap_s": max_gap_s,
            "hold_edges_s": hold_edges_s,
            "max_range_km": max_range_km,
            "n_sweeps": n_sweeps,
            "max_elev_deg": max_elev_deg,
        },
        notes=list(dict.fromkeys(notes)),
        timing={
            "total_s": round(_time.perf_counter() - t_all, 3),
            "volumes_s": round(t_read, 3),
            "integrate_s": round(t_loop, 3),
        },
        source_files=[s.path or s.name for s in scans],
    )
    if collector is not None:
        result.basin_series = collector.finish(result)
    rep(100, "QPE ready")
    return result


def _unique_names(names: Sequence[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in names:
        seen[n] = seen.get(n, 0) + 1
        out.append(n if seen[n] == 1 else f"{n}_{seen[n]}")
    return out

"""Flash-flood indicators: rainfall against guidance, return period and the cell's own climatology.

These are **indicators computed from numbers you supply**, not a flood forecast and not a warning.
Nothing is
downloaded: guidance, precipitation-frequency and climatology grids are files you obtained yourself
(NetCDF, GeoTIFF or CSV, formats below); the rainfall is the radar estimate of the app (a
``QpeResult``).

Three indicators
----------------
``ffg_ratio``
    ``rain / guidance`` for a duration ``D`` (1, 3, 6, 24 h ...), the rainfall of the period divided
    by a
    gridded flash-flood guidance depth of the same duration. 1 means the rain equals the guidance
    value. The
    guidance itself comes with its issuer's assumptions (soil state, basin response); they are
    **not** modelled
    here. The classic criterion "3-h rain >= guidance" is one of the baselines of the flash-flood
    lab report
    (4 % of warned or reported cell-hours caught, false-alarm ratio 0.92): a weak criterion, shown
    as such.
``return_period``
    The return period of the depth of duration ``D`` from precipitation-frequency grids (depth for
    2, 5, 10 ...
    years, e.g. NOAA Atlas 14 or a regional equivalent), by log-log interpolation per cell. Values
    below the
    smallest or above the largest period are flagged, never extrapolated. Those grids describe
    **point** depths; a
    cell is a few square kilometres, so the area-mean/point difference is small at 1 km but not zero
    (see
    :mod:`engine.hydro.basins` for the basin case).
``relative_1h``
    The one-hour rain divided by the cell's own climatological reference (for example the 99.5th
    percentile of
    its one-hour totals from a record you have; :func:`climatology_from_stack` computes it from your
    grids).
    This is the **reference rule** of the flash-flood lab: a machine-learning model with 36
    predictors did not beat it
    (PR-AUC 0.022 against 0.025 for the 1-h rain and for this ratio; best CSI of any method 0.044),
    so the app
    shows this transparent rule and nothing learned.

How good that is: weak. In the lab's test on May to September 2025 (276 million land cell-hours, the
outcome a
flash-flood warning or report within the next 3 h) the best critical success index any method
reached was 0.044,
the false-alarm ratio at useful hit rates was 0.94 to 0.97, and 43 % of the positive cell-hours had
less than 1 mm
of rain in the previous 3 h (the warning came before the rain). The label is a forecaster's
decision, not a
measured flood, and skill differed strongly between regions. Read the indicator as "this cell has
more rain
than usual", never as "this cell will flood".

Provenance
----------
Rain from the radar is RETRIEVED, so the indicators are labelled **RETRIEVED indicator** (class of
the input).
If the rain is a nowcast, pass ``input_class="FORECAST"``: the layer is then FORECAST. Every layer
carries
the labels, the inputs (file names, variable, units, the vintage and valid time you give), the
period, the
thresholds and the sentence that it is experimental guidance, not an official warning.

Formats of the files you supply (all regular latitude/longitude grids or points, WGS 84)
-------------------------------------------------------------------------------------
NetCDF
    One variable (``variable=``) with 1-D ``lat``/``latitude`` and ``lon``/``longitude`` coordinates
    (any order of dimensions, latitudes ascending or descending). ``units`` attribute: ``mm``,
    ``in``,
    ``cm``, ``m`` (or ``years`` for a return-period grid). ``valid_time`` / ``vintage`` / ``source``
    global
    attributes are carried into the output when present.
GeoTIFF
    One band, north-up, geographic WGS 84 (ModelPixelScale and ModelTiepoint tags), float or
    integer; the
    nodata value of the file is honoured; units are given on the command line (default mm).
CSV
    ``lat,lon,value`` points (any extra columns are ignored; ``value`` may be named after the
    quantity);
    each cell takes the nearest point within ``max_distance_km`` (default 25 km), otherwise no
    value.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from engine import provenance as prov
from engine.hydro.qpe import QpeError

PRODUCT_TEXT = (
    "Experimental guidance, not an official warning. Follow your official weather service "
    "(in the US, the National Weather Service)."
)
NOT_MODELLED = (
    "Antecedent soil moisture, snow, urban drainage, terrain and basin response are not modelled "
    "unless contained in the guidance you supplied."
)

UNIT_TO_MM = {
    "mm": 1.0,
    "millimeter": 1.0,
    "millimeters": 1.0,
    "millimetre": 1.0,
    "millimetres": 1.0,
    "kg m-2": 1.0,
    "kg/m2": 1.0,
    "cm": 10.0,
    "in": 25.4,
    "inch": 25.4,
    "inches": 25.4,
    "m": 1000.0,
}

#: ratio of rain to guidance: class edges and labels (no wording that suggests a warning)
RATIO_EDGES = (0.5, 0.75, 1.0, 1.5)
RATIO_LABELS = (
    "below 50 % of guidance",
    "50 to 75 %",
    "75 to 100 %",
    "100 to 150 % (at or above guidance)",
    "150 % or more of guidance",
)
RETURN_EDGES = (2.0, 5.0, 10.0, 25.0, 50.0, 100.0)
RETURN_LABELS = (
    "under 2 years",
    "2 to 5 years",
    "5 to 10 years",
    "10 to 25 years",
    "25 to 50 years",
    "50 to 100 years",
    "100 years or more (not extrapolated)",
)
REL_EDGES = (0.5, 1.0)
REL_LABELS = (
    "below half of the cell's reference",
    "half to the reference",
    "above the cell's own reference",
)


class IndicatorError(QpeError):
    """A wrong input file or option of the flood indicators."""


# ---------------------------------------------------------------------------
# Reading the files you supply
# ---------------------------------------------------------------------------


@dataclass
class SourceField:
    """A field on its own regular lat/lon grid (or as scattered points) as read from a file."""

    values: np.ndarray  # (nlat, nlon), NaN where missing; empty for points
    lat: np.ndarray  # 1-D ascending (grid) or 1-D point latitudes
    lon: np.ndarray
    units: str
    meta: dict[str, Any]
    is_points: bool = False
    point_values: np.ndarray | None = None


def _meta(
    path: Path, variable: str | None, units: str, extra: dict[str, Any] | None = None
) -> dict:
    m: dict[str, Any] = {"file": path.name, "variable": variable, "units_in_file": units}
    for k, v in (extra or {}).items():
        if v not in (None, ""):
            m[k] = v
    return m


def read_netcdf(
    path: str | Path, variable: str | None = None, units: str | None = None
) -> SourceField:
    """A 2-D variable of a NetCDF file with 1-D latitude and longitude coordinates."""
    import xarray as xr

    p = Path(path).expanduser()
    if not p.is_file():
        raise IndicatorError(f"file not found: {path}. Check the path.")
    try:
        ds = xr.open_dataset(p, decode_times=False)
    except Exception as exc:
        raise IndicatorError(f"cannot read {p.name} as NetCDF: {exc}") from exc
    try:
        names = [n for n, v in ds.data_vars.items() if v.ndim >= 2]
        if variable is None:
            if len(names) != 1:
                raise IndicatorError(
                    f"{p.name} has several 2-D variables ({', '.join(names)}): "
                    "say which with variable="
                )
            variable = names[0]
        if variable not in ds:
            raise IndicatorError(f"{p.name} has no variable {variable!r} ({', '.join(names)})")
        da = ds[variable]

        def find(cands: Sequence[str]) -> str:
            for c in cands:
                if c in da.coords or c in ds.coords:
                    return c
            raise IndicatorError(
                f"{p.name}: {variable} needs 1-D {'/'.join(cands)} coordinates "
                "(a regular lat/lon grid)"
            )

        latn = find(("lat", "latitude", "y"))
        lonn = find(("lon", "longitude", "x"))
        lat, lon = ds[latn].values.astype(float), ds[lonn].values.astype(float)
        if lat.ndim != 1 or lon.ndim != 1:
            raise IndicatorError(f"{p.name}: only regular grids with 1-D lat/lon are supported")
        da = da.squeeze(drop=True)
        if da.ndim != 2:
            raise IndicatorError(f"{p.name}: {variable} must be 2-D after removing length-1 axes")
        da = (
            da.transpose(da.dims[0] if da.dims[0] in (latn,) else latn, lonn)
            if (latn in da.dims and lonn in da.dims)
            else da
        )
        vals = da.values.astype(float)
        fill = da.attrs.get("_FillValue", da.encoding.get("_FillValue"))
        if fill is not None:
            vals = np.where(vals == fill, np.nan, vals)
        if lat[0] > lat[-1]:
            lat, vals = lat[::-1], vals[::-1]
        u = units or str(da.attrs.get("units", "")) or "mm"
        g = ds.attrs
        meta = _meta(
            p,
            variable,
            u,
            {
                "source": g.get("source") or g.get("institution"),
                "vintage": g.get("vintage"),
                "valid_time": g.get("valid_time") or g.get("time_coverage_end"),
                "title": g.get("title"),
            },
        )
        return SourceField(vals, lat, lon, u, meta)
    finally:
        ds.close()


def read_geotiff(path: str | Path, units: str = "mm", nodata: float | None = None) -> SourceField:
    """A single-band, north-up, geographic (WGS 84) GeoTIFF, read with Pillow (no GDAL)."""
    from PIL import Image

    p = Path(path).expanduser()
    if not p.is_file():
        raise IndicatorError(f"file not found: {path}. Check the path.")
    Image.MAX_IMAGE_PIXELS = 100_000_000
    try:
        im = Image.open(p)
        tags = im.tag_v2
        scale = tags.get(33550)  # ModelPixelScaleTag
        tie = tags.get(33922)  # ModelTiepointTag
        gdal_nodata = tags.get(42113)
        arr = np.array(im, dtype=float)
    except Exception as exc:
        raise IndicatorError(f"cannot read {p.name} as a GeoTIFF: {exc}") from exc
    if scale is None or tie is None or len(scale) < 2 or len(tie) < 6:
        raise IndicatorError(
            f"{p.name} has no georeferencing tags (ModelPixelScale and ModelTiepoint): "
            "give a north-up geographic GeoTIFF, or a NetCDF or CSV"
        )
    if arr.ndim != 2:
        raise IndicatorError(f"{p.name}: only single-band GeoTIFFs are supported")
    dx, dy = float(scale[0]), float(scale[1])
    x0, y0 = float(tie[3]) - float(tie[0]) * dx, float(tie[4]) + float(tie[1]) * dy
    if not (abs(x0) <= 360 and abs(y0) <= 90 and dx < 5 and dy < 5):
        raise IndicatorError(
            f"{p.name} does not look geographic (degrees): reproject it to WGS 84 (EPSG:4326) first"
        )
    ny, nx = arr.shape
    lon = x0 + (np.arange(nx) + 0.5) * dx  # cell centres (area-is-area raster)
    lat = y0 - (np.arange(ny) + 0.5) * dy
    nd = nodata
    if nd is None and gdal_nodata:
        try:
            nd = float(str(gdal_nodata).strip("\x00 "))
        except ValueError:
            nd = None
    if nd is not None:
        arr = np.where(arr == nd, np.nan, arr)
    return SourceField(arr[::-1], lat[::-1], lon, units, _meta(p, None, units))


def read_points_csv(path: str | Path, units: str = "mm", value: str | None = None) -> SourceField:
    """``lat,lon,value`` rows (case-insensitive names ``lat/latitude``, ``lon/lng/longitude``)."""
    import pandas as pd

    p = Path(path).expanduser()
    if not p.is_file():
        raise IndicatorError(f"file not found: {path}. Check the path.")
    df = pd.read_csv(p, comment="#")
    cols = {c.lower().strip(): c for c in df.columns}

    def pick(*names: str) -> str | None:
        for n in names:
            if n in cols:
                return cols[n]
        return None

    la, lo = pick("lat", "latitude"), pick("lon", "lng", "longitude")
    va = value if value in df.columns else pick("value", "depth_mm", "depth", "mm", "ffg", "ari")
    if va is None and len(df.columns) >= 3:
        rest = [c for c in df.columns if c not in (la, lo)]
        va = rest[-1] if rest else None
    if not (la and lo and va):
        raise IndicatorError(
            f"{p.name}: a points file has columns lat, lon and a value column (found "
            f"{', '.join(map(str, df.columns))})"
        )
    d = df[[la, lo, va]].dropna()
    return SourceField(
        np.empty((0, 0)),
        d[la].to_numpy(float),
        d[lo].to_numpy(float),
        units,
        _meta(p, va, units),
        is_points=True,
        point_values=d[va].to_numpy(float),
    )


def read_field(
    path: str | Path,
    *,
    variable: str | None = None,
    units: str | None = None,
) -> SourceField:
    """Read a NetCDF, GeoTIFF or points CSV by extension."""
    ext = Path(path).suffix.lower()
    if ext in (".nc", ".nc4", ".cdf"):
        return read_netcdf(path, variable, units)
    if ext in (".tif", ".tiff"):
        return read_geotiff(path, units or "mm")
    if ext in (".csv", ".txt"):
        return read_points_csv(path, units or "mm", variable)
    raise IndicatorError(
        f"cannot read {Path(path).name}: use NetCDF (.nc), GeoTIFF (.tif) or a lat,lon,value CSV"
    )


def to_mm(values: np.ndarray, units: str) -> np.ndarray:
    key = units.strip().lower()
    if key not in UNIT_TO_MM:
        raise IndicatorError(
            f"unknown depth unit {units!r}: use mm, cm, in or m (give units= or fix the file)"
        )
    return values * UNIT_TO_MM[key]


# ---------------------------------------------------------------------------
# Onto the rainfall grid
# ---------------------------------------------------------------------------


def regrid(src: SourceField, grid: Any, *, max_distance_km: float = 25.0) -> np.ndarray:
    """The field on the cells of ``grid``: bilinear between grid points of a lat/lon grid (NaN if
    any of
    the four is missing, NaN outside the file's grid), nearest point within ``max_distance_km`` for
    points."""
    lon, lat = grid.lonlat()
    if src.is_points:
        return _nearest(src, lon, lat, max_distance_km)
    la, lo = src.lat, src.lon
    if la.size < 2 or lo.size < 2:
        raise IndicatorError("a gridded file needs at least 2 x 2 points")
    fi = np.interp(lat, la, np.arange(la.size), left=np.nan, right=np.nan)
    fj = np.interp(lon, lo, np.arange(lo.size), left=np.nan, right=np.nan)
    ok = np.isfinite(fi) & np.isfinite(fj)
    i0 = np.clip(np.floor(np.where(ok, fi, 0)).astype(int), 0, la.size - 2)
    j0 = np.clip(np.floor(np.where(ok, fj, 0)).astype(int), 0, lo.size - 2)
    wi, wj = np.where(ok, fi, 0) - i0, np.where(ok, fj, 0) - j0
    v = src.values
    out = (
        v[i0, j0] * (1 - wi) * (1 - wj)
        + v[i0 + 1, j0] * wi * (1 - wj)
        + v[i0, j0 + 1] * (1 - wi) * wj
        + v[i0 + 1, j0 + 1] * wi * wj
    )
    return np.where(ok, out, np.nan)


def _nearest(src: SourceField, lon: np.ndarray, lat: np.ndarray, max_km: float) -> np.ndarray:
    plat, plon, pv = src.lat, src.lon, src.point_values
    assert pv is not None
    out = np.full(lon.shape, np.nan)
    flat_lon, flat_lat = lon.ravel(), lat.ravel()
    best = np.full(flat_lon.shape, np.inf)
    val = np.full(flat_lon.shape, np.nan)
    kx = 111.195 * np.cos(np.radians(flat_lat))
    for la, lo, v in zip(plat, plon, pv, strict=True):  # points are few (a table), cells many
        d = np.hypot((flat_lat - la) * 111.195, (flat_lon - lo) * kx)
        better = d < best
        best = np.where(better, d, best)
        val = np.where(better, v, val)
    val = np.where(best <= max_km, val, np.nan)
    out = val.reshape(lon.shape)
    return out


# ---------------------------------------------------------------------------
# The indicators
# ---------------------------------------------------------------------------


def classify(values: np.ndarray, edges: Sequence[float]) -> np.ndarray:
    """int8 class index (0 .. len(edges)); -1 where the value is missing."""
    v = np.asarray(values, dtype=float)
    out = np.digitize(np.where(np.isfinite(v), v, 0.0), edges).astype(np.int8)
    return np.where(np.isfinite(v), out, -1).astype(np.int8)


def ratio_to_guidance(rain_mm: np.ndarray, guidance_mm: np.ndarray) -> np.ndarray:
    """``rain / guidance`` where both are finite and the guidance is positive, else NaN."""
    r = np.asarray(rain_mm, dtype=float)
    g = np.asarray(guidance_mm, dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(np.isfinite(r) & np.isfinite(g) & (g > 0), r / g, np.nan)


def _radarx(name: str) -> Any:
    """A function of ``radarx.retrieve`` when the installed radarx has it, else None.

    The algorithms live in radarx (``return_period``, ``climatological_reference``); this module
    wraps them. The built-in copies below are a **transitional fallback** for radarx versions
    that do not have them yet (they are tested to agree) and go when the minimum radarx version
    includes them.
    """
    try:
        import radarx.retrieve as rt
    except ImportError:
        return None
    return getattr(rt, name, None)


def uses_radarx() -> dict[str, bool]:
    """Which of the wrapped radarx functions are in use (the rest run the built-in fallback)."""
    return {n: _radarx(n) is not None for n in ("return_period", "climatological_reference")}


def return_period_grid(
    depth_mm: np.ndarray, periods: Sequence[float], depth_grids_mm: Sequence[np.ndarray]
) -> tuple[np.ndarray, np.ndarray]:
    """Return period (years) of ``depth_mm`` per cell from depth grids for ``periods`` (log-log
    interpolation).

    Returns ``(years, flag)``: ``flag`` is -1 below the smallest period's depth (years = smallest
    period),
    +1 above the largest (years = largest period), 0 inside, NaN years where any input is missing.
    """
    per = np.asarray(periods, dtype=float)
    if per.size < 2 or np.any(np.diff(per) <= 0) or np.any(per <= 0):
        raise IndicatorError("give at least two increasing return periods (years)")
    stack = np.stack([np.asarray(g, dtype=float) for g in depth_grids_mm])  # (n_per, ny, nx)
    if stack.shape[0] != per.size:
        raise IndicatorError("one depth grid per return period is needed")
    d = np.asarray(depth_mm, dtype=float)
    fn = _radarx("return_period")
    if fn is not None:
        import xarray as xr

        dims = ("y", "x")
        da = xr.DataArray(d, dims=dims)
        tab = xr.DataArray(stack, dims=("period", *dims), coords={"period": per})
        out = fn(da, per, tab)
        return out["return_period"].values.astype(float), out["flag"].values.astype(np.int8)
    ok = np.isfinite(d) & np.all(np.isfinite(stack), axis=0) & np.all(stack > 0, axis=0) & (d > 0)
    ok &= np.all(np.diff(stack, axis=0) >= 0, axis=0)  # depth must not fall with the period
    lnp = np.log(per)
    years = np.full(d.shape, np.nan)
    flag = np.zeros(d.shape, dtype=np.int8)
    ln_d = np.log(np.where(ok, d, 1.0))
    ln_s = np.log(np.where(ok[None], stack, 1.0))
    below = ok & (ln_d < ln_s[0])
    above = ok & (ln_d > ln_s[-1])
    inside = ok & ~below & ~above
    years[below] = per[0]
    flag[below] = -1
    years[above] = per[-1]
    flag[above] = 1
    # segment index per cell: the last k with ln_s[k] <= ln_d
    k = np.zeros(d.shape, dtype=int)
    for j in range(1, per.size - 1):
        k = np.where(ln_d >= ln_s[j], j, k)
    kk = k[None]
    lo = np.take_along_axis(ln_s, kk, 0)[0]
    hi = np.take_along_axis(ln_s, kk + 1, 0)[0]
    with np.errstate(invalid="ignore", divide="ignore"):
        t = np.where(hi > lo, (ln_d - lo) / (hi - lo), 0.0)
    ly = lnp[k] + t * (lnp[k + 1] - lnp[k])
    years[inside] = np.exp(ly[inside])
    return years, np.where(ok, flag, 0).astype(np.int8)


def climatology_from_stack(grids: Sequence[np.ndarray], q: float = 0.995) -> np.ndarray:
    """Per-cell quantile ``q`` of a stack of one-hour totals (your record): the reference of
    ``relative_1h``. Cells with fewer than 30 finite values get NaN (a quantile of a handful of
    hours says
    nothing about a 99.5th percentile)."""
    if not 0.5 <= q < 1.0:
        raise IndicatorError("the reference quantile is between 0.5 and 1 (0.995)")
    st = np.stack([np.asarray(g, dtype=float) for g in grids])
    fn = _radarx("climatological_reference")
    if fn is not None:
        import xarray as xr

        rec = xr.DataArray(st, dims=("time", "y", "x"))
        return fn(rec, quantile=q, dim="time", min_count=30).values.astype(float)
    n = np.isfinite(st).sum(axis=0)
    out = np.nanquantile(np.where(np.isfinite(st), st, np.nan), q, axis=0)
    return np.where(n >= 30, out, np.nan)


@dataclass
class IndicatorLayer:
    name: str
    label: str
    units: str
    values: np.ndarray
    classes: np.ndarray
    edges: tuple[float, ...]
    class_labels: tuple[str, ...]
    provenance: dict[str, Any]
    flag: np.ndarray | None = None


@dataclass
class IndicatorResult:
    layers: dict[str, IndicatorLayer]
    grid: Any
    rain_mm: np.ndarray
    rain_field: str
    period: tuple[str, str]
    duration_h: float
    notes: list[str] = field(default_factory=list)
    input_class: str = "RETRIEVED"
    created: str = ""
    inputs: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "label": f"{self.input_class} INDICATOR",
            "product_text": PRODUCT_TEXT,
            "not_modelled": NOT_MODELLED,
            "period": list(self.period),
            "duration_h": self.duration_h,
            "rain_field": self.rain_field,
            "notes": self.notes,
            "layers": {},
        }
        area = self.grid.cell_areas_km2()
        for name, lay in self.layers.items():
            v = lay.values
            fin = np.isfinite(v)
            shares = {}
            for k, lab in enumerate(lay.class_labels):
                m = lay.classes == k
                shares[lab] = {"cells": int(m.sum()), "area_km2": float(area[m].sum())}
            out["layers"][name] = {
                "label": lay.label,
                "units": lay.units,
                "cells_with_value": int(fin.sum()),
                "max": float(np.nanmax(v)) if fin.any() else None,
                "classes": shares,
                "provenance": lay.provenance,
            }
        return out


def _window_note(result: Any, duration_h: float) -> tuple[str | None, bool]:
    """``(note, lower_bound)``: how the period of the rainfall compares with the guidance
    duration."""
    have_h = result.duration_s / 3600.0
    if have_h <= 0:
        raise IndicatorError(
            "the indicators need an accumulation over a period (several volumes), not one rate"
        )
    if have_h > duration_h * 1.05:
        raise IndicatorError(
            f"the rainfall period ({have_h:.2f} h) is longer than the {duration_h:g} h of the "
            f"guidance: give --start/--end for exactly the {duration_h:g} h you want to compare"
        )
    if have_h < duration_h * 0.9:
        return (
            f"The rainfall period is {have_h * 60:.0f} min, shorter than the {duration_h:g} h of "
            f"the guidance: the {duration_h:g} h rainfall can only be larger, so ratios are lower "
            "bounds and return "
            "periods are lower bounds.",
            True,
        )
    return None, False


def compute(
    result: Any,
    *,
    duration_h: float = 1.0,
    guidance: tuple[np.ndarray, dict[str, Any]] | None = None,
    frequency: tuple[Sequence[float], Sequence[np.ndarray], dict[str, Any]] | None = None,
    climatology: tuple[np.ndarray, dict[str, Any]] | None = None,
    rain_field: str | None = None,
    input_class: str = "RETRIEVED",
) -> IndicatorResult:
    """The indicators of a QPE result. ``guidance``/``climatology`` are ``(array_mm_on_the_grid,
    meta)``,
    ``frequency`` ``(periods, depth_arrays_mm, meta)`` as produced by :func:`load_inputs`."""
    if input_class not in ("RETRIEVED", "FORECAST"):
        raise IndicatorError("input_class is RETRIEVED (radar estimate) or FORECAST (a nowcast)")
    if guidance is None and frequency is None and climatology is None:
        raise IndicatorError("give at least one of guidance, return-period grids or a climatology")
    name = rain_field or (
        "accumulation_adjusted" if result.accumulation_adjusted is not None else "accumulation"
    )
    rain = np.asarray(getattr(result, name), dtype=float)
    note, lower = _window_note(result, duration_h)
    notes = [NOT_MODELLED]
    if note:
        notes.append(note)
    created = prov.utc_stamp()
    base_prov = {
        "class": input_class,
        "label": f"{input_class} INDICATOR",
        "product_text": PRODUCT_TEXT,
        "rain_field": name,
        "rain_provenance": "ADJUSTED (gauges)"
        if name.endswith("adjusted")
        else "RETRIEVED (radar)",
        "period": [prov.iso_utc(result.start), prov.iso_utc(result.end)],
        "duration_h": duration_h,
        "lower_bound": lower,
        "software": prov.software_string(),
        "created": created,
    }
    layers: dict[str, IndicatorLayer] = {}
    dtag = f"{duration_h:g}h"
    inputs: list[str] = []
    if guidance is not None:
        g, meta = guidance
        r = ratio_to_guidance(rain, g)
        layers[f"ffg_ratio_{dtag}"] = IndicatorLayer(
            f"ffg_ratio_{dtag}",
            f"Rain / guidance, {duration_h:g} h",
            "1",
            r.astype(np.float32),
            classify(r, RATIO_EDGES),
            RATIO_EDGES,
            RATIO_LABELS,
            {
                **base_prov,
                "method": "rain of the period divided by the guidance depth of the same duration",
                "guidance": meta,
                "thresholds": list(RATIO_EDGES),
            },
        )
        inputs.append(meta.get("file", "guidance"))
    if frequency is not None:
        periods, grids, meta = frequency
        years, flag = return_period_grid(rain, periods, grids)
        layers[f"return_period_{dtag}"] = IndicatorLayer(
            f"return_period_{dtag}",
            f"Return period of the {duration_h:g} h rain",
            "years",
            years.astype(np.float32),
            classify(years, RETURN_EDGES),
            RETURN_EDGES,
            RETURN_LABELS,
            {
                **base_prov,
                "method": "log-log interpolation of the supplied depth-for-return-period grids per "
                "cell; "
                "values outside the supplied periods are flagged, not extrapolated",
                "frequency": meta,
                "periods_years": [float(p) for p in periods],
                "point_depths_note": "precipitation-frequency grids are point depths",
            },
            flag,
        )
        inputs.append(meta.get("file", "frequency"))
    if climatology is not None:
        c, meta = climatology
        rel = ratio_to_guidance(rain, c)
        layers["relative_1h"] = IndicatorLayer(
            "relative_1h",
            "Rain relative to the cell's own climatology",
            "1",
            rel.astype(np.float32),
            classify(rel, REL_EDGES),
            REL_EDGES,
            REL_LABELS,
            {
                **base_prov,
                "method": "rain of the period divided by the cell's climatological reference "
                "(the reference rule of the flash-flood lab report)",
                "climatology": meta,
                "lab_result": "a 36-predictor machine-learning model did not beat this rule on "
                "276 million cell-hours (PR-AUC 0.022 against 0.025); the rule is itself weak "
                "(best CSI 0.044): an indicator of unusual rain, not a flood prediction",
                "thresholds": list(REL_EDGES),
            },
        )
        inputs.append(meta.get("file", "climatology"))
        if abs(duration_h - 1.0) > 1e-9:
            notes.append(
                f"The relative indicator is defined for one-hour rain; the period here is "
                f"{duration_h:g} h."
            )
    return IndicatorResult(
        layers=layers,
        grid=result.grid,
        rain_mm=rain.astype(np.float32),
        rain_field=name,
        period=(prov.iso_utc(result.start), prov.iso_utc(result.end)),
        duration_h=duration_h,
        notes=notes,
        input_class=input_class,
        created=created,
        inputs=inputs,
    )


def load_inputs(
    grid: Any,
    *,
    guidance: str | Path | None = None,
    guidance_units: str | None = None,
    guidance_variable: str | None = None,
    frequency: Sequence[tuple[float, str | Path]] | None = None,
    frequency_units: str | None = None,
    frequency_variable: str | None = None,
    climatology: str | Path | None = None,
    climatology_units: str | None = None,
    climatology_variable: str | None = None,
    max_distance_km: float = 25.0,
    vintage: str | None = None,
) -> dict[str, Any]:
    """Read and regrid the files to ``grid``; returns the keyword arguments of :func:`compute`.

    ``frequency`` is a list of ``(return_period_years, file)``. ``vintage`` is your statement of the
    data
    vintage (for example "NWS FFG issued 2024-05-01 18Z"); it is carried into every output and, when
    absent,
    the output says "vintage: not given" (the file's own attribute is used when it has one).
    """
    kw: dict[str, Any] = {}

    def one(path: Any, units: str | None, var: str | None) -> tuple[np.ndarray, dict[str, Any]]:
        src = read_field(path, variable=var, units=units)
        arr = regrid(src, grid, max_distance_km=max_distance_km)
        meta = dict(src.meta)
        meta["units_converted_to"] = "mm"
        meta["vintage"] = vintage or meta.get("vintage") or "not given"
        meta["regridded"] = (
            "bilinear to the analysis grid"
            if not src.is_points
            else f"nearest point within {max_distance_km:g} km"
        )
        valid = np.isfinite(arr)
        meta["cells_covered"] = int(valid.sum())
        return to_mm(arr, src.units), meta

    if guidance is not None:
        kw["guidance"] = one(guidance, guidance_units, guidance_variable)
    if frequency:
        items = sorted(((float(p), f) for p, f in frequency), key=lambda t: t[0])
        grids, metas = [], []
        for p, f in items:
            a, m = one(f, frequency_units, frequency_variable)
            grids.append(a)
            metas.append({**m, "return_period_years": p})
        kw["frequency"] = (
            [p for p, _ in items],
            grids,
            {
                "file": ", ".join(m["file"] for m in metas),
                "grids": metas,
                "vintage": metas[0]["vintage"],
            },
        )
    if climatology is not None:
        kw["climatology"] = one(climatology, climatology_units, climatology_variable)
    return kw


# ---------------------------------------------------------------------------
# Figure and files
# ---------------------------------------------------------------------------

_CLASS_COLORS = {
    "ratio": ["#f1f1f1", "#fee8c8", "#fdbb84", "#e34a33", "#7f0000"],
    "return": ["#f1f1f1", "#deebf7", "#9ecae1", "#6baed6", "#3182bd", "#08519c", "#08306b"],
    "relative": ["#f1f1f1", "#fdd49e", "#d7301f"],
}


def figure(ind: IndicatorResult, name: str):
    """Map of one indicator in classes, with the label, the guidance sentence and the inputs on the
    picture."""
    import matplotlib

    matplotlib.use("Agg", force=False)
    import textwrap

    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.patches import Patch

    lay = ind.layers[name]
    kind = (
        "ratio" if name.startswith("ffg") else "return" if name.startswith("return") else "relative"
    )
    cols = _CLASS_COLORS[kind]
    g = ind.grid
    ext = [
        (g.x[0] - g.cell_m / 2) / 1e3,
        (g.x[-1] + g.cell_m / 2) / 1e3,
        (g.y[0] - g.cell_m / 2) / 1e3,
        (g.y[-1] + g.cell_m / 2) / 1e3,
    ]
    fig, ax = plt.subplots(figsize=(7.4, 6.8), constrained_layout=True)
    cm = ListedColormap(cols)
    cm.set_bad("#eef0f2")
    arr = np.ma.masked_where(lay.classes < 0, lay.classes)
    ax.imshow(
        arr,
        origin="lower",
        extent=ext,
        cmap=cm,
        norm=BoundaryNorm(np.arange(-0.5, len(cols)), len(cols)),
        interpolation="nearest",
    )
    ax.legend(
        handles=[
            Patch(facecolor=c, edgecolor="#555", label=t)
            for c, t in zip(cols, lay.class_labels, strict=True)
        ],
        fontsize=7,
        loc="lower left",
        title=f"{lay.label} [{lay.units}]",
        title_fontsize=7,
    )
    ax.set_xlabel("km east of the grid origin")
    ax.set_ylabel("km north of the grid origin")
    ax.set_aspect("equal")
    ax.set_title(
        f"{lay.label}   [{ind.input_class} INDICATOR]\n{ind.period[0]} to {ind.period[1]}",
        fontsize=9,
    )
    srcs = []
    for k in ("guidance", "frequency", "climatology"):
        d = lay.provenance.get(k)
        if d:
            srcs.append(f"{k}: {d.get('file')} (vintage: {d.get('vintage', 'not given')})")
    foot = PRODUCT_TEXT + "  " + "; ".join(srcs) + "  " + NOT_MODELLED
    ax.annotate(
        textwrap.fill(foot, 105),
        (0.0, -0.09),
        xycoords="axes fraction",
        va="top",
        fontsize=6.3,
        color="#444",
    )
    return fig


def to_dataset(ind: IndicatorResult):
    """CF NetCDF grid of the indicators with the labels and inputs as attributes."""
    import json

    import pyproj
    import xarray as xr

    g = ind.grid
    lon, lat = g.lonlat()
    crs = pyproj.CRS.from_proj4(g.proj4())
    ds = xr.Dataset(coords={"y": g.y, "x": g.x})
    ds["y"].attrs = {"standard_name": "projection_y_coordinate", "units": "m", "axis": "Y"}
    ds["x"].attrs = {"standard_name": "projection_x_coordinate", "units": "m", "axis": "X"}
    ds["lat"] = (
        ("y", "x"),
        lat.astype(np.float32),
        {"standard_name": "latitude", "units": "degrees_north"},
    )
    ds["lon"] = (
        ("y", "x"),
        lon.astype(np.float32),
        {"standard_name": "longitude", "units": "degrees_east"},
    )
    ds = ds.set_coords(["lat", "lon"])
    ds["crs"] = xr.DataArray(
        np.int32(0),
        attrs={
            "grid_mapping_name": "azimuthal_equidistant",
            "latitude_of_projection_origin": float(g.origin[0]),
            "longitude_of_projection_origin": float(g.origin[1]),
            "false_easting": 0.0,
            "false_northing": 0.0,
            "crs_wkt": crs.to_wkt(),
        },
    )
    common = {
        "provenance_type": ind.input_class,
        "product_label": f"{ind.input_class} INDICATOR",
        "product_text": PRODUCT_TEXT,
        "not_modelled": NOT_MODELLED,
        "period": "/".join(ind.period),
        "grid_mapping": "crs",
        "coordinates": "lat lon",
    }
    ds["rainfall"] = (
        ("y", "x"),
        ind.rain_mm,
        {**common, "long_name": f"rainfall of the period ({ind.rain_field})", "units": "mm"},
    )
    for name, lay in ind.layers.items():
        attrs = {
            **common,
            "long_name": lay.label,
            "units": lay.units,
            "indicator_parameters": json.dumps(lay.provenance, default=str, sort_keys=True),
        }
        ds[name] = (("y", "x"), lay.values, attrs)
        ds[f"{name}_class"] = (
            ("y", "x"),
            lay.classes,
            {
                **common,
                "long_name": f"{lay.label}, class",
                "class_labels": " | ".join(lay.class_labels),
                "comment": "-1 = no value (missing input)",
            },
        )
        if lay.flag is not None:
            ds[f"{name}_flag"] = (
                ("y", "x"),
                lay.flag,
                {
                    **common,
                    "long_name": "-1 below the smallest supplied period, +1 above the largest, 0 inside",  # noqa: E501
                },
            )
    ds.attrs = {
        "Conventions": "CF-1.10",
        "title": f"Flash-flood indicators ({ind.input_class} indicator)",
        "history": f"{ind.created} created by {prov.software_string()}",
        "source": ", ".join(ind.inputs),
        "comment": PRODUCT_TEXT,
    }
    return ds


def run(
    result: Any,
    *,
    duration_h: float = 1.0,
    input_class: str = "RETRIEVED",
    **inputs: Any,
) -> IndicatorResult:
    """Read and regrid the files (``guidance=``, ``frequency=[(years, file), ...]``,
    ``climatology=``
    and their ``*_units`` / ``*_variable``, ``vintage``, ``max_distance_km``: see
    :func:`load_inputs`)
    and compute the indicators of a QPE result."""
    kw = load_inputs(result.grid, **inputs)
    return compute(result, duration_h=duration_h, input_class=input_class, **kw)

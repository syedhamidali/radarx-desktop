"""Outputs of a QPE result: CF NetCDF, map picture, figure, tables - all with provenance.

Every field carries ``provenance_type`` (``RETRIEVED``, or ``ADJUSTED`` for a gauge-adjusted
field), the method, its parameters and the uncertainty model, as variable attributes; the whole
record is also in the global attribute ``provenance`` (JSON). An estimate is never labelled as an
observation.
"""

from __future__ import annotations

import io
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from engine import cfwrite
from engine import provenance as prov
from engine.hydro import qpe as hqpe
from engine.hydro.accumulate import QpeResult

#: name -> (label, units, default colour range, colormap, "kind")
FIELDS: dict[str, tuple[str, str, tuple[float, float], str, str]] = {
    "accumulation": ("Rainfall accumulation (radar estimate)", "mm", (0.0, 50.0), "viridis", "sum"),
    "accumulation_adjusted": (
        "Rainfall accumulation (gauge-adjusted)",
        "mm",
        (0.0, 50.0),
        "viridis",
        "sum",
    ),
    "accumulation_sigma": ("Accumulation uncertainty (1 sigma)", "mm", (0.0, 25.0), "magma", "sum"),
    "rate_max": ("Maximum rain rate", "mm/h", (0.0, 100.0), "YlGnBu", "max"),
    "rate_last": ("Rain rate at the end", "mm/h", (0.0, 100.0), "YlGnBu", "inst"),
    "valid_fraction": ("Fraction of the period with data", "1", (0.0, 1.0), "cividis", "mean"),
    "quality": ("Quality index (0-1)", "1", (0.0, 1.0), "cividis", "mean"),
    "beam_height_agl_m": ("Beam height above ground/radar", "m", (0.0, 4000.0), "plasma", "mean"),
    "range_km": ("Range from the serving radar", "km", (0.0, 230.0), "plasma", "mean"),
    "adjustment_factor": ("Gauge adjustment factor", "1", (0.0, 2.0), "PuOr_r", "inst"),
}


def field_array(result: QpeResult, name: str) -> np.ndarray:
    arr = getattr(result, "accumulation_sigma" if name == "accumulation_sigma" else name, None)
    if name == "adjustment_factor":
        arr = result.adjustment_factor
    if arr is None:
        raise hqpe.QpeError(f"the result has no field {name!r}")
    return np.asarray(arr)


def _hide_dry(arr: np.ndarray, name: str) -> np.ndarray:
    """Amounts and rates below 0.05 are drawn as "no rain" (transparent)."""
    if FIELDS[name][4] in ("sum", "max", "inst") and name != "adjustment_factor":
        a = np.asarray(arr, dtype=np.float32)
        return np.where(a >= 0.05, a, np.nan)
    return np.asarray(arr)


def default_range(result: QpeResult, name: str) -> tuple[float, float]:
    """Colour range from the data for amounts and rates, fixed for fractions and indexes."""
    lo, hi = FIELDS[name][2]
    if name in (
        "accumulation",
        "accumulation_adjusted",
        "accumulation_sigma",
        "rate_max",
        "rate_last",
    ):
        a = field_array(result, name)
        fin = a[np.isfinite(a) & (a > 0)]
        if fin.size:
            top = float(np.percentile(fin, 99.5))
            # round to a readable value (1, 2, 5 x 10^n)
            mag = 10 ** math.floor(math.log10(max(top, 1e-3)))
            for m in (1, 2, 5, 10):
                if top <= m * mag:
                    return 0.0, float(m * mag)
        return lo, hi
    return lo, hi


# ---------------------------------------------------------------------------
# NetCDF (CF-1.10 grid)
# ---------------------------------------------------------------------------


def _prov_attrs(result: QpeResult, adjusted: bool) -> dict[str, Any]:
    p = result.provenance(adjusted=adjusted)
    attrs = {
        "provenance_type": p["type"],
        "retrieval_method": str(p["method"]),
        "retrieval_parameters": json.dumps(p["parameters"], default=str, sort_keys=True),
        "uncertainty_model": p["uncertainty"]["model"],
    }
    if adjusted and result.adjusted:
        attrs["adjustment_method"] = str(result.adjusted["method"])
        attrs["adjustment_gauge_source"] = str(result.adjusted["gauge_source"])
        attrs["adjustment_n_gauges"] = int(result.adjusted["n_gauges"])
    return attrs


def to_dataset(result: QpeResult) -> xr.Dataset:
    """A CF-1.10 grid dataset: AEQD ``x``/``y`` (m), 2-D ``lat``/``lon``, ``crs``, time bounds."""
    import pyproj

    grid = result.grid
    ny, nx = grid.shape
    lon, lat = grid.lonlat()
    crs = pyproj.CRS.from_proj4(grid.proj4())
    ds = xr.Dataset(coords={"y": grid.y, "x": grid.x})
    ds["y"].attrs = {
        "standard_name": "projection_y_coordinate",
        "long_name": "y of cell centre",
        "units": "m",
        "axis": "Y",
    }
    ds["x"].attrs = {
        "standard_name": "projection_x_coordinate",
        "long_name": "x of cell centre",
        "units": "m",
        "axis": "X",
    }
    ds["lat"] = (
        ("y", "x"),
        lat.astype(np.float32),
        {
            "standard_name": "latitude",
            "long_name": "latitude of cell centre",
            "units": "degrees_north",
        },
    )
    ds["lon"] = (
        ("y", "x"),
        lon.astype(np.float32),
        {
            "standard_name": "longitude",
            "long_name": "longitude of cell centre",
            "units": "degrees_east",
        },
    )
    ds = ds.set_coords(["lat", "lon"])
    ds["crs"] = xr.DataArray(
        np.int32(0),
        attrs={
            "grid_mapping_name": "azimuthal_equidistant",
            "latitude_of_projection_origin": float(grid.origin[0]),
            "longitude_of_projection_origin": float(grid.origin[1]),
            "false_easting": 0.0,
            "false_northing": 0.0,
            "semi_major_axis": 6378137.0,
            "inverse_flattening": 298.257223563,
            "long_name": "azimuthal equidistant grid of the analysis",
            "crs_wkt": crs.to_wkt(),
        },
    )
    t0 = np.datetime64(result.start, "ns")
    t1 = np.datetime64(result.end, "ns")
    ds = ds.assign_coords(time=t1)
    ds["time"].attrs = {
        "standard_name": "time",
        "long_name": "end of the accumulation period (UTC)",
        "bounds": "time_bounds",
    }
    ds["time_bounds"] = (
        ("bnds",),
        np.array([t0, t1], dtype="datetime64[ns]"),
        {"long_name": "start and end of the accumulation period (UTC)"},
    )
    ref = provenance_ref(t0)
    adjusted_present = result.accumulation_adjusted is not None
    base = _prov_attrs(result, False)
    names = {
        "accumulation": dict(
            long_name="rainfall accumulation, liquid-water equivalent, radar estimate",
            units="mm",
            standard_name="lwe_thickness_of_precipitation_amount",
            cell_methods="time: sum",
            comment=(
                "Integral of the composite rain rate over the period (trapezoid rule with "
                "advection-corrected interpolation between scans); time without data is not "
                "counted - see valid_time_fraction."
            ),
            **base,
        ),
        "accumulation_uncertainty": dict(
            long_name="1-sigma uncertainty of the accumulation",
            units="mm",
            comment=(
                "Errors taken as fully correlated in time (upper bound). Covers DSD "
                "variability and calibration/KDP noise only; see the uncertainty_model."
            ),
            **base,
        ),
        "rain_rate_max": dict(
            long_name="maximum composite rain rate during the period",
            units="mm h-1",
            standard_name="lwe_precipitation_rate",
            cell_methods="time: maximum",
            **base,
        ),
        "rain_rate_last": dict(
            long_name="composite rain rate at the end of the period",
            units="mm h-1",
            standard_name="lwe_precipitation_rate",
            **base,
        ),
        "valid_time_fraction": {
            "long_name": "fraction of the period with a radar estimate",
            "units": "1",
            "valid_min": np.float32(0),
            "valid_max": np.float32(1),
        },
        "quality_index": {
            "long_name": "beam quality index, time mean",
            "units": "1",
            "comment": "exp(-h/H0) exp(-(s/S0)^2) (1 - blockage); an index, not a probability",
            "valid_min": np.float32(0),
            "valid_max": np.float32(1),
        },
        "beam_height": {
            "long_name": "height of the beam centre above ground (above the radar without a DEM)",
            "units": "m",
            "comment": "Effective-Earth-radius beam model, k_e = 4/3",
        },
        "range_to_radar": {
            "long_name": "ground distance to the serving radar, time mean",
            "units": "km",
        },
    }
    data = {
        "accumulation": result.accumulation,
        "accumulation_uncertainty": result.accumulation_sigma,
        "rain_rate_max": result.rate_max,
        "rain_rate_last": result.rate_last,
        "valid_time_fraction": result.valid_fraction,
        "quality_index": result.quality,
        "beam_height": result.beam_height_agl_m,
        "range_to_radar": result.range_km,
    }
    if adjusted_present:
        names["accumulation_adjusted"] = dict(
            long_name="rainfall accumulation, liquid-water equivalent, adjusted to rain gauges",
            units="mm",
            standard_name="lwe_thickness_of_precipitation_amount",
            cell_methods="time: sum",
            comment=(
                "Radar accumulation adjusted to gauge totals; an estimate that merges two "
                "imperfect measurements, not an observation."
            ),
            **_prov_attrs(result, True),
        )
        data["accumulation_adjusted"] = result.accumulation_adjusted
        names["adjustment_factor"] = dict(
            long_name="adjusted / radar accumulation",
            units="1",
            comment="1 where the radar accumulation is below 0.1 mm",
            **_prov_attrs(result, True),
        )
        data["adjustment_factor"] = result.adjustment_factor
    for name, arr in data.items():
        a = np.asarray(arr, dtype=np.float32)
        ds[name] = (("y", "x"), a, {**names[name], "grid_mapping": "crs", "coordinates": "lat lon"})
        ds[name].encoding = cfwrite.field_encoding(a.shape)
    ds["accumulation"].attrs["ancillary_variables"] = (
        "accumulation_uncertainty valid_time_fraction quality_index"
    )
    if adjusted_present:
        ds["accumulation_adjusted"].attrs["ancillary_variables"] = "adjustment_factor"
    flags = np.array(sorted(hqpe.FLAG_MEANINGS), dtype=np.uint8)
    ds["quality_flags"] = (
        ("y", "x"),
        result.flags.astype(np.uint8),
        {
            "long_name": "quality flags, set if present at any time",
            "flag_masks": flags,
            "flag_meanings": " ".join(hqpe.FLAG_MEANINGS[int(f)] for f in flags),
            "grid_mapping": "crs",
            "coordinates": "lat lon",
            "comment": "bit field; set where >= 25 % of the gates of the serving beam have it",
        },
    )
    ds["quality_flags"].encoding = {
        "dtype": "uint8",
        "_FillValue": None,
        **cfwrite.compression_encoding((ny, nx)),
    }
    ds["n_radars"] = (
        ("y", "x"),
        result.n_radars.astype(np.uint8),
        {
            "long_name": "largest number of radars with an estimate in the cell",
            "units": "1",
            "grid_mapping": "crs",
            "coordinates": "lat lon",
        },
    )
    ds["n_radars"].encoding = {
        "dtype": "uint8",
        "_FillValue": np.uint8(255),
        **cfwrite.compression_encoding((ny, nx)),
    }
    # radars
    nrad = len(result.radars)
    ds["radar_latitude"] = (
        ("radar",),
        np.array([r["latitude"] for r in result.radars], dtype=np.float64),
        {"standard_name": "latitude", "units": "degrees_north", "long_name": "radar latitude"},
    )
    ds["radar_longitude"] = (
        ("radar",),
        np.array([r["longitude"] for r in result.radars], dtype=np.float64),
        {"standard_name": "longitude", "units": "degrees_east", "long_name": "radar longitude"},
    )
    ds["radar_altitude"] = (
        ("radar",),
        np.array([r["altitude_m"] for r in result.radars], dtype=np.float64),
        {
            "standard_name": "altitude",
            "units": "m",
            "long_name": "radar altitude above mean sea level",
            "positive": "up",
        },
    )
    ds["radar_scans"] = (
        ("radar",),
        np.array([r["scans"] for r in result.radars], dtype=np.int32),
        {"long_name": "number of volumes used", "units": "1"},
    )
    ds = cfwrite.set_char_coord(
        ds, "radar_name", "radar", [r["name"] for r in result.radars], "radar name"
    )
    ds["radar_name"].attrs["cf_role"] = (
        "timeseries_id" if nrad == 0 else ds["radar_name"].attrs.get("cf_role", "")
    )
    if not ds["radar_name"].attrs.get("cf_role"):
        ds["radar_name"].attrs.pop("cf_role", None)
    ds["time"].encoding = {
        "units": f"seconds since {ref}",
        "calendar": "standard",
        "dtype": "float64",
        "_FillValue": None,
    }
    ds["time_bounds"].encoding = {
        "units": f"seconds since {ref}",
        "calendar": "standard",
        "dtype": "float64",
        "_FillValue": None,
    }
    p = result.provenance(adjusted=adjusted_present)
    ds.attrs = prov.standard_global_attrs(
        title="Radar quantitative precipitation estimate (accumulation)",
        source_files=result.source_files,
        action="hydrology qpe",
        params={"method": p["method"], **result.options},
        source="quantitative precipitation estimation from "
        + ", ".join(r["name"] for r in result.radars)
        + " with radarx relations (RETRIEVED, not an observation)",
        references=(
            "Fulton et al. 1998 doi:10.1175/1520-0434(1998)013<0377:TWRA>2.0.CO;2; "
            "Giangrande and Ryzhkov 2008 doi:10.1175/2008JAMC1753.1; "
            "Ryzhkov et al. 2014 doi:10.1175/JTECH-D-13-00038.1; "
            "Gal-Chen 1982 doi:10.1175/1520-0469(1982)039<2279:EIFAMF>2.0.CO;2; "
            "CF Conventions https://cfconventions.org/"
        ),
        comment=" ".join(result.notes[:6]),
        conventions="CF-1.10",
        extra={
            "provenance": json.dumps(p, default=str, sort_keys=True),
            "provenance_type": p["type"],
            "time_coverage_start": prov.iso_utc(result.start),
            "time_coverage_end": prov.iso_utc(result.end),
            "geospatial_lat_min": float(np.nanmin(lat)),
            "geospatial_lat_max": float(np.nanmax(lat)),
            "geospatial_lon_min": float(np.nanmin(lon)),
            "geospatial_lon_max": float(np.nanmax(lon)),
            "grid_resolution_m": float(grid.cell_m),
        },
    )
    return ds


def provenance_ref(t: np.datetime64) -> str:
    return prov.iso_utc(t)


def write_netcdf(result: QpeResult, path: str | Path) -> Path:
    """Write the CF NetCDF4 file (see :func:`to_dataset`)."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    to_dataset(result).to_netcdf(p, engine="netcdf4", format="NETCDF4")
    return p


# ---------------------------------------------------------------------------
# Picture on the map (reuses the mosaic renderer and its map layer)
# ---------------------------------------------------------------------------


def render_png(
    result: QpeResult,
    name: str = "accumulation",
    vmin: float | None = None,
    vmax: float | None = None,
) -> dict[str, Any]:
    """RGBA PNG (Web-Mercator, transparent without data) of a field and what the map needs."""
    from engine import mosaic

    field = _hide_dry(field_array(result, name), name)
    lay = result.grid.layout
    if lay is None:
        raise hqpe.QpeError("this grid has no map layout (it was not built with make_grid)")
    lo, hi = default_range(result, name)
    lo = lo if vmin is None else float(vmin)
    hi = hi if vmax is None else float(vmax)
    if hi <= lo:
        raise hqpe.QpeError("vmax must be greater than vmin")
    image = mosaic._resample_to_image(np.asarray(field, dtype=np.float32), lay)
    lut, cmap = mosaic._build_lut(FIELDS[name][3])
    png = mosaic.render_png(image, lut, lo, hi)
    finite = image[np.isfinite(image)]
    return {
        "png": png,
        "image": image,
        "vmin": lo,
        "vmax": hi,
        "cmap": cmap,
        "legend": mosaic._legend(lut, lo, hi),
        "units": FIELDS[name][1],
        "label": FIELDS[name][0],
        "bbox": result.grid.bbox,
        "corners": [
            [result.grid.bbox[0], result.grid.bbox[3]],
            [result.grid.bbox[2], result.grid.bbox[3]],
            [result.grid.bbox[2], result.grid.bbox[1]],
            [result.grid.bbox[0], result.grid.bbox[1]],
        ],
        "stats": {
            "valid_fraction": float(finite.size / image.size) if image.size else 0.0,
            "min": float(finite.min()) if finite.size else None,
            "max": float(finite.max()) if finite.size else None,
            "mean": float(finite.mean()) if finite.size else None,
        },
        "image_shape": list(image.shape),
    }


def mosaic_record(result: QpeResult, name: str = "accumulation", **kw: Any):
    """A :class:`engine.mosaic.MosaicRecord` of one field so the app's map layer, probe and PNG
    export work on it unchanged."""
    from engine import mosaic

    r = render_png(result, name, **kw)
    grid = result.grid
    ny, nx = grid.shape
    stats = [
        mosaic.RadarStat(
            f"qpe-{x['name']}",  # unique keys for the map layer / panel lists
            x["name"],
            x["latitude"],
            x["longitude"],
            x["altitude_m"],
            x["first"],
        )
        for x in result.radars
    ]
    label = f"{FIELDS[name][0]} [{'ADJUSTED' if name.endswith('adjusted') else 'RETRIEVED'}]"
    return mosaic.MosaicRecord(
        mosaic_id=__import__("uuid").uuid4().hex[:12],
        product="qpe",
        variable=label,
        units=FIELDS[name][1],
        height_m=None,
        threshold_dbz=None,
        resolution_m=grid.cell_m,
        image=r["image"],
        bbox=grid.bbox,
        origin=grid.origin,
        x0=float(grid.x[0]),
        y0=float(grid.y[0]),
        cell_m=grid.cell_m,
        radar_values=np.full((len(stats), ny, nx), np.nan, dtype=np.float32),
        level_m=np.nan_to_num(result.beam_height_agl_m, nan=0.0).astype(np.float32),
        radars=stats,
        overlap_fraction=0.0,
        vmin=r["vmin"],
        vmax=r["vmax"],
        cmap=r["cmap"],
        legend=r["legend"],
        png=r["png"],
        stats=r["stats"],
        analysis_time=prov.iso_utc(result.end),
        merge={"applied": False, "qpe": True},
        grid_info={
            "nx": r["image_shape"][1],
            "ny": r["image_shape"][0],
            "resolution_m": grid.cell_m,
            "west": grid.bbox[0],
            "south": grid.bbox[1],
            "east": grid.bbox[2],
            "north": grid.bbox[3],
            "cells_aeqd": [nx, ny],
            "levels": 1,
        },
        warnings=list(result.notes),
    )


# ---------------------------------------------------------------------------
# Figure
# ---------------------------------------------------------------------------


def figure(
    result: QpeResult,
    name: str = "accumulation",
    *,
    vmin: float | None = None,
    vmax: float | None = None,
    dpi: int = 150,
):
    """A matplotlib map of a field (km from the grid origin) with provenance in the title."""
    import matplotlib

    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt

    label, units, _, cmap, _ = FIELDS[name]
    arr = _hide_dry(field_array(result, name), name)
    lo, hi = default_range(result, name)
    lo = lo if vmin is None else vmin
    hi = hi if vmax is None else vmax
    g = result.grid
    ext = [
        (g.x[0] - g.cell_m / 2) / 1e3,
        (g.x[-1] + g.cell_m / 2) / 1e3,
        (g.y[0] - g.cell_m / 2) / 1e3,
        (g.y[-1] + g.cell_m / 2) / 1e3,
    ]
    fig, ax = plt.subplots(figsize=(7.2, 6.4), constrained_layout=True)
    cm = plt.get_cmap(cmap).copy()
    cm.set_bad("#eef0f2")
    im = ax.imshow(
        np.ma.masked_invalid(arr),
        origin="lower",
        extent=ext,
        cmap=cm,
        vmin=lo,
        vmax=hi,
        interpolation="nearest",
    )
    cb = fig.colorbar(im, ax=ax, shrink=0.85, pad=0.02)
    cb.set_label(f"{label} [{units}]")
    # radar sites and 50 km rings
    import pyproj

    tr = pyproj.Transformer.from_crs("EPSG:4326", g.proj4(), always_xy=True)
    for r in result.radars:
        x, y = tr.transform(r["longitude"], r["latitude"])
        ax.plot(x / 1e3, y / 1e3, "k^", ms=6)
        ax.annotate(
            r["name"], (x / 1e3, y / 1e3), textcoords="offset points", xytext=(5, 4), fontsize=8
        )
    ax.set_xlabel("km east of the grid origin")
    ax.set_ylabel("km north of the grid origin")
    ax.set_aspect("equal")
    kind = "ADJUSTED" if name.endswith("adjusted") else "RETRIEVED"
    ax.set_title(
        f"{label}\n{prov.iso_utc(result.start)} to {prov.iso_utc(result.end)}   "
        f"[{kind}: an estimate, not an observation]",
        fontsize=9,
    )
    method = result.config.get("method_text") or result.config.get("method")
    import textwrap

    ax.annotate(
        textwrap.fill(f"method: {method}", 92),
        (0.0, -0.1),
        xycoords="axes fraction",
        va="top",
        fontsize=6.5,
        color="#555",
    )
    return fig


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def basin_table_csv(result: QpeResult) -> str:
    """CSV of the basin statistics (one row per basin) with a provenance comment block."""
    bs = result.basin_series
    if bs is None:
        raise hqpe.QpeError("no basins were given")
    rows = bs.summary()
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    buf = io.StringIO()
    buf.write(
        prov.csv_comment_block(
            title="Basin rainfall statistics ("
            + ("ADJUSTED to gauges" if bs.adjusted else "RETRIEVED radar estimate")
            + ")",
            source_files=result.source_files,
            params={"period": f"{prov.iso_utc(result.start)}/{prov.iso_utc(result.end)}"},
            extra={"provenance_type": "ADJUSTED" if bs.adjusted else "RETRIEVED"},
        )
    )
    buf.write(",".join(keys) + "\n")
    for r in rows:
        buf.write(
            ",".join(
                ""
                if r.get(k) is None
                else (f"{r[k]:.6g}" if isinstance(r[k], float) else str(r[k]).replace(",", ";"))
                for k in keys
            )
            + "\n"
        )
    return buf.getvalue()


def basin_series_csv(result: QpeResult, basin_index: int = 0) -> str:
    bs = result.basin_series
    if bs is None:
        raise hqpe.QpeError("no basins were given")
    rows = bs.rows(basin_index)
    buf = io.StringIO()
    buf.write(
        f"# basin: {bs.weights[basin_index].basin.id}; provenance: "
        f"{'ADJUSTED' if bs.adjusted else 'RETRIEVED'}\n"
    )
    buf.write("time,mean_rate_mm_h,peak_cell_rate_mm_h,coverage,cumulative_mm\n")
    for r in rows:
        mean, peak, cum = r["mean_rate_mm_h"], r["peak_cell_rate_mm_h"], r["cumulative_mm"]
        buf.write(
            f"{r['time']},{'' if mean is None else format(mean, '.4f')},"
            f"{'' if peak is None else format(peak, '.3f')},"
            f"{r['coverage']:.3f},{'' if cum is None else format(cum, '.4f')}\n"
        )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Saving by extension
# ---------------------------------------------------------------------------


def default_field(result: QpeResult) -> str:
    """The field a picture shows by default: the adjusted accumulation when there is one."""
    return "accumulation_adjusted" if result.accumulation_adjusted is not None else "accumulation"


def save(result: QpeResult, out: Any, *, field: str | None = None) -> list[Path]:
    """Write ``out`` (a path or a list of paths) by extension and return every file written.

    ``.nc`` CF NetCDF; ``.png`` colour image of ``field`` plus ``.pgw`` / ``.prj`` for GIS;
    ``.json`` the summary (provenance, statistics, basins, validation); ``.csv`` the basin table
    (and ``<name>_<basin>_series.csv`` with the basin series); ``.pdf`` / ``.svg`` a figure.
    """
    from engine import mosaic

    items = [out] if isinstance(out, (str, Path)) else list(out)
    written: list[Path] = []
    name = field or default_field(result)
    for item in items:
        path = Path(item).expanduser()
        ext = path.suffix.lower()
        path.parent.mkdir(parents=True, exist_ok=True)
        if ext == ".nc":
            written.append(write_netcdf(result, path))
        elif ext == ".png":
            rec = mosaic_record(result, name)
            path.write_bytes(mosaic.png_bytes(rec))
            path.with_suffix(".pgw").write_text(mosaic.world_file(rec), encoding="ascii")
            path.with_suffix(".prj").write_text(mosaic.projection_wkt(), encoding="ascii")
            written += [path, path.with_suffix(".pgw"), path.with_suffix(".prj")]
        elif ext == ".json":
            path.write_text(json.dumps(_jsonable(result.to_dict()), indent=2), encoding="utf-8")
            written.append(path)
        elif ext == ".csv":
            path.write_text(basin_table_csv(result), encoding="utf-8")
            written.append(path)
            bs = result.basin_series
            for k, bw in enumerate(bs.weights if bs is not None else []):
                sp = path.with_name(f"{path.stem}_{_slug(bw.basin.id)}_series.csv")
                sp.write_text(basin_series_csv(result, k), encoding="utf-8")
                written.append(sp)
        elif ext in (".pdf", ".svg"):
            import matplotlib.pyplot as plt

            fig = figure(result, name)
            fig.savefig(path)
            plt.close(fig)
            written.append(path)
        else:
            raise hqpe.QpeError(
                f"cannot write {path.name}: use .nc (NetCDF), .png (image + world file), "
                ".json (summary), .csv (basin table) or .pdf/.svg (figure)"
            )
    return written


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9]+", "_", str(text)).strip("_") or "basin"


def _jsonable(obj: Any) -> Any:
    """Plain Python for ``json.dump`` (numpy scalars, NaN -> null)."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, (np.ndarray,)):
        return _jsonable(obj.tolist())
    return obj


def series_figure(result: QpeResult, bin_minutes: float = 60.0, max_basins: int = 6):
    """Basin-mean rain rate (line), hyetograph bars and cumulative depth.

    One panel per basin (at most ``max_basins``); the title carries the provenance label.
    """
    import matplotlib

    matplotlib.use("Agg", force=False)
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    bs = result.basin_series
    if bs is None:
        raise hqpe.QpeError("no basins were given")
    n = min(len(bs.weights), max_basins)
    fig, axes = plt.subplots(n, 1, figsize=(7.4, 2.6 * n + 0.8), sharex=True, squeeze=False)
    t = np.array(bs.times, dtype="datetime64[ns]").astype("datetime64[s]").astype(object)
    label = "ADJUSTED" if bs.adjusted else "RETRIEVED"
    for k in range(n):
        ax = axes[k, 0]
        ax2 = ax.twinx()
        hy = bs.hyetograph(k, bin_minutes)
        starts = [np.datetime64(s.rstrip("Z"), "s").astype(object) for s in hy["start"]]
        ends = [np.datetime64(s.rstrip("Z"), "s").astype(object) for s in hy["end"]]
        widths = [(e - s).total_seconds() / 86400.0 for s, e in zip(starts, ends, strict=True)]
        ax.bar(
            starts,
            hy["intensity_mm_h"],
            width=widths,
            align="edge",
            color="#9ecae1",
            alpha=0.7,
            label=f"{bin_minutes:g} min mean intensity",
        )
        ax.plot(t, bs.mean_rate[k], color="#08519c", lw=1.6, label="basin-mean rate")
        cum = ax2.plot(t, bs.cum_mean[k], color="#d95f02", lw=1.6, label="cumulative depth (mean)")
        ax.set_ylabel("rain rate [mm/h]")
        ax2.set_ylabel("depth [mm]")
        ax2.set_zorder(ax.get_zorder() + 1)
        ax2.patch.set_visible(False)
        s = bs.stats[k]
        ax.set_title(
            f"{s['id']}: {s['area_km2']:,.0f} km2, mean accumulation "
            f"{(s.get('accumulation_mean_mm') or 0):.1f} mm, max cell rate "
            f"{(s.get('max_cell_rate_mm_h') or 0):.0f} mm/h "
            f"[{label}: an estimate, not an observation]",
            fontsize=8.5,
        )
        if k == 0:
            h1, l1 = ax.get_legend_handles_labels()
            h2, l2 = ax2.get_legend_handles_labels()
            ax.legend(h1 + h2, l1 + l2, fontsize=7, loc="upper left", frameon=False)
        del cum
    axes[-1, 0].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    axes[-1, 0].set_xlabel("time (UTC)")
    fig.tight_layout()
    return fig

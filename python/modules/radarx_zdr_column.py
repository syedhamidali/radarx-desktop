"""radarx: ZDR column depth above the freezing level (Snyder et al. 2015)."""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np

from engine.plugins import ModuleError, Result, module, param, polygon


def _load_adapter():
    """Import the sibling ``_radarx_adapter.py`` (modules are loaded by path)."""
    name = "xradar_module_radarx_adapter"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).with_name("_radarx_adapter.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


A = _load_adapter()
MAX_COLUMNS = 40
MAX_CELLS = 120e6  # grid points (x * y * z) allowed before the request is refused

NOTES = """\
ZDR column detection of Snyder et al. (2015, section 3), radarx.retrieve.zdr_column. Drops \
lofted by an updraft above the freezing level freeze slowly, leaving a column of ZDR >= 1 dB \
above the 0 C level whose depth follows updraft strength. At every horizontal grid point the \
module counts the vertically consecutive grid levels with ZDR >= threshold starting at the \
first level at or above the freezing level; the depth is the sum of the layer thicknesses. \
A column that does not reach the freezing level, or whose first level is missing or below \
the threshold, has depth 0. Points with RHOHV below 0.8 (hail spikes, depolarization streaks) \
are dropped. A minimum-reflectivity condition (20 dBZ by default) is a radarx addition that \
keeps the noise of weak echo out - the paper has none.

The volume is first gridded to a Cartesian 3-D grid with radarx's cone gridding \
(radarx.grid.grid_cones; ZDR, RHOHV, DBZH), so the column depth is a Cartesian product. It is \
shown on the selected sweep's polar grid by sampling the grid at each gate's ground position, \
and the column objects (connected areas deeper than the minimum depth) are outlined in x/y \
metres. The freezing level is NOT read from the file: enter it (m above sea level, e.g. from a \
sounding). Results are sensitive to it and to the ZDR calibration (a negative bias shortens \
columns). Depth can only be seen where sweeps cover levels above the freezing level - near \
the radar a column goes through the top of the highest sweep, and at long range the lowest \
beam may already be above the freezing level. Paper uses 250 m vertical spacing; the paper's \
Gaussian smoothing of the depth is not applied. KDP columns are not included."""


@module(
    id="radarx_zdr_column",
    title="ZDR Column (radarx)",
    description=(
        "radarx.retrieve.zdr_column / zdr_column_metrics: depth of the ZDR >= 1 dB column "
        "above the freezing level, an updraft indicator (Snyder et al. 2015). Grids the "
        "volume to 3-D first; you supply the freezing level."
    ),
    category="Storm",
    requires=["ZDR"],
    optional=["DBZH", "RHOHV"],
    needs=A.NEEDS,
    params=[
        param(
            "freezing_level_m",
            "number",
            4000.0,
            "Freezing level (m ASL)",
            min=500,
            max=7000,
            step=100,
            help="Height of the 0 °C level above sea level, e.g. from a sounding",
        ),
        param("threshold", "number", 1.0, "ZDR threshold (dB)", min=0.25, max=3, step=0.25),
        param("min_rhohv", "number", 0.8, "Min RHOHV", min=0.5, max=1.0, step=0.01),
        param(
            "min_dbz",
            "number",
            20.0,
            "Min reflectivity (dBZ, -30 = off)",
            min=-30,
            max=50,
            step=1,
            help="radarx addition: weak echo with ZDR >= 1 dB is noise, not an updraft",
        ),
        param(
            "min_depth_m",
            "number",
            1000.0,
            "Column object min depth (m)",
            min=250,
            max=5000,
            step=250,
        ),
        param(
            "min_area_km2",
            "number",
            4.0,
            "Column object min area (km²)",
            min=0,
            max=200,
            step=1,
            help="Smaller objects are ignored (single-pixel noise)",
        ),
        param(
            "grid_spacing_m",
            "number",
            500.0,
            "Horizontal grid spacing (m)",
            min=250,
            max=1000,
            step=250,
            help="Smaller = slower and more memory",
        ),
        param(
            "z_step_m",
            "number",
            250.0,
            "Vertical grid spacing (m)",
            min=250,
            max=500,
            step=250,
            help="Snyder et al. use 250 m; column depth is quantised to this",
        ),
        param(
            "max_range_km",
            "number",
            100.0,
            "Grid radius (km)",
            min=20,
            max=200,
            step=10,
            help="Cartesian grid extent around the radar",
        ),
    ],
    version="1.0",
)
def run(
    ctx,
    freezing_level_m,
    threshold,
    min_rhohv,
    min_dbz,
    min_depth_m,
    min_area_km2,
    grid_spacing_m,
    z_step_m,
    max_range_km,
):
    rt = A.rx()
    from radarx.grid import grid_cones

    ds = A.prepare_sweep(ctx)
    tree = ctx.datatree
    names = A.sweep_names(tree)
    if len(names) < 3:
        raise ModuleError("A ZDR column needs a volume with several sweeps; this file has few")
    wanted = ["ZDR"]
    if ctx.has("RHOHV"):
        wanted.append("RHOHV")
    use_dbz = min_dbz > -30 and ctx.has("DBZH")
    if use_dbz:
        wanted.append("DBZH")
    # Field names of the lowest dual-pol sweep (aliases such as differential_reflectivity)
    variables = {w: ctx.find(w).name for w in wanted}

    z0 = math.ceil(A.site_altitude(ds) / z_step_m) * z_step_m
    z_top = freezing_level_m + 7000.0
    z = np.arange(z0, z_top + z_step_m, z_step_m, dtype=np.float64)
    r = float(max_range_km) * 1000.0
    n_xy = int(2 * r / grid_spacing_m) + 1
    if n_xy * n_xy * z.size > MAX_CELLS:
        raise ModuleError(
            "The requested grid is too large; increase the grid spacing or reduce the radius"
        )
    x = np.arange(-n_xy // 2 + 1, n_xy // 2 + 1, dtype=np.float64) * grid_spacing_m
    y = x.copy()

    ctx.progress(15, f"Gridding the volume ({n_xy} x {n_xy} x {z.size})")
    with A.captured_warnings() as warns, A.guard("ZDR column"):
        grid = grid_cones(tree, list(variables.values()), x=x, y=y, z=z)
        ctx.progress(60, "Detecting ZDR columns")
        kwargs: dict = {
            "zdr": variables["ZDR"],
            "freezing_level": float(freezing_level_m),
            "threshold": float(threshold),
        }
        if "RHOHV" in variables:
            kwargs["rhohv"] = variables["RHOHV"]
            kwargs["min_rhohv"] = float(min_rhohv)
        if use_dbz:
            kwargs["dbz"] = variables["DBZH"]
            kwargs["min_dbz"] = float(min_dbz)
        depth = rt.zdr_column(grid, **kwargs)
        metrics = rt.zdr_column_metrics(depth, min_depth=float(min_depth_m))

    depth_yx = np.asarray(depth.transpose("y", "x").values, dtype=np.float32)
    shown = A.sample_cartesian(depth_yx, x, y, ds)
    shown = np.where(shown > 0, shown, np.nan).astype(np.float32)  # 0 = no column

    # Column objects on the Cartesian map
    from scipy import ndimage

    valid = np.isfinite(depth_yx) & (depth_yx >= min_depth_m)
    labels, _n = ndimage.label(valid, structure=np.ones((3, 3), dtype=int))
    cell_km2 = grid_spacing_m * grid_spacing_m / 1e6
    objects = []
    for lab, sl in enumerate(ndimage.find_objects(labels), start=1):
        if sl is None:
            continue
        sub = labels[sl] == lab
        iy, ix = np.nonzero(sub)
        gy, gx = iy + sl[0].start, ix + sl[1].start
        vals = depth_yx[gy, gx]
        if sub.sum() * cell_km2 < min_area_km2:
            continue
        objects.append(
            {
                "cx": float(x[gx].mean()),
                "cy": float(y[gy].mean()),
                "max_depth": float(vals.max()),
                "mean_depth": float(vals.mean()),
                "area_km2": float(sub.sum() * cell_km2),
                "volume_km3": float(vals.sum() * grid_spacing_m * grid_spacing_m / 1e9),
                "gx": gx,
                "gy": gy,
            }
        )
    objects.sort(key=lambda o: -o["max_depth"])
    all_objects = objects
    objects = objects[:MAX_COLUMNS]

    rows, features = [], []
    for i, o in enumerate(objects, start=1):
        oid = f"Z{i:02d}"
        az, rng = A.xy_to_polar(o["cx"], o["cy"])
        rows.append(
            {
                "id": oid,
                "azimuth": round(az, 1),
                "range_km": round(rng / 1000.0, 1),
                "max_depth_km": round(o["max_depth"] / 1000.0, 2),
                "mean_depth_km": round(o["mean_depth"] / 1000.0, 2),
                "area_km2": round(o["area_km2"], 1),
                "volume_km3": round(o["volume_km3"], 2),
            }
        )
        ring = A.hull_xy(x[o["gx"]], y[o["gy"]], pad=grid_spacing_m / 2.0)
        label = f"{o['max_depth'] / 1000.0:.1f} km"
        if ring:
            features.append(polygon(oid, ring, frame="xy", label=label, color="#06b6d4", fill=0.2))
        else:
            features.append(
                {
                    "id": oid,
                    "kind": "point",
                    "frame": "xy",
                    "coords": [[o["cx"], o["cy"]]],
                    "label": label,
                    "color": "#06b6d4",
                }
            )

    res = Result()
    top = max(float(np.nanmax(shown)) if np.isfinite(shown).any() else 0.0, 2000.0)
    res.field("ZDR column depth (radarx)", shown, units="m", vmin=0.0, vmax=top, cmap="plasma")
    res.scalars(
        [
            {
                "label": "Max column depth",
                "value": float(metrics["ZDR_COLUMN_MAX_DEPTH"]),
                "unit": "m",
            },
            {
                "label": f"Area of columns ≥ {min_depth_m:g} m",
                "value": float(metrics["ZDR_COLUMN_AREA"]),
                "unit": "km²",
            },
            {
                "label": "Volume of columns",
                "value": float(metrics["ZDR_COLUMN_VOLUME"]),
                "unit": "km³",
            },
            {"label": "Column objects", "value": len(all_objects)},
            {"label": "Freezing level", "value": float(freezing_level_m), "unit": "m ASL"},
            {
                "label": "Grid",
                "value": f"{n_xy} x {n_xy} x {z.size}, {grid_spacing_m:g} m / {z_step_m:g} m",
            },
            {"label": "radarx", "value": A.radarx_version()},
        ]
    )
    res.table(
        f"ZDR column objects (depth ≥ {min_depth_m:g} m)",
        [
            {"key": "id", "label": "Column"},
            {"key": "azimuth", "label": "Azimuth", "unit": "°", "digits": 1},
            {"key": "range_km", "label": "Range", "unit": "km", "digits": 1},
            {"key": "max_depth_km", "label": "Max depth", "unit": "km", "digits": 2},
            {"key": "mean_depth_km", "label": "Mean depth", "unit": "km", "digits": 2},
            {"key": "area_km2", "label": "Area", "unit": "km²", "digits": 1},
            {"key": "volume_km3", "label": "Volume", "unit": "km³", "digits": 2},
        ],
        rows,
        id_key="id",
    )
    res.overlay("ZDR columns", features)
    res.text(NOTES, title="About this method")
    return A.add_warnings(res, warns)

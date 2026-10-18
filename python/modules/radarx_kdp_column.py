"""radarx: KDP columns above the freezing level (van Lier-Walqui et al. 2016)."""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np

from engine.plugins import ModuleError, Result, module, param, polygon


def _load_extra():
    """Import the sibling ``_radarx_extra.py`` (modules are loaded by path)."""
    name = "xradar_module_radarx_extra"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).with_name("_radarx_extra.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


X = _load_extra()
A = X.A
MAX_COLUMNS = 40
MAX_CELLS = 120e6  # grid points (x * y * z) allowed before the request is refused

NOTES = """\
KDP column detection of van Lier-Walqui et al. (2016, section 2e), radarx.retrieve.kdp_column. \
Rain and liquid-coated hydrometeors that an updraft carries above the freezing level give a \
positive specific differential phase there. KDP (degrees per km) is integrated, positive \
values only, over a slab above the freezing level; the integral is smoothed with a Gaussian \
filter, its regional maxima are the cores of the columns and a watershed segmentation gives \
each column its extent. The column top is the highest level at or above the freezing level \
with KDP at or above the threshold anywhere in the column of the grid.

The volume is gridded to a Cartesian 3-D grid with radarx's cone gridding \
(radarx.grid.grid_cones; KDP, RHOHV, DBZH) and the products are shown on the selected \
sweep's polar grid by sampling the grid at each gate's ground position. The column objects \
are outlined in x/y metres. KDP comes from the file when it has it; otherwise it is \
estimated from PHIDP sweep by sweep with radarx.retrieve.estimate_kdp (the Band parameter \
scales that estimate). The freezing level is NOT read from the file: enter it (m above sea \
level, e.g. from a sounding); the result depends on it directly.

The threshold (0.75 deg/km) is the S-band value of the paper; it scales inversely with the \
wavelength (about 1.5 at C band, 2.3 at X band). The slab depth (2 km by default) and the \
smoothing width are radarx choices where the paper is open, and so is the minimum depth \
that makes a grid column part of a KDP column. A KDP column can only be seen where the \
sweeps cover the levels above the freezing level, and KDP above the melting layer is noisy: \
the correlation (RHOHV >= 0.8) and reflectivity (>= 20 dBZ by default) conditions keep \
non-weather echo out. KDP columns trail the ZDR column (ZDR Column module) and are \
displaced from it: they mark where the lofted liquid is, not the updraft core itself."""


@module(
    id="radarx_kdp_column",
    title="KDP Column (radarx)",
    description=(
        "radarx.retrieve.kdp_column / kdp_column_metrics: KDP columns above the freezing "
        "level, an updraft/liquid-water-aloft indicator (van Lier-Walqui et al. 2016). Grids "
        "the volume to 3-D first; you supply the freezing level. KDP is estimated from PHIDP "
        "when the file has none."
    ),
    category="Storm",
    requires=[],
    optional=["KDP", "PHIDP", "RHOHV", "DBZH"],
    needs=A.NEEDS,
    scope="volume",
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
        param(
            "show",
            "select",
            "top_above_freezing",
            "Show",
            options=["top_above_freezing", "slab_integral", "slab_depth"],
            help=(
                "Column top above the freezing level (m), the positive KDP integral in the "
                "slab (deg) or the thickness of the slab levels with KDP above the threshold (m)"
            ),
        ),
        param(
            "threshold",
            "number",
            0.75,
            "KDP threshold (deg/km)",
            min=0.25,
            max=4,
            step=0.25,
            help="0.75 at S band (paper); about 1.5 at C band and 2.3 at X band",
        ),
        param(
            "slab_depth_m",
            "number",
            2000.0,
            "Slab depth (m)",
            min=500,
            max=4000,
            step=250,
            help="Depth above the freezing level over which KDP is integrated",
        ),
        param(
            "min_rhohv",
            "number",
            0.8,
            "Min RHOHV",
            min=0.5,
            max=1.0,
            step=0.01,
            help="Gates with a lower correlation coefficient (hail, debris, noise) are dropped",
        ),
        param(
            "min_dbz",
            "number",
            20.0,
            "Min reflectivity (dBZ, -30 = off)",
            min=-30,
            max=50,
            step=1,
            help="Weak echo has noisy KDP; needs DBZH (skipped when the file has none)",
        ),
        param(
            "min_depth_m",
            "number",
            1000.0,
            "Column min depth (m)",
            min=250,
            max=4000,
            step=250,
            help="A grid column joins a KDP column where its smoothed integral is at least "
            "that of a layer this deep at the threshold",
        ),
        param(
            "smooth",
            "number",
            1.0,
            "Smoothing (grid cells)",
            min=0,
            max=4,
            step=0.5,
            help="Gaussian width applied to the slab integral (0 = none)",
        ),
        param(
            "min_area_km2",
            "number",
            4.0,
            "Column object min area (km²)",
            min=0,
            max=200,
            step=1,
            help="Smaller objects are not listed",
        ),
        param(
            "band",
            "select",
            "S",
            "Radar band (KDP estimate)",
            options=["S", "C", "X"],
            help="Only used when KDP has to be estimated from PHIDP",
        ),
        param(
            "grid_spacing_m",
            "number",
            1000.0,
            "Horizontal grid spacing (m)",
            min=500,
            max=2000,
            step=250,
            help="Smaller = slower and more memory",
        ),
        param(
            "z_step_m",
            "number",
            500.0,
            "Vertical grid spacing (m)",
            min=250,
            max=1000,
            step=250,
            help="Vertical grid spacing; the column top is quantised to it",
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
    show,
    threshold,
    slab_depth_m,
    min_rhohv,
    min_dbz,
    min_depth_m,
    smooth,
    min_area_km2,
    band,
    grid_spacing_m,
    z_step_m,
    max_range_km,
):
    rt = A.rx()
    kdp_column = X.need(rt, "kdp_column", "The KDP column")
    kdp_column_metrics = X.need(rt, "kdp_column_metrics", "The KDP column")
    from radarx.grid import grid_cones
    from scipy import ndimage

    ds = X.selected_sweep(ctx)
    if len(A.sweep_names(ctx.datatree)) < 3:
        raise ModuleError("A KDP column needs a volume with several sweeps; this file has few")

    ctx.progress(5, "Reading the volume")
    vol = X.volume(
        ctx,
        any_of=("KDP",),
        keep=("RHOHV", "DBZH"),
        estimate_kdp=True,
        band=band,
        progress=(5, 30),
    )
    with_kdp = [n for n in vol.names if "KDP" in vol.tree[n].data_vars]
    if len(with_kdp) < 3:
        raise ModuleError(
            "Fewer than three sweeps have KDP, and none could be derived: the volume needs a "
            "KDP field or PHIDP (the module estimates KDP from PHIDP). "
            + (X.describe_skipped(vol) or "")
        )
    use_rho = any("RHOHV" in vol.tree[n].data_vars for n in with_kdp)
    use_dbz = min_dbz > -30 and any("DBZH" in vol.tree[n].data_vars for n in with_kdp)
    notes = []
    if vol.kdp_estimated:
        notes.append(
            f"KDP was estimated from PHIDP on {len(vol.kdp_estimated)} of {len(with_kdp)} "
            "sweeps (radarx.retrieve.estimate_kdp)."
        )
    if min_dbz > -30 and not use_dbz:
        notes.append("No reflectivity in the volume: the minimum-dBZ condition was skipped.")
    if not use_rho:
        notes.append("No RHOHV in the volume: the correlation condition was skipped.")

    z0 = math.floor(max(freezing_level_m - 2 * z_step_m, 0.0) / z_step_m) * z_step_m
    z_top = freezing_level_m + slab_depth_m + 5000.0
    z = np.arange(z0, z_top + z_step_m, z_step_m, dtype=np.float64)
    r = float(max_range_km) * 1000.0
    n_xy = int(2 * r / grid_spacing_m) + 1
    if n_xy * n_xy * z.size > MAX_CELLS:
        raise ModuleError(
            "The requested grid is too large; increase the grid spacing or reduce the radius"
        )
    x = np.arange(-n_xy // 2 + 1, n_xy // 2 + 1, dtype=np.float64) * grid_spacing_m
    y = x.copy()
    variables = ["KDP"]
    if use_rho:
        variables.append("RHOHV")
    if use_dbz:
        variables.append("DBZH")

    ctx.progress(35, f"Gridding the volume ({n_xy} x {n_xy} x {z.size})")
    with A.captured_warnings() as warns, A.guard("KDP column"):
        grid = grid_cones(vol.tree, variables, x=x, y=y, z=z)
        ctx.progress(70, "Detecting KDP columns")
        kwargs: dict = {
            "kdp": "KDP",
            "freezing_level": float(freezing_level_m),
            "threshold": float(threshold),
            "slab_depth": float(slab_depth_m),
            "min_depth": float(min_depth_m),
            "smooth": float(smooth) if smooth > 0 else None,
        }
        if use_rho:
            kwargs["rhohv"] = "RHOHV"
            kwargs["min_rhohv"] = float(min_rhohv)
        if use_dbz:
            kwargs["dbz"] = "DBZH"
            kwargs["min_dbz"] = float(min_dbz)
        cols = kdp_column(grid, **kwargs)
        metrics = kdp_column_metrics(cols)

    labels = np.asarray(cols["KDP_COLUMN_OBJECT"].transpose("y", "x").values, dtype=np.int64)
    top = np.asarray(cols["KDP_COLUMN_TOP"].transpose("y", "x").values, dtype=np.float32)
    integral = np.asarray(cols["KDP_SLAB_INTEGRAL"].transpose("y", "x").values, dtype=np.float32)
    depth = np.asarray(cols["KDP_SLAB_DEPTH"].transpose("y", "x").values, dtype=np.float32)
    inside = labels > 0
    top_above = np.where(inside, top - float(freezing_level_m), np.nan).astype(np.float32)
    if show == "slab_integral":
        plane = np.where(integral > 0, integral, np.nan)
        name, units, cmap = "KDP slab integral (radarx)", "deg", "YlGnBu"
        vmax = max(float(np.nanpercentile(plane, 99.5)) if np.isfinite(plane).any() else 1.0, 1.0)
    elif show == "slab_depth":
        plane = np.where(depth > 0, depth, np.nan)
        name, units, cmap = "KDP slab depth (radarx)", "m", "YlGnBu"
        vmax = max(float(slab_depth_m), 500.0)
    else:
        plane = top_above
        name, units, cmap = "KDP column top above freezing level (radarx)", "m", "plasma"
        vmax = max(float(np.nanmax(plane)) if np.isfinite(plane).any() else 0.0, 2000.0)
    shown = A.sample_cartesian(plane.astype(np.float32), x, y, ds)

    # Column objects on the Cartesian map
    cell_km2 = grid_spacing_m * grid_spacing_m / 1e6
    objects = []
    for lab, sl in enumerate(ndimage.find_objects(labels), start=1):
        if sl is None:
            continue
        sub = labels[sl] == lab
        iy, ix = np.nonzero(sub)
        gy, gx = iy + sl[0].start, ix + sl[1].start
        area = float(sub.sum() * cell_km2)
        if area < min_area_km2:
            continue
        tops = top[gy, gx]
        integ = integral[gy, gx]
        w = np.where(np.isfinite(integ) & (integ > 0), integ, 0.0)
        wsum = float(w.sum())
        cx = float((w * x[gx]).sum() / wsum) if wsum > 0 else float(x[gx].mean())
        cy = float((w * y[gy]).sum() / wsum) if wsum > 0 else float(y[gy].mean())
        objects.append(
            {
                "cx": cx,
                "cy": cy,
                "max_top": float(np.nanmax(tops)) if np.isfinite(tops).any() else float("nan"),
                "peak_integral": float(np.nanmax(integ)) if np.isfinite(integ).any() else 0.0,
                "area_km2": area,
                "gx": gx,
                "gy": gy,
            }
        )
    objects.sort(key=lambda o: -(o["max_top"] if math.isfinite(o["max_top"]) else -1e30))
    n_objects = len(objects)
    objects = objects[:MAX_COLUMNS]

    rows, features = [], []
    for i, o in enumerate(objects, start=1):
        oid = f"K{i:02d}"
        az, rng = A.xy_to_polar(o["cx"], o["cy"])
        above = o["max_top"] - float(freezing_level_m)
        rows.append(
            {
                "id": oid,
                "azimuth": round(az, 1),
                "range_km": round(rng / 1000.0, 1),
                "top_km": round(o["max_top"] / 1000.0, 2) if math.isfinite(o["max_top"]) else None,
                "above_fl_km": round(above / 1000.0, 2) if math.isfinite(above) else None,
                "peak_integral": round(o["peak_integral"], 2),
                "area_km2": round(o["area_km2"], 1),
            }
        )
        label = f"+{above / 1000.0:.1f} km" if math.isfinite(above) else oid
        ring = A.hull_xy(x[o["gx"]], y[o["gy"]], pad=grid_spacing_m / 2.0)
        if ring:
            features.append(polygon(oid, ring, frame="xy", label=label, color="#14b8a6", fill=0.2))
        else:
            features.append(
                {
                    "id": oid,
                    "kind": "point",
                    "frame": "xy",
                    "coords": [[o["cx"], o["cy"]]],
                    "label": label,
                    "color": "#14b8a6",
                }
            )

    def _num(key):
        val = float(metrics[key].values)
        return val if math.isfinite(val) else None

    res = Result()
    res.field(name, shown, units=units, vmin=0.0, vmax=vmax, cmap=cmap)
    res.scalars(
        [
            {"label": "KDP columns", "value": int(metrics["KDP_COLUMN_COUNT"].values)},
            {"label": "Highest column top", "value": _num("KDP_COLUMN_MAX_TOP"), "unit": "m ASL"},
            {"label": "Area of columns", "value": _num("KDP_COLUMN_AREA"), "unit": "km²"},
            {
                "label": f"Volume with KDP ≥ {threshold:g}",
                "value": _num("KDP_VOLUME"),
                "unit": "km³",
            },
            {"label": "Listed objects", "value": n_objects},
            {"label": "Freezing level", "value": float(freezing_level_m), "unit": "m ASL"},
            {
                "label": "Grid",
                "value": f"{n_xy} x {n_xy} x {z.size}, {grid_spacing_m:g} m / {z_step_m:g} m",
            },
            {"label": "radarx", "value": X.version()},
        ]
    )
    res.table(
        f"KDP column objects (area ≥ {min_area_km2:g} km²)",
        [
            {"key": "id", "label": "Column"},
            {"key": "azimuth", "label": "Azimuth", "unit": "°", "digits": 1},
            {"key": "range_km", "label": "Range", "unit": "km", "digits": 1},
            {"key": "top_km", "label": "Top", "unit": "km ASL", "digits": 2},
            {"key": "above_fl_km", "label": "Above 0 °C", "unit": "km", "digits": 2},
            {"key": "peak_integral", "label": "Peak integral", "unit": "deg", "digits": 2},
            {"key": "area_km2", "label": "Area", "unit": "km²", "digits": 1},
        ],
        rows,
        id_key="id",
    )
    res.overlay("KDP columns", features)
    if X.describe_skipped(vol):
        notes.append("Sweeps left out: " + X.describe_skipped(vol))
    res.text("\n".join([*notes, NOTES]), title="About this method")
    return A.add_warnings(res, warns)

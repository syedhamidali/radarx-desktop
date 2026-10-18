"""Storm cell detection and statistics (unified cell backend, see docs/CELLS.md)."""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import numpy as np

from engine import cells as cell_engine
from engine.plugins import ModuleError, Result, line, module, param, point, polygon

MAX_FEATURES = 200  # cap table rows / overlay shapes for very noisy sweeps
SPLIT_STEP_DBZ = 10.0  # extra thresholds used by "split touching cores"
logger = logging.getLogger(__name__)


def _load_utils():
    """Import the sibling ``_radar_utils.py`` (modules are loaded by path, not as a package)."""
    name = "xradar_module_radar_utils"
    if name not in sys.modules:
        path = Path(__file__).with_name("_radar_utils.py")
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


U = _load_utils()


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


def _volume_columns(ctx, dbz_name: str) -> dict[str, np.ndarray]:
    """VIL (kg/m2) and echo top (km) of the whole volume on the selected sweep's grid.

    Needs radarx and a multi-sweep volume; returns ``{}`` (never raises) when that is not
    available, so the plain sweep statistics are always produced.
    """
    try:
        adapter = _load_adapter()
        tree = ctx.datatree
        if len(adapter.sweep_names(tree)) < 2:
            return {}
        import radarx.retrieve as rt

        if not (hasattr(rt, "vil") and hasattr(rt, "echo_top")):
            return {}
        ds = adapter.prepare_sweep(ctx)
        with adapter.captured_warnings():
            vil = rt.vil(tree, dbz_name, fill_below=True)["VIL"]
            top = rt.echo_top(tree, dbz_name, threshold=18.0)
        out: dict[str, np.ndarray] = {}
        for key, da, scale in (("vil", vil, 1.0), ("echo_top", top, 1e-3)):
            ray_dim = next(d for d in da.dims if d != "range")
            src = da.transpose(ray_dim, "range")
            ground = da["ground_range"].values
            mapped = adapter.remap_polar(src.values, src[ray_dim].values, ground, ds)
            out[key] = (mapped * np.float32(scale)).astype(np.float32)
        out["vil"] = np.where(out["vil"] > 0, out["vil"], np.nan).astype(np.float32)
        return out
    except Exception as exc:  # optional extra: any failure just drops the VIL columns
        logger.info("VIL / echo-top columns skipped: %s", exc)
        return {}


@module(
    id="storm_detection",
    title="Storm Cell Detection",
    description=(
        "Finds contiguous regions of reflectivity above a threshold (8-connected "
        "components), discards those smaller than a minimum area, and reports each "
        "cell's centroid, position, area, intensity, VIL / echo top (volumes) and, if a "
        "storm motion is given, its motion arrow. Uses radarx when installed (optional "
        "splitting of touching cores), a built-in algorithm otherwise."
    ),
    category="Storm",
    requires=["DBZH"],
    params=[
        param(
            "threshold_dbz", "number", 35.0, "Reflectivity threshold (dBZ)", min=0, max=75, step=1
        ),
        param("min_size_km2", "number", 10.0, "Minimum cell area (km²)", min=0, max=5000, step=1),
        param(
            "split_cells",
            "bool",
            False,
            "Split touching cores",
            help="Separate cells that hold several cores 10/20 dBZ above the threshold (radarx)",
        ),
        param(
            "with_vil",
            "bool",
            True,
            "VIL / echo top columns",
            help="Needs a volume with several sweeps and radarx; skipped otherwise",
        ),
        param(
            "motion_speed",
            "number",
            0.0,
            "Storm motion speed (m/s, 0 = none)",
            min=0,
            max=60,
            step=1,
            help="Draws a motion arrow on every cell and fills the speed column",
        ),
        param(
            "motion_from",
            "number",
            270.0,
            "Storm motion from (°)",
            min=0,
            max=360,
            step=5,
            help="Meteorological direction the storms move from (270 = from the west)",
        ),
        param("arrow_minutes", "number", 15.0, "Arrow length (min)", min=1, max=120, step=1),
        param(
            "backend",
            "select",
            "auto",
            "Algorithm",
            options=list(cell_engine.BACKENDS),
            help="auto = radarx if installed, else the built-in numpy implementation",
        ),
    ],
    version="1.1",
)
def run(
    ctx,
    threshold_dbz,
    min_size_km2,
    split_cells=False,
    with_vil=True,
    motion_speed=0.0,
    motion_from=270.0,
    arrow_minutes=15.0,
    backend="auto",
):
    try:
        da = U.sweep_array(ctx, "DBZH")
    except ValueError as exc:
        raise ModuleError(str(exc)) from exc

    lat, lon = cell_engine.site_lat_lon(ctx.datatree)
    elevation = cell_engine.dataset_elevation_deg(ctx.dataset)
    thresholds = None
    if split_cells:
        thresholds = [t for t in (threshold_dbz + SPLIT_STEP_DBZ * i for i in range(3)) if t <= 90]

    extra = {}
    if with_vil:
        ctx.progress(5, "VIL / echo top")
        extra = _volume_columns(ctx, ctx.find("DBZH").name)

    ctx.progress(10, "Labelling cells")
    scan = cell_engine.identify(
        da,
        threshold_dbz=threshold_dbz,
        min_size_km2=min_size_km2,
        thresholds=thresholds,
        backend=backend,
        lat=lat,
        lon=lon,
        elevation_deg=elevation,
        extra_fields=extra or None,
    )
    cells = list(scan)
    if motion_speed > 0:
        cell_engine.set_motion(cells, motion_speed, motion_from)
    ctx.progress(80, f"{len(cells)} cells")

    res = Result()
    scalars = [
        {"label": "Cells", "value": len(cells)},
        {
            "label": "Largest cell",
            "value": max((c.area_km2 for c in cells), default=0.0),
            "unit": "km²",
        },
        {
            "label": "Max reflectivity",
            "value": max((c.max_dbz for c in cells), default=None),
            "unit": "dBZ",
        },
        {"label": "Threshold", "value": threshold_dbz, "unit": "dBZ"},
        {"label": "Algorithm", "value": scan.backend_label},
    ]
    if scan.note:
        scalars.append({"label": "Note", "value": scan.note})
    res.scalars(scalars)

    shown = cells[:MAX_FEATURES]
    rows = cell_engine.cell_rows(shown)
    res.table(
        f"Storm cells ({len(cells)})"
        if len(cells) <= MAX_FEATURES
        else f"Storm cells (top {MAX_FEATURES} of {len(cells)})",
        cell_engine.table_columns(rows),
        rows,
        id_key="id",
    )

    features = []
    for c in shown:
        colour = U.dbz_color(c.max_dbz)
        pts = U.order_polygon([p[0] for p in c.boundary_points], [p[1] for p in c.boundary_points])
        label = f"{c.id} · {c.max_dbz:.0f} dBZ"
        if pts:
            features.append(polygon(c.id, pts, label=label, color=colour))
        else:  # degenerate (tiny) cell: mark the centroid instead
            features.append(
                point(c.id, [c.centroid_az % 360.0, c.centroid_range], label=label, color=colour)
            )
        arrow = cell_engine.arrow_coords(c, arrow_minutes)
        if arrow:
            features.append(line(f"{c.id}:motion", arrow, color="#38bdf8"))
    res.overlay("Storm cells", features)
    return res

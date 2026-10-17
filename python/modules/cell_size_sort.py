"""Rank storm cells by size, intensity or distance and highlight the top N."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from engine.plugins import ModuleError, Result, module, param, point, polygon
from engine.tracking import identify_cells

SORT_KEYS = ["area_km2", "max_dbz", "mean_dbz", "centroid_range"]
SORT_LABELS = {
    "area_km2": "Area (km²)",
    "max_dbz": "Max reflectivity (dBZ)",
    "mean_dbz": "Mean reflectivity (dBZ)",
    "centroid_range": "Range from radar (m)",
}


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


@module(
    id="cell_size_sort",
    title="Rank Storm Cells",
    description=(
        "Identifies storm cells (reflectivity above a threshold) and ranks them by "
        "area, peak or mean reflectivity, or range from the radar. The top N are "
        "labelled #1, #2, ... on the map."
    ),
    category="Storm",
    requires=["DBZH"],
    params=[
        param(
            "threshold_dbz", "number", 35.0, "Reflectivity threshold (dBZ)", min=0, max=75, step=1
        ),
        param("min_size_km2", "number", 10.0, "Minimum cell area (km²)", min=0, max=5000, step=1),
        param("sort_by", "select", "area_km2", "Rank by", options=SORT_KEYS),
        param("descending", "bool", True, "Largest first"),
        param("top_n", "int", 10, "Cells to show", min=1, max=100, step=1),
    ],
    version="1.0",
)
def run(ctx, threshold_dbz, min_size_km2, sort_by, descending, top_n):
    try:
        da = U.sweep_array(ctx, "DBZH")
    except ValueError as exc:
        raise ModuleError(str(exc)) from exc

    ctx.progress(10, "Labelling cells")
    cells = identify_cells(da, threshold_dbz=threshold_dbz, min_size_km2=min_size_km2)
    ranked = sorted(cells, key=lambda c: getattr(c, sort_by), reverse=bool(descending))
    top = ranked[:top_n]

    res = Result()
    res.scalars(
        [
            {"label": "Cells found", "value": len(cells)},
            {"label": "Shown", "value": len(top)},
            {"label": "Ranked by", "value": SORT_LABELS[sort_by]},
            {"label": "Total area", "value": sum(c.area_km2 for c in cells), "unit": "km²"},
        ]
    )

    rows = [
        {
            "rank": i,
            "id": c.id,
            "area_km2": round(c.area_km2, 1),
            "max_dbz": round(c.max_dbz, 1),
            "mean_dbz": round(c.mean_dbz, 1),
            "centroid_az": round(c.centroid_az % 360.0, 1),
            "centroid_range_km": round(c.centroid_range / 1000.0, 1),
        }
        for i, c in enumerate(top, start=1)
    ]
    res.table(
        f"Top {len(top)} cells by {SORT_LABELS[sort_by]}",
        [
            {"key": "rank", "label": "#"},
            {"key": "id", "label": "Cell"},
            {"key": "area_km2", "label": "Area", "unit": "km²", "digits": 1},
            {"key": "max_dbz", "label": "Max", "unit": "dBZ", "digits": 1},
            {"key": "mean_dbz", "label": "Mean", "unit": "dBZ", "digits": 1},
            {"key": "centroid_az", "label": "Azimuth", "unit": "°", "digits": 1},
            {"key": "centroid_range_km", "label": "Range", "unit": "km", "digits": 1},
        ],
        rows,
        id_key="id",
    )

    features = []
    for i, c in enumerate(top, start=1):
        colour = U.dbz_color(c.max_dbz)
        pts = U.order_polygon([p[0] for p in c.boundary_points], [p[1] for p in c.boundary_points])
        if pts:
            features.append(polygon(c.id, pts, label=f"#{i}", color=colour, fill=0.2))
        else:
            features.append(
                point(c.id, [c.centroid_az % 360.0, c.centroid_range], label=f"#{i}", color=colour)
            )
    res.overlay(f"Top {len(top)} cells", features)
    return res

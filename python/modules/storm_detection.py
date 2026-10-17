"""Storm cell detection: connected components of reflectivity above a threshold."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from engine.plugins import ModuleError, Result, module, param, point, polygon
from engine.tracking import identify_cells

MAX_FEATURES = 200  # cap table rows / overlay shapes for very noisy sweeps


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
    id="storm_detection",
    title="Storm Cell Detection",
    description=(
        "Finds contiguous regions of reflectivity above a threshold (8-connected "
        "components), discards those smaller than a minimum area, and reports each "
        "cell's centroid, area and intensity."
    ),
    category="Storm",
    requires=["DBZH"],
    params=[
        param(
            "threshold_dbz", "number", 35.0, "Reflectivity threshold (dBZ)", min=0, max=75, step=1
        ),
        param("min_size_km2", "number", 10.0, "Minimum cell area (km²)", min=0, max=5000, step=1),
    ],
    version="1.0",
)
def run(ctx, threshold_dbz, min_size_km2):
    try:
        da = U.sweep_array(ctx, "DBZH")
    except ValueError as exc:
        raise ModuleError(str(exc)) from exc

    ctx.progress(10, "Labelling cells")
    cells = identify_cells(da, threshold_dbz=threshold_dbz, min_size_km2=min_size_km2)
    ctx.progress(80, f"{len(cells)} cells")

    res = Result()
    res.scalars(
        [
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
        ]
    )

    shown = cells[:MAX_FEATURES]
    rows = [
        {
            "id": c.id,
            "centroid_az": round(c.centroid_az % 360.0, 1),
            "centroid_range_km": round(c.centroid_range / 1000.0, 1),
            "area_km2": round(c.area_km2, 1),
            "max_dbz": round(c.max_dbz, 1),
            "mean_dbz": round(c.mean_dbz, 1),
        }
        for c in shown
    ]
    res.table(
        f"Storm cells ({len(cells)})"
        if len(cells) <= MAX_FEATURES
        else f"Storm cells (top {MAX_FEATURES} of {len(cells)})",
        [
            {"key": "id", "label": "Cell"},
            {"key": "centroid_az", "label": "Azimuth", "unit": "°", "digits": 1},
            {"key": "centroid_range_km", "label": "Range", "unit": "km", "digits": 1},
            {"key": "area_km2", "label": "Area", "unit": "km²", "digits": 1},
            {"key": "max_dbz", "label": "Max", "unit": "dBZ", "digits": 1},
            {"key": "mean_dbz", "label": "Mean", "unit": "dBZ", "digits": 1},
        ],
        rows,
        id_key="id",
    )

    features = []
    for c in shown:
        colour = U.dbz_color(c.max_dbz)
        pts = U.order_polygon([p[0] for p in c.boundary_points], [p[1] for p in c.boundary_points])
        label = f"{c.max_dbz:.0f} dBZ"
        if pts:
            features.append(polygon(c.id, pts, label=label, color=colour))
        else:  # degenerate (tiny) cell: mark the centroid instead
            features.append(
                point(c.id, [c.centroid_az % 360.0, c.centroid_range], label=label, color=colour)
            )
    res.overlay("Storm cells", features)
    return res

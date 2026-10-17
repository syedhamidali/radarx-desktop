"""Template for a new analysis module — copy this file, rename it, edit, done.

Files starting with ``_`` are ignored by discovery, so this one never loads.

Where to put your copy
    python/modules/my_module.py          (ships with the app)
    ~/.xradar-desktop/modules/my_module.py   (personal, survives updates)
    any dir listed in $XRADAR_MODULES_DIR

It appears in the Modules panel on the next start, or immediately after
pressing "Reload" in the panel. The form, the Run button, the result tables and
the map overlay are all generated from what you declare below.
"""

import numpy as np

from engine.plugins import Result, module, param, polygon


@module(
    id="my_module",  # unique, [a-z0-9_-]
    title="My Module",  # shown in the panel
    description="One sentence on what this does and its limits.",
    category="Custom",  # panel groups modules by category
    requires=["DBZH"],  # canonical names; aliases (DBZ, DBZHC, ...) resolve automatically
    optional=["RHOHV"],  # use ctx.has("RHOHV") before ctx.field("RHOHV")
    params=[
        # type: number | int | bool | select | string — values arrive validated and clamped
        param("threshold", "number", 40.0, "Threshold (dBZ)", min=0, max=80, step=1),
        param("top_n", "int", 5, "Keep top N", min=1, max=50),
        param("mode", "select", "max", "Rank by", options=["max", "mean"]),
    ],
)
def run(ctx, threshold, top_n, mode):
    z = ctx.field("DBZH").values  # (azimuth, range) float array, NaN = no data
    azimuth, range_m = ctx.polar_coords()  # degrees, metres

    ctx.progress(30, "Thresholding...")
    hot = np.isfinite(z) & (z >= threshold)

    res = Result()
    res.scalars({"Gates above threshold": int(hot.sum()), "Max (dBZ)": float(np.nanmax(z))})

    # Table rows; id_key links a row to the overlay feature with the same id.
    rows = [{"id": "A1", "az": float(azimuth[0]), "range_km": float(range_m[-1] / 1000)}]
    res.table(
        "Example",
        columns=[
            {"key": "id", "label": "ID"},
            {"key": "az", "label": "Azimuth", "unit": "deg", "digits": 1},
            {"key": "range_km", "label": "Range", "unit": "km", "digits": 1},
        ],
        rows=rows,
        id_key="id",
    )

    # Overlay drawn on the PPI. frame="polar" -> [azimuth_deg, range_m]; frame="xy" -> [x_m, y_m]
    res.overlay(
        "Example outline",
        [polygon("A1", [[10, 20000], [10, 30000], [20, 30000], [20, 20000]], label="A1")],
    )
    res.text("Explain heuristics and limitations here.")
    return res

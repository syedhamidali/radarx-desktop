"""Hydrology: area-weighted rain rate over basins from the volume's best-beam composite."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

from engine.hydro import qpe as hq
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

NOTES = """\
RETRIEVED, not observed. The volume's lowest beams are averaged onto a 1 km (default) grid cell by \
cell (best beam: lowest unblocked), and each basin polygon is intersected EXACTLY with the cells: \
a cell's weight is the area of the polygon inside it times the true cell area (not a pixel count). \
Mean = sum(w R) / sum(w) over cells with an estimate; coverage is the share of the basin area with \
an estimate. This is the instantaneous rate of one volume: for rainfall depth over a period use \
`rxd basin` (several volumes, advection-corrected)."""


@module(
    id="hydro_basin_rate",
    title="Basin Rain Rate (area-weighted)",
    description=(
        "Area-weighted mean, maximum and area above thresholds of the rain rate of this volume "
        "over basin polygons from a GeoJSON / shapefile / GeoPackage file. Draws the basins on "
        "the PPI. For depth over a period use `rxd basin`."
    ),
    category="Hydrology",
    requires=["DBZH"],
    optional=["ZDR", "RHOHV", "PHIDP", "KDP"],
    needs=A.NEEDS,
    scope="volume",
    params=[
        param(
            "basins_file",
            "string",
            "",
            "Basin file (.geojson / .shp / .gpkg)",
            help="Path on this computer; nothing is uploaded or downloaded",
        ),
        param("id_field", "string", "", "Attribute used as basin id (empty = auto)"),
        param("method", "select", "auto", "QPE method", options=["auto", *hq.METHODS[1:]]),
        param(
            "relation",
            "select",
            "wsr88d",
            "Z-R relation",
            options=[k for k, r in hq.ZR_RELATIONS.items() if r.kind == "rain"],
        ),
        param(
            "freezing_level_m",
            "number",
            0.0,
            "Freezing level (m ASL, 0 = unknown)",
            min=0,
            max=8000,
            step=100,
        ),
        param("resolution_m", "number", 1000.0, "Grid cell (m)", min=250, max=5000, step=250),
        param("threshold_a", "number", 10.0, "Rate threshold A (mm/h)", min=0, max=300, step=1),
        param("threshold_b", "number", 50.0, "Rate threshold B (mm/h)", min=0, max=300, step=1),
    ],
    version="1.0",
)
def run(
    ctx,
    basins_file,
    id_field,
    method,
    relation,
    freezing_level_m,
    resolution_m,
    threshold_a,
    threshold_b,
):
    import pyproj

    from engine.hydro import accumulate as acc
    from engine.hydro import basins as hb

    if not basins_file.strip():
        raise ModuleError("Give the basin file in 'Basin file' (GeoJSON, shapefile or GeoPackage)")
    try:
        bset = hb.BasinSet.from_file(
            basins_file.strip(), **({"id_field": id_field.strip()} if id_field.strip() else {})
        )
    except hb.BasinError as exc:
        raise ModuleError(str(exc)) from exc
    config = {"method": method, "relation": relation}
    if freezing_level_m > 0:
        config["freezing_level_m"] = float(freezing_level_m)
    reader = acc.TreeReader(ctx.raw_datatree)
    ctx.progress(15, "Rain rate of the volume")
    with A.guard("Basin rain rate"):
        try:
            result = acc.accumulate([reader], config, resolution_m=float(resolution_m), basins=bset)
        except hq.QpeError as exc:
            raise ModuleError(str(exc)) from exc
    bs = result.basin_series
    thr = tuple(sorted({float(threshold_a), float(threshold_b)}))
    rate = result.rate_last
    rows = []
    for w in bs.weights:
        s = hb.zonal_stats(rate, w, thr)
        rows.append(
            {
                "id": s["id"],
                "name": s["name"],
                "area": s["area_km2"],
                "mean": s["mean"],
                "max": s["max"],
                "p90": s["p90"],
                "coverage": 100.0 * s["coverage"],
                **{f"above_{t:g}": s[f"area_ge_{t:g}_km2"] for t in thr},
            }
        )
    res = Result()
    res.scalars(
        [
            {"label": "Basins", "value": len(rows)},
            {
                "label": "Method",
                "value": result.config["methods_used"][0]
                if result.config["methods_used"]
                else method,
            },
            {"label": "Provenance", "value": "RETRIEVED (estimate, not an observation)"},
            {
                "label": "Grid",
                "value": f"{result.grid.shape[1]} x {result.grid.shape[0]} @ {resolution_m:g} m",
            },
        ]
    )
    cols = [
        {"key": "id", "label": "Basin"},
        {"key": "area", "label": "Area", "unit": "km2", "digits": 1},
        {"key": "mean", "label": "Mean rate", "unit": "mm/h", "digits": 2},
        {"key": "p90", "label": "P90", "unit": "mm/h", "digits": 2},
        {"key": "max", "label": "Max cell", "unit": "mm/h", "digits": 1},
        {"key": "coverage", "label": "Coverage", "unit": "%", "digits": 0},
        *[
            {"key": f"above_{t:g}", "label": f"Area >= {t:g} mm/h", "unit": "km2", "digits": 1}
            for t in thr
        ],
    ]
    res.table("Basin rain rate", cols, rows, id_key="id")
    # basin outlines on the PPI (metres east/north of the radar)
    site = A.site_coords(ctx.datatree)
    lat, lon = float(site["latitude"]), float(site["longitude"])
    tr = pyproj.Transformer.from_crs(
        "EPSG:4326", f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m", always_xy=True
    )
    feats = []
    for b in bset.basins[:60]:
        for k, poly in enumerate(b.polygons):
            x, y = tr.transform(poly[0][:, 0], poly[0][:, 1])
            feats.append(
                polygon(
                    f"{b.id}" if k == 0 else f"{b.id}#{k}",
                    np.column_stack([x, y]).tolist(),
                    frame="xy",
                    label=b.name or b.id,
                )
            )
    res.overlay("Basins", feats)
    res.text(NOTES + "\n\n" + "\n".join(result.notes), title="About this method")
    return res

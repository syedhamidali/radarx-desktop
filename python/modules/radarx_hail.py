"""radarx: hail products (SHI, MESH, POSH, HKE) of a volume - Witt et al. (1998)."""

from __future__ import annotations

import importlib.util
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

PRODUCTS = {
    "MESH": ("MESH (radarx)", "mm", "Maximum expected size of hail"),
    "SHI": ("SHI (radarx)", "J/m/s", "Severe hail index"),
    "POSH": ("POSH (radarx)", "%", "Probability of severe hail"),
    "HKE": ("HKE (radarx)", "J/m²/s", "Hail kinetic energy flux"),
}

NOTES = """\
Hail detection algorithm of the WSR-88D (Witt et al. 1998, Eqs. 1-6), radarx.retrieve.\
hail_products, computed on every reflectivity column of the volume (as the MRMS version does):

 E = 5e-6 x 10^(0.084 Z) x W(Z)  [kinetic energy flux], W(Z) rises from 0 at 40 dBZ to 1 at 50 dBZ
 SHI = 0.1 x integral of W_T(H) x E dH from the 0 C level up, W_T rises from 0 at the 0 C \
level to 1 at the -20 C level
 MESH = 2.54 x SHI^0.5 mm (fitted so that ~75 % of 147 hail reports were smaller)
 POSH = 29 ln(SHI / WT) + 50 %, WT = max(57.5 H0 - 121, 20), H0 = 0 C height above the radar in km

The 0 C and -20 C heights are NOT read from the file: enter them (metres above sea level, \
from a sounding or model). MESH is very sensitive to them. Hail values are the products of a \
reflectivity profile, not a measurement of the hail at the ground; 'cone of silence' columns \
near the radar and columns at long range have few beams above the freezing level, so SHI is \
a lower bound there (see the truncated-top share). Not implemented in radarx: the probability \
of hail of any size (POH), and the operational rounding of POSH to 10 % and MESH to 6.35 mm. \
Outlined cells are connected regions of the selected product above the minimum MESH. The \
products live on the polar grid of the lowest sweep and are mapped onto the selected sweep by \
nearest azimuth and ground range."""


@module(
    id="radarx_hail",
    title="Hail: SHI / MESH / POSH (radarx)",
    description=(
        "radarx.retrieve.hail_products: severe hail index, maximum expected hail size (MESH), "
        "probability of severe hail and hail kinetic energy from the reflectivity columns of "
        "the whole volume (Witt et al. 1998). You supply the 0 C and -20 C heights."
    ),
    category="Hazards",
    requires=["DBZH"],
    needs=A.NEEDS,
    params=[
        param("product", "select", "MESH", "Show", options=list(PRODUCTS)),
        param(
            "freezing_level_m",
            "number",
            4000.0,
            "0 °C level (m ASL)",
            min=500,
            max=7000,
            step=100,
            help="Height of the freezing level above sea level, e.g. from a sounding",
        ),
        param(
            "minus20_level_m",
            "number",
            7500.0,
            "-20 °C level (m ASL)",
            min=1500,
            max=11000,
            step=100,
            help="Height of the -20 °C level; must be above the freezing level",
        ),
        param("zl", "number", 40.0, "W(Z) lower (dBZ)", min=20, max=50, step=1),
        param("zu", "number", 50.0, "W(Z) upper (dBZ)", min=30, max=65, step=1),
        param(
            "min_mesh_mm",
            "number",
            25.4,
            "Outline cells with MESH ≥ (mm)",
            min=5,
            max=100,
            step=1,
            help="25.4 mm = 1 inch, the severe-hail criterion",
        ),
    ],
    version="1.0",
)
def run(ctx, product, freezing_level_m, minus20_level_m, zl, zu, min_mesh_mm):
    rt = A.rx()
    ds = A.prepare_sweep(ctx)
    tree = ctx.datatree
    if len(A.sweep_names(tree)) < 2:
        raise ModuleError("Hail products need a volume with several sweeps; this file has one")
    if minus20_level_m <= freezing_level_m:
        raise ModuleError("The -20 °C level must be higher than the 0 °C level")
    if zu <= zl:
        raise ModuleError("The upper W(Z) reflectivity must be above the lower one")
    ctx.progress(25, "Integrating hail kinetic energy over the columns")
    with A.captured_warnings() as warns, A.guard("Hail products"):
        out = rt.hail_products(
            tree,
            ctx.find("DBZH").name,
            freezing_level=float(freezing_level_m),
            minus20_level=float(minus20_level_m),
            zl=float(zl),
            zu=float(zu),
        )

    ground = out["ground_range"].values
    ray_dim = next(d for d in out["MESH"].dims if d != "range")
    az_src = out[ray_dim].values

    def mapped(name: str) -> np.ndarray:
        da = out[name].transpose(ray_dim, "range")
        return A.remap_polar(da.values, az_src, ground, ds)

    arrays = {n: mapped(n) for n in ("MESH", "SHI", "POSH", "HKE")}
    truncated = mapped("SHI_TOP_TRUNCATED")
    shown = arrays[product]
    label, units, long_name = PRODUCTS[product]
    positive = np.where(shown > 0, shown, np.nan).astype(np.float32)  # 0 = no hail signal
    finite = np.isfinite(positive)

    azimuth, range_m = ctx.polar_coords()
    regions = A.polar_regions(
        np.nan_to_num(arrays["MESH"], nan=0.0) >= float(min_mesh_mm),
        arrays["MESH"],
        azimuth,
        range_m,
        min_area_km2=1.0,
        max_regions=40,
    )

    res = Result()
    if finite.any():
        top = float(np.nanpercentile(positive[finite], 99.5))
        vmax = 100.0 if product == "POSH" else max(top, 1e-3)
    else:
        vmax = 1.0
    res.field(label, positive, units=units, vmin=0.0, vmax=vmax, cmap="turbo")

    def peak(name: str) -> float | None:
        arr = arrays[name]
        return float(np.nanmax(arr)) if np.isfinite(arr).any() else None

    res.scalars(
        [
            {"label": "Max MESH", "value": peak("MESH"), "unit": "mm"},
            {"label": "Max SHI", "value": peak("SHI"), "unit": "J/m/s"},
            {"label": "Max POSH", "value": peak("POSH"), "unit": "%"},
            {"label": "Max hail kinetic energy flux", "value": peak("HKE"), "unit": "J/m²/s"},
            {
                "label": f"Columns with MESH ≥ {min_mesh_mm:g} mm",
                "value": int((np.nan_to_num(arrays["MESH"]) >= min_mesh_mm).sum()),
            },
            {"label": "Hail cells outlined", "value": len(regions)},
            {
                "label": "Columns with a cut-off top",
                "value": f"{100.0 * float(np.nanmean(truncated > 0)):.1f} %",
            },
            {
                "label": "Levels used",
                "value": f"0 °C {freezing_level_m:.0f} m, -20 °C {minus20_level_m:.0f} m ASL",
            },
            {"label": "Sweeps used", "value": len(A.sweep_names(tree))},
            {"label": "radarx", "value": A.radarx_version()},
        ]
    )

    rows, features = [], []
    for i, reg in enumerate(regions, start=1):
        rid = f"H{i:02d}"
        sel_az = reg["centroid_az"]
        rows.append(
            {
                "id": rid,
                "azimuth": round(sel_az, 1),
                "range_km": round(reg["centroid_range_m"] / 1000.0, 1),
                "max_mesh": round(reg["peak"], 1),
                "mean_mesh": round(reg["mean"], 1),
                "area_km2": round(reg["area_km2"], 1),
            }
        )
        label_txt = f"{reg['peak']:.0f} mm"
        if reg["outline"]:
            features.append(
                polygon(rid, reg["outline"], label=label_txt, color="#dc2626", fill=0.15)
            )
        else:
            features.append(
                {
                    "id": rid,
                    "kind": "point",
                    "frame": "polar",
                    "coords": [[sel_az, reg["centroid_range_m"]]],
                    "label": label_txt,
                    "color": "#dc2626",
                }
            )
    res.table(
        f"Hail cells (MESH ≥ {min_mesh_mm:g} mm)",
        [
            {"key": "id", "label": "Cell"},
            {"key": "azimuth", "label": "Azimuth", "unit": "°", "digits": 1},
            {"key": "range_km", "label": "Range", "unit": "km", "digits": 1},
            {"key": "max_mesh", "label": "Max MESH", "unit": "mm", "digits": 1},
            {"key": "mean_mesh", "label": "Mean MESH", "unit": "mm", "digits": 1},
            {"key": "area_km2", "label": "Area", "unit": "km²", "digits": 1},
        ],
        rows,
        id_key="id",
    )
    res.overlay("Hail cells", features)
    res.text(f"Showing: {long_name}.\n\n{NOTES}", title="About this method")
    return A.add_warnings(res, warns)

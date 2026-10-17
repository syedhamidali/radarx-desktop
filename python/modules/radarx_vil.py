"""radarx: vertically integrated liquid, VIL density and echo-top height of a volume."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

from engine.plugins import ModuleError, Result, module, param


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
    "vil": ("VIL (radarx)", "kg/m²", 1.0, "turbo"),
    "vil_density": ("VIL density (radarx)", "g/m³", 1.0, "turbo"),
    "echo_top": ("Echo top (radarx)", "km", 1e-3, "turbo"),
}

NOTES = """\
Column products of the whole volume (radarx.retrieve.vil / vil_density / echo_top). A column \
is an (azimuth, ground range) position of the lowest sweep; every other sweep contributes the \
value of its nearest ray and gate at its beam height (4/3 effective Earth radius). The result \
is therefore on the polar grid of the LOWEST sweep and is mapped onto the sweep you selected \
by nearest azimuth and ground range - select the lowest sweep to see it unchanged.

VIL = 3.44e-6 sum[ ((Z_i + Z_i+1)/2)^(4/7) dh ] kg/m2 with Z in mm6/m3 (Greene and Clark 1972, \
coefficient confirmed by Seo et al. 2020); reflectivity above the cap (56 dBZ) is limited to \
it as in the operational product. Nothing is assumed below the lowest or above the highest \
beam: near the radar (cone of silence) and at long range VIL is a lower bound unless \
'Fill below lowest beam' is on (it extends the lowest value down to the ground; radarx \
finds the result about 0.78 of the Level III DVL product for light VIL and ~0.99 of MRMS VIL \
- one event, not a calibration). VIL density is VIL divided by the echo-top height above the \
radar (Amburn and Wolf 1997; >3.5 g/m3 is used as a large-hail hint, definition not checked \
against the paper). Echo top is the highest 18 dBZ (default) height interpolated in dBZ \
between beams after Lakshmanan et al. (2013), in km above sea level. The reflectivity of the \
melting layer is enhanced, so VIL over a bright band is overestimated."""


@module(
    id="radarx_vil",
    title="VIL / Echo Top (radarx)",
    description=(
        "radarx.retrieve.vil, vil_density, echo_top: volume column products from all sweeps - "
        "vertically integrated liquid (Greene & Clark 1972), VIL density (Amburn & Wolf 1997) "
        "and echo-top height (Lakshmanan et al. 2013), shown on the selected sweep's grid."
    ),
    category="Storm",
    requires=["DBZH"],
    needs=A.NEEDS,
    scope="volume",
    params=[
        param("product", "select", "vil", "Product", options=list(PRODUCTS)),
        param(
            "fill_below",
            "bool",
            True,
            "Fill below lowest beam",
            help="Extend the lowest valid value down to the ground (like a pseudo-CAPPI)",
        ),
        param("dbz_cap", "number", 56.0, "Reflectivity cap (dBZ)", min=40, max=80, step=1),
        param(
            "min_dbz",
            "number",
            0.0,
            "No-echo below (dBZ)",
            min=-20,
            max=40,
            step=1,
            help="Values below this count as no echo",
        ),
        param(
            "echo_top_dbz",
            "number",
            18.0,
            "Echo-top threshold (dBZ)",
            min=0,
            max=40,
            step=1,
            help="Echo top and VIL density only",
        ),
    ],
    version="1.0",
)
def run(ctx, product, fill_below, dbz_cap, min_dbz, echo_top_dbz):
    rt = A.rx()
    ds = A.prepare_sweep(ctx)
    tree = ctx.datatree
    if len(A.sweep_names(tree)) < 2:
        raise ModuleError("VIL and echo top need a volume with several sweeps; this file has one")
    dbz_name = ctx.find("DBZH").name
    ctx.progress(20, f"Integrating {len(A.sweep_names(tree))} sweeps")
    with A.captured_warnings() as warns, A.guard("Column products"):
        if product == "vil":
            out = rt.vil(
                tree,
                dbz_name,
                fill_below=bool(fill_below),
                dbz_cap=float(dbz_cap),
                min_dbz=float(min_dbz),
            )
            da = out["VIL"]
            truncated = out["VIL_TOP_TRUNCATED"]
        elif product == "vil_density":
            da = rt.vil_density(
                tree,
                dbz_name,
                fill_below=bool(fill_below),
                dbz_cap=float(dbz_cap),
                min_dbz=float(min_dbz),
                threshold=float(echo_top_dbz),
            )
            da = da[next(iter(da.data_vars))] if hasattr(da, "data_vars") else da
            truncated = None
        else:
            da = rt.echo_top(tree, dbz_name, threshold=float(echo_top_dbz))
            truncated = None

    name, units, scale, cmap = PRODUCTS[product]
    ground = da["ground_range"].values if "ground_range" in da.coords else None
    if ground is None:
        raise ModuleError("radarx returned no ground_range coordinate; cannot map to the sweep")
    ray_dim = next(d for d in da.dims if d != "range")
    src = da.transpose(ray_dim, "range")
    mapped = A.remap_polar(src.values, src[ray_dim].values, ground, ds) * np.float32(scale)
    if product == "vil":
        mapped = np.where(mapped > 0, mapped, np.nan).astype(np.float32)  # 0 = no echo
    finite = np.isfinite(mapped)
    peak = float(np.nanmax(mapped)) if finite.any() else None

    res = Result()
    top = float(np.nanpercentile(mapped[finite], 99.5)) if finite.any() else 1.0
    lo = 0.0
    res.field(name, mapped, units=units, vmin=lo, vmax=max(top, 1e-3), cmap=cmap)
    scal = [
        {"label": "Maximum", "value": peak, "unit": units},
        {"label": "Gates with echo", "value": int(finite.sum())},
    ]
    if finite.any():
        ia, ir = np.unravel_index(np.nanargmax(mapped), mapped.shape)
        scal.append(
            {
                "label": "Maximum at (azimuth / range)",
                "value": (
                    f"{float(ds['azimuth'].values[ia]):.1f} deg / "
                    f"{float(ds['range'].values[ir]) / 1000:.1f} km"
                ),
            }
        )
    if truncated is not None:
        t = A.remap_polar(
            truncated.transpose(ray_dim, "range").values.astype(np.float32),
            src[ray_dim].values,
            ground,
            ds,
        )
        scal.append(
            {
                "label": "Columns whose top is cut off by the highest beam",
                "value": f"{100.0 * float(np.nanmean(t > 0)):.1f} %",
            }
        )
    scal.append(
        {
            "label": "Sweeps used",
            "value": len(A.sweep_names(tree)),
        }
    )
    scal.append({"label": "radarx", "value": A.radarx_version()})
    res.scalars(scal)
    res.text(NOTES, title="About this method")
    return A.add_warnings(res, warns)

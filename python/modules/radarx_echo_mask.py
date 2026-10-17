"""radarx: meteorological / non-meteorological echo classification (QC)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

from engine.plugins import Result, module, param


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

CATEGORIES = [
    {"value": 1, "label": "Meteorological", "color": "#22c55e"},
    {"value": 2, "label": "Non-meteorological", "color": "#ef4444"},
    {"value": 3, "label": "Speckle (removed)", "color": "#f59e0b"},
]

NOTES = """\
Gate-by-gate fuzzy-logic separation of weather echo from clutter, insects/birds, noise and \
anomalous propagation (radarx.retrieve.echo_mask).

method "radarx": weighted mean of trapezoidal memberships of RHOHV, ZDR, the along-ray texture \
of ZDR and PHIDP, DBZH texture and the 'spin' count, in the manner of Gourley et al. (2007) \
and Krause (2016); membership corners, weights and the 0.6 threshold are radarx's own choices. \
method "krause2016": the MetSignal algorithm of Krause (2016, JTECH 33, 1875-1885) with its \
published memberships and weights (threshold 0.8 warm season, 0.7 cold season).

Fields that the sweep does not have are left out of the weighted mean, so reflectivity-only \
sweeps still work (texture features only) but are much less reliable. On NEXRAD split cuts the \
Doppler (velocity) cuts carry no dual-pol fields: run this on the surveillance cut of the same \
elevation instead. Speckle = small connected meteorological regions removed by min_size."""


@module(
    id="radarx_echo_mask",
    title="Echo Classification (radarx)",
    description=(
        "radarx.retrieve.echo_mask: fuzzy-logic gate classification into meteorological and "
        "non-meteorological echo (Gourley et al. 2007 / Krause 2016 style). Run it before "
        "KDP, hydrometeor classification or rain rates."
    ),
    category="QC",
    requires=["DBZH"],
    optional=["ZDR", "RHOHV", "PHIDP", "VRADH"],
    needs=A.NEEDS,
    params=[
        param(
            "method",
            "select",
            "radarx",
            "Method",
            options=["radarx", "krause2016"],
            help="'radarx' = radarx's own memberships; 'krause2016' = published MetSignal scheme",
        ),
        param(
            "threshold",
            "number",
            0.0,
            "Score threshold (0 = default)",
            min=0,
            max=1,
            step=0.05,
            help="Default 0.6 (radarx) or 0.8/0.7 warm/cold (krause2016)",
        ),
        param(
            "season",
            "select",
            "warm",
            "Season (krause2016, default threshold)",
            options=["warm", "cold"],
        ),
        param(
            "window_km",
            "number",
            0.0,
            "Texture window (km, 0 = default 1.5)",
            min=0,
            max=10,
            step=0.25,
            help="Only for method 'radarx'",
        ),
        param(
            "min_size",
            "int",
            0,
            "Min region size (gates, 0 = default)",
            min=0,
            max=500,
            step=1,
            help="Smaller connected meteorological regions are removed as speckle",
        ),
        param("include_score", "bool", False, "Also show the meteorological score (0-1)"),
    ],
    version="1.0",
)
def run(ctx, method, threshold, season, window_km, min_size, include_score):
    rt = A.rx()
    ds = A.prepare_sweep(ctx)
    ctx.progress(20, "Classifying echo")
    kwargs: dict = {"method": method}
    if threshold > 0:
        kwargs["threshold"] = float(threshold)
    elif method == "krause2016" and season == "cold":
        kwargs["threshold"] = 0.7
    if method == "radarx" and window_km > 0:
        kwargs["window"] = float(window_km)
    if min_size > 0:
        kwargs["min_size"] = int(min_size)
    with A.captured_warnings() as warns, A.guard("Echo classification"):
        out = rt.echo_mask(ds, **kwargs)

    ctx.progress(80, "Preparing result")
    cls = A.polar_array(out["ECHO_CLASS"], ds, "ECHO_CLASS")
    shown = np.where(cls > 0, cls, np.nan).astype(np.float32)
    echo = int((cls > 0).sum())
    met = int((cls == 1).sum())
    non = int((cls == 2).sum())
    spk = int((cls == 3).sum())
    used = [n for n in ("DBZH", "ZDR", "RHOHV", "PHIDP", "VRADH") if ctx.has(n)]

    res = Result()
    res.field("Echo class", shown, categories=CATEGORIES, units="class")
    if include_score:
        score = A.polar_array(out["METEO_SCORE"], ds, "METEO_SCORE")
        res.field("Meteorological score", score, vmin=0.0, vmax=1.0, units="", cmap="viridis")
    res.scalars(
        [
            {"label": "Gates with echo", "value": echo},
            {"label": "Meteorological", "value": met},
            {"label": "Non-meteorological", "value": non},
            {"label": "Speckle removed", "value": spk},
            {
                "label": "Meteorological share of echo",
                "value": 100.0 * met / echo if echo else None,
                "unit": "%",
            },
            {"label": "Fields used", "value": ", ".join(used)},
            {"label": "radarx", "value": A.radarx_version()},
        ]
    )
    res.text(NOTES, title="About this method")
    return A.add_warnings(res, warns)

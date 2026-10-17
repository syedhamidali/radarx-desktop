"""radarx: fuzzy-logic hydrometeor classification."""

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

NOTES = """\
radarx.retrieve.hid assigns every gate the class with the highest fuzzy-logic score computed \
from DBZH, ZDR, KDP and RHOHV (and temperature / melting-layer height when known).

method "park" (S band, default): the WSR-88D algorithm of Park et al. (2009, WAF 24, 730-748): \
trapezoidal membership functions (their Table 1), class-dependent weights (Table 2), additive \
aggregation, a confidence vector for attenuation (PHIDP), low RHOHV and beam blockage, hard \
class thresholds (Table 3) and - when the melting layer is known - the allowed classes by \
beam height relative to it. Classes: dry snow, wet snow, ice crystals, graupel, big drops, \
light/moderate rain, heavy rain, rain-hail mixture. method "dolan" (C and X band): beta \
membership functions of Dolan and Rutledge (2009) / Dolan et al. (2013) with their hybrid \
aggregation (radarx applies the 2013 aggregation at every band). method "thompson": winter \
precipitation scheme of Thompson et al. (2014).

Practical notes: KDP is estimated from PHIDP with radarx.retrieve.estimate_kdp when the sweep \
has no KDP. Give the freezing-level height (m above sea level, from a sounding or model) so \
that the Park scheme knows where the melting layer is; without it, snow classes can appear \
at low levels and rain classes aloft. Gates removed by the echo mask (on by default) stay \
unclassified. Fields are used as they are: correct attenuation and ZDR calibration first. \
Scores are not probabilities."""


@module(
    id="radarx_hid",
    title="Hydrometeor Classification (radarx)",
    description=(
        "radarx.retrieve.hid: fuzzy-logic hydrometeor identification - Park et al. (2009) at "
        "S band, Dolan et al. (2013) at C/X band, Thompson et al. (2014) for winter. "
        "Returns a colour-coded class map (rain, big drops, graupel, hail mixture, snow, ...)."
    ),
    category="Dual-pol",
    requires=["DBZH", "ZDR"],
    optional=["RHOHV", "PHIDP", "KDP"],
    needs=A.NEEDS,
    params=[
        param(
            "method",
            "select",
            "auto",
            "Method",
            options=["auto", "park", "dolan", "thompson"],
            help="auto = Park at S band, Dolan at C/X band",
        ),
        param(
            "band",
            "select",
            "S",
            "Radar band",
            options=["S", "C", "X"],
            help="NEXRAD WSR-88D = S",
        ),
        param(
            "freezing_level_m",
            "number",
            0.0,
            "Freezing level (m ASL, 0 = unknown)",
            min=0,
            max=8000,
            step=100,
            help="Sets the melting layer for the Park scheme",
        ),
        param(
            "ml_thickness_m",
            "number",
            500.0,
            "Melting layer thickness (m)",
            min=100,
            max=2000,
            step=100,
            help="Depth below the freezing level treated as melting layer (radarx choice)",
        ),
        param("use_echo_mask", "bool", True, "Apply radarx echo mask first"),
        param(
            "min_confidence",
            "number",
            0.0,
            "Min class score (0-0.95)",
            min=0,
            max=0.95,
            step=0.05,
            help="Gates whose best score is not above this stay unclassified",
        ),
    ],
    version="1.0",
)
def run(ctx, method, band, freezing_level_m, ml_thickness_m, use_echo_mask, min_confidence):
    rt = A.rx()
    ds = A.prepare_sweep(ctx)
    notes: list[str] = []
    with A.captured_warnings() as warns:
        ds, estimated = A.with_kdp(rt, ds, band=band)
        if estimated:
            notes.append(
                "KDP was estimated from PHIDP (radarx.retrieve.estimate_kdp, band as set)."
            )
        kwargs: dict = {
            "method": method,
            "band": band,
            "min_confidence": float(min_confidence),
            "scores": False,
        }
        if use_echo_mask:
            ctx.progress(15, "Echo mask")
            kwargs["mask"] = A.meteo_mask(rt, ds)
        if freezing_level_m > 0:
            kwargs["melting_layer"] = (
                float(max(freezing_level_m - ml_thickness_m, 0.0)),
                float(freezing_level_m),
            )
        ctx.progress(40, "Classifying hydrometeors")
        with A.guard("Hydrometeor classification"):
            out = rt.hid(ds, **kwargs)

    hid = A.polar_array(out["HID"], ds, "HID")
    cats = A.hid_categories(out["HID"].attrs)
    shown = np.where(hid > 0, hid, np.nan).astype(np.float32)
    classified = int((hid > 0).sum())
    used = out["HID"].attrs
    res = Result()
    res.field("Hydrometeor class (radarx)", shown, categories=cats, units="class")
    rows = []
    for c in cats:
        count = int((hid == c["value"]).sum())
        rows.append(
            {
                "class": c["label"],
                "code": c["value"],
                "gates": count,
                "share": round(100.0 * count / classified, 1) if classified else 0.0,
            }
        )
    rows.sort(key=lambda r: -r["gates"])
    res.scalars(
        [
            {"label": "Classified gates", "value": classified},
            {
                "label": "Most common class",
                "value": rows[0]["class"] if rows and rows[0]["gates"] else None,
            },
            {
                "label": "Method / band",
                "value": f"{used.get('method', method)} / {used.get('band', band)}",
            },
            {
                "label": "Melting layer",
                "value": (
                    f"{freezing_level_m - ml_thickness_m:.0f}-{freezing_level_m:.0f} m ASL"
                    if freezing_level_m > 0
                    else "not given"
                ),
            },
            {"label": "Echo mask", "value": "on" if use_echo_mask else "off"},
            {"label": "radarx", "value": A.radarx_version()},
        ]
    )
    res.table(
        "Class statistics",
        [
            {"key": "class", "label": "Class"},
            {"key": "code", "label": "Code", "digits": 0},
            {"key": "gates", "label": "Gates", "digits": 0},
            {"key": "share", "label": "Share", "unit": "%", "digits": 1},
        ],
        rows,
        id_key="class",
    )
    notes.append(NOTES)
    res.text("\n".join(notes), title="About this method")
    return A.add_warnings(res, warns)

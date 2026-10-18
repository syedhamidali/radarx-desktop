"""radarx: melting layer (bright band) detection from Z, ZDR and RhoHV."""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np

from engine.plugins import ModuleError, Result, module, param


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
NAMES = ["DBZH", "ZDR", "RHOHV"]
CATEGORIES = [
    {"value": 1, "label": "Below the layer (liquid)", "color": "#3b82f6"},
    {"value": 2, "label": "In the melting layer", "color": "#f59e0b"},
    {"value": 3, "label": "Above the layer (ice)", "color": "#cbd5e1"},
]

NOTES = """\
The melting layer shows in dual-polarisation data as a RhoHV minimum with maxima of Z and ZDR \
(bright band). Three radarx detectors:

ppi (radarx.retrieve.melting_layer_ppi, after Giangrande et al. 2008): on every ray of the \
sweeps between the chosen elevations (4-10 degrees by default) gates with 0.90 < RhoHV < 0.97 \
and a Z (30-47 dBZ) and ZDR (0.8-2.5 dB) maximum within 500 m above them mark the layer; the \
bottom and top are the 20th and 80th percentile of the heights of these points within a \
sliding azimuth sector. It keeps the azimuth dependence and needs no assumption of a \
horizontally uniform layer, but needs sweeps at 4-10 degrees and a layer between ~1 and 6 km \
(the beam width blurs the layer; that correction is not applied).

profile (radarx.retrieve.rd_qvp + melting_layer): the range-defined quasi-vertical profile of all \
sweeps within the radius (azimuthal means, Tobin and Kumjian 2017) is searched for the ZDR peak \
with a co-located RhoHV minimum and Z maximum; top and bottom are where RhoHV returns to its \
background (onset). The thresholds are radarx's own, looser than the paper's. Assumes \
precipitation all around the radar.

profile_gradient (radarx.retrieve.melting_layer_gradient, Allabakash et al. 2019): the same \
profile, with top and bottom from the gradients of Z, ZDR and RhoHV and their products. Made \
for stratiform precipitation; the search window keeps it near the layer.

The field shows each echo gate of the selected sweep below / in / above the detected layer \
(beam-centre height, 4/3 Earth model); for the ppi method the layer of the gate's own azimuth \
is used where it exists. Heights are above sea level. Convective storms with hail, strong \
updrafts or no horizontally uniform stratiform echo give poor or no detections. Entering the \
freezing level (profile method) restricts the search to the range around it."""


@module(
    id="radarx_melting_layer",
    title="Melting Layer (radarx)",
    description=(
        "radarx.retrieve.melting_layer_ppi / melting_layer / melting_layer_gradient: height "
        "of the bright band from the RhoHV minimum with Z and ZDR maxima (Giangrande et al. "
        "2008; Allabakash et al. 2019). Shows gates below / in / above the layer, with "
        "top, bottom and thickness."
    ),
    category="Dual-pol",
    requires=["DBZH", "ZDR", "RHOHV"],
    optional=["KDP", "PHIDP"],
    needs=A.NEEDS,
    scope="volume",
    params=[
        param(
            "method",
            "select",
            "ppi",
            "Method",
            options=["ppi", "profile", "profile_gradient"],
            help="ppi = radial rules on 4-10° sweeps (per azimuth); profile = RD-QVP of the "
            "whole volume; profile_gradient = RD-QVP with gradient rules (needs newer radarx)",
        ),
        param(
            "min_elevation",
            "number",
            4.0,
            "PPI: min elevation (°)",
            min=0.5,
            max=20,
            step=0.5,
            help="PPI method. Lowest sweep elevation used (Giangrande et al.: 4)",
        ),
        param(
            "max_elevation",
            "number",
            10.0,
            "PPI: max elevation (°)",
            min=1,
            max=30,
            step=0.5,
            help="PPI method. Highest sweep elevation used (Giangrande et al.: 10)",
        ),
        param(
            "sector_deg",
            "number",
            20.0,
            "PPI: azimuth sector (°)",
            min=5,
            max=120,
            step=5,
            help="Width of the sliding azimuth window of the percentiles",
        ),
        param(
            "min_points",
            "int",
            20,
            "PPI: min points per sector",
            min=3,
            max=500,
            step=1,
            help="PPI method. Fewest layer points in a sector for a top and bottom",
        ),
        param(
            "rmax_km",
            "number",
            50.0,
            "Profile: radius (km)",
            min=10,
            max=150,
            step=5,
            help="Only gates within this ground distance enter the profile",
        ),
        param(
            "dz_m",
            "number",
            100.0,
            "Profile: height step (m)",
            min=50,
            max=500,
            step=50,
            help="Profile methods. Height spacing of the range-defined QVP",
        ),
        param(
            "search_min_m",
            "number",
            1000.0,
            "Search from (m ASL)",
            min=0,
            max=6000,
            step=100,
            help="Lowest height searched for the layer",
        ),
        param(
            "search_max_m",
            "number",
            6000.0,
            "Search to (m ASL)",
            min=1000,
            max=10000,
            step=100,
            help="Highest height searched for the layer",
        ),
        param(
            "freezing_level_m",
            "number",
            0.0,
            "Freezing level (m ASL, 0 = unknown)",
            min=0,
            max=8000,
            step=100,
            help="Profile method: searches within -1 km / +0.5 km of it and reports the offset",
        ),
    ],
    version="1.0",
)
def run(
    ctx,
    method,
    min_elevation,
    max_elevation,
    sector_deg,
    min_points,
    rmax_km,
    dz_m,
    search_min_m,
    search_max_m,
    freezing_level_m,
):
    rt = A.rx()
    if search_max_m <= search_min_m:
        raise ModuleError("'Search to' must be above 'Search from'")
    if method == "ppi" and max_elevation <= min_elevation:
        raise ModuleError("PPI max elevation must be above the min elevation")
    if method == "ppi":
        func = X.need(rt, "melting_layer_ppi", "The PPI melting layer")
    elif method == "profile":
        func = X.need(rt, "melting_layer", "The profile melting layer")
        X.need(rt, "rd_qvp", "The profile melting layer")
    else:
        func = X.need(rt, "melting_layer_gradient", "The gradient melting layer")
        X.need(rt, "rd_qvp", "The gradient melting layer")

    ds = X.selected_sweep(ctx)
    ctx.progress(5, "Reading the volume")
    vol = X.volume(
        ctx,
        require=tuple(NAMES),
        elevation_range=(min_elevation, max_elevation) if method == "ppi" else None,
        progress=(5, 40),
    )
    if not vol.names:
        what = f"between {min_elevation:g}° and {max_elevation:g}° " if method == "ppi" else ""
        raise ModuleError(
            f"No sweep {what}has reflectivity, ZDR and RhoHV. "
            + (
                "The PPI method needs dual-pol sweeps at moderate elevation (widen the "
                "elevation range, or use the profile method). "
                if method == "ppi"
                else ""
            )
            + X.describe_skipped(vol)
        )
    notes: list[str] = []
    profile = None
    per_az = None
    ctx.progress(50, "Detecting the melting layer")
    with A.captured_warnings() as warns, A.guard("Melting layer"):
        if method == "ppi":
            out = func(
                vol.tree,
                elevation_range=(float(min_elevation), float(max_elevation)),
                sector=float(sector_deg),
                min_points=int(min_points),
                height_max=float(search_max_m),
            )
            top = _scalar(out["melting_layer_top_all"])
            bottom = _scalar(out["melting_layer_bottom_all"])
            points = int(out["melting_layer_points_all"].values)
            per_az = out
            reference = None
        else:
            profile = rt.rd_qvp(vol.tree, NAMES, rmax=float(rmax_km) * 1000.0, dz=float(dz_m))
            if method == "profile":
                kwargs = {"height_range": (float(search_min_m), float(search_max_m))}
                if freezing_level_m > 0:
                    kwargs["freezing_level"] = float(freezing_level_m)
                found = func(profile, **kwargs)
            else:
                found = func(
                    profile,
                    height_range=(float(search_min_m), float(search_max_m)),
                    top_window=500.0,
                )
            top = _scalar(found["melting_layer_top"])
            bottom = _scalar(found["melting_layer_bottom"])
            points = 0
            reference = float(freezing_level_m) if freezing_level_m > 0 else None
            if "melting_layer_peak" in found:
                peak = _scalar(found["melting_layer_peak"])
                if math.isfinite(peak):
                    notes.append(f"ZDR peak of the layer at {peak:.0f} m ASL.")

    detected = math.isfinite(top) and math.isfinite(bottom) and top > bottom
    res = Result()
    items: list[dict] = [
        {"label": "Melting layer", "value": "detected" if detected else "not detected"},
        {"label": "Top", "value": top if detected else None, "unit": "m ASL"},
        {"label": "Bottom", "value": bottom if detected else None, "unit": "m ASL"},
        {"label": "Thickness", "value": top - bottom if detected else None, "unit": "m"},
    ]
    if detected and reference is not None:
        items.append({"label": "Top minus freezing level", "value": top - reference, "unit": "m"})
    if method == "ppi":
        items.append({"label": "Points in the layer", "value": points})
    items += [
        {"label": "Method", "value": method},
        {"label": "Sweeps used", "value": len(vol.names)},
        {"label": "radarx", "value": X.version()},
    ]

    if detected:
        shown = _classify(ds, vol.site.get("altitude", 0.0), top, bottom, per_az)
        res.field("Melting layer position (radarx)", shown, categories=CATEGORIES, units="class")
    else:
        notes.append(
            "No melting layer was detected. It needs stratiform-type precipitation with a "
            "RhoHV dip and Z/ZDR maxima inside the searched heights; widen 'Search from/to', "
            "give the freezing level, try the other method, or use a volume with more "
            "high-elevation sweeps (ppi) / more sweeps within the radius (profile)."
        )
    res.scalars(items)
    if per_az is not None:
        res.table(
            "Melting layer by azimuth (sector centres)",
            _az_columns(),
            _az_rows(per_az, top, bottom),
        )
    if profile is not None:
        res.table(
            "Profile used (range-defined QVP, heights above sea level)",
            [
                {"key": "height_asl_km", "label": "Height ASL", "unit": "km", "digits": 2},
                {"key": "DBZH", "label": "Z", "unit": "dBZ", "digits": 1},
                {"key": "ZDR", "label": "ZDR", "unit": "dB", "digits": 2},
                {"key": "RHOHV", "label": "RhoHV", "digits": 3},
                {"key": "layer", "label": "In layer"},
            ],
            _profile_table(
                profile, top, bottom, detected, search_min_m - 1000.0, search_max_m + 1500.0
            ),
        )
    if X.describe_skipped(vol):
        notes.append("Sweeps left out: " + X.describe_skipped(vol))
    res.text("\n".join([*notes, NOTES]), title="About this method")
    return A.add_warnings(res, warns)


def _scalar(da) -> float:
    vals = np.asarray(da.values, dtype=np.float64).ravel()
    vals = vals[np.isfinite(vals)]
    return float(vals[0]) if vals.size else float("nan")


def _classify(ds, altitude, top, bottom, per_az):
    """1 below / 2 in / 3 above the layer for every echo gate of the selected sweep."""
    rng = np.asarray(ds["range"].values, dtype=np.float64)
    h = X.beam_height_asl(rng, A.sweep_elevation(ds), altitude)[None, :]
    n_az = ds.sizes["azimuth"]
    tops = np.full(n_az, top)
    bots = np.full(n_az, bottom)
    if per_az is not None:
        src = np.asarray(per_az["azimuth"].values, dtype=np.float64)
        ray, gap = A.nearest_ray(src, np.asarray(ds["azimuth"].values, dtype=np.float64))
        t_az = np.asarray(per_az["melting_layer_top"].values, dtype=np.float64)[ray]
        b_az = np.asarray(per_az["melting_layer_bottom"].values, dtype=np.float64)[ray]
        ok = np.isfinite(t_az) & np.isfinite(b_az) & (t_az > b_az) & (gap <= 2.0)
        tops = np.where(ok, t_az, tops)
        bots = np.where(ok, b_az, bots)
    cls = np.where(h < bots[:, None], 1.0, np.where(h > tops[:, None], 3.0, 2.0))
    if "DBZH" in ds.data_vars:
        cls = np.where(np.isfinite(ds["DBZH"].values), cls, np.nan)
    return cls.astype(np.float32)


def _az_columns():
    return [
        {"key": "sector", "label": "Azimuth"},
        {"key": "bottom", "label": "Bottom", "unit": "m ASL", "digits": 0},
        {"key": "top", "label": "Top", "unit": "m ASL", "digits": 0},
        {"key": "thickness", "label": "Thickness", "unit": "m", "digits": 0},
        {"key": "points", "label": "Points", "digits": 0},
    ]


def _az_rows(out, top_all, bottom_all):
    az = np.asarray(out["azimuth"].values, dtype=np.float64)
    top = np.asarray(out["melting_layer_top"].values, dtype=np.float64)
    bot = np.asarray(out["melting_layer_bottom"].values, dtype=np.float64)
    pts = np.asarray(out["melting_layer_points"].values)
    rows = []
    for centre in range(15, 360, 30):
        i = int(np.argmin(np.abs(((az - centre + 180.0) % 360.0) - 180.0)))
        t, b = float(top[i]), float(bot[i])
        have = math.isfinite(t) and math.isfinite(b)
        rows.append(
            {
                "sector": f"{centre - 15:03d}-{centre + 15:03d}°",
                "bottom": round(b) if have else None,
                "top": round(t) if have else None,
                "thickness": round(t - b) if have else None,
                "points": int(pts[i]),
            }
        )
    return rows


def _profile_table(profile, top, bottom, detected, lo, hi):
    rows = []
    for row in X.profile_rows(profile, NAMES):
        h = row["height_asl_km"] * 1000.0
        if lo <= h <= hi:
            row["layer"] = "yes" if detected and bottom <= h <= top else ""
            rows.append(row)
    return rows

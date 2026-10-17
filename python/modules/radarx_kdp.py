"""radarx: differential-phase processing and KDP estimation."""

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
radarx.retrieve.estimate_kdp processes every ray of the raw differential phase along range:

1. gates are meteorological if PHIDP is finite, RHOHV >= min_rhohv (0.85, Park et al. 2009) \
and the circular texture of PHIDP over 2 km is small;
2. the PHIDP sign convention is detected, the system offset is removed (mean of the first \
gates, pooled over the sweep) and the phase is unfolded;
3. the phase is filtered along range ("hubbert": iterative low-pass filtering after Hubbert \
and Bringi 1995; "vulpiani": iterative KDP-bounded reconstruction in the manner of Vulpiani \
et al. 2012; "monotone": least-squares non-decreasing fit, Maesaka et al. 2012);
4. KDP is half the least-squares slope of the processed phase over a window that is short \
(heavy rain, Z >= 40 dBZ) or long (elsewhere), after the WSR-88D choice of Park et al. (2009).

Most thresholds (texture limit, offset gates, filter window, iterations) are radarx's own \
choices, not values from those papers. KDP is NaN at non-meteorological gates. For C and X \
band set the band: the S-band defaults of the texture limit and backscatter threshold are \
scaled by 1 (S), 2 (C) or 3 (X) (radarx choices). Values below about 0.3 deg/km are \
dominated by noise (see the uncertainty field)."""


@module(
    id="radarx_kdp",
    title="KDP Estimation (radarx)",
    description=(
        "radarx.retrieve.estimate_kdp: processes PHIDP (offset, unfolding, range filtering; "
        "Hubbert & Bringi 1995 / Vulpiani 2012 / Maesaka 2012 style) and estimates the "
        "specific differential phase KDP with a reflectivity-adaptive window (Park et al. 2009)."
    ),
    category="Dual-pol",
    requires=["PHIDP"],
    optional=["RHOHV", "DBZH"],
    needs=A.NEEDS,
    params=[
        param(
            "output",
            "select",
            "KDP",
            "Show",
            options=["KDP", "PHIDP_processed", "KDP_UNCERTAINTY"],
            help="KDP (deg/km), the processed PHIDP (deg) or the KDP standard deviation",
        ),
        param(
            "method",
            "select",
            "hubbert",
            "Filtering method",
            options=["hubbert", "vulpiani", "monotone"],
        ),
        param(
            "band",
            "select",
            "S",
            "Radar band",
            options=["S", "C", "X"],
            help="Scales texture/backscatter thresholds (radarx choice: S=1, C=2, X=3)",
        ),
        param(
            "min_rhohv", "number", 0.85, "Min RHOHV of weather gates", min=0.5, max=1.0, step=0.01
        ),
        param(
            "kdp_window_short_km",
            "number",
            2.0,
            "KDP window, Z >= 40 dBZ (km)",
            min=0.5,
            max=6,
            step=0.25,
        ),
        param(
            "kdp_window_long_km",
            "number",
            6.0,
            "KDP window, elsewhere (km)",
            min=1,
            max=15,
            step=0.5,
        ),
        param(
            "filter_window_km",
            "number",
            0.0,
            "Phase filter window (km, 0 = adaptive)",
            min=0,
            max=6,
            step=0.25,
        ),
    ],
    version="1.0",
)
def run(
    ctx, output, method, band, min_rhohv, kdp_window_short_km, kdp_window_long_km, filter_window_km
):
    rt = A.rx()
    ds = A.prepare_sweep(ctx)
    kwargs: dict = {
        "method": method,
        "band": band,
        "min_rhohv": float(min_rhohv),
        "kdp_window": (
            float(min(kdp_window_short_km, kdp_window_long_km)),
            float(max(kdp_window_short_km, kdp_window_long_km)),
        ),
        "phidp": "PHIDP",
    }
    if ctx.has("RHOHV"):
        kwargs["rhohv"] = "RHOHV"
    if ctx.has("DBZH"):
        kwargs["dbz"] = "DBZH"
    if filter_window_km > 0:
        kwargs["filter_window"] = float(filter_window_km)
    ctx.progress(20, "Processing PHIDP and estimating KDP")
    with A.captured_warnings() as warns, A.guard("KDP estimation"):
        out = rt.estimate_kdp(ds, **kwargs)

    kdp = A.polar_array(out["KDP"], ds, "KDP")
    phi = A.polar_array(out["PHIDP_processed"], ds, "PHIDP_processed")
    unc = A.polar_array(out["KDP_UNCERTAINTY"], ds, "KDP_UNCERTAINTY")
    finite = np.isfinite(kdp)
    res = Result()
    if output == "KDP":
        top = float(np.nanpercentile(kdp, 99.5)) if finite.any() else 2.0
        res.field("KDP (radarx)", kdp, units="deg/km", vmin=-1.0, vmax=max(2.0, top), cmap="YlGnBu")
    elif output == "PHIDP_processed":
        res.field("PHIDP processed (radarx)", phi, units="deg", cmap="plasma")
    else:
        res.field("KDP uncertainty (radarx)", unc, units="deg/km", cmap="magma")
    attrs = out["PHIDP_processed"].attrs
    offset = np.asarray(out["PHIDP_OFFSET"].values, dtype=float)
    unc_ok = np.isfinite(unc).any()
    res.scalars(
        [
            {"label": "Gates with KDP", "value": int(finite.sum())},
            {
                "label": "KDP max",
                "value": float(np.nanmax(kdp)) if finite.any() else None,
                "unit": "deg/km",
            },
            {
                "label": "KDP 99th percentile",
                "value": float(np.nanpercentile(kdp, 99)) if finite.any() else None,
                "unit": "deg/km",
            },
            {
                "label": "Median uncertainty",
                "value": float(np.nanmedian(unc)) if unc_ok else None,
                "unit": "deg/km",
            },
            {
                "label": "System PHIDP offset",
                "value": float(np.nanmedian(offset)) if np.isfinite(offset).any() else None,
                "unit": "deg",
            },
            {"label": "PHIDP sign used", "value": attrs.get("phidp_sign")},
            {"label": "Filter window", "value": attrs.get("filter_window"), "unit": "km"},
            {"label": "Method / band", "value": f"{method} / {band}"},
            {"label": "radarx", "value": A.radarx_version()},
        ]
    )
    res.text(NOTES, title="About this method")
    return A.add_warnings(res, warns)

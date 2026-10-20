"""Hydrology: rain rate of one sweep with a selectable relation, uncertainty and beam quality."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

from engine.hydro import qpe as hq
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

RELATIONS = [k for k, r in hq.ZR_RELATIONS.items() if r.kind == "rain"]
SNOW_RELATIONS = [k for k, r in hq.ZR_RELATIONS.items() if r.kind == "snow"]

CATEGORIES = [
    {"value": hq.REL_Z, "label": "R(Z)", "color": "#3b82f6"},
    {"value": hq.REL_ZDR, "label": "R(Z,ZDR)", "color": "#10b981"},
    {"value": hq.REL_KDP, "label": "R(KDP)", "color": "#f59e0b"},
    {"value": hq.REL_A, "label": "R(A)", "color": "#8b5cf6"},
    {"value": hq.REL_SNOW, "label": "S(Z) snow", "color": "#06b6d4"},
]

NOTES = """\
RETRIEVED, not observed: every value is an estimate from a model of the drop size distribution.

* Relations (R in mm/h, Z in mm^6/m^3, zdr = 10^(ZDR/10), KDP in deg/km): R(Z) = a Z^b (default \
WSR-88D Z = 300 R^1.4, Fulton et al. 1998); R(KDP) = 44 |KDP|^0.822 and R(Z,ZDR) = 1.42e-2 Z^0.770 \
zdr^-1.67 at S band (Giangrande & Ryzhkov 2008); R(A) = a(T) A^b(T), Ryzhkov et al. 2014. The \
hybrid chooses per gate with the hydrometeor class (radarx HID, Park et al. 2009) and the R(Z) \
regime (below 6 mm/h R(Z); to 50 mm/h R(Z,ZDR); above R(KDP) where KDP >= 0.3 deg/km).
* Uncertainty is the 1-sigma of ln R: the DSD scatter of the relation (radarx relation_scatter) \
combined with the calibration (Z +-1 dB, ZDR +-0.2 dB) and KDP noise. It does NOT include the \
vertical profile of reflectivity, the bright band, beam overshoot or advection: look at the beam \
height and the quality index, and at the freezing level setting.
* The rate is at the beam height of this sweep, not at the ground. Rain relations are not valid in \
the melting layer or above: give the freezing level to have those gates left empty.
* Hail contaminates Z: reflectivity is capped (53 dBZ by default); R(KDP) is immune to hail \
calibration and attenuation but noisy below 0.3 deg/km."""


@module(
    id="hydro_qpe",
    title="Rain Rate (hydrology QPE)",
    description=(
        "Rain rate from R(Z) with a selectable Z-R relation (WSR-88D, Marshall-Palmer, tropical, "
        "cool-season, snow Z-S), R(KDP), R(Z,ZDR), R(A) or a hybrid that switches by hydrometeor "
        "class and rain-rate regime. Outputs the rate, its uncertainty and the beam quality; "
        "labelled RETRIEVED. For accumulations over a period use `rxd qpe`."
    ),
    category="Hydrology",
    requires=["DBZH"],
    optional=["ZDR", "RHOHV", "PHIDP", "KDP"],
    needs=A.NEEDS,
    params=[
        param(
            "method",
            "select",
            "auto",
            "Method",
            options=["auto", *hq.METHODS[1:]],
            help="auto = hybrid if ZDR and PHIDP/KDP exist, else R(Z,ZDR), else R(Z)",
        ),
        param(
            "relation",
            "select",
            "wsr88d",
            "Z-R relation (rain)",
            options=RELATIONS,
            help="Used by method z and by the light-rain regime of the hybrid",
        ),
        param(
            "precip",
            "select",
            "rain",
            "Precipitation",
            options=["rain", "snow"],
            help="snow uses a Z-S relation (very uncertain); the snow relation is chosen below",
        ),
        param(
            "snow_relation",
            "select",
            SNOW_RELATIONS[0],
            "Z-S relation (snow)",
            options=SNOW_RELATIONS,
        ),
        param("band", "select", "S", "Radar band", options=["S", "C", "X"]),
        param(
            "freezing_level_m",
            "number",
            0.0,
            "Freezing level (m ASL, 0 = unknown)",
            min=0,
            max=8000,
            step=100,
            help="Beams inside the melting layer or above give no rain estimate",
        ),
        param(
            "phase_policy",
            "select",
            "auto",
            "Ice-phase classes (hybrid)",
            options=["auto", "exclude", "rain"],
            help="auto: gates the classification calls snow / ice get no estimate only when a "
            "freezing level is given (the classification needs the temperature; without it "
            "light rain is often labelled snow); otherwise they are estimated as rain and flagged",
        ),
        param("z_cap", "number", 53.0, "Reflectivity cap (dBZ)", min=40, max=70, step=1),
        param(
            "attenuation",
            "select",
            "auto",
            "Attenuation correction",
            options=["auto", "on", "off"],
            help="auto = ZPHI correction at C and X band, flag only at S band",
        ),
        param(
            "sigma_z_db", "number", 1.0, "Z calibration uncertainty (dB)", min=0, max=5, step=0.1
        ),
        param(
            "sigma_zdr_db",
            "number",
            0.2,
            "ZDR calibration uncertainty (dB)",
            min=0,
            max=2,
            step=0.05,
        ),
        param("vmax", "number", 0.0, "Colour scale max (mm/h, 0 = auto)", min=0, max=300, step=5),
    ],
    version="1.0",
)
def run(
    ctx,
    method,
    relation,
    precip,
    snow_relation,
    band,
    freezing_level_m,
    phase_policy,
    z_cap,
    attenuation,
    sigma_z_db,
    sigma_zdr_db,
    vmax,
):
    snow = precip == "snow"
    config = {
        "method": method,
        "relation": snow_relation if snow else relation,
        "precip": precip,
        "band": band,
        "z_cap": float(z_cap) if z_cap else None,
        "attenuation": attenuation,
        "phase_policy": phase_policy,
        "sigma_z_db": float(sigma_z_db),
        "sigma_zdr_db": float(sigma_zdr_db),
    }
    if freezing_level_m > 0:
        config["freezing_level_m"] = float(freezing_level_m)
    ds = A.prepare_sweep(ctx)
    ctx.progress(20, "Estimating rain rate")
    with A.captured_warnings() as warns, A.guard("Rain rate"):
        try:
            sq = hq.estimate_sweep(ds, config)
        except hq.QpeError as exc:
            raise ModuleError(str(exc)) from exc
    ctx.progress(85, "Packing fields")
    rate = sq.to_input_order(sq.rate)
    sigma = sq.to_input_order(sq.sigma)
    quality = sq.to_input_order(sq.quality)
    rel = sq.to_input_order(sq.relation).astype(np.float32)
    rel[rel == 0] = np.nan
    finite = np.isfinite(rate)
    raining = finite & (rate >= 0.1)
    top = (
        float(vmax)
        if vmax > 0
        else max(
            10.0, min(float(np.nanpercentile(rate[raining], 99)) if raining.any() else 10.0, 150.0)
        )
    )
    res = Result()
    res.field("Rain rate (RETRIEVED)", rate, units="mm/h", vmin=0.0, vmax=top, cmap="PrecipRate")
    res.field(
        "Rate uncertainty (1 sigma)",
        np.where(raining, 100.0 * sigma, np.nan),
        units="%",
        vmin=0.0,
        vmax=100.0,
    )
    res.field("Beam quality index", np.where(finite, quality, np.nan), units="", vmin=0.0, vmax=1.0)
    res.field("Relation used", rel, categories=CATEGORIES)
    if sq.snr_db is not None:
        res.field("SNR (dB)", sq.to_input_order(sq.snr_db), units="dB", vmin=0.0, vmax=40.0)
    counts = sq.meta["gate_counts"]
    flags = sq.flags
    scal = [
        {"label": "Method", "value": sq.meta["method"]},
        {"label": "Provenance", "value": "RETRIEVED (estimate, not an observation)"},
        {"label": "Gates with an estimate", "value": int(finite.sum())},
        {"label": "Gates >= 0.1 mm/h", "value": int(raining.sum())},
        {
            "label": "Max rain rate",
            "value": float(np.nanmax(rate)) if finite.any() else None,
            "unit": "mm/h",
        },
        {
            "label": "Mean over raining gates",
            "value": float(rate[raining].mean()) if raining.any() else None,
            "unit": "mm/h",
        },
        {
            "label": "Median 1-sigma uncertainty",
            "value": float(100 * np.nanmedian(sigma[raining])) if raining.any() else None,
            "unit": "%",
        },
        {"label": "Beam elevation", "value": round(sq.elevation_deg, 2), "unit": "deg"},
        {
            "label": "Beam height at 50 km (above radar)",
            "value": float(np.interp(50e3, sq.ground_range_m, sq.beam_height_m)),
            "unit": "m",
        },
        {
            "label": "Gates flagged melting layer / ice",
            "value": int((flags & hq.FLAG_ML > 0).sum()),
        },
        {
            "label": "Gates flagged hail / capped",
            "value": int(((flags & (hq.FLAG_HAIL | hq.FLAG_ZCAP)) > 0).sum()),
        },
        {"label": "Gates flagged attenuation", "value": int(((flags & hq.FLAG_ATTEN) > 0).sum())},
        {
            "label": "Gates with no valid relation",
            "value": int(((flags & hq.FLAG_NOEST) > 0).sum()),
        },
    ]
    res.scalars(scal)
    res.table(
        "Gates by relation",
        [{"key": "relation", "label": "Relation"}, {"key": "gates", "label": "Gates"}],
        [{"relation": k, "gates": v} for k, v in counts.items() if v],
    )
    res.text(sq.meta["relation"], title="Relation used")
    res.text("\n".join([*sq.meta["notes"], "", NOTES]), title="About this method")
    return A.add_warnings(res, warns)

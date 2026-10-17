"""radarx: quantitative precipitation estimation (rain rate) from dual-pol radar."""

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

RELATIONS = {
    "hid_blend": "R chosen from the hydrometeor class (Giangrande & Ryzhkov 2008)",
    "z_zdr": "R(Z, ZDR) = a Z^b zdr^c",
    "kdp": "R(KDP) = a |KDP|^b",
    "z": "R(Z) = a Z^b",
}

NOTES = """\
Power-law rain-rate relations of radarx.retrieve.qpe (all coefficients are printed in the \
cited papers; nothing is fitted here):

* "z": R = a Z^b. S band: 0.017 Z^0.714, the operational WSR-88D relation (Z = 300 R^1.4); \
X band: 0.029 Z^0.67 (Ryzhkov et al. 2014). No C-band default exists - choose another relation.
* "z_zdr": R = a Z^b zdr^c with zdr = 10^(ZDR/10). S band: 1.42e-2 Z^0.770 zdr^-1.67 \
(Giangrande & Ryzhkov 2007, central Oklahoma); C and X band from Bringi & Chandrasekar (2001) \
T-matrix simulations.
* "kdp": R = a |KDP|^b (S band 44.0 |KDP|^0.822; X 16.9 |KDP|^0.801; C 30.5 |KDP|^0.85). \
Independent of calibration and attenuation but noisy below ~0.3 deg/km; negative KDP gives \
negative rate.
* "hid_blend": the relation depends on the hydrometeor class (radarx.retrieve.hid), after \
Giangrande & Ryzhkov (2008): R(Z,ZDR) in rain, R(KDP) in rain-hail mixture, and NO estimate \
(NaN) for hail, graupel, snow, ice and wet snow.

Coefficients depend on the drop size distribution (regime) they were fitted for: S-band \
Oklahoma relations overestimate rain with large drops and underestimate rain with many small \
drops. Rain rates are only as good as the inputs - correct attenuation (C/X band) and ZDR \
calibration first. The rate is the instantaneous gate rate at the beam height of this sweep, \
not rain at the ground, and is meaningless in the melting layer or above it. Reflectivity is \
capped at the chosen value (default 53 dBZ, as in the WSR-88D relation) to limit hail \
contamination."""


@module(
    id="radarx_qpe",
    title="Rain Rate (radarx)",
    description=(
        "radarx.retrieve.rain_rate*: instantaneous rain rate (mm/h) from R(Z), R(Z,ZDR), "
        "R(KDP) or a hydrometeor-class blend (Giangrande & Ryzhkov 2008; Bringi & "
        "Chandrasekar 2001; Ryzhkov et al. 2014). KDP and the classification are computed "
        "with radarx when the sweep lacks them."
    ),
    category="Precipitation",
    requires=["DBZH"],
    optional=["ZDR", "RHOHV", "PHIDP", "KDP"],
    needs=A.NEEDS,
    params=[
        param(
            "relation",
            "select",
            "auto",
            "Relation",
            options=["auto", *RELATIONS],
            help="auto = hid_blend if ZDR and PHIDP/KDP exist, else z_zdr, else z. "
            "hid_blend uses R(Z,ZDR) in rain and R(KDP) in rain-hail mixture",
        ),
        param(
            "band",
            "select",
            "S",
            "Radar band",
            options=["S", "C", "X"],
            help="Selects the default coefficients; no C-band R(Z) exists",
        ),
        param(
            "z_cap",
            "number",
            53.0,
            "Reflectivity cap (dBZ)",
            min=40,
            max=70,
            step=1,
            help="Reflectivity above this is set to it before R(Z) and R(Z,ZDR)",
        ),
        param(
            "min_zdr",
            "number",
            0.0,
            "ZDR floor (dB)",
            min=-3,
            max=3,
            step=0.1,
            help="ZDR below this is raised to it: zdr^-1.67 explodes for negative/noisy ZDR",
        ),
        param("use_echo_mask", "bool", True, "Apply radarx echo mask first"),
        param(
            "freezing_level_m",
            "number",
            0.0,
            "Freezing level (m ASL, 0 = unknown)",
            min=0,
            max=8000,
            step=100,
            help="hid_blend only: sets the melting layer of the classification",
        ),
        param(
            "vmax",
            "number",
            0.0,
            "Colour scale max (mm/h, 0 = auto)",
            min=0,
            max=300,
            step=5,
        ),
    ],
    version="1.0",
)
def run(ctx, relation, band, z_cap, min_zdr, use_echo_mask, freezing_level_m, vmax):
    rt = A.rx()
    ds = A.prepare_sweep(ctx)
    notes: list[str] = []
    if relation == "auto":
        has_phase = ctx.has("KDP") or ctx.has("PHIDP")
        relation = (
            "hid_blend" if ctx.has("ZDR") and has_phase else "z_zdr" if ctx.has("ZDR") else "z"
        )
        notes.append(f"Relation chosen automatically: {relation}.")
    dbz = "DBZH"
    with A.captured_warnings() as warns:
        mask = None
        flag = None
        if use_echo_mask:
            ctx.progress(10, "Echo mask")
            mask = A.meteo_mask(rt, ds)
        if relation in ("kdp", "hid_blend"):
            ds, estimated = A.with_kdp(rt, ds, band=band)
            if estimated:
                notes.append("KDP was estimated from PHIDP (radarx.retrieve.estimate_kdp).")
            if not any(n in ds.data_vars for n in ("KDP", "specific_differential_phase")):
                raise ModuleError(
                    f"The '{relation}' relation needs KDP or PHIDP, which this sweep does not have"
                )
        if relation in ("z_zdr", "hid_blend") and not ctx.has("ZDR"):
            raise ModuleError(
                f"The '{relation}' relation needs ZDR, which this sweep does not have"
            )

        ctx.progress(45, "Estimating rain rate")
        with A.guard("Rain rate"):
            if relation == "z":
                rate = rt.rain_rate_z(ds, dbz, band=band, z_cap=float(z_cap))
            elif relation == "z_zdr":
                rate = rt.rain_rate_z_zdr(
                    ds,
                    dbz,
                    "ZDR",
                    band=band,
                    z_cap=float(z_cap),
                    min_zdr=float(min_zdr),
                )
            elif relation == "kdp":
                rate = rt.rain_rate_kdp(ds, band=band)
            else:
                hid_kwargs: dict = {"band": band, "dbz": dbz, "scores": False}
                if mask is not None:
                    hid_kwargs["mask"] = mask
                if freezing_level_m > 0:
                    hid_kwargs["melting_layer"] = (
                        max(float(freezing_level_m) - 500.0, 0.0),
                        float(freezing_level_m),
                    )
                hid = rt.hid(ds, **hid_kwargs)["HID"]
                out = rt.rain_rate(
                    ds, hid=hid, band=band, z_cap=float(z_cap), dbz=dbz, min_zdr=float(min_zdr)
                )
                rate = out["RAIN_RATE"]
                flag = A.polar_array(out["RAIN_RATE_FLAG"], ds, "RAIN_RATE_FLAG")
    arr = A.polar_array(rate, ds, "rain rate")
    if mask is not None:
        arr = np.where(A.polar_array(mask, ds, "mask") > 0, arr, np.nan).astype(np.float32)

    finite = np.isfinite(arr)
    raining = finite & (arr >= 0.1)
    top = (
        float(vmax)
        if vmax > 0
        else max(
            10.0, min(float(np.nanpercentile(arr[raining], 99)) if raining.any() else 10.0, 150.0)
        )
    )
    res = Result()
    res.field("Rain rate (radarx)", arr, units="mm/h", vmin=0.0, vmax=top, cmap="PrecipRate")
    scal = [
        {"label": "Relation", "value": RELATIONS[relation]},
        {"label": "Gates with an estimate", "value": int(finite.sum())},
        {"label": "Gates >= 0.1 mm/h", "value": int(raining.sum())},
        {
            "label": "Max rain rate",
            "value": float(np.nanmax(arr)) if finite.any() else None,
            "unit": "mm/h",
        },
        {
            "label": "Mean over raining gates",
            "value": float(arr[raining].mean()) if raining.any() else None,
            "unit": "mm/h",
        },
        {"label": "Band", "value": band},
    ]
    if relation == "hid_blend":
        no_rel = int(((flag == 1) & (mask_ok(mask, ds))).sum()) if flag is not None else 0
        scal.append({"label": "Gates with no valid relation (hail, snow, ...)", "value": no_rel})
    scal.append({"label": "radarx", "value": A.radarx_version()})
    res.scalars(scal)
    notes.append(NOTES)
    res.text("\n".join(notes), title="About this method")
    return A.add_warnings(res, warns)


def mask_ok(mask, ds):
    """Boolean ``(n_az, n_range)`` array of the echo mask (all True when no mask is used)."""
    if mask is None:
        return np.ones((ds.sizes["azimuth"], ds.sizes["range"]), dtype=bool)
    return A.polar_array(mask, ds, "mask") > 0

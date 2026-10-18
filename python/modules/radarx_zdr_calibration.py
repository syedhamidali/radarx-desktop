"""radarx: ZDR bias (calibration offset) from light rain, dry snow or vertical pointing."""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import xarray as xr

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
AUTO = -99.0  # sentinel for "use radarx's default" in number parameters

NOTES = """\
ZDR calibration (Ryzhkov et al. 2005, radarx.retrieve.zdr_offset). The offset is the measured \
ZDR minus the value the target must have, so the corrected field is ZDR - offset (shown as \
'ZDR corrected'). Three natural targets:

light_rain (default): rain at 20-25 dBZ with RhoHV >= 0.98, where drops are small and nearly \
spherical; the reference is the ZDR of a Marshall-Palmer drop size distribution at the gate's \
reflectivity (about 0.3-0.4 dB), computed from radarx's scattering tables (a radarx choice; \
Ryzhkov et al. use a disdrometer-based table). Use scans through widespread stratiform or light \
rain at some elevation; the reference is elevation dependent and radarx applies the dependence \
with the sweep's elevation.

dry_snow: dry aggregated snow has an intrinsic ZDR near 0.2 dB (a radarx choice within the usual \
0-0.5 dB range). The gates must be above the melting layer: enter its top in 'Snow above' \
(see the Melting Layer module) - without it the method is refused.

vertical: a vertically pointing (birdbath) scan in rain or snow, where the intrinsic ZDR is 0 dB \
whatever the drop size.

The result is the median (or mean) over the selected gates with its spread and standard error. \
It is only as good as the selection: ground clutter, the melting layer, hail, biological \
scatterers and differential attenuation bias it, and a Z calibration error shifts the \
light-rain reference slightly. Correct attenuation first at C and X band. The offset can change \
with elevation, temperature and time; compare several sweeps and volumes. A bias within about \
0.1 dB is usually considered good; beyond ~0.25 dB hydrometeor classification and \
ZDR-based rainfall estimates are noticeably affected (rules of thumb)."""


@module(
    id="radarx_zdr_calibration",
    title="ZDR Calibration (radarx)",
    description=(
        "radarx.retrieve.zdr_offset: estimates the ZDR bias from light rain, dry snow or "
        "vertical pointing (Ryzhkov et al. 2005) and shows the bias-corrected ZDR. Reports "
        "the offset with its spread, standard error and gate count."
    ),
    category="QC",
    requires=["ZDR", "DBZH"],
    optional=["RHOHV", "SNR"],
    needs=A.NEEDS,
    params=[
        param(
            "method",
            "select",
            "light_rain",
            "Method",
            options=["light_rain", "dry_snow", "vertical"],
            help="Natural target with a known intrinsic ZDR",
        ),
        param(
            "min_rhohv",
            "number",
            0.98,
            "Min RHOHV",
            min=0.8,
            max=1.0,
            step=0.005,
            help="Keeps only pure, uniform precipitation",
        ),
        param(
            "z_low_dbz",
            "number",
            AUTO,
            "Z from (dBZ, -99 = method default)",
            min=-99,
            max=80,
            step=1,
            help="Defaults: light rain 20, dry snow 0, vertical none",
        ),
        param(
            "z_high_dbz",
            "number",
            AUTO,
            "Z to (dBZ, -99 = method default)",
            min=-99,
            max=80,
            step=1,
            help="Defaults: light rain 25, dry snow 30, vertical none",
        ),
        param(
            "snow_above_m",
            "number",
            0.0,
            "Snow above (m ASL, dry snow)",
            min=0,
            max=12000,
            step=100,
            help="Only gates whose beam is higher than this (above the melting layer) are "
            "used; required for dry_snow",
        ),
        param(
            "max_range_km",
            "number",
            0.0,
            "Max range (km, 0 = all)",
            min=0,
            max=400,
            step=5,
            help="Near ranges are less affected by attenuation and beam broadening",
        ),
        param(
            "min_snr_db",
            "number",
            0.0,
            "Min SNR (dB, 0 = off)",
            min=0,
            max=40,
            step=1,
            help="Needs an SNR field on the sweep",
        ),
        param(
            "statistic",
            "select",
            "median",
            "Statistic",
            options=["median", "mean"],
            help="Median is robust against remaining outliers",
        ),
        param(
            "min_count",
            "int",
            100,
            "Min gates",
            min=10,
            max=100000,
            step=10,
            help="Fewer selected gates give no estimate",
        ),
        param(
            "reference_db",
            "number",
            AUTO,
            "Reference ZDR (dB, -99 = auto)",
            min=-99,
            max=3,
            step=0.05,
            help="Intrinsic ZDR at 0° elevation of the target; auto = Marshall-Palmer for light "
            "rain, 0.2 dry snow, 0 vertical",
        ),
        param(
            "band",
            "select",
            "auto",
            "Radar band (light-rain reference)",
            options=["auto", "S", "C", "X"],
            help="auto = from the radar frequency",
        ),
    ],
    version="1.0",
)
def run(
    ctx,
    method,
    min_rhohv,
    z_low_dbz,
    z_high_dbz,
    snow_above_m,
    max_range_km,
    min_snr_db,
    statistic,
    min_count,
    reference_db,
    band,
):
    rt = A.rx()
    zdr_offset = X.need(rt, "zdr_offset", "The ZDR calibration")
    ds = X.selected_sweep(ctx)
    if method == "dry_snow" and snow_above_m <= 0:
        raise ModuleError(
            "The dry-snow method needs the height above which the echo is snow: enter the top "
            "of the melting layer (m ASL) in 'Snow above' (see the Melting Layer module), or "
            "use the light-rain method."
        )
    if (z_low_dbz > AUTO) != (z_high_dbz > AUTO):
        raise ModuleError("Set both 'Z from' and 'Z to', or leave both at -99")
    if z_low_dbz > AUTO and z_high_dbz <= z_low_dbz:
        raise ModuleError("'Z to' must be above 'Z from'")
    use_snr = min_snr_db > 0 and "SNR" in ds.data_vars
    notes: list[str] = []
    if min_snr_db > 0 and not use_snr:
        notes.append("No SNR field on this sweep: the SNR condition was skipped.")
    if "RHOHV" not in ds.data_vars:
        notes.append("No RHOHV on this sweep: the correlation condition was skipped.")

    rng = np.asarray(ds["range"].values, dtype=np.float64)
    mask = None
    if method == "dry_snow" or max_range_km > 0:
        keep = np.ones((ds.sizes["azimuth"], rng.size), dtype=bool)
        if method == "dry_snow":
            height = X.beam_height_asl(
                rng, A.sweep_elevation(ds), X.site_scalars(ctx.datatree).get("altitude", 0.0)
            )
            keep &= (height > float(snow_above_m))[None, :]
        if max_range_km > 0:
            keep &= (rng <= float(max_range_km) * 1000.0)[None, :]
        mask = xr.DataArray(
            keep, dims=("azimuth", "range"), coords={"azimuth": ds["azimuth"], "range": ds["range"]}
        )

    kwargs: dict = {
        "zdr": "ZDR",
        "dbz": "DBZH",
        "min_rhohv": float(min_rhohv),
        "statistic": statistic,
        "min_count": int(min_count),
    }
    if "RHOHV" in ds.data_vars:
        kwargs["rhohv"] = "RHOHV"
    if use_snr:
        kwargs["snr"] = "SNR"
        kwargs["min_snr"] = float(min_snr_db)
    if z_low_dbz > AUTO:
        kwargs["z_range"] = (float(z_low_dbz), float(z_high_dbz))
    if reference_db > AUTO:
        kwargs["reference"] = float(reference_db)
    found_band = X.radar_band(ctx.datatree, ds) if band == "auto" else band
    if found_band:
        kwargs["band"] = found_band
    if mask is not None:
        kwargs["mask"] = mask

    ctx.progress(30, "Estimating the ZDR offset")
    with A.captured_warnings() as warns, A.guard("ZDR calibration"):
        out = zdr_offset(ds, method, **kwargs)

    def val(name):
        if name not in out:
            return float("nan")
        v = np.asarray(out[name].values, dtype=np.float64).ravel()
        return float(v[0]) if v.size else float("nan")

    offset, mean, median = val("ZDR_OFFSET"), val("ZDR_OFFSET_MEAN"), val("ZDR_OFFSET_MEDIAN")
    std, se, count = val("ZDR_OFFSET_STD"), val("ZDR_OFFSET_SE"), val("ZDR_OFFSET_COUNT")
    n = int(count) if math.isfinite(count) else 0
    if not math.isfinite(offset):
        raise ModuleError(
            f"No ZDR offset: only {n} gates passed the selection (need at least {min_count}). "
            f"Select a sweep through widespread, uniform "
            f"{'light rain' if method == 'light_rain' else 'precipitation'}, lower 'Min RHOHV' "
            "or 'Min gates', widen the Z range, or raise 'Max range'."
        )

    zdr = A.polar_array(ds["ZDR"], ds, "ZDR")
    corrected = (zdr - np.float32(offset)).astype(np.float32)
    res = Result()
    res.field(
        "ZDR corrected (radarx offset)",
        corrected,
        units="dB",
        vmin=-1.0,
        vmax=5.0,
        cmap="Spectral_r",
    )
    verdict = _verdict(offset, se)
    res.scalars(
        [
            {"label": "ZDR offset (bias)", "value": offset, "unit": "dB"},
            {"label": "Standard error", "value": se, "unit": "dB"},
            {"label": "Spread of selected gates", "value": std, "unit": "dB"},
            {"label": "Mean of selection", "value": mean, "unit": "dB"},
            {"label": "Median of selection", "value": median, "unit": "dB"},
            {"label": "Gates used", "value": n},
            {"label": "Assessment", "value": verdict},
            {"label": "Method", "value": method},
            {"label": "Statistic", "value": statistic},
            {"label": "Radar band", "value": found_band or "S (assumed)"},
            {"label": "Sweep elevation", "value": A.sweep_elevation(ds), "unit": "°"},
            {"label": "Min RHOHV", "value": float(min_rhohv)},
            {"label": "radarx", "value": X.version()},
        ]
    )
    text = [
        f"ZDR bias {offset:+.2f} dB (± {se:.2f} dB standard error, {n} gates, "
        f"{statistic} of the selection).",
        f"Corrected ZDR = ZDR {'-' if offset >= 0 else '+'} {abs(offset):.2f} dB.",
        f"{verdict[0].upper()}{verdict[1:]}.",
    ]
    res.text("\n".join(text), title="Summary")
    res.text("\n".join([*notes, NOTES]), title="About this method")
    return A.add_warnings(res, warns)


def _verdict(offset: float, se: float) -> str:
    a = abs(offset)
    if a <= 0.1:
        return "within ±0.1 dB: well calibrated"
    if a <= 0.25:
        return "0.1-0.25 dB: small bias, correction recommended"
    return "above 0.25 dB: significant bias, correct before using ZDR products"

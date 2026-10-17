"""radarx: clean radar data - echo mask + SNR gate -> cleaned fields and a QC flag map."""

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

CLEAN, LOW_SNR, NON_MET, SPECKLE = 1, 2, 3, 4
CATEGORIES = [
    {"value": CLEAN, "label": "Clean (meteorological)", "color": "#22c55e"},
    {"value": LOW_SNR, "label": "Low SNR (noise)", "color": "#6b7280"},
    {"value": NON_MET, "label": "Non-meteorological", "color": "#ef4444"},
    {"value": SPECKLE, "label": "Speckle (removed)", "color": "#38bdf8"},
]
LABELS = {c["value"]: c["label"] for c in CATEGORIES}

NOTES = """\
Cleaning = deciding gate by gate whether an echo is weather, then hiding what is not.

1. Low SNR: gates whose signal-to-noise ratio is below the minimum are noise (needs an SNR field; \
without one radarx still uses the texture of the other fields).
2. Non-meteorological: radarx.retrieve.echo_mask, a fuzzy-logic score from RhoHV, ZDR, the \
along-ray texture of ZDR / PhiDP / DBZH (weather is smooth, clutter / insects / noise are not). \
This is more robust than fixed thresholds: it keeps the interior of heavy rain, where RhoHV or \
SNR dip locally, and rejects noisy regions that happen to pass a single threshold.
3. Speckle: small isolated meteorological regions are removed (min size).

Outputs: cleaned reflectivity / ZDR / velocity (hidden where flagged) and the flag map showing why \
each gate was removed. Optional noise-bias correction (Gourley et al. 2006) of ZDR and RhoHV is \
only valid for moments that still contain the noise - leave it off if your data are already noise \
corrected.

Not fixed by any mask: partial beam blockage (radial streaks / dark wedges with ZDR spikes along \
the rays - use radarx beam_blockage with a terrain model), ZDR calibration offsets, attenuation, \
range folding. Run on all sweeps to view the cleaned volume in 3D."""


@module(
    id="radarx_clean",
    title="Clean radar data (radarx QC)",
    description=(
        "radarx echo mask + SNR gate: hides noise, clutter and non-meteorological echo, outputs "
        "cleaned reflectivity / ZDR / velocity and a flag map. Recommended cleaner."
    ),
    category="QC",
    requires=["DBZH"],
    optional=["SNR", "ZDR", "RHOHV", "PHIDP", "VRADH"],
    needs=A.NEEDS,
    params=[
        param(
            "snr_min",
            "number",
            3.0,
            "Min SNR (dB)",
            min=-10,
            max=30,
            step=0.5,
            help="Gates below this signal-to-noise ratio are noise (needs an SNR field).",
        ),
        param(
            "method",
            "select",
            "radarx",
            "Echo-mask method",
            options=["radarx", "krause2016"],
            help="'radarx' = radarx's memberships; 'krause2016' = published MetSignal scheme.",
        ),
        param(
            "threshold",
            "number",
            0.0,
            "Score threshold (0 = default)",
            min=0,
            max=1,
            step=0.05,
            help="Higher = stricter (more removed). Default 0.6 (radarx) / 0.8 (krause2016).",
        ),
        param(
            "min_size",
            "int",
            0,
            "Min region size (gates, 0 = default)",
            min=0,
            max=500,
            step=1,
            help="Smaller connected meteorological regions are removed as speckle.",
        ),
        param(
            "noise_correction",
            "bool",
            False,
            "Also output noise-corrected ZDR / RhoHV",
            help="Only for moments that still contain the noise (needs SNR).",
        ),
    ],
    version="1.0",
)
def run(ctx, snr_min, method, threshold, min_size, noise_correction):
    rt = A.rx()
    ds = A.prepare_sweep(ctx)
    z = A.polar_array(ds["DBZH"], ds, "DBZH")
    has_data = np.isfinite(z)
    n_valid = int(has_data.sum())
    if n_valid == 0:
        raise ModuleError("This sweep has no valid reflectivity gates")
    has_snr = "SNR" in ds.data_vars

    ctx.progress(15, "Classifying echo")
    kwargs: dict = {"method": method, "min_snr": float(snr_min)}
    if has_snr:
        kwargs["snr"] = "SNR"
    if threshold > 0:
        kwargs["threshold"] = float(threshold)
    if min_size > 0:
        kwargs["min_size"] = int(min_size)
    with A.captured_warnings() as warns, A.guard("Echo classification"):
        out = rt.echo_mask(ds, **kwargs)
    cls = A.polar_array(out["ECHO_CLASS"], ds, "ECHO_CLASS")

    flags = np.where(has_data, float(CLEAN), np.nan)
    low_snr = np.zeros(z.shape, dtype=bool)
    if has_snr:
        snr = A.polar_array(ds["SNR"], ds, "SNR")
        with np.errstate(invalid="ignore"):
            low_snr = has_data & (snr < snr_min)
    flags[has_data & (cls == 2)] = NON_MET
    flags[has_data & (cls == 3)] = SPECKLE
    flags[low_snr] = LOW_SNR  # noise is the most specific explanation: it wins
    keep = has_data & (flags == CLEAN)

    ctx.progress(70, "Building cleaned fields")
    res = Result()
    kept = int(keep.sum())
    res.scalars(
        {
            "Gates with data": n_valid,
            "Kept (clean)": f"{100.0 * kept / n_valid:.1f} %",
            "Removed": f"{100.0 * (n_valid - kept) / n_valid:.1f} %",
            "Echo mask": f"{method} (radarx {A.radarx_version()})",
            "SNR gate": f"< {snr_min:g} dB" if has_snr else "skipped (no SNR field)",
        }
    )
    rows = [
        {
            "flag": LABELS[c],
            "gates": int((flags == c).sum()),
            "pct": round(100.0 * int((flags == c).sum()) / n_valid, 2),
        }
        for c in (CLEAN, LOW_SNR, NON_MET, SPECKLE)
    ]
    res.table(
        "Gates by flag", ["flag", "gates", {"key": "pct", "label": "% of valid", "digits": 2}], rows
    )

    res.field("Cleaned reflectivity", np.where(keep, z, np.nan), units="dBZ", vmin=-20.0, vmax=70.0)
    zdr = A.polar_array(ds["ZDR"], ds, "ZDR") if "ZDR" in ds.data_vars else None
    if zdr is not None:
        res.field("Cleaned ZDR", np.where(keep, zdr, np.nan), units="dB", vmin=-3.0, vmax=6.0)
    if "VRADH" in ds.data_vars:
        vel = A.polar_array(ds["VRADH"], ds, "VRADH")
        res.field("Cleaned velocity", np.where(keep, vel, np.nan), units="m/s")
    if "RHOHV" in ds.data_vars:
        rho = A.polar_array(ds["RHOHV"], ds, "RHOHV")
        res.field("Cleaned RhoHV", np.where(keep, rho, np.nan), vmin=0.5, vmax=1.0)

    if noise_correction:
        if not has_snr:
            res.text("Noise correction skipped: this sweep has no SNR field.", title="Note")
        else:
            ctx.progress(85, "Correcting noise bias")
            with A.captured_warnings() as w2, A.guard("Noise correction"):
                if zdr is not None:
                    zc = A.polar_array(
                        rt.correct_zdr_noise(ds, zdr="ZDR", snr="SNR", min_snr=float(snr_min)),
                        ds,
                        "ZDR",
                    )
                    res.field(
                        "Noise-corrected ZDR",
                        np.where(keep, zc, np.nan),
                        units="dB",
                        vmin=-3.0,
                        vmax=6.0,
                    )
                if "RHOHV" in ds.data_vars:
                    rc = A.polar_array(
                        rt.correct_rhohv_noise(
                            ds,
                            rhohv="RHOHV",
                            snr="SNR",
                            zdr=bool(zdr is not None),
                            min_snr=float(snr_min),
                        ),
                        ds,
                        "RHOHV",
                    )
                    res.field(
                        "Noise-corrected RhoHV", np.where(keep, rc, np.nan), vmin=0.5, vmax=1.0
                    )
            warns = [*warns, *w2]

    res.field("QC flags", flags, categories=CATEGORIES)
    res.text(NOTES, title="How the cleaning works and its limits")
    return A.add_warnings(res, warns)

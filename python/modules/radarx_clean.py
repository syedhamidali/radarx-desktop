"""radarx: clean radar data (optional QC steps and gap filling) -> cleaned fields + flag map."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

from engine.plugins import ModuleError, Result, module, param


def _load(name: str, filename: str):
    """Import a sibling helper file (modules are loaded by path, not as a package)."""
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


A = _load("xradar_module_radarx_adapter", "_radarx_adapter.py")
G = _load("xradar_module_gapfill", "_gapfill.py")

CLEAN, LOW_SNR, NON_MET, SPECKLE, FILLED = 1, 2, 3, 4, 5
CATEGORIES = [
    {"value": CLEAN, "label": "Clean (meteorological)", "color": "#22c55e"},
    {"value": LOW_SNR, "label": "Low SNR (noise)", "color": "#6b7280"},
    {"value": NON_MET, "label": "Non-meteorological", "color": "#ef4444"},
    {"value": SPECKLE, "label": "Speckle (removed)", "color": "#38bdf8"},
    {"value": FILLED, "label": "Filled (interpolated)", "color": "#f59e0b"},
]
LABELS = {c["value"]: c["label"] for c in CATEGORIES}

NOTES = """\
Every step is optional (switch it off to keep those gates). In order:

1. SNR gate: gates whose signal-to-noise ratio is below the minimum are noise (needs an SNR \
field).
2. Echo mask: radarx.retrieve.echo_mask, a fuzzy-logic score from RhoHV, ZDR and the along-ray \
texture of ZDR / PhiDP / DBZH (weather is smooth, clutter / insects / noise are not). More \
robust than fixed thresholds: it keeps the interior of heavy rain, where RhoHV or SNR dip locally.
3. Speckle: small isolated meteorological regions are removed (min size).
4. Gap filling: small patches that were removed but are well surrounded by clean data are \
filled by a smooth (Gaussian, distance-weighted) interpolation of their clean neighbours. A \
coverage test keeps large noisy regions empty: nothing is invented where there is not enough \
good data around. Filled values are separate outputs ("Gap-filled ...") and are marked in the \
flag map; the plain cleaned fields are never altered.
5. Smoothing (off by default): light Gaussian smoothing of the clean gates.

Optional noise-bias correction (Gourley et al. 2006) of ZDR and RhoHV is only valid for \
moments that still contain the noise - leave it off if your data are already noise corrected.

Not fixed by any mask or fill: partial beam blockage (radial streaks / dark wedges with ZDR \
spikes along the rays - use radarx beam_blockage with a terrain model), ZDR calibration \
offsets, attenuation, range folding. Filled gates are estimates, not measurements. Run on all \
sweeps to view the cleaned volume in 3D."""


@module(
    id="radarx_clean",
    title="Clean radar data (radarx QC)",
    description=(
        "Optional QC steps (SNR gate, radarx echo mask, speckle removal) and optional gap "
        "filling / smoothing; outputs cleaned and gap-filled fields and a flag map. "
        "Recommended cleaner."
    ),
    category="QC",
    requires=["DBZH"],
    optional=["SNR", "ZDR", "RHOHV", "PHIDP", "VRADH"],
    needs=A.NEEDS,
    params=[
        param(
            "use_snr_gate",
            "bool",
            True,
            "Step 1: SNR gate",
            help="Hide gates below the minimum SNR (needs an SNR field).",
        ),
        param(
            "snr_min",
            "number",
            3.0,
            "   Min SNR (dB)",
            min=-10,
            max=30,
            step=0.5,
        ),
        param(
            "use_echo_mask",
            "bool",
            True,
            "Step 2: echo mask (radarx)",
            help="Hide non-meteorological echo (clutter, insects, noise) found by radarx.",
        ),
        param(
            "method",
            "select",
            "radarx",
            "   Echo-mask method",
            options=["radarx", "krause2016"],
            help="'radarx' = radarx's memberships; 'krause2016' = published MetSignal scheme.",
        ),
        param(
            "threshold",
            "number",
            0.0,
            "   Score threshold (0 = default)",
            min=0,
            max=1,
            step=0.05,
            help="Higher = stricter (more removed). Default 0.6 (radarx) / 0.8 (krause2016).",
        ),
        param(
            "remove_speckle",
            "bool",
            True,
            "Step 3: remove speckle",
            help="Remove small isolated meteorological regions.",
        ),
        param(
            "min_size",
            "int",
            0,
            "   Min region size (gates, 0 = default)",
            min=0,
            max=500,
            step=1,
        ),
        param(
            "fill_gaps",
            "bool",
            True,
            "Step 4: fill small gaps (smooth interpolation)",
            help="Fill removed patches that are well surrounded by clean data.",
        ),
        param(
            "fill_radius_km",
            "number",
            0.6,
            "   Fill radius (km)",
            min=0.1,
            max=10,
            step=0.1,
            help=(
                "Patches larger than about this are not filled. 0.6 = tiny holes only; "
                "2-3 also bridges narrow blocked radials; larger values start to grow echo edges."
            ),
        ),
        param(
            "fill_min_coverage",
            "number",
            0.5,
            "   Min clean coverage around a gap",
            min=0.1,
            max=1.0,
            step=0.05,
            help="Fraction of the neighbourhood that must be clean data; higher = fewer fills.",
        ),
        param(
            "smooth",
            "bool",
            False,
            "Step 5: smooth clean gates",
            help="Light Gaussian smoothing of the clean gates (separate 'Smoothed' outputs).",
        ),
        param(
            "smooth_radius_km",
            "number",
            0.3,
            "   Smoothing radius (km)",
            min=0.1,
            max=5,
            step=0.1,
        ),
        param(
            "noise_correction",
            "bool",
            False,
            "Also output noise-corrected ZDR / RhoHV",
            help="Only for moments that still contain the noise (needs SNR).",
        ),
    ],
    version="1.1",
)
def run(
    ctx,
    use_snr_gate,
    snr_min,
    use_echo_mask,
    method,
    threshold,
    remove_speckle,
    min_size,
    fill_gaps,
    fill_radius_km,
    fill_min_coverage,
    smooth,
    smooth_radius_km,
    noise_correction,
):
    rt = A.rx()
    ds = A.prepare_sweep(ctx)
    z = A.polar_array(ds["DBZH"], ds, "DBZH")
    has_data = np.isfinite(z)
    n_valid = int(has_data.sum())
    if n_valid == 0:
        raise ModuleError("This sweep has no valid reflectivity gates")
    has_snr = "SNR" in ds.data_vars
    azimuth, range_m = ctx.polar_coords()
    wrap = G.covers_full_circle(azimuth)

    flags = np.where(has_data, float(CLEAN), np.nan)
    steps = []

    # --- step 2/3: echo mask (also yields the speckle class) -----------------------------------
    if use_echo_mask or remove_speckle:
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
        if use_echo_mask:
            flags[has_data & (cls == 2)] = NON_MET
            steps.append("echo mask")
        if remove_speckle:
            flags[has_data & (cls == 3)] = SPECKLE
            steps.append("speckle")
    else:
        warns = []

    # --- step 1: SNR gate (most specific explanation, so it wins) -------------------------------
    if use_snr_gate:
        if has_snr:
            snr = A.polar_array(ds["SNR"], ds, "SNR")
            with np.errstate(invalid="ignore"):
                flags[has_data & (snr < snr_min)] = LOW_SNR
            steps.insert(0, "SNR gate")
        else:
            steps.append("SNR gate skipped (no SNR field)")

    keep = has_data & (flags == CLEAN)
    removed = has_data & ~keep

    # --- step 4: gap filling ---------------------------------------------------------------------
    fill_ok = np.zeros(z.shape, dtype=bool)
    sigma_fill = None
    if fill_gaps and removed.any() and keep.any():
        ctx.progress(55, "Filling small gaps")
        r_mid = float(np.median(np.broadcast_to(range_m, z.shape)[keep]))
        sigma_fill = G.window_sigmas(range_m, azimuth, fill_radius_km, r_mid)
        _, fill_ok = G.fill_gaps(
            z, keep, removed, sigma=sigma_fill, min_coverage=fill_min_coverage, wrap_azimuth=wrap
        )
        steps.append("gap fill")
        flags[fill_ok] = FILLED
    if smooth and keep.any():
        steps.append("smoothing")

    ctx.progress(70, "Building cleaned fields")
    res = Result()
    n_keep, n_fill = int(keep.sum()), int(fill_ok.sum())
    res.scalars(
        {
            "Gates with data": n_valid,
            "Kept (clean)": f"{100.0 * n_keep / n_valid:.1f} %",
            "Filled (interpolated)": f"{100.0 * n_fill / n_valid:.1f} %",
            "Still empty": f"{100.0 * (n_valid - n_keep - n_fill) / n_valid:.1f} %",
            "Steps applied": ", ".join(steps) or "none (nothing removed)",
            "radarx": A.radarx_version(),
        }
    )
    rows = [
        {
            "flag": LABELS[c],
            "gates": int((flags == c).sum()),
            "pct": round(100.0 * int((flags == c).sum()) / n_valid, 2),
        }
        for c in (CLEAN, LOW_SNR, NON_MET, SPECKLE, FILLED)
    ]
    res.table(
        "Gates by flag", ["flag", "gates", {"key": "pct", "label": "% of valid", "digits": 2}], rows
    )

    smooth_sigma = None
    if smooth and keep.any():
        r_mid = float(np.median(np.broadcast_to(range_m, z.shape)[keep]))
        smooth_sigma = G.window_sigmas(range_m, azimuth, smooth_radius_km, r_mid)

    def emit(title, values, units="", vmin=None, vmax=None, fillable=True):
        """Cleaned (+ gap-filled, + smoothed) variants of one field."""
        res.field(
            f"Cleaned {title}", np.where(keep, values, np.nan), units=units, vmin=vmin, vmax=vmax
        )
        if fill_gaps and fillable and sigma_fill is not None:
            filled, _ = G.fill_gaps(
                values,
                keep,
                removed,
                sigma=sigma_fill,
                min_coverage=fill_min_coverage,
                wrap_azimuth=wrap,
            )
            res.field(
                f"Gap-filled {title}",
                np.where(keep, values, filled),
                units=units,
                vmin=vmin,
                vmax=vmax,
            )
        if smooth_sigma is not None and fillable:
            sm = G.smooth_clean(values, keep, sigma=smooth_sigma, wrap_azimuth=wrap)
            res.field(f"Smoothed {title}", sm, units=units, vmin=vmin, vmax=vmax)

    emit("reflectivity", z, "dBZ", -20.0, 70.0)
    zdr = A.polar_array(ds["ZDR"], ds, "ZDR") if "ZDR" in ds.data_vars else None
    if zdr is not None:
        emit("ZDR", zdr, "dB", -3.0, 6.0)
    if "RHOHV" in ds.data_vars:
        emit("RhoHV", A.polar_array(ds["RHOHV"], ds, "RHOHV"), "", 0.5, 1.0)
    if "VRADH" in ds.data_vars:  # not interpolated: velocity can fold between neighbouring gates
        emit("velocity", A.polar_array(ds["VRADH"], ds, "VRADH"), "m/s", fillable=False)

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

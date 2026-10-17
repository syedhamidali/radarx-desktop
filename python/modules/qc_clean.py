"""Clean radar data (no dependencies): optional threshold rules + optional gap filling."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

from engine.plugins import ModuleError, Result, module, param


def _load_gapfill():
    name = "xradar_module_gapfill"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("_gapfill.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


G = _load_gapfill()

# Flag codes (start at 1: derived variables keep 0 for "no data" in the 3-D texture).
CLEAN, LOW_SNR, LOW_NCP, LOW_RHO, CLUTTER, SPECKLE, FILLED = 1, 2, 3, 4, 5, 6, 7
CATEGORIES = [
    {"value": CLEAN, "label": "Clean", "color": "#22c55e"},
    {"value": LOW_SNR, "label": "Low SNR (noise)", "color": "#6b7280"},
    {"value": LOW_NCP, "label": "Low NCP / SQI", "color": "#a78bfa"},
    {"value": LOW_RHO, "label": "Low RhoHV (non-meteorological)", "color": "#f59e0b"},
    {"value": CLUTTER, "label": "Ground clutter", "color": "#ef4444"},
    {"value": SPECKLE, "label": "Isolated speckle", "color": "#38bdf8"},
    {"value": FILLED, "label": "Filled (interpolated)", "color": "#fbbf24"},
]
LABELS = {c["value"]: c["label"] for c in CATEGORIES}

EXPLANATION = """\
Every rule is optional (switch it off to keep those gates). Gates are flagged by the first rule \
that applies, in this order:
1. Low SNR - signal-to-noise below the minimum (skipped if the file has no SNR).
2. Low NCP / SQI - signal quality below the minimum (skipped if absent).
3. Low RhoHV - correlation coefficient below the minimum: non-meteorological echo and noise \
(skipped if absent).
4. Ground clutter - near the radar, strong echo with almost no Doppler velocity and imperfect \
RhoHV (skipped if there is no velocity).
5. Isolated speckle - remaining gates with fewer than N valid neighbours.
6. Gap filling (optional) - removed gates well surrounded by clean data are filled by smooth \
interpolation of their neighbours (separate "Gap-filled ..." outputs; large noisy regions stay \
empty). Light smoothing of the clean gates is a separate option (off by default).

What this does NOT fix: partial beam blockage (radial streaks / dark wedges with ZDR spikes along \
the rays), ZDR calibration offsets, attenuation, range-folded or second-trip echo, sea clutter or \
anomalous propagation without a velocity signature. Filled gates are estimates, not measurements. \
Fixed thresholds are crude: 'Clean radar data (radarx QC)' is more robust."""


def _find(ctx, name):
    da = ctx.find(name)
    if da is None:
        return None
    if da.dims != ("azimuth", "range"):
        extra = [d for d in da.dims if d not in ("azimuth", "range")]
        da = da.isel(dict.fromkeys(extra, 0)).transpose("azimuth", "range")
    return da.values.astype(np.float64)


def _neighbour_count(valid: np.ndarray, wrap_azimuth: bool) -> np.ndarray:
    """Number of valid 8-neighbours of every gate (azimuth wraps only for full-circle sweeps)."""
    pad = np.pad(valid.astype(np.int16), ((1, 1), (1, 1)), mode="constant")
    if wrap_azimuth:
        pad[0, 1:-1], pad[-1, 1:-1] = valid[-1], valid[0]
    total = np.zeros(valid.shape, dtype=np.int16)
    n_az, n_rng = valid.shape
    for da in (-1, 0, 1):
        for dr in (-1, 0, 1):
            if da or dr:
                total += pad[1 + da : 1 + da + n_az, 1 + dr : 1 + dr + n_rng]
    return total


@module(
    id="qc_clean",
    title="Clean radar data (quick thresholds)",
    description=(
        "No-dependency fallback: fixed SNR / NCP / RhoHV / clutter / speckle rules (each "
        "optional) and optional gap filling. For better masks use 'Clean radar data (radarx QC)'."
    ),
    category="QC",
    requires=["DBZH"],
    optional=["SNR", "NCP", "RHOHV", "ZDR", "VRADH"],
    params=[
        param("use_snr", "bool", True, "Rule 1: SNR gate"),
        param("snr_min", "number", 5.0, "   Min SNR (dB)", min=-20, max=40, step=0.5),
        param("use_ncp", "bool", True, "Rule 2: NCP / SQI gate"),
        param("ncp_min", "number", 0.3, "   Min NCP / SQI", min=0, max=1, step=0.05),
        param("use_rhohv", "bool", True, "Rule 3: RhoHV gate"),
        param(
            "rhohv_min",
            "number",
            0.8,
            "   Min RhoHV",
            min=0,
            max=1,
            step=0.01,
            help="Rain is typically > 0.95; 0.8 keeps most meteorological echo.",
        ),
        param("remove_clutter", "bool", True, "Rule 4: remove ground clutter"),
        param("clutter_range_km", "number", 25.0, "   Clutter within (km)", min=1, max=100, step=1),
        param(
            "clutter_vel",
            "number",
            1.0,
            "   Clutter |velocity| below (m/s)",
            min=0.1,
            max=5,
            step=0.1,
        ),
        param(
            "clutter_dbz",
            "number",
            15.0,
            "   Clutter reflectivity above (dBZ)",
            min=-10,
            max=60,
            step=1,
        ),
        param(
            "despeckle_min",
            "int",
            3,
            "Rule 5: speckle, min valid neighbours",
            min=0,
            max=8,
            step=1,
            help="0 disables despeckling.",
        ),
        param(
            "fill_gaps",
            "bool",
            True,
            "Rule 6: fill small gaps (smooth interpolation)",
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
        ),
        param("smooth", "bool", False, "Smooth the clean gates"),
        param(
            "smooth_radius_km", "number", 0.3, "   Smoothing radius (km)", min=0.1, max=5, step=0.1
        ),
    ],
)
def run(
    ctx,
    use_snr,
    snr_min,
    use_ncp,
    ncp_min,
    use_rhohv,
    rhohv_min,
    remove_clutter,
    clutter_range_km,
    clutter_vel,
    clutter_dbz,
    despeckle_min,
    fill_gaps,
    fill_radius_km,
    fill_min_coverage,
    smooth,
    smooth_radius_km,
):
    z = _find(ctx, "DBZH")
    if z is None or z.ndim != 2:
        raise ModuleError("This sweep has no 2-D reflectivity field")
    snr, ncp, rho = _find(ctx, "SNR"), _find(ctx, "NCP"), _find(ctx, "RHOHV")
    vel, zdr = _find(ctx, "VRADH"), _find(ctx, "ZDR")
    azimuth, range_m = ctx.polar_coords()
    range_km = np.broadcast_to(range_m / 1000.0, z.shape)
    wrap = G.covers_full_circle(azimuth)

    has_data = np.isfinite(z)
    flags = np.where(has_data, float(CLEAN), np.nan)
    undecided = has_data.copy()

    def flag(mask, code):
        nonlocal undecided
        hit = undecided & mask
        flags[hit] = code
        undecided = undecided & ~hit

    used, skipped = [], []
    with np.errstate(invalid="ignore"):
        for name, enabled, arr, test, code in (
            ("SNR", use_snr, snr, lambda a: a < snr_min, LOW_SNR),
            ("NCP / SQI", use_ncp, ncp, lambda a: a < ncp_min, LOW_NCP),
            ("RhoHV", use_rhohv, rho, lambda a: a < rhohv_min, LOW_RHO),
        ):
            if not enabled:
                continue
            if arr is None or arr.shape != z.shape:
                skipped.append(name)
                continue
            used.append(name)
            flag(test(arr), code)
        if remove_clutter and vel is not None and vel.shape == z.shape:
            used.append("clutter")
            near = range_km < clutter_range_km
            weak_rho = (rho < 0.95) if rho is not None and rho.shape == z.shape else True
            flag(near & (np.abs(vel) < clutter_vel) & (z > clutter_dbz) & weak_rho, CLUTTER)
        elif remove_clutter:
            skipped.append("clutter (no velocity)")
        if despeckle_min > 0:
            flag(_neighbour_count(undecided, wrap) < despeckle_min, SPECKLE)
            used.append("speckle")

    keep = has_data & (flags == CLEAN)
    removed = has_data & ~keep
    n_valid = int(has_data.sum())
    if n_valid == 0:
        raise ModuleError("This sweep has no valid reflectivity gates")

    fill_ok = np.zeros(z.shape, dtype=bool)
    sigma_fill = sigma_smooth = None
    r_mid = float(np.median(np.broadcast_to(range_m, z.shape)[keep])) if keep.any() else 0.0
    if fill_gaps and removed.any() and keep.any():
        sigma_fill = G.window_sigmas(range_m, azimuth, fill_radius_km, r_mid)
        _, fill_ok = G.fill_gaps(
            z, keep, removed, sigma=sigma_fill, min_coverage=fill_min_coverage, wrap_azimuth=wrap
        )
        flags[fill_ok] = FILLED
        used.append("gap fill")
    if smooth and keep.any():
        sigma_smooth = G.window_sigmas(range_m, azimuth, smooth_radius_km, r_mid)
        used.append("smoothing")

    res = Result()
    n_keep, n_fill = int(keep.sum()), int(fill_ok.sum())
    res.scalars(
        {
            "Gates with data": n_valid,
            "Kept (clean)": f"{100.0 * n_keep / n_valid:.1f} %",
            "Filled (interpolated)": f"{100.0 * n_fill / n_valid:.1f} %",
            "Still empty": f"{100.0 * (n_valid - n_keep - n_fill) / n_valid:.1f} %",
            "Rules applied": ", ".join(used) or "none (nothing removed)",
            **({"Rules skipped (missing field)": ", ".join(skipped)} if skipped else {}),
        }
    )
    rows = []
    for code in (CLEAN, LOW_SNR, LOW_NCP, LOW_RHO, CLUTTER, SPECKLE, FILLED):
        n = int((flags == code).sum())
        rows.append({"flag": LABELS[code], "gates": n, "pct": round(100.0 * n / n_valid, 2)})
    res.table(
        "Gates by flag", ["flag", "gates", {"key": "pct", "label": "% of valid", "digits": 2}], rows
    )

    def emit(title, values, units="", vmin=None, vmax=None, fillable=True):
        res.field(
            f"Cleaned {title}", np.where(keep, values, np.nan), units=units, vmin=vmin, vmax=vmax
        )
        if sigma_fill is not None and fillable:
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
        if sigma_smooth is not None and fillable:
            sm = G.smooth_clean(values, keep, sigma=sigma_smooth, wrap_azimuth=wrap)
            res.field(f"Smoothed {title}", sm, units=units, vmin=vmin, vmax=vmax)

    emit("reflectivity", z, "dBZ", -20.0, 70.0)
    if zdr is not None and zdr.shape == z.shape:
        emit("ZDR", zdr, "dB", -3.0, 6.0)
    if rho is not None and rho.shape == z.shape:
        emit("RhoHV", rho, "", 0.5, 1.0)
    if vel is not None and vel.shape == z.shape:  # not interpolated: velocity can fold
        emit("velocity", vel, "m/s", fillable=False)
    res.field("QC flags", flags, categories=CATEGORIES)
    res.text(EXPLANATION, title="How the mask works and its limits")
    return res

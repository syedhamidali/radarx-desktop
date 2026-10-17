"""Clean radar data: one multi-field quality mask (SNR, NCP/SQI, RhoHV, ground clutter, speckle).

A single-variable filter cannot know that a reflectivity gate is noise because *another* field
(RhoHV, SNR, NCP) says so. This module combines them into one transparent flag map, then outputs
cleaned copies of reflectivity (and ZDR / velocity when present) so they can be viewed in 2D or,
with "All sweeps", in 3D.
"""

from __future__ import annotations

import numpy as np

from engine.plugins import ModuleError, Result, module, param

# Flag codes (start at 1: derived variables keep 0 for "no data" in the 3-D texture).
CLEAN, LOW_SNR, LOW_NCP, LOW_RHO, CLUTTER, SPECKLE = 1, 2, 3, 4, 5, 6
CATEGORIES = [
    {"value": CLEAN, "label": "Clean", "color": "#22c55e"},
    {"value": LOW_SNR, "label": "Low SNR (noise)", "color": "#6b7280"},
    {"value": LOW_NCP, "label": "Low NCP / SQI", "color": "#a78bfa"},
    {"value": LOW_RHO, "label": "Low RhoHV (non-meteorological)", "color": "#f59e0b"},
    {"value": CLUTTER, "label": "Ground clutter", "color": "#ef4444"},
    {"value": SPECKLE, "label": "Isolated speckle", "color": "#38bdf8"},
]
LABELS = {c["value"]: c["label"] for c in CATEGORIES}

EXPLANATION = """\
Gates are flagged by the first rule that applies, in this order:
1. Low SNR - signal-to-noise below the minimum (noise-dominated gates; skipped if the file \
has no SNR).
2. Low NCP / SQI - signal quality below the minimum (skipped if absent).
3. Low RhoHV - correlation coefficient below the minimum: non-meteorological echo and noise \
(skipped if absent).
4. Ground clutter - near the radar, strong echo with almost no Doppler velocity and imperfect \
RhoHV (skipped if there is no velocity).
5. Isolated speckle - remaining gates with fewer than N valid neighbours.

What this does NOT fix: partial beam blockage (radial streaks / dark wedges with ZDR spikes along \
the rays), ZDR calibration offsets, attenuation, range-folded or second-trip echo, sea clutter or \
anomalous propagation without a velocity signature. Check the flag map and tune the thresholds for \
your radar (X-band and S-band differ: X-band RhoHV is lower in heavy rain)."""


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
        "No-dependency fallback: fixed SNR / NCP / RhoHV / clutter / speckle thresholds in one "
        "flag map. For better masks use 'Clean radar data (radarx QC)'."
    ),
    category="QC",
    requires=["DBZH"],
    optional=["SNR", "NCP", "RHOHV", "ZDR", "VRADH"],
    params=[
        param(
            "snr_min",
            "number",
            5.0,
            "Min SNR (dB)",
            min=-20,
            max=40,
            step=0.5,
            help="Gates below this signal-to-noise ratio are noise.",
        ),
        param("ncp_min", "number", 0.3, "Min NCP / SQI", min=0, max=1, step=0.05),
        param(
            "rhohv_min",
            "number",
            0.8,
            "Min RhoHV",
            min=0,
            max=1,
            step=0.01,
            help="Rain is typically > 0.95; 0.8 keeps most meteorological echo.",
        ),
        param("remove_clutter", "bool", True, "Remove ground clutter"),
        param("clutter_range_km", "number", 25.0, "Clutter within (km)", min=1, max=100, step=1),
        param(
            "clutter_vel", "number", 1.0, "Clutter |velocity| below (m/s)", min=0.1, max=5, step=0.1
        ),
        param(
            "clutter_dbz",
            "number",
            15.0,
            "Clutter reflectivity above (dBZ)",
            min=-10,
            max=60,
            step=1,
        ),
        param(
            "despeckle_min",
            "int",
            3,
            "Speckle: min valid neighbours",
            min=0,
            max=8,
            step=1,
            help="0 disables despeckling.",
        ),
    ],
)
def run(
    ctx,
    snr_min,
    ncp_min,
    rhohv_min,
    remove_clutter,
    clutter_range_km,
    clutter_vel,
    clutter_dbz,
    despeckle_min,
):
    z = _find(ctx, "DBZH")
    if z is None or z.ndim != 2:
        raise ModuleError("This sweep has no 2-D reflectivity field")
    snr, ncp, rho = _find(ctx, "SNR"), _find(ctx, "NCP"), _find(ctx, "RHOHV")
    vel, zdr = _find(ctx, "VRADH"), _find(ctx, "ZDR")
    azimuth, range_m = ctx.polar_coords()
    range_km = np.broadcast_to(range_m / 1000.0, z.shape)

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
        for name, arr, test, code in (
            ("SNR", snr, lambda a: a < snr_min, LOW_SNR),
            ("NCP / SQI", ncp, lambda a: a < ncp_min, LOW_NCP),
            ("RhoHV", rho, lambda a: a < rhohv_min, LOW_RHO),
        ):
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
            full_circle = bool(
                len(azimuth) > 1
                and (azimuth.max() - azimuth.min())
                > 360.0 - 2 * np.median(np.abs(np.diff(azimuth)))
            )
            counts = _neighbour_count(undecided, full_circle)
            flag(counts < despeckle_min, SPECKLE)
            used.append("speckle")

    keep = has_data & (flags == CLEAN)
    n_valid = int(has_data.sum())
    if n_valid == 0:
        raise ModuleError("This sweep has no valid reflectivity gates")

    res = Result()
    kept_pct = 100.0 * int(keep.sum()) / n_valid
    res.scalars(
        {
            "Gates with data": n_valid,
            "Kept (clean)": f"{kept_pct:.1f} %",
            "Removed": f"{100.0 - kept_pct:.1f} %",
            "Tests used": ", ".join(used) or "none",
            **({"Tests skipped (missing field)": ", ".join(skipped)} if skipped else {}),
        }
    )
    rows = []
    for code in (CLEAN, LOW_SNR, LOW_NCP, LOW_RHO, CLUTTER, SPECKLE):
        n = int((flags == code).sum())
        rows.append({"flag": LABELS[code], "gates": n, "pct": round(100.0 * n / n_valid, 2)})
    res.table(
        "Gates by flag", ["flag", "gates", {"key": "pct", "label": "% of valid", "digits": 2}], rows
    )

    res.field("Cleaned reflectivity", np.where(keep, z, np.nan), units="dBZ", vmin=-20.0, vmax=70.0)
    if zdr is not None and zdr.shape == z.shape:
        res.field("Cleaned ZDR", np.where(keep, zdr, np.nan), units="dB", vmin=-3.0, vmax=6.0)
    if vel is not None and vel.shape == z.shape:
        res.field("Cleaned velocity", np.where(keep, vel, np.nan), units="m/s")
    res.field("QC flags", flags, categories=CATEGORIES)
    res.text(EXPLANATION, title="How the mask works and its limits")
    return res

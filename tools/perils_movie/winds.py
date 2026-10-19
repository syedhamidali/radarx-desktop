"""Multi-Doppler winds (RETRIEVED) on the shared grid for each step: ``winds/step_NNNN.npz``.

Method
------
For every step the radial velocities of the radars of that step (the dealiased ``VEL`` of the gridded
files, "Corrected mean doppler velocity" in the files: the de-aliasing was done by the data owners, not here)
are

1. moved to the step time by translating each radar's field with the storm motion (``motion``) times the
   radar's time offset (Gal-Chen 1982 frame-of-reference correction as a pure translation, linear
   interpolation; the motion is the pattern motion of the reflectivity of the same window, see ``tracks.py``),
2. averaged horizontally with a 3 x 3 NaN-aware mean and sampled every 1 km (the analysis grid is 1 km, half
   the 500 m of the data grid; the gridded data carry a radius of influence of 0.5 to 3.5 km anyway),
3. fed to ``radarx.retrieve.multi_doppler`` (variational analysis, the cost structure of Gao et al. 1999:
   observation term for every radar, anelastic mass continuity, smoothness; fall speed removed from
   ``w`` with the radarx fall-speed relation and the freezing level of the environment profile; ``w = 0`` at
   the bottom and the top of the grid) with an observation error of 2 m/s and no background wind,
4. masked where fewer than two radars see the cell or the largest horizontal beam crossing angle is below
   30 degrees (the analytic dual-Doppler solution ``dual_doppler_analytic`` is run on the same input; its
   u and v error estimate is stored as ``uv_error`` and its crossing angle as ``crossing_deg``).

Derived: vertical vorticity ``dv/dx - du/dy`` and horizontal divergence ``du/dx + dv/dy`` (centred
differences at 1 km), storm-relative winds ``u - Us``, ``v - Vs``.

Limits (also in docs/PERILS_BUNDLE.md): the variational solver stops at its iteration limit
(``converged`` is stored: it did not converge fully), ``w`` depends strongly on the boundary condition
and the 1 km smoothing and is the least reliable component, the lowest ~500 m and the top levels of the grid
are not well constrained, gridding artefacts (radius of influence) bias ``w`` (Collis et al. 2010), X-band
velocities are not corrected for attenuation, there is no background wind outside the dual-Doppler area.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np
from scipy import ndimage


def advect_field(
    a: np.ndarray, shift_x_m: float, shift_y_m: float, cell_m: float = 500.0
) -> np.ndarray:
    """Translate ``a`` (nz, ny, nx) by (shift_x, shift_y) metres east / north, NaN aware (linear)."""
    if abs(shift_x_m) < 1.0 and abs(shift_y_m) < 1.0:
        return a
    ok = np.isfinite(a)
    filled = np.where(ok, a, 0.0)
    sh = (0.0, shift_y_m / cell_m, shift_x_m / cell_m)
    num = ndimage.shift(filled, sh, order=1, mode="constant", cval=0.0)
    den = ndimage.shift(ok.astype(np.float32), sh, order=1, mode="constant", cval=0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(den > 0.5, num / den, np.nan)
    return out.astype(np.float32)


def coarsen(a: np.ndarray, factor: int = 2) -> np.ndarray:
    """NaN-aware 3 x 3 mean sampled every ``factor`` cells (nz, ny, nx) -> (nz, ny/f, nx/f)."""
    ok = np.isfinite(a).astype(np.float32)
    filled = np.where(np.isfinite(a), a, 0.0).astype(np.float32)
    k = (1, 3, 3)
    num = ndimage.uniform_filter(filled, size=k, mode="constant") * 9.0
    den = ndimage.uniform_filter(ok, size=k, mode="constant") * 9.0
    with np.errstate(invalid="ignore", divide="ignore"):
        m = np.where(den >= 3.0, num / den, np.nan)
    return m[:, ::factor, ::factor].astype(np.float32)


def synthesize(
    radars: list[dict[str, Any]],
    z_m: np.ndarray,
    x_m: np.ndarray,
    y_m: np.ndarray,
    freezing_m: float,
    motion: tuple[float, float],
    obs_error: float = 2.0,
    max_iterations: int = 300,
    min_crossing_deg: float = 30.0,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    """Run the synthesis for one step.

    ``radars``: dicts with ``name``, ``vel`` and ``dbz`` (nz, ny, nx; ASL grid, 500 m), ``x_m``, ``y_m`` (radar
    position east / north of the grid origin), ``alt_m`` (ASL) and ``offset_s`` (volume time minus step time).
    Returns the arrays (on the 1 km grid) and a summary dict.
    """
    import radarx.retrieve as rxr
    import xarray as xr

    t0 = time.time()
    vels, dbzs = [], []
    for r in radars:
        # data valid at t + offset: bring it to t (move the pattern by motion * (t - t_data) = -motion * offset)
        vv = advect_field(r["vel"], -motion[0] * r["offset_s"], -motion[1] * r["offset_s"])
        dd = advect_field(r["dbz"], -motion[0] * r["offset_s"], -motion[1] * r["offset_s"])
        vels.append(coarsen(vv))
        dbzs.append(coarsen(dd))
    xc, yc = x_m[::2], y_m[::2]
    ds = xr.Dataset(
        {
            "VRADH": (("radar", "z", "y", "x"), np.stack(vels)),
            "DBZH": (("radar", "z", "y", "x"), np.stack(dbzs)),
            "radar_x": ("radar", [r["x_m"] for r in radars]),
            "radar_y": ("radar", [r["y_m"] for r in radars]),
            "radar_altitude": ("radar", [r["alt_m"] for r in radars]),
        },
        coords={"radar": [r["name"] for r in radars], "z": z_m, "y": yc, "x": xc},
    )
    ana = rxr.dual_doppler_analytic(
        ds,
        field="VRADH",
        dbz="DBZH",
        obs_error=obs_error,
        min_crossing_angle=min_crossing_deg,
        freezing_level=float(freezing_m),
        boundary="obrien",
    )
    var = rxr.multi_doppler(
        ds,
        None,
        field="VRADH",
        dbz="DBZH",
        freezing_level=float(freezing_m),
        w_boundary=["bottom", "top"],
        levels=3,
        max_iterations=max_iterations,
        obs_error=obs_error,
        error=False,
    )
    u = np.asarray(var["u"].values, np.float32)
    v = np.asarray(var["v"].values, np.float32)
    w = np.asarray(var["w"].values, np.float32)
    nrad = np.asarray(var["n_radars"].values)
    cross = np.asarray(var["beam_crossing_angle"].values, np.float32)
    valid = (nrad >= 2) & (cross >= min_crossing_deg)
    speed = np.hypot(u, v)
    valid &= speed <= 70.0
    for a in (u, v, w):
        a[~valid] = np.nan
    dx = float(xc[1] - xc[0])
    dy = float(yc[1] - yc[0])
    with np.errstate(invalid="ignore"):
        dudx = np.gradient(u, dx, axis=2)
        dudy = np.gradient(u, dy, axis=1)
        dvdx = np.gradient(v, dx, axis=2)
        dvdy = np.gradient(v, dy, axis=1)
    vort = (dvdx - dudy).astype(np.float32)
    div = (dudx + dvdy).astype(np.float32)
    uv_err = np.asarray(ana["u_error"].values, np.float32)
    uv_err2 = np.asarray(ana["v_error"].values, np.float32)
    uv_error = np.sqrt(0.5 * (uv_err**2 + uv_err2**2))
    uv_error[~valid] = np.nan
    # residual between the retrieved wind projected on each beam and the observation
    res = np.asarray(var["vr_residual"].values, np.float32)
    rms = {}
    for i, r in enumerate(radars):
        m = valid & np.isfinite(res[i])
        rms[r["name"]] = float(np.sqrt(np.mean(res[i][m] ** 2))) if m.any() else None
    arrays = {
        "u": u,
        "v": v,
        "w": w,
        "vorticity": vort,
        "divergence": div,
        "u_sr": (u - np.float32(motion[0])),
        "v_sr": (v - np.float32(motion[1])),
        "uv_error": uv_error,
        "crossing_deg": cross,
        "nradars": nrad.astype(np.uint8),
    }
    for k in ("vorticity", "divergence"):
        arrays[k][~valid] = np.nan
    summ = {
        "radars": [r["name"] for r in radars],
        "offsets_s": {r["name"]: r["offset_s"] for r in radars},
        "valid_fraction": float(valid.mean()),
        "valid_cells": int(valid.sum()),
        "converged": str(var.attrs.get("converged")),
        "iterations": str(var.attrs.get("iterations")),
        "run_time_s": round(time.time() - t0, 1),
        "vr_residual_rms_ms": rms,
        "w_boundary": "w = 0 at bottom and top",
        "u_max_ms": _f(np.nanmax(speed[valid]) if valid.any() else np.nan),
        "w_p1_p99_ms": [_f(np.nanpercentile(w[valid], 1)), _f(np.nanpercentile(w[valid], 99))]
        if valid.any()
        else None,
        "vorticity_p99_s-1": _f(np.nanpercentile(np.abs(vort[valid]), 99), 5)
        if valid.any()
        else None,
        "uv_error_median_ms": _f(np.nanmedian(uv_error[valid])) if valid.any() else None,
        "grid": {"dx_km": dx / 1000.0, "nx": int(xc.size), "ny": int(yc.size), "nz": int(z_m.size)},
        "motion_ms": list(motion),
        "obs_error_ms": obs_error,
    }
    return arrays, summ


def _f(x, d=2):
    return None if not np.isfinite(x) else round(float(x), d)

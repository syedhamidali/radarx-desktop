"""radarx: Doppler velocity dealiasing (region-based and UNRAVEL)."""

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
Doppler velocities are only known modulo 2 x the Nyquist velocity (Vn). radarx.retrieve.\
dealias_velocity finds the integer fold of every gate.

method "region" (default): radarx's own combination of published ideas, not one paper's \
algorithm. Neighbouring gates whose velocities differ by less than threshold x Vn form \
regions that share one fold; the folds minimise the summed squared velocity jump across \
region boundaries (least-squares criterion of Jing and Wiener 1993) with a spanning-tree start \
and coordinate descent; the absolute fold of the largest region comes from a reference wind \
(Eilts and Smith 1990) if you give one, otherwise its mean velocity is brought close to zero. \
method "unravel": Louf et al. (2020, JTECH 37, 741-758); needs no reference but is about 40x \
slower and removes some noisy gates (including vortex cores).

Limits: this module dealiases only the selected sweep (no volume continuity from the sweep \
below). Without a reference wind the absolute fold assumes echo all around the radar; for \
partial coverage or strong winds give the reference wind below, and set the maximum speed to \
protect vortex cores. If the Nyquist velocity is not in the file it is estimated as the \
largest valid |velocity| of the sweep, which is right for aliased data and an under-estimate \
otherwise - enter the real value when you know it (NEXRAD: 20-35 m/s depending on the cut)."""


@module(
    id="radarx_dealias",
    title="Velocity Dealiasing (radarx)",
    description=(
        "radarx.retrieve.dealias_velocity: unfolds Doppler velocity beyond the Nyquist "
        "interval with a region-based least-squares method (Jing & Wiener 1993, Eilts & Smith "
        "1990) or UNRAVEL (Louf et al. 2020). Shows the unfolded field or the fold map."
    ),
    category="Kinematics",
    requires=["VRADH"],
    needs=A.NEEDS,
    params=[
        param(
            "output",
            "select",
            "velocity",
            "Show",
            options=["velocity", "fold_number"],
            help="Dealiased velocity (m/s) or the integer number of Nyquist folds added",
        ),
        param(
            "method",
            "select",
            "region",
            "Method",
            options=["region", "unravel"],
            help="'unravel' needs no reference but is ~40x slower",
        ),
        param(
            "nyquist_velocity",
            "number",
            0.0,
            "Nyquist velocity (m/s, 0 = auto)",
            min=0,
            max=100,
            step=0.5,
            help="0 = from the file, else estimated from the largest valid |velocity|",
        ),
        param(
            "threshold",
            "number",
            0.3,
            "Region threshold (x Vn)",
            min=0.05,
            max=0.9,
            step=0.05,
            help="Region method: neighbouring gates closer than this x Vn share a fold",
        ),
        param(
            "max_speed",
            "number",
            0.0,
            "Max plausible speed (m/s, 0 = off)",
            min=0,
            max=150,
            step=1,
            help="Region method: undo folds that push a small cluster beyond this speed",
        ),
        param(
            "wind_speed",
            "number",
            0.0,
            "Reference wind speed (m/s, 0 = none)",
            min=0,
            max=100,
            step=1,
            help="Uniform wind that fixes the absolute fold (region method)",
        ),
        param(
            "wind_from",
            "number",
            270.0,
            "Reference wind from (deg)",
            min=0,
            max=360,
            step=5,
        ),
        param(
            "alpha",
            "number",
            0.8,
            "UNRAVEL alpha",
            min=0.3,
            max=1.0,
            step=0.05,
            help="UNRAVEL acceptance window (x Vn); radarx default 0.8",
        ),
    ],
    version="1.0",
)
def run(ctx, output, method, nyquist_velocity, threshold, max_speed, wind_speed, wind_from, alpha):
    rt = A.rx()
    ds = A.prepare_sweep(ctx)
    name = "VRADH"
    nyq, source = A.nyquist_velocity(ds, name, nyquist_velocity)
    wind = A.uniform_wind_profile(wind_speed, wind_from) if method == "region" else None

    ctx.progress(20, f"Dealiasing ({method}, Vn={nyq:g} m/s)")
    with A.captured_warnings() as warns:
        da = A.dealias(
            rt,
            ds,
            name,
            nyq,
            threshold=threshold,
            max_speed=max_speed,
            method=method,
            alpha=alpha,
            wind_profile=wind,
        )
    out = A.polar_array(da, ds, "dealiased velocity")
    raw = np.asarray(ds[name].values, dtype=np.float32)
    raw = np.where(np.abs(raw) <= 1.01 * nyq, raw, np.nan)  # flag codes are not data
    both = np.isfinite(out) & np.isfinite(raw)
    folds = np.where(both, np.rint((out - raw) / (2.0 * nyq)), np.nan).astype(np.float32)
    changed = int((both & (np.abs(folds) >= 1)).sum())
    valid = int(both.sum())

    res = Result()
    if output == "velocity":
        lim = max(float(nyq), A.symmetric_limit(out, 99.5))
        res.field(
            "Dealiased velocity (radarx)", out, units="m/s", vmin=-lim, vmax=lim, cmap="NWSVel"
        )
    else:
        top = int(np.nanmax(np.abs(folds))) if np.isfinite(folds).any() else 1
        res.field(
            "Nyquist folds (radarx)",
            folds,
            units="folds",
            vmin=-max(top, 1),
            vmax=max(top, 1),
            cmap="RdBu_r",
        )
    res.scalars(
        [
            {"label": "Nyquist velocity", "value": nyq, "unit": "m/s"},
            {"label": "Nyquist source", "value": source},
            {"label": "Valid gates", "value": valid},
            {"label": "Gates unfolded", "value": changed},
            {
                "label": "Share unfolded",
                "value": 100.0 * changed / valid if valid else None,
                "unit": "%",
            },
            {
                "label": "Max |V| before",
                "value": float(np.nanmax(np.abs(raw))) if np.isfinite(raw).any() else None,
                "unit": "m/s",
            },
            {
                "label": "Max |V| after",
                "value": float(np.nanmax(np.abs(out))) if np.isfinite(out).any() else None,
                "unit": "m/s",
            },
            {"label": "Absolute fold fixed by", "value": da.attrs.get("dealias_anchor")},
            {"label": "Method", "value": method},
            {"label": "radarx", "value": A.radarx_version()},
        ]
    )
    res.text(NOTES, title="About this method")
    return A.add_warnings(res, warns)

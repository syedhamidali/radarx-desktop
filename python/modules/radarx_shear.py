"""radarx: azimuthal shear and radial divergence by linear least-squares derivatives."""

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

TORNADO_THRESHOLD = 0.006  # s-1, Veillette et al. 2025 / NWS TPA (radarx's rotation_couplets)

NOTES = """\
Linear least-squares derivative (LLSD) technique (Smith and Elmore 2004; Miller et al. 2013; \
Mahalik et al. 2019): at every gate the radial velocities of the gates in a window of fixed \
physical size are fitted by weighted least squares with the plane v = a + b s + c dr, \
s = r x d(theta). The slope b is the azimuthal shear and c the radial divergence (both in \
1/s). For solid-body rotation the azimuthal shear is half the vertical vorticity; for \
axisymmetric convergence the radial divergence is half the horizontal divergence. Positive \
azimuthal shear is cyclonic rotation in the Northern Hemisphere (velocity increasing \
clockwise from inbound to outbound).

The window has a fixed size in metres (default 750 m along the beam x 2500 m across it, the \
smallest kernel of Smith and Elmore), so the number of rays in it shrinks with range. Window \
shape, uniform/Gaussian weights and the 50 % valid-gate rule are radarx's choices.

Velocity must be dealiased: a fold line is a velocity jump of 2 Vn and produces spurious \
shear. With "Dealias first" the module unfolds the sweep with radarx's region-based method \
(see the Velocity Dealiasing module for its limits and for how the Nyquist velocity is \
found) before the fit. Shear that is real needs azimuthal resolution: 0.5 degrees at 100 km is \
870 m, so small vortices are only resolved close to the radar."""


@module(
    id="radarx_shear",
    title="Azimuthal Shear / Divergence (LLSD)",
    description=(
        "radarx.retrieve.llsd: azimuthal shear (rotation) or radial divergence of the Doppler "
        "velocity by linear least-squares derivatives (Smith & Elmore 2004; Mahalik et al. "
        "2019). Cyclonic rotation is positive. Values of about 0.006 s-1 and more are the "
        "usual threshold for tornadic circulations."
    ),
    category="Kinematics",
    requires=["VRADH"],
    optional=["DBZH"],
    needs=A.NEEDS,
    params=[
        param(
            "output",
            "select",
            "azimuthal_shear",
            "Show",
            options=["azimuthal_shear", "radial_divergence"],
        ),
        param(
            "window_range_m",
            "number",
            750.0,
            "Window along beam (m)",
            min=250,
            max=3000,
            step=250,
        ),
        param(
            "window_azimuth_m",
            "number",
            2500.0,
            "Window across beam (m)",
            min=500,
            max=10000,
            step=250,
        ),
        param(
            "weights",
            "select",
            "uniform",
            "Weights",
            options=["uniform", "gaussian"],
        ),
        param(
            "median_filter",
            "bool",
            False,
            "3x3 median filter first",
            help="Smith & Elmore (2004) prefilter; removes spikes but lowers small-vortex peaks",
        ),
        param("dealias_first", "bool", True, "Dealias velocity first"),
        param(
            "nyquist_velocity",
            "number",
            0.0,
            "Nyquist velocity (m/s, 0 = auto)",
            min=0,
            max=100,
            step=0.5,
        ),
        param(
            "min_dbz",
            "number",
            -30.0,
            "Ignore gates below (dBZ, -30 = off)",
            min=-30,
            max=40,
            step=1,
            help="Needs DBZH on the sweep; shear at weak-echo gates is mostly noise",
        ),
    ],
    version="1.0",
)
def run(
    ctx,
    output,
    window_range_m,
    window_azimuth_m,
    weights,
    median_filter,
    dealias_first,
    nyquist_velocity,
    min_dbz,
):
    rt = A.rx()
    ds = A.prepare_sweep(ctx)
    name = "VRADH"
    notes: list[str] = []
    with A.captured_warnings() as warns:
        field = name
        if dealias_first:
            nyq, source = A.nyquist_velocity(ds, name, nyquist_velocity)
            ctx.progress(20, f"Dealiasing (Vn={nyq:g} m/s)")
            da = A.dealias(rt, ds, name, nyq)
            ds = ds.assign({"VRADH_dealiased": da})
            field = "VRADH_dealiased"
            notes.append(f"Velocity dealiased with Nyquist {nyq:g} m/s ({source}).")

        kwargs: dict = {
            "window": (float(window_range_m), float(window_azimuth_m)),
            "weights": weights,
            "median_filter": True if median_filter else None,
        }
        if min_dbz > -30 and ctx.has("DBZH"):
            dbz = ds["DBZH"]
            kwargs["mask"] = dbz >= float(min_dbz)
        ctx.progress(60, "Fitting least-squares derivatives")
        with A.guard("LLSD shear"):
            out = rt.llsd(ds, field, **kwargs)

    arr = A.polar_array(out[output], ds, output)
    finite = np.isfinite(arr)
    lim = max(A.symmetric_limit(arr, 99.5), 0.004)
    res = Result()
    if output == "azimuthal_shear":
        res.field("Azimuthal shear (radarx)", arr, units="1/s", vmin=-lim, vmax=lim, cmap="RdBu_r")
    else:
        res.field(
            "Radial divergence (radarx)", arr, units="1/s", vmin=-lim, vmax=lim, cmap="RdBu_r"
        )
    scal = [
        {"label": "Gates with a fit", "value": int(finite.sum())},
        {
            "label": "Max (positive)",
            "value": float(np.nanmax(arr)) if finite.any() else None,
            "unit": "1/s",
        },
        {
            "label": "Min (negative)",
            "value": float(np.nanmin(arr)) if finite.any() else None,
            "unit": "1/s",
        },
    ]
    if output == "azimuthal_shear":
        scal.append(
            {
                "label": f"Gates >= {TORNADO_THRESHOLD} (cyclonic)",
                "value": int((arr >= TORNADO_THRESHOLD).sum()),
            }
        )
        scal.append(
            {
                "label": f"Gates <= -{TORNADO_THRESHOLD} (anticyclonic)",
                "value": int((arr <= -TORNADO_THRESHOLD).sum()),
            }
        )
        if finite.any() and np.nanmax(arr) > 0:
            ia, ir = np.unravel_index(np.nanargmax(arr), arr.shape)
            scal.append(
                {
                    "label": "Peak at (azimuth / range)",
                    "value": (
                        f"{float(ds['azimuth'].values[ia]):.1f} deg / "
                        f"{float(ds['range'].values[ir]) / 1000:.1f} km"
                    ),
                }
            )
    scal.append({"label": "radarx", "value": A.radarx_version()})
    res.scalars(scal)
    res.text("\n".join([*notes, NOTES]), title="About this method")
    return A.add_warnings(res, warns)

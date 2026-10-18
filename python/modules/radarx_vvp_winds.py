"""radarx: wind profile from one Doppler radar (VAD ring fits or VVP analysis volumes)."""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np

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
LEVELS_AGL_M = (500.0, 1000.0, 2000.0, 3000.0, 6000.0)
MAX_WIND_ERROR = 4.0  # m/s; VVP volumes with a larger standard error of u or v are dropped

NOTES = """\
Wind profile from the radial velocities of a single Doppler radar. Two retrievals of radarx:

VAD (radarx.retrieve.vad_profile, Browning and Wexler 1968): on every range ring of every \
sweep the radial velocity is fitted by v_r = a0 + u cos(el) sin(az) + v cos(el) cos(az), which \
assumes the wind is uniform around the ring. The ring winds of all sweeps are averaged in \
height bins (weighted by their gate counts). The second harmonic of the fit gives the \
horizontal divergence. Rings with too few gates, too little azimuth coverage (a sector of echo) \
or - optionally - a large residual are dropped, so a profile needs echo all around the radar \
(precipitation or clear-air return at several heights); a lone storm gives a poor VAD.

VVP (radarx.retrieve.vvp_winds, Waldteufel and Corbin 1979; Doviak and Zrnic 1993, sec. 9.3): \
the same idea for analysis volumes - cylinders of the chosen radius and depth centred on a grid \
- where a wind that is linear in position is fitted to all gates inside, so it works with echo \
in only part of the domain. The profile shown is the median over the volumes of each layer \
(volumes whose fit has a standard error above 4 m/s are dropped); the "linear" model also \
gives the divergence. The u and v of a volume away from the radar are the wind plus a \
vorticity term (half the vorticity times the distance to the radar); the median over a \
domain centred on the radar mostly cancels it.

Both methods need dealiased velocity: by default every sweep is dealiased first with the \
region method (radarx.retrieve.dealias_velocity), using the Nyquist velocity in the file, or one \
estimated from the largest |velocity| (enter it if the estimate looks wrong). The wind is the \
wind of the scatterers: it includes the fall speed through the vertical term, which matters \
little at low elevations. Heights are above ground level (the radar's altitude); directions \
are the direction the wind blows from. The profile is only as good as the echo: a missing \
level means no valid ring or volume there."""


@module(
    id="radarx_vvp_winds",
    title="Wind Profile VAD/VVP (radarx)",
    description=(
        "radarx.retrieve.vad_profile / vvp_winds: wind speed and direction by height from the "
        "Doppler velocities of one radar (Browning & Wexler 1968; Waldteufel & Corbin 1979), "
        "plus divergence, low-level winds and bulk shear. Works on a volume or a single sweep."
    ),
    category="Kinematics",
    requires=["VRADH"],
    optional=["DBZH"],
    needs=A.NEEDS,
    scope="volume",
    params=[
        param(
            "method",
            "select",
            "vad",
            "Method",
            options=["vad", "vvp"],
            help="vad = velocity-azimuth display (ring fits, needs echo all around); "
            "vvp = linear wind in analysis volumes (works with partial echo)",
        ),
        param(
            "model",
            "select",
            "linear",
            "VVP model",
            options=["uniform", "linear"],
            help="VVP only. uniform = constant wind per volume; linear adds the horizontal "
            "gradients and vertical shear (needs several sweeps); gives the divergence",
        ),
        param(
            "height_step_m",
            "number",
            500.0,
            "Height step (m)",
            min=100,
            max=1500,
            step=100,
            help="Thickness of the height bins (VAD) or of the analysis volumes (VVP)",
        ),
        param(
            "max_height_km",
            "number",
            8.0,
            "Top of profile (km AGL)",
            min=1,
            max=20,
            step=0.5,
            help="Top of the wind profile above the radar",
        ),
        param(
            "max_range_km",
            "number",
            60.0,
            "Max range (km)",
            min=10,
            max=250,
            step=5,
            help="Gates farther than this are ignored; far rings of low sweeps average the "
            "wind over hundreds of km",
        ),
        param(
            "radius_km",
            "number",
            15.0,
            "VVP volume radius (km)",
            min=3,
            max=60,
            step=1,
            help="VVP only. Radius of an analysis cylinder, also the node spacing",
        ),
        param(
            "min_gates",
            "int",
            100,
            "Min gates per ring / volume",
            min=10,
            max=2000,
            step=10,
            help="Fewest valid gates in a VAD ring or VVP volume for a fit",
        ),
        param(
            "max_rms",
            "number",
            0.0,
            "Max ring residual (m/s, 0 = off)",
            min=0,
            max=30,
            step=0.5,
            help="VAD only. Rejects rings where the wind is far from uniform (e.g. near storms)",
        ),
        param(
            "min_elevation",
            "number",
            0.0,
            "Min elevation (°)",
            min=0,
            max=45,
            step=0.5,
            help="Sweeps below this elevation are not used",
        ),
        param(
            "max_elevation",
            "number",
            45.0,
            "Max elevation (°)",
            min=1,
            max=90,
            step=1,
            help="Sweeps above this elevation are not used",
        ),
        param(
            "dealias_first",
            "bool",
            True,
            "Dealias velocity first",
            help="Region dealiasing of every sweep; off for already dealiased velocity",
        ),
        param(
            "nyquist_velocity",
            "number",
            0.0,
            "Nyquist velocity (m/s, 0 = auto)",
            min=0,
            max=100,
            step=0.5,
            help="0 = from the file, else estimated from the largest |velocity|",
        ),
    ],
    version="1.0",
)
def run(
    ctx,
    method,
    model,
    height_step_m,
    max_height_km,
    max_range_km,
    radius_km,
    min_gates,
    max_rms,
    min_elevation,
    max_elevation,
    dealias_first,
    nyquist_velocity,
):
    rt = A.rx()
    if method == "vad":
        X.need(rt, "vad_profile", "The VAD wind profile")
    else:
        X.need(rt, "vvp_winds", "The VVP wind profile")
    notes: list[str] = []
    ctx.progress(5, "Reading the volume")
    vol = X.volume(
        ctx,
        any_of=("VRADH",),
        keep=("DBZH",),
        elevation_range=(float(min_elevation), float(max_elevation)),
        progress=(5, 25),
    )
    if not vol.names:
        raise ModuleError(
            "No sweep with Doppler velocity (VRADH) in the elevation range "
            f"{min_elevation:g}-{max_elevation:g}°. NEXRAD split cuts keep velocity on every "
            "second low-level sweep; widen the elevation range or check the file. "
            + X.describe_skipped(vol)
        )
    altitude = vol.site.get("altitude", 0.0)

    with A.captured_warnings() as warns:
        tree = vol.tree
        if dealias_first:
            tree = _dealias_all(ctx, rt, vol, nyquist_velocity, notes)
        top_asl = altitude + float(max_height_km) * 1000.0
        ctx.progress(70, "Fitting winds")
        if method == "vad":
            prof = _vad(
                rt, tree, altitude, top_asl, height_step_m, max_range_km, min_gates, max_rms
            )
        else:
            prof = _vvp(
                rt,
                tree,
                vol,
                altitude,
                top_asl,
                height_step_m,
                max_range_km,
                radius_km,
                min_gates,
                model,
                notes,
            )
    height = prof["height_agl"]
    good = np.isfinite(prof["u"]) & np.isfinite(prof["v"])
    if not good.any():
        raise ModuleError(
            "No height has a valid wind. VAD needs echo all around the radar at some height "
            "(try the VVP method, a smaller min-gates value, a larger range or a wider "
            "elevation range); VVP needs at least min-gates gates in a volume. "
            + ("Velocity was not dealiased. " if not dealias_first else "")
        )
    speed = np.hypot(prof["u"], prof["v"])
    direction = np.array(
        [
            X.wind_dir_from(u, v) if np.isfinite(u) and np.isfinite(v) else np.nan
            for u, v in zip(prof["u"], prof["v"], strict=True)
        ]
    )

    rows = []
    for i in np.flatnonzero(good):
        div = prof["divergence"][i]
        rows.append(
            {
                "height_km": round(float(height[i]) / 1000.0, 2),
                "height_asl_km": round(float(height[i] + altitude) / 1000.0, 2),
                "speed": round(float(speed[i]), 1),
                "speed_kt": round(float(speed[i]) * X.MS_TO_KT, 0),
                "direction": round(float(direction[i]), 0),
                "compass": X.compass(float(direction[i])),
                "u": round(float(prof["u"][i]), 1),
                "v": round(float(prof["v"][i]), 1),
                "divergence": (round(float(div) * 1e3, 3) if np.isfinite(div) else None),
                "n": int(prof["count"][i]),
            }
        )

    levels = _level_winds(height, prof["u"], prof["v"])
    shears = _bulk_shears(height, prof["u"], prof["v"])
    imax = int(np.nanargmax(np.where(good, speed, np.nan)))
    items = [
        {"label": "Levels with a wind", "value": int(good.sum())},
        {
            "label": "Lowest wind",
            "value": f"{speed[np.flatnonzero(good)[0]]:.1f} m/s from "
            f"{direction[np.flatnonzero(good)[0]]:.0f}° at "
            f"{height[np.flatnonzero(good)[0]]:.0f} m AGL",
        },
        {
            "label": "Max wind speed",
            "value": float(speed[imax]),
            "unit": "m/s",
        },
        {"label": "Height of max wind", "value": float(height[imax]), "unit": "m AGL"},
    ]
    for h_m, (spd, dirn) in levels.items():
        label = f"Wind at {h_m / 1000.0:g} km"
        items.append(
            {
                "label": label,
                "value": (f"{spd:.1f} m/s from {dirn:.0f}°" if math.isfinite(spd) else None),
            }
        )
    for (lo, hi), val in shears.items():
        items.append(
            {
                "label": f"Bulk shear {lo / 1000.0:g}-{hi / 1000.0:g} km",
                "value": val if math.isfinite(val) else None,
                "unit": "m/s",
            }
        )
    items += [
        {"label": "Method", "value": method.upper() + (f" ({model})" if method == "vvp" else "")},
        {"label": "Sweeps used", "value": len(vol.names)},
        {"label": "Dealiased", "value": "yes" if dealias_first else "no"},
        {"label": "radarx", "value": X.version()},
    ]

    res = Result()
    res.scalars(items)
    res.table(
        f"Wind profile ({method.upper()}, {height_step_m:g} m steps, heights above the radar)",
        [
            {"key": "height_km", "label": "Height AGL", "unit": "km", "digits": 2},
            {"key": "height_asl_km", "label": "Height ASL", "unit": "km", "digits": 2},
            {"key": "speed", "label": "Speed", "unit": "m/s", "digits": 1},
            {"key": "speed_kt", "label": "Speed", "unit": "kt", "digits": 0},
            {"key": "direction", "label": "From", "unit": "°", "digits": 0},
            {"key": "compass", "label": "Dir."},
            {"key": "u", "label": "u (east)", "unit": "m/s", "digits": 1},
            {"key": "v", "label": "v (north)", "unit": "m/s", "digits": 1},
            {"key": "divergence", "label": "Divergence", "unit": "1e-3/s", "digits": 3},
            {"key": "n", "label": "Rings / gates", "digits": 0},
        ],
        rows,
    )
    res.text(_summary(levels, shears, speed, direction, height, good, imax), title="Summary")
    if X.describe_skipped(vol):
        notes.append("Sweeps left out: " + X.describe_skipped(vol))
    res.text("\n".join([*notes, NOTES]), title="About this method")
    return A.add_warnings(res, warns)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _dealias_all(ctx, rt, vol, nyquist_velocity, notes):
    """Volume tree with every sweep's VRADH replaced by its dealiased copy."""
    import xarray as xr

    nodes = {"/": vol.tree.root.to_dataset()}
    kept = []
    for i, name in enumerate(vol.names):
        ctx.progress(25 + 40 * i // max(len(vol.names), 1), f"Dealiasing {name}")
        ds = vol.sweep(name)
        try:
            nyq, _src = A.nyquist_velocity(ds, "VRADH", nyquist_velocity)
            da = A.dealias(rt, ds, "VRADH", nyq)
        except ModuleError as exc:
            notes.append(f"{name} not dealiased and left out: {exc}")
            continue
        nodes[f"/{name}"] = ds.assign(VRADH=da.astype(np.float32).transpose(*ds["VRADH"].dims))
        kept.append(name)
    if not kept:
        raise ModuleError(
            "Velocity could not be dealiased on any sweep. Enter the Nyquist velocity "
            "(m/s) or switch off 'Dealias velocity first' for data that are already dealiased."
        )
    notes.append(f"Velocity dealiased on {len(kept)} of {len(vol.names)} sweeps (region method).")
    vol.names[:] = kept
    return xr.DataTree.from_dict(nodes)


def _vad(rt, tree, altitude, top_asl, step, max_range_km, min_gates, max_rms):
    kwargs = {
        "height_bins": np.arange(altitude, top_asl + step, float(step)),
        "min_gates": int(min_gates),
        "max_range": float(max_range_km) * 1000.0,
        "min_elevation": 0.0,  # the sweeps were already selected by elevation
        "max_elevation": 90.0,
    }
    if max_rms > 0:
        kwargs["max_rms"] = float(max_rms)
    with A.guard("VAD wind profile"):
        out = rt.vad_profile(tree, "VRADH", **kwargs)

    def pick(name):
        return np.asarray(out[name].values, dtype=np.float64)

    return {
        "height_agl": pick("height") - altitude,
        "u": pick("u"),
        "v": pick("v"),
        "divergence": pick("divergence"),
        "count": pick("vad_rings"),
    }


def _vvp(rt, tree, vol, altitude, top_asl, step, max_range_km, radius_km, min_gates, model, notes):
    n_dop = len(vol.names)
    if n_dop < 2 and model == "linear":
        model = "uniform"
        notes.append("Only one Doppler sweep: the uniform VVP model was used (no vertical terms).")
    radius = float(radius_km) * 1000.0
    reach = float(max_range_km) * 1000.0
    n = int(reach // radius)
    xs = np.arange(-n, n + 1) * radius
    zs = np.arange(altitude + step / 2.0, top_asl, float(step))
    if zs.size == 0:
        raise ModuleError("The profile top is below the first layer; raise the top of the profile")
    with A.guard("VVP wind profile"):
        out = rt.vvp_winds(
            tree,
            "VRADH",
            model=model,
            radius=radius,
            depth=float(step),
            x=xs,
            y=xs,
            z=zs,
            min_gates=int(min_gates),
            max_range=reach,
        )
    u, v = out["u"].values, out["v"].values
    gates = out["vvp_gates"].values.astype(np.float64)
    ok = np.isfinite(u) & np.isfinite(v) & (gates >= min_gates)
    for err in ("u_error", "v_error"):  # ill-conditioned volumes (edges of the echo)
        ok &= ~(np.asarray(out[err].values, dtype=np.float64) > MAX_WIND_ERROR)

    def layer_median(a):
        """Median over the good volumes of every layer (robust to the odd bad fit)."""
        a = np.where(ok, np.asarray(a, dtype=np.float64), np.nan)
        flat = a.reshape(a.shape[0], -1)
        res = np.full(flat.shape[0], np.nan)
        for k in range(flat.shape[0]):
            row = flat[k][np.isfinite(flat[k])]
            if row.size:
                res[k] = float(np.median(row))
        return res

    div = (
        layer_median(out["divergence"].values) if "divergence" in out else np.full(zs.size, np.nan)
    )
    return {
        "height_agl": np.asarray(out["z"].values, dtype=np.float64) - altitude,
        "u": layer_median(u),
        "v": layer_median(v),
        "divergence": div,
        "count": ok.sum(axis=(1, 2)).astype(np.float64),
    }


def _level_winds(height, u, v):
    out = {}
    for h in LEVELS_AGL_M:
        uu, vv = X.interp_valid(height, u, h), X.interp_valid(height, v, h)
        if math.isfinite(uu) and math.isfinite(vv):
            out[h] = (math.hypot(uu, vv), X.wind_dir_from(uu, vv))
        else:
            out[h] = (float("nan"), float("nan"))
    return out


def _bulk_shears(height, u, v):
    out = {}
    for lo, hi in ((0.0, 1000.0), (0.0, 3000.0), (0.0, 6000.0)):
        # wind at the lowest valid level stands in for the surface
        valid = np.isfinite(u) & np.isfinite(v)
        h0 = max(lo, float(height[valid].min())) if valid.any() else lo
        du = X.interp_valid(height, u, hi) - X.interp_valid(height, u, h0)
        dv = X.interp_valid(height, v, hi) - X.interp_valid(height, v, h0)
        out[(lo, hi)] = (
            math.hypot(du, dv) if math.isfinite(du) and math.isfinite(dv) else float("nan")
        )
    return out


def _summary(levels, shears, speed, direction, height, good, imax):
    lines = []
    for h, (spd, dirn) in levels.items():
        if math.isfinite(spd):
            lines.append(
                f"{h / 1000.0:g} km AGL: {spd:.1f} m/s ({spd * X.MS_TO_KT:.0f} kt) from "
                f"{dirn:.0f}° ({X.compass(dirn)})"
            )
    lines.append(
        f"Strongest wind in the profile: {speed[imax]:.1f} m/s ({speed[imax] * X.MS_TO_KT:.0f} kt) "
        f"from {direction[imax]:.0f}° at {height[imax] / 1000.0:.2f} km AGL."
    )
    sh = [
        f"{lo / 1000.0:g}-{hi / 1000.0:g} km: {val:.1f} m/s"
        for (lo, hi), val in shears.items()
        if math.isfinite(val)
    ]
    if sh:
        lines.append("Bulk wind difference " + "; ".join(sh) + ".")
    lines.append(
        f"{int(good.sum())} levels with a wind, from {height[good].min() / 1000.0:.2f} to "
        f"{height[good].max() / 1000.0:.2f} km AGL."
    )
    return "\n".join(lines)

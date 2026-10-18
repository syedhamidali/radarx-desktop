"""radarx: quasi-vertical profile (QVP / RD-QVP) of the dual-pol fields."""

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
CORE = ["DBZH", "ZDR", "RHOHV"]
COLUMNS = {
    "DBZH": ("Z", "dBZ", 1),
    "ZDR": ("ZDR", "dB", 2),
    "RHOHV": ("RhoHV", "", 3),
    "KDP": ("KDP", "deg/km", 2),
}

NOTES = """\
A quasi-vertical profile (Ryzhkov et al. 2016, radarx.retrieve.qvp) is the azimuthal average of \
a high-elevation PPI: each range gate is reduced over all rays and assigned to its beam height, \
so one sweep gives one vertical profile of the polarimetric variables. Ryzhkov et al. use \
elevations above 10-20 degrees. Gates with RhoHV <= 0.6 or Z <= -10 dBZ are left out and at \
least 30 valid gates on the circle are needed (all adjustable); decibel quantities (Z, ZDR) \
are averaged in linear units, or the median is taken. The radius of the averaging circle \
grows with height, so a QVP shows precipitation that is the same all around the radar, not a \
single storm.

When no sweep is steep enough, the range-defined QVP (radarx.retrieve.rd_qvp; Tobin and Kumjian \
2017 as described by Murphy et al. 2020) combines the azimuthal means of all sweeps inside the \
chosen radius with weights that decrease with distance - it reaches the ground and is the \
default for typical low-elevation scans; the weights are radarx's own choice. Heights are \
above sea level (the radar's altitude is added). KDP is used when the file has it. Turn on the \
elevation correction to refer ZDR and KDP of slant-looking beams to the horizontal (matters \
above ~20 degrees).

Typical signatures: a ZDR peak with a RhoHV minimum and a Z maximum marks the melting layer \
(the Melting Layer module detects it); a ZDR column or enhanced ZDR in snow marks dendritic \
growth near -15 C; low RhoHV and high ZDR in the lowest bins are often clutter or biological \
scatterers."""


@module(
    id="radarx_qvp",
    title="Quasi-Vertical Profile (radarx)",
    description=(
        "radarx.retrieve.qvp / rd_qvp: azimuthal-mean vertical profile of Z, ZDR, RhoHV (and "
        "KDP) from a high-elevation PPI or, for low-elevation scans, from all sweeps "
        "within a radius (Ryzhkov et al. 2016; Tobin & Kumjian 2017). Table, bright-band "
        "signature scalars and a text summary."
    ),
    category="Dual-pol",
    requires=["DBZH", "ZDR", "RHOHV"],
    optional=["KDP"],
    needs=A.NEEDS,
    scope="volume",
    params=[
        param(
            "method",
            "select",
            "auto",
            "Method",
            options=["auto", "qvp", "rd_qvp"],
            help="qvp = one steep sweep; rd_qvp = all sweeps within the radius; "
            "auto = qvp if a sweep reaches the minimum elevation, else rd_qvp",
        ),
        param(
            "qvp_min_elevation",
            "number",
            10.0,
            "Auto: min QVP elevation (°)",
            min=3,
            max=45,
            step=1,
            help="Auto uses a single-sweep QVP only if a dual-pol sweep is at least this steep",
        ),
        param(
            "qvp_elevation",
            "number",
            0.0,
            "QVP sweep elevation (°, 0 = highest)",
            min=0,
            max=90,
            step=0.5,
            help="qvp only. Uses the sweep closest to this elevation",
        ),
        param(
            "rmax_km",
            "number",
            50.0,
            "RD-QVP radius (km)",
            min=10,
            max=150,
            step=5,
            help="rd_qvp only. Gates farther than this ground distance are ignored",
        ),
        param(
            "dz_m",
            "number",
            250.0,
            "Height step (m)",
            min=50,
            max=1000,
            step=50,
            help="Vertical resolution of the table (and of the rd_qvp profile)",
        ),
        param(
            "max_height_km",
            "number",
            12.0,
            "Top of profile (km ASL)",
            min=2,
            max=20,
            step=0.5,
            help="Profile heights above this are not listed",
        ),
        param(
            "reduction",
            "select",
            "mean",
            "Azimuthal reduction",
            options=["mean", "median"],
            help="Median is robust against outliers such as clutter spikes",
        ),
        param(
            "min_rhohv",
            "number",
            0.6,
            "Min RHOHV of gates",
            min=0.0,
            max=1.0,
            step=0.05,
            help="Ryzhkov et al. use 0.6",
        ),
        param(
            "min_dbz",
            "number",
            -10.0,
            "Min Z of gates (dBZ)",
            min=-30,
            max=30,
            step=1,
            help="Gates weaker than this are left out of the average (Ryzhkov et al.: -10)",
        ),
        param(
            "min_count",
            "int",
            30,
            "Min valid gates on the circle",
            min=1,
            max=500,
            step=1,
            help="Fewest valid gates on an averaging circle for a value (paper: > 30)",
        ),
        param(
            "elevation_correction",
            "bool",
            False,
            "Refer ZDR / KDP to the horizontal",
            help="Corrects for the slant beam; useful above ~20° elevation",
        ),
    ],
    version="1.0",
)
def run(
    ctx,
    method,
    qvp_min_elevation,
    qvp_elevation,
    rmax_km,
    dz_m,
    max_height_km,
    reduction,
    min_rhohv,
    min_dbz,
    min_count,
    elevation_correction,
):
    rt = A.rx()
    ctx.progress(5, "Reading the volume")
    vol = X.volume(ctx, require=tuple(CORE), keep=("KDP",), progress=(5, 50))
    if not vol.names:
        raise ModuleError(
            "No sweep has reflectivity, ZDR and RhoHV, so no dual-pol profile can be made. "
            "Open a dual-pol volume (on NEXRAD split cuts they are on every second low-level "
            "sweep). " + X.describe_skipped(vol)
        )
    names = list(CORE)
    if any("KDP" in vol.tree[n].data_vars for n in vol.names):
        names.append("KDP")
    elevations = {n: X.elevation_of(vol.sweep(n)) for n in vol.names}
    steepest = max(elevations, key=elevations.get)
    chosen = method
    if method == "auto":
        chosen = "qvp" if elevations[steepest] >= qvp_min_elevation else "rd_qvp"
    notes: list[str] = []
    ctx.progress(55, "Computing the profile")
    common = {
        "min_rhohv": float(min_rhohv),
        "min_dbz": float(min_dbz),
        "min_count": int(min_count),
        "reduction": reduction,
        "elevation_correction": bool(elevation_correction),
    }
    with A.captured_warnings() as warns, A.guard("Quasi-vertical profile"):
        if chosen == "qvp":
            qvp = X.need(rt, "qvp", "The QVP")
            kwargs = dict(common)
            if qvp_elevation > 0:
                kwargs["elevation"] = float(qvp_elevation)
            else:
                kwargs["sweep"] = steepest
            profile = qvp(vol.tree, names, counts=True, **kwargs)
            used_el = float(profile["elevation"].values)
            notes.append(
                f"Single-sweep QVP of the {used_el:.1f}° sweep (Ryzhkov et al. 2016)."
                + (
                    " This is a low elevation: the averaging circle is wide and the "
                    "profile is only valid for horizontally uniform precipitation."
                    if used_el < 10
                    else ""
                )
            )
        else:
            rd_qvp = X.need(rt, "rd_qvp", "The RD-QVP")
            profile = rd_qvp(
                vol.tree,
                names,
                rmax=float(rmax_km) * 1000.0,
                dz=float(dz_m),
                counts=True,
                **common,
            )
            notes.append(
                f"Range-defined QVP of {len(vol.names)} sweeps within {rmax_km:g} km "
                f"(elevations {min(elevations.values()):.1f}-{max(elevations.values()):.1f}°)."
            )

    h = np.asarray(profile["height"].values, dtype=np.float64)
    top = float(max_height_km) * 1000.0
    data = {n: np.asarray(profile[n].values, dtype=np.float64).reshape(-1) for n in names}
    count = np.asarray(profile[f"{names[0]}_count"].values, dtype=np.float64).reshape(-1)
    keep = h <= top
    h, count = h[keep], count[keep]
    data = {n: v[keep] for n, v in data.items()}
    if not any(np.isfinite(v).any() for v in data.values()):
        raise ModuleError(
            "The profile has no valid height: too few valid gates per circle. Lower 'Min valid "
            "gates', 'Min RHOHV' or 'Min Z', or for the single-sweep QVP choose a steeper sweep "
            "or use rd_qvp."
        )
    bh, bdata, bcount = _bin(h, data, count, float(dz_m))

    rows = []
    for i in range(bh.size):
        if not any(np.isfinite(bdata[n][i]) for n in names):
            continue
        row = {"height_asl_km": round(float(bh[i]) / 1000.0, 3)}
        for n in names:
            digits = COLUMNS[n][2]
            row[n] = round(float(bdata[n][i]), digits) if np.isfinite(bdata[n][i]) else None
        row["count"] = int(bcount[i])
        rows.append(row)

    sig = _signature(bh, bdata)
    altitude = vol.site.get("altitude", 0.0)
    items = [
        {"label": "Profile type", "value": "QVP (single sweep)" if chosen == "qvp" else "RD-QVP"},
        {
            "label": "Lowest bin",
            "value": float(bh[np.isfinite(bdata["DBZH"])][0])
            if np.isfinite(bdata["DBZH"]).any()
            else None,
            "unit": "m ASL",
        },
        {
            "label": "Top bin",
            "value": float(bh[np.isfinite(bdata["DBZH"])][-1])
            if np.isfinite(bdata["DBZH"]).any()
            else None,
            "unit": "m ASL",
        },
        {"label": "Radar altitude", "value": float(altitude), "unit": "m"},
        {"label": "Max Z", "value": sig["zmax"], "unit": "dBZ"},
        {"label": "Height of max Z", "value": sig["zmax_h"], "unit": "m ASL"},
        {"label": "Max ZDR", "value": sig["zdrmax"], "unit": "dB"},
        {"label": "Height of max ZDR", "value": sig["zdrmax_h"], "unit": "m ASL"},
        {"label": "Min RhoHV", "value": sig["rhomin"]},
        {"label": "Height of min RhoHV", "value": sig["rhomin_h"], "unit": "m ASL"},
        {"label": "Echo top (Z ≥ 5 dBZ)", "value": sig["echo_top"], "unit": "m ASL"},
        {"label": "Bright-band signature", "value": sig["bright_band"]},
        {"label": "Sweeps used", "value": len(vol.names)},
        {"label": "radarx", "value": X.version()},
    ]

    res = Result()
    res.scalars(items)
    columns = [{"key": "height_asl_km", "label": "Height ASL", "unit": "km", "digits": 2}]
    for n in names:
        label, unit, digits = COLUMNS[n]
        columns.append({"key": n, "label": label, "unit": unit, "digits": digits})
    columns.append({"key": "count", "label": "Gates", "digits": 0})
    res.table(
        f"Quasi-vertical profile ({'QVP' if chosen == 'qvp' else 'RD-QVP'}, "
        f"{dz_m:g} m bins, heights above sea level)",
        columns,
        rows,
    )
    res.text(_summary(sig, altitude, chosen), title="Summary")
    if X.describe_skipped(vol):
        notes.append("Sweeps left out: " + X.describe_skipped(vol))
    res.text("\n".join([*notes, NOTES]), title="About this method")
    return A.add_warnings(res, warns)


def _bin(h, data, count, dz):
    """Average a profile into ``dz`` bins (bin centres); counts are summed."""
    if h.size == 0:
        return h, data, count
    step = np.median(np.diff(h)) if h.size > 1 else dz
    if step >= dz * 0.99:  # already at least as coarse as the bins
        return h, data, count
    lo = math.floor(h.min() / dz) * dz
    idx = np.floor((h - lo) / dz).astype(np.int64)
    n = int(idx.max()) + 1
    centres = lo + (np.arange(n) + 0.5) * dz
    out = {}
    for name, v in data.items():
        res = np.full(n, np.nan)
        for k in range(n):
            vals = v[idx == k]
            vals = vals[np.isfinite(vals)]
            if vals.size:
                res[k] = float(vals.mean())
        out[name] = res
    cnt = np.bincount(idx, weights=np.nan_to_num(count), minlength=n)
    return centres, out, cnt


def _signature(h, data):
    def extreme(name, fn):
        v = data[name]
        if not np.isfinite(v).any():
            return None, None
        i = int(fn(np.where(np.isfinite(v), v, np.nan)))
        return float(v[i]), float(h[i])

    zmax, zmax_h = extreme("DBZH", np.nanargmax)
    zdrmax, zdrmax_h = extreme("ZDR", np.nanargmax)
    rhomin, rhomin_h = extreme("RHOHV", np.nanargmin)
    z = data["DBZH"]
    above = np.flatnonzero(np.isfinite(z) & (z >= 5.0))
    echo_top = float(h[above[-1]]) if above.size else None
    bright = "not found"
    # Search away from the ground (clutter, noise) and where there is echo
    window = np.isfinite(z) & (z >= 15.0) & (h >= (h[0] + 500.0 if h.size else 0.0))
    if window.sum() >= 3:
        zd = np.where(window, data["ZDR"], np.nan)
        rh = np.where(window, data["RHOHV"], np.nan)
        zz = np.where(window, z, np.nan)
        if np.isfinite(zd).any() and np.isfinite(rh).any():
            i_zd, i_rh, i_z = (int(np.nanargmax(zd)), int(np.nanargmin(rh)), int(np.nanargmax(zz)))
            # ZDR peak >= 0.8 dB with the RhoHV dip (< 0.97) and the Z maximum close to it
            if (
                zd[i_zd] >= 0.8
                and rh[i_rh] < 0.97
                and abs(h[i_zd] - h[i_rh]) <= 500.0
                and abs(h[i_zd] - h[i_z]) <= 750.0
            ):
                bright = f"yes, near {0.5 * (h[i_zd] + h[i_rh]):.0f} m ASL"
    return {
        "zmax": zmax,
        "zmax_h": zmax_h,
        "zdrmax": zdrmax,
        "zdrmax_h": zdrmax_h,
        "rhomin": rhomin,
        "rhomin_h": rhomin_h,
        "echo_top": echo_top,
        "bright_band": bright,
    }


def _summary(sig, altitude, chosen):
    lines = []
    if sig["zmax"] is not None:
        lines.append(f"Strongest reflectivity {sig['zmax']:.1f} dBZ at {sig['zmax_h']:.0f} m ASL.")
    if sig["zdrmax"] is not None:
        lines.append(f"ZDR peaks at {sig['zdrmax']:.2f} dB at {sig['zdrmax_h']:.0f} m ASL.")
    if sig["rhomin"] is not None:
        lines.append(f"RhoHV is lowest ({sig['rhomin']:.3f}) at {sig['rhomin_h']:.0f} m ASL.")
    if sig["echo_top"] is not None:
        lines.append(f"Echo top (Z ≥ 5 dBZ) at {sig['echo_top']:.0f} m ASL.")
    lines.append(
        "Bright-band signature (ZDR peak, RhoHV dip and Z maximum co-located): "
        + sig["bright_band"]
        + "."
    )
    lines.append(f"Heights are above sea level; the radar is at {altitude:.0f} m.")
    return "\n".join(lines)

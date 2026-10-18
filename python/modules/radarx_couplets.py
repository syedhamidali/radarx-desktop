"""radarx: rotation couplets (mesocyclone / tornadic vortex candidates) from LLSD shear."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import xarray as xr

from engine.plugins import ModuleError, Result, module, param, point


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
MAX_ROWS = 60

NOTES = """\
radarx.retrieve.rotation_couplets: compact maxima of the LLSD azimuthal shear (Smith and \
Elmore 2004; Mahalik et al. 2019). A couplet is an 8-connected region of gates whose shear is \
at or above the threshold (default 0.006 1/s, the value Veillette et al. 2025 apply to \
azimuthal shear in their baselines and credit to the NWS Tornado Probability Algorithm, \
Sandmael et al. 2023) and at least the minimum area. For each couplet the peak shear, the \
velocity difference Delta-V across it (inbound and outbound extremes within 5 km of arc at \
the range of the peak) and the rotational velocity Vrot = Delta-V / 2 are reported.

This is a simple physical diagnostic, not the machine-learning Tornado Probability Algorithm \
and not a tornado detection: range-folded or poorly dealiased velocity, gust fronts, \
convergence lines and noise at weak-echo gates all produce couplets. The radarx tornado \
probability CNN (TorNet) and a tornado debris signature algorithm are not part of this module. \
Velocity is dealiased first by default (region method, see the Velocity Dealiasing module for \
limits). With 'Tilts needed' above 1 a couplet must continue on that many elevation angles \
(peaks linked within 5 km on neighbouring tilts, after the association idea of the WSR-88D \
Mesocyclone Detection Algorithm); this runs on the whole volume."""


def _dealiased(rt, ds: xr.Dataset, nyquist: float, notes: list[str], label: str) -> xr.Dataset:
    nyq, source = A.nyquist_velocity(ds, "VRADH", nyquist)
    notes.append(f"{label}: Nyquist {nyq:g} m/s ({source})")
    return ds.assign({"VRADH_dealiased": A.dealias(rt, ds, "VRADH", nyq)})


@module(
    id="radarx_couplets",
    title="Rotation Couplets (radarx)",
    description=(
        "radarx.retrieve.rotation_couplets: finds compact maxima of LLSD azimuthal shear "
        "above a threshold and reports peak shear, Delta-V and rotational velocity "
        "(Smith & Elmore 2004; threshold after Veillette et al. 2025 / Sandmael et al. 2023). "
        "A diagnostic for mesocyclone and tornadic-vortex candidates, not a tornado detector."
    ),
    category="Hazards",
    requires=["VRADH"],
    optional=["DBZH"],
    needs=A.NEEDS,
    params=[
        param(
            "threshold",
            "number",
            0.006,
            "Shear threshold (1/s)",
            min=0.002,
            max=0.05,
            step=0.001,
        ),
        param("min_area_km2", "number", 0.5, "Min area (km²)", min=0, max=20, step=0.1),
        param(
            "rotation",
            "select",
            "cyclonic",
            "Rotation",
            options=["cyclonic", "anticyclonic", "both"],
            help="Cyclonic = positive shear (Northern Hemisphere mesocyclones)",
        ),
        param(
            "min_dbz",
            "number",
            10.0,
            "Min reflectivity (dBZ, -30 = off)",
            min=-30,
            max=50,
            step=1,
            help="Gates below this cannot be part of a couplet (needs DBZH on the sweep)",
        ),
        param(
            "min_tilts",
            "int",
            1,
            "Tilts needed (1 = this sweep only)",
            min=1,
            max=6,
            step=1,
            help="2+ requires vertical continuity; runs on the whole volume (slower)",
        ),
        param("median_filter", "bool", False, "3x3 median filter first"),
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
    ],
    version="1.0",
)
def run(
    ctx,
    threshold,
    min_area_km2,
    rotation,
    min_dbz,
    min_tilts,
    median_filter,
    dealias_first,
    nyquist_velocity,
):
    rt = A.rx()
    notes: list[str] = []
    kwargs = {
        "threshold": float(threshold),
        "min_area": float(min_area_km2) * 1e6,
        "rotation": rotation,
        "min_dbz": float(min_dbz) if min_dbz > -30 else None,
        "median_filter": True if median_filter else None,
    }
    if kwargs["min_dbz"] is not None and not ctx.has("DBZH"):
        kwargs["min_dbz"] = None
        notes.append("No reflectivity on this sweep: the minimum-dBZ condition was skipped.")

    with A.captured_warnings() as warns:
        if min_tilts <= 1:
            ds = A.prepare_sweep(ctx, georeference=True)
            if dealias_first:
                ctx.progress(20, "Dealiasing")
                ds = _dealiased(rt, ds, nyquist_velocity, notes, "Velocity")
            else:
                kwargs["field"] = "VRADH"
            ctx.progress(60, "Finding couplets")
            with A.guard("Rotation couplets"):
                found = rt.rotation_couplets(ds, **kwargs)
        else:
            found = _volume_couplets(
                ctx, rt, kwargs, int(min_tilts), dealias_first, nyquist_velocity, notes
            )

    n_total = int(found.sizes["couplet"])
    n = min(n_total, MAX_ROWS)
    rows, features = [], []
    for i in range(n):
        cid = f"C{i + 1:02d}"
        az, rng = float(found["azimuth"].values[i]), float(found["range"].values[i])
        shear = float(found["azimuthal_shear"].values[i])
        vrot = float(found["rotational_velocity"].values[i])
        rows.append(
            {
                "id": cid,
                "azimuth": round(az, 1),
                "range_km": round(rng / 1000.0, 1),
                "shear": round(shear, 4),
                "delta_v": round(float(found["delta_v"].values[i]), 1),
                "vrot": round(vrot, 1),
                "area_km2": round(float(found["area"].values[i]), 2),
                **({"n_tilts": int(found["n_tilts"].values[i])} if "n_tilts" in found else {}),
            }
        )
        color = "#ef4444" if shear >= 0 else "#3b82f6"
        features.append(point(cid, [az, rng], label=f"{vrot:.0f} m/s", color=color))

    res = Result()
    shears = np.abs(found["azimuthal_shear"].values) if n_total else np.zeros(0)
    vrots = found["rotational_velocity"].values if n_total else np.zeros(0)
    res.scalars(
        [
            {"label": "Couplets", "value": n_total},
            {
                "label": "Strongest shear",
                "value": float(shears.max()) if n_total else None,
                "unit": "1/s",
            },
            {
                "label": "Max Vrot",
                "value": float(np.nanmax(vrots)) if n_total and np.isfinite(vrots).any() else None,
                "unit": "m/s",
            },
            {"label": "Threshold", "value": float(threshold), "unit": "1/s"},
            {"label": "Tilts needed", "value": int(min_tilts)},
            {"label": "radarx", "value": A.radarx_version()},
        ]
    )
    columns = [
        {"key": "id", "label": "Couplet"},
        {"key": "azimuth", "label": "Azimuth", "unit": "°", "digits": 1},
        {"key": "range_km", "label": "Range", "unit": "km", "digits": 1},
        {"key": "shear", "label": "Peak shear", "unit": "1/s", "digits": 4},
        {"key": "delta_v", "label": "Delta-V", "unit": "m/s", "digits": 1},
        {"key": "vrot", "label": "Vrot", "unit": "m/s", "digits": 1},
        {"key": "area_km2", "label": "Area", "unit": "km²", "digits": 2},
    ]
    if "n_tilts" in found:
        columns.append({"key": "n_tilts", "label": "Tilts", "digits": 0})
    title = f"Rotation couplets ({n_total}" + (f", strongest {n} shown)" if n_total > n else ")")
    res.table(title, columns, rows, id_key="id")
    res.overlay("Rotation couplets", features)
    body = "\n".join([*notes, NOTES])
    res.text(body, title="About this method")
    return A.add_warnings(res, warns)


def _volume_couplets(ctx, rt, kwargs, min_tilts, dealias_first, nyquist, notes):
    """Couplets of the selected sweep that continue on ``min_tilts`` tilts of the volume."""
    tree = ctx.datatree
    site = A.site_coords(tree)
    nodes: dict[str, xr.Dataset] = {"/": tree.root.to_dataset(inherit=False)}
    selected = A.selected_name(ctx)
    for i, name in enumerate(A.sweep_names(tree)):
        ctx.progress(10 + 40 * i // max(len(tree.children), 1), f"Preparing {name}")
        node = tree[name].to_dataset()
        node = A.standardize(node, ctx.var_overrides)
        if "VRADH" not in node.data_vars:
            continue
        node = node.assign_coords(**{k: v for k, v in site.items() if k not in node.coords})
        node = A.georeference_sweep(node)
        if dealias_first:
            try:
                node = _dealiased(rt, node, nyquist if name == selected else 0.0, [], name)
            except ModuleError as exc:
                if name == selected:
                    raise
                notes.append(f"{name} skipped: {exc}")
                continue
        nodes[name] = node.load()
    if selected not in nodes:
        raise ModuleError("The selected sweep has no velocity")
    if len(nodes) < min_tilts + 1:
        raise ModuleError(
            f"Only {len(nodes) - 1} sweeps with velocity are available; "
            f"cannot require {min_tilts} tilts"
        )
    sub = xr.DataTree.from_dict(nodes)
    notes.append(
        f"Vertical continuity required over {min_tilts} tilts of the {len(nodes) - 1} "
        "velocity sweeps of this volume"
    )
    ctx.progress(70, "Finding couplets")
    kw = dict(kwargs)
    kw["min_tilts"] = min_tilts
    with A.guard("Rotation couplets"):
        out = rt.rotation_couplets(sub, **kw)
    if selected not in out.children:
        raise ModuleError("No couplet result for the selected sweep")
    return out[selected].to_dataset()

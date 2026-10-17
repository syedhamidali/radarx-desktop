"""radarx: ZDR arc and KDP-ZDR separation of supercells (Wilson & Van Den Broeke 2021)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

from engine.plugins import ModuleError, Result, line, module, param, point, polygon


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
MAX_STORMS = 40

NOTES = """\
Object-based ZDR arc / KDP-ZDR separation algorithm of Wilson and Van Den Broeke (2021, JTECH \
38, 371-386), radarx.retrieve.zdr_arc. The ZDR arc is the band of large drops (high ZDR) \
along the inflow edge of a supercell's forward flank; small drops are carried farther into \
the storm, where KDP is enhanced (the KDP 'foot'). The distance and direction between the two \
indicate low-level shear and helicity.

Steps, as in the paper: (1) ZDR is masked where the reflectivity gradient does not point \
within 120 degrees of the forward-flank downdraft (FFD) vector - the direction you enter - \
and where RHOHV < 0.6; (2) the masked ZDR is contoured at 3.25 dB (the paper's 3.5 dB lowered \
for gridding smoothing) and every region is a candidate arc; (3) storm objects are the \
centroids of >= 45 dBZ regions larger than 20 km2 (50 dBZ cores inside regions larger than \
300 km2); a candidate belongs to the nearest storm within 30 km; (4) the candidates of a storm \
form its arc; (5) KDP >= 1.5 deg/km where Z >= 35 dBZ within 15 km of a storm is its foot, and \
the separation vector points from the foot centroid to the arc centroid.

IMPORTANT DIFFERENCES FROM THE PAPER: the paper removes non-arc candidates (rear flank, north \
side, small storms) with a random forest trained on 4724 hand-labelled candidates; that model \
cannot be rebuilt, so here EVERY candidate with a storm is counted as arc and the arc area is \
over-estimated where the rear flank has high ZDR. Storm tracking from scan to scan is not \
included. The algorithm is for supercells: squall-line leading edges and ZDR under developing \
updrafts are often detected as arcs. ZDR should be calibrated (the 3.25 dB threshold depends \
on it).

The selected sweep (use a low elevation) is resampled to a Cartesian grid centred on the radar \
(nearest neighbour, default 500 m, the paper uses 493 m) and analysed there, so the results \
are in metres east/north of the radar. KDP is estimated from PHIDP with radarx when the sweep \
has none. The gridded map shows ZDR of the cells accepted as arc, sampled back onto the sweep. \
Outlines are convex hulls of the arc cells. The existing 'ZDR Arc Candidates (heuristic)' \
module is a simpler shape-only alternative that needs no radarx."""


@module(
    id="radarx_zdr_arc",
    title="ZDR Arc & KDP-ZDR Separation (radarx)",
    description=(
        "radarx.retrieve.zdr_arc: ZDR arc area, mean/top-10 ZDR and KDP-ZDR separation vector "
        "(length, angle to storm motion) per storm, after Wilson & Van Den Broeke (2021). "
        "Needs the forward-flank direction; the paper's random-forest arc filter is not "
        "included."
    ),
    category="Storm",
    requires=["DBZH", "ZDR"],
    optional=["RHOHV", "PHIDP", "KDP"],
    needs=A.NEEDS,
    scope="volume",
    params=[
        param(
            "ffd_direction",
            "number",
            225.0,
            "FFD direction (° bearing, into the storm)",
            min=0,
            max=360,
            step=5,
            help="Perpendicular to the forward flank, pointing into the storm; "
            "225 = storm moving to the NE",
        ),
        param(
            "storm_motion",
            "number",
            45.0,
            "Storm motion (° bearing it moves toward)",
            min=0,
            max=360,
            step=5,
            help="For the separation angle only",
        ),
        param("zdr_threshold", "number", 3.25, "ZDR arc threshold (dB)", min=1, max=6, step=0.25),
        param(
            "kdp_threshold", "number", 1.5, "KDP foot threshold (deg/km)", min=0.5, max=6, step=0.25
        ),
        param("min_rhohv", "number", 0.6, "Min RHOHV", min=0.3, max=1.0, step=0.05),
        param("min_dbz", "number", 20.0, "Min reflectivity (dBZ)", min=0, max=45, step=1),
        param(
            "max_distance_km",
            "number",
            30.0,
            "Max arc-to-storm distance (km)",
            min=5,
            max=80,
            step=5,
        ),
        param(
            "grid_spacing_m",
            "number",
            500.0,
            "Grid spacing (m)",
            min=250,
            max=1000,
            step=250,
        ),
        param(
            "max_range_km",
            "number",
            100.0,
            "Analysis radius (km)",
            min=20,
            max=200,
            step=10,
        ),
    ],
    version="1.0",
)
def run(
    ctx,
    ffd_direction,
    storm_motion,
    zdr_threshold,
    kdp_threshold,
    min_rhohv,
    min_dbz,
    max_distance_km,
    grid_spacing_m,
    max_range_km,
):
    rt = A.rx()
    ds = A.prepare_sweep(ctx)
    notes: list[str] = []
    with A.captured_warnings() as warns:
        ds, estimated = A.with_kdp(rt, ds)
        has_kdp = any(n in ds.data_vars for n in ("KDP", "specific_differential_phase"))
        if estimated:
            notes.append("KDP was estimated from PHIDP (radarx.retrieve.estimate_kdp defaults).")
        if not has_kdp:
            notes.append("No KDP or PHIDP on this sweep: KDP foot and separation are not computed.")

        fields = {
            "DBZH": A.polar_array(ctx.field("DBZH"), ds, "DBZH"),
            "ZDR": A.polar_array(ctx.field("ZDR"), ds, "ZDR"),
        }
        if ctx.has("RHOHV"):
            fields["RHOHV"] = A.polar_array(ctx.field("RHOHV"), ds, "RHOHV")
        if has_kdp:
            kname = "KDP" if "KDP" in ds.data_vars else "specific_differential_phase"
            fields["KDP"] = A.polar_array(ds[kname], ds, "KDP")
        ctx.progress(25, "Resampling the sweep to a Cartesian grid")
        grid = A.sweep_to_cartesian(fields, ds, float(grid_spacing_m), float(max_range_km) * 1000.0)
        if grid.sizes["x"] < 10:
            raise ModuleError("The sweep covers too small an area for the analysis")

        ctx.progress(50, "Finding ZDR arcs and KDP feet")
        kwargs: dict = {
            "zdr": "ZDR",
            "dbz": "DBZH",
            "ffd_direction": float(ffd_direction),
            "storm_motion": float(storm_motion),
            "zdr_threshold": float(zdr_threshold),
            "kdp_threshold": float(kdp_threshold),
            "min_rhohv": float(min_rhohv),
            "min_dbz": float(min_dbz),
            "max_distance": float(max_distance_km),
        }
        if "RHOHV" in fields:
            kwargs["rhohv"] = "RHOHV"
        if "KDP" in fields:
            kwargs["kdp"] = "KDP"
        with A.guard("ZDR arc"):
            out = rt.zdr_arc(grid, **kwargs)

    x_axis = grid["x"].values
    y_axis = grid["y"].values
    arc_cells = np.asarray(out["ZDR_ARC_STORM"].transpose("y", "x").values)
    zdr_grid = np.asarray(grid["ZDR"].transpose("y", "x").values, dtype=np.float32)
    arc_zdr = np.where(arc_cells >= 0, zdr_grid, np.nan).astype(np.float32)
    shown = A.sample_cartesian(arc_zdr, x_axis, y_axis, ds, flat=True)

    n_storms = int(out.sizes["storm"])
    n_cand = int(out.sizes["candidate"])
    n_arc = int(np.asarray(out["candidate_arc"].values).sum()) if n_cand else 0
    storm = {
        k: np.asarray(out[k].values, dtype=float) for k in out.data_vars if "storm" in out[k].dims
    }
    # variables with a 'storm' dim only (candidate_* have the 'candidate' dim)
    rows, features = [], []
    cell_gx, cell_gy = np.meshgrid(x_axis, y_axis)
    for s in range(min(n_storms, MAX_STORMS)):
        sid = f"S{s + 1:02d}"
        has_arc = np.isfinite(storm["arc_area"][s])
        sx, sy = float(storm["storm_x"][s]), float(storm["storm_y"][s])
        az, rng = A.xy_to_polar(sx, sy)
        rows.append(
            {
                "id": sid,
                "azimuth": round(az, 1),
                "range_km": round(rng / 1000.0, 1),
                "storm_area_km2": round(float(storm["storm_area"][s]), 1),
                "arc_area_km2": _r(storm["arc_area"][s], 1),
                "arc_mean_zdr": _r(storm["arc_mean_zdr"][s], 2),
                "arc_top10_zdr": _r(storm["arc_top10_zdr"][s], 2),
                "foot_area_km2": _r(storm["foot_area"][s], 1),
                "separation_km": _r(storm["separation_length"][s], 1),
                "separation_angle": _r(storm["separation_angle"][s], 0),
            }
        )
        features.append(point(sid, [sx, sy], frame="xy", label=sid, color="#f59e0b"))
        if has_arc:
            sel = arc_cells == s
            ring = A.hull_xy(cell_gx[sel], cell_gy[sel], pad=grid_spacing_m / 2.0)
            label = f"{float(storm['arc_mean_zdr'][s]):.1f} dB"
            if ring:
                features.append(
                    polygon(f"{sid}-arc", ring, frame="xy", label=label, color="#a855f7", fill=0.25)
                )
            else:
                features.append(
                    point(
                        f"{sid}-arc",
                        [float(storm["arc_x"][s]), float(storm["arc_y"][s])],
                        frame="xy",
                        label=label,
                        color="#a855f7",
                    )
                )
        if np.isfinite(storm["separation_length"][s]):
            features.append(
                line(
                    f"{sid}-sep",
                    [
                        [float(storm["foot_x"][s]), float(storm["foot_y"][s])],
                        [float(storm["arc_x"][s]), float(storm["arc_y"][s])],
                    ],
                    frame="xy",
                    label=f"{float(storm['separation_length'][s]):.1f} km",
                    color="#10b981",
                )
            )

    res = Result()
    res.field("ZDR arc cells (radarx)", shown, units="dB", vmin=0.0, vmax=6.0, cmap="plasma")
    finite_arc = storm["arc_area"][np.isfinite(storm["arc_area"])] if n_storms else np.zeros(0)
    res.scalars(
        [
            {"label": "Storm objects", "value": n_storms},
            {"label": "ZDR arc candidates", "value": n_cand},
            {"label": "Candidates counted as arc", "value": n_arc},
            {
                "label": "Total arc area",
                "value": float(finite_arc.sum()) if finite_arc.size else None,
                "unit": "km²",
            },
            {
                "label": "Max separation",
                "value": _max(storm.get("separation_length")),
                "unit": "km",
            },
            {"label": "FFD / storm motion", "value": f"{ffd_direction:g}° / {storm_motion:g}°"},
            {
                "label": "Grid",
                "value": f"{grid.sizes['x']} x {grid.sizes['y']} at {grid_spacing_m:g} m",
            },
            {"label": "radarx", "value": A.radarx_version()},
        ]
    )
    if rows:
        res.table(
            f"Storms ({n_storms})",
            [
                {"key": "id", "label": "Storm"},
                {"key": "azimuth", "label": "Azimuth", "unit": "°", "digits": 1},
                {"key": "range_km", "label": "Range", "unit": "km", "digits": 1},
                {"key": "storm_area_km2", "label": "Storm area", "unit": "km²", "digits": 1},
                {"key": "arc_area_km2", "label": "Arc area", "unit": "km²", "digits": 1},
                {"key": "arc_mean_zdr", "label": "Arc mean ZDR", "unit": "dB", "digits": 2},
                {"key": "arc_top10_zdr", "label": "Arc top-10 ZDR", "unit": "dB", "digits": 2},
                {"key": "foot_area_km2", "label": "KDP foot area", "unit": "km²", "digits": 1},
                {"key": "separation_km", "label": "Separation", "unit": "km", "digits": 1},
                {"key": "separation_angle", "label": "Angle to motion", "unit": "°", "digits": 0},
            ],
            rows,
            id_key="id",
        )
    res.overlay("Storms, ZDR arcs, separation vectors", features)
    notes.append(NOTES)
    res.text("\n".join(notes), title="About this method")
    return A.add_warnings(res, warns)


def _r(value, digits: int):
    v = float(value)
    return round(v, digits) if np.isfinite(v) else None


def _max(arr):
    if arr is None or not np.isfinite(arr).any():
        return None
    return float(np.nanmax(arr))

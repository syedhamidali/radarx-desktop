"""Heuristic ZDR arc detection from gated, elongated high-ZDR regions."""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np

from engine._lazy import LazyModule
from engine.plugins import ModuleError, Result, module, param, point, polygon

ndimage = LazyModule("scipy.ndimage")  # imported on first use (~150 ms)

MAX_CANDIDATES = 300  # largest regions examined in detail
MAX_ARCS = 100  # rows / shapes reported
NEIGHBOURS = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], dtype=np.uint8)
EIGHT = np.ones((3, 3), dtype=int)

EXPLANATION = """\
This is a simple heuristic, not a validated ZDR-arc algorithm.

1. Gates are kept where DBZH >= z_min, ZDR >= zdr_min and (if the sweep has RHOHV) \
RHOHV >= rhohv_min. High RHOHV screens out non-meteorological echo.
2. The mask is lightly cleaned (gates with fewer than two set neighbours are dropped), \
then split into 8-connected regions (joined across the 0/360 seam).
3. For each region the range-weighted gate area, mean/max ZDR and a centroid are \
computed. The principal axes of the region's x/y gate positions give an elongation \
(major/minor axis length ratio) and a length estimate (extent along the major axis).
4. Regions with elongation >= min_elongation and area >= min_area_km2 are reported \
as arc candidates.

Limits: shape is the only "arc" criterion, so elongated areas of large drops that are \
not a storm-relative ZDR arc (e.g. a melting-layer or bright-band ring on a high-elevation \
sweep, a precipitation edge, or a sector clipped by the sweep boundary) will also be \
flagged. The method does not use storm motion, the low-level inflow side, or ZDR column \
context, ignores calibration or attenuation biases in ZDR, and curved arcs are measured \
along the straight major axis, which underestimates their length. Treat results as \
candidates to inspect, not detections."""


def _load_utils():
    """Import the sibling ``_radar_utils.py`` (modules are loaded by path, not as a package)."""
    name = "xradar_module_radar_utils"
    if name not in sys.modules:
        path = Path(__file__).with_name("_radar_utils.py")
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


U = _load_utils()


def _despeckle(mask: np.ndarray, wrap: bool) -> np.ndarray:
    """Drop gates with fewer than 2 of 8 neighbours set (isolated gates, 1-gate spurs).

    Deliberately lighter than a morphological opening: at long range arcs are only 2-3 rays
    wide and an opening would shatter them. Wraps in azimuth for full-circle sweeps.
    """
    m = mask.astype(np.uint8)
    if wrap:
        m = np.pad(m, ((1, 1), (0, 0)), mode="wrap")
    count = ndimage.convolve(m, NEIGHBOURS, mode="constant", cval=0)
    if wrap:
        count = count[1:-1]
    return mask & (count >= 2)


def _label(mask: np.ndarray, wrap: bool) -> np.ndarray:
    """8-connected labelling that also joins regions touching across the azimuth seam."""
    labels, n = ndimage.label(mask, structure=EIGHT)
    if not (wrap and n and mask[0].any() and mask[-1].any()):
        return labels
    first, last = labels[0], labels[-1]
    pairs = []
    for la, lb in ((last, first), (last[:-1], first[1:]), (last[1:], first[:-1])):
        keep = (la > 0) & (lb > 0)
        pairs.append(np.column_stack([la[keep], lb[keep]]))
    parent = np.arange(n + 1)

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a, b in np.unique(np.concatenate(pairs), axis=0):
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    mapping = np.array([find(i) for i in range(n + 1)])
    return mapping[labels]


@module(
    id="zdr_arc",
    title="ZDR Arc Candidates (heuristic)",
    description=(
        "HEURISTIC: flags elongated regions of high differential reflectivity "
        "(ZDR) inside moderate-to-strong echo as ZDR-arc candidates. Uses simple "
        "thresholds plus a shape test, not a validated detection algorithm; see the "
        "notes in the result for limits."
    ),
    category="Dual-pol",
    requires=["DBZH", "ZDR"],
    optional=["RHOHV"],
    params=[
        param("z_min", "number", 30.0, "Min reflectivity (dBZ)", min=-10, max=70, step=1),
        param("zdr_min", "number", 2.0, "Min ZDR (dB)", min=0, max=8, step=0.1),
        param("rhohv_min", "number", 0.9, "Min RHOHV (if available)", min=0.5, max=1.0, step=0.01),
        param(
            "min_elongation",
            "number",
            2.0,
            "Min elongation (major/minor)",
            min=1.0,
            max=20,
            step=0.1,
        ),
        param("min_area_km2", "number", 5.0, "Min area (km²)", min=0, max=5000, step=1),
    ],
    version="1.0",
)
def run(ctx, z_min, zdr_min, rhohv_min, min_elongation, min_area_km2):
    try:
        z = U.sweep_array(ctx, "DBZH").values
        zdr = U.sweep_array(ctx, "ZDR").values
        rho = U.sweep_array(ctx, "RHOHV").values if ctx.has("RHOHV") else None
    except ValueError as exc:
        raise ModuleError(str(exc)) from exc
    azimuth, range_m = ctx.polar_coords()
    n_az, n_rng = z.shape
    if n_az < 3 or n_rng < 3:
        raise ModuleError("Sweep is too small for ZDR arc analysis")

    wrap = U.covers_full_circle(azimuth)
    dr = float(np.median(np.diff(range_m)))
    dtheta = math.radians(U.azimuth_spacing_deg(azimuth))
    gate_area_m2 = np.maximum(range_m, dr / 2.0) * dtheta * dr  # per range bin
    gate_area_km2 = gate_area_m2 / 1e6

    ctx.progress(10, "Gating")
    mask = (z >= z_min) & (zdr >= zdr_min)
    if rho is not None:
        mask &= rho >= rhohv_min
    gated = int(mask.sum())
    mask = _despeckle(mask, wrap)

    ctx.progress(30, "Labelling regions")
    labels = _label(mask, wrap)
    objects = ndimage.find_objects(labels)
    ids = np.array([i + 1 for i, o in enumerate(objects) if o is not None], dtype=int)
    if ids.size:
        area_grid = np.broadcast_to(gate_area_km2, labels.shape)
        areas = np.atleast_1d(ndimage.sum(area_grid, labels, index=ids))
    else:
        areas = np.zeros(0)
    n_regions = int((areas >= min_area_km2).sum())
    cand = ids[np.argsort(-areas)][:MAX_CANDIDATES]
    cand_area = dict(zip(ids.tolist(), areas.tolist(), strict=True))

    ctx.progress(50, f"Analysing {len(cand)} regions")
    found = []
    floor = dr * dr / 12.0  # variance of a single gate footprint; avoids 0/0 on 1-gate-wide shapes
    for label_id in cand:
        area = cand_area[int(label_id)]
        if area < min_area_km2:
            continue
        sl = objects[label_id - 1]
        sub = labels[sl] == label_id
        ai, ri = np.nonzero(sub)
        ai_g, ri_g = ai + sl[0].start, ri + sl[1].start
        w = gate_area_m2[ri_g]
        x, y = U.polar_to_xy(azimuth[ai_g], range_m[ri_g])
        wsum = w.sum()
        dx, dy = x - (w * x).sum() / wsum, y - (w * y).sum() / wsum
        cov = (
            np.array(
                [
                    [(w * dx * dx).sum(), (w * dx * dy).sum()],
                    [(w * dx * dy).sum(), (w * dy * dy).sum()],
                ]
            )
            / wsum
        )
        evals, evecs = np.linalg.eigh(cov)  # ascending
        lam_minor, lam_major = max(evals[0], floor), max(evals[1], floor)
        elongation = math.sqrt(lam_major / lam_minor)
        if elongation < min_elongation:
            continue
        major = evecs[:, 1]
        minor = evecs[:, 0]
        p_major = dx * major[0] + dy * major[1]
        p_minor = dx * minor[0] + dy * minor[1]
        length_m = float(p_major.max() - p_major.min()) + dr
        width_m = float(p_minor.max() - p_minor.min()) + dr

        wrap_rows = wrap and sub.shape[0] == n_az
        if wrap_rows:
            padded = np.pad(sub, ((1, 1), (0, 0)), mode="wrap")
            eroded = ndimage.binary_erosion(padded)[1:-1]
        else:
            eroded = ndimage.binary_erosion(sub)
        ei, er = np.nonzero(sub & ~eroded)
        if ei.size == 0:
            ei, er = ai, ri
        outline = U.order_polygon(azimuth[ei + sl[0].start], range_m[er + sl[1].start], hull=True)

        zv = zdr[ai_g, ri_g]
        c_az = U.circular_mean_deg(azimuth[ai_g], w)
        c_rng = float((w * range_m[ri_g]).sum() / wsum)
        found.append(
            {
                "area_km2": area,
                "mean_zdr": float(np.nanmean(zv)),
                "max_zdr": float(np.nanmax(zv)),
                "mean_rhohv": float(np.nanmean(rho[ai_g, ri_g])) if rho is not None else None,
                "centroid_az": c_az,
                "centroid_range_m": c_rng,
                "length_km": length_m / 1000.0,
                "width_km": width_m / 1000.0,
                "elongation": elongation,
                "orientation_deg": math.degrees(math.atan2(major[0], major[1])) % 180.0,
                "outline": outline,
            }
        )

    found.sort(key=lambda f: f["area_km2"], reverse=True)
    n_found = len(found)
    found = found[:MAX_ARCS]
    for i, f in enumerate(found, start=1):
        f["id"] = f"A{i:03d}"

    ctx.progress(90, f"{n_found} arc candidates")
    res = Result()
    res.scalars(
        [
            {"label": "Arc candidates", "value": n_found},
            {"label": "Total arc area", "value": sum(f["area_km2"] for f in found), "unit": "km²"},
            {
                "label": "Max ZDR",
                "value": max((f["max_zdr"] for f in found), default=None),
                "unit": "dB",
            },
            {
                "label": "Longest candidate",
                "value": max((f["length_km"] for f in found), default=None),
                "unit": "km",
            },
            {"label": "Gates passing thresholds", "value": gated},
            {"label": "Regions >= min area (any shape)", "value": n_regions},
            {
                "label": "RHOHV screening",
                "value": "on" if rho is not None else "off (no RHOHV field)",
            },
        ]
    )

    rows = [
        {
            "id": f["id"],
            "centroid_az": round(f["centroid_az"], 1),
            "centroid_range_km": round(f["centroid_range_m"] / 1000.0, 1),
            "area_km2": round(f["area_km2"], 1),
            "mean_zdr": round(f["mean_zdr"], 2),
            "max_zdr": round(f["max_zdr"], 2),
            "length_km": round(f["length_km"], 1),
            "width_km": round(f["width_km"], 1),
            "elongation": round(f["elongation"], 1),
            "orientation_deg": round(f["orientation_deg"], 0),
            **(
                {"mean_rhohv": None if f["mean_rhohv"] is None else round(f["mean_rhohv"], 3)}
                if rho is not None
                else {}
            ),
        }
        for f in found
    ]
    columns = [
        {"key": "id", "label": "Arc"},
        {"key": "centroid_az", "label": "Azimuth", "unit": "°", "digits": 1},
        {"key": "centroid_range_km", "label": "Range", "unit": "km", "digits": 1},
        {"key": "area_km2", "label": "Area", "unit": "km²", "digits": 1},
        {"key": "mean_zdr", "label": "Mean ZDR", "unit": "dB", "digits": 2},
        {"key": "max_zdr", "label": "Max ZDR", "unit": "dB", "digits": 2},
        {"key": "length_km", "label": "Length", "unit": "km", "digits": 1},
        {"key": "width_km", "label": "Width", "unit": "km", "digits": 1},
        {"key": "elongation", "label": "Elongation", "digits": 1},
        {"key": "orientation_deg", "label": "Axis bearing", "unit": "°", "digits": 0},
    ]
    if rho is not None:
        columns.append({"key": "mean_rhohv", "label": "Mean RHOHV", "digits": 3})
    res.table(f"ZDR arc candidates ({n_found})", columns, rows, id_key="id")

    features = []
    for f in found:
        label = f"{f['mean_zdr']:.1f} dB"
        if f["outline"]:
            features.append(polygon(f["id"], f["outline"], label=label, color="#a855f7", fill=0.2))
        else:
            features.append(
                point(
                    f["id"], [f["centroid_az"], f["centroid_range_m"]], label=label, color="#a855f7"
                )
            )
    res.overlay("ZDR arc candidates", features)
    res.text(EXPLANATION, title="About this heuristic")
    return res

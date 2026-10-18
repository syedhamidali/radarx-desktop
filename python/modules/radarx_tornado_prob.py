"""radarx: tornado probability (TorNet CNN) with a rotation-score fallback."""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np

from engine.plugins import ModuleError, Result, module, param, polygon


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
MAX_ROWS = 40
DEFAULT_MODEL = "tornet-baseline-v1"
SHEAR_SATURATION = 0.02  # 1/s: LLSD azimuthal shear that scores 1.0 in the heuristic
VROT_SATURATION = 20.0  # m/s: rotational velocity that scores 1.0 in the heuristic

NOTES_ML = """\
Tornado probability of the TorNet CNN baseline (Veillette et al. 2025), \
radarx.retrieve.tornado_probability, run with ONNX Runtime. The network sees image chips \
(60 degrees x 60 km) of the two lowest tilts (0.5 and 0.9 degrees) of reflectivity, dealiased \
velocity, KDP, RHOHV, ZDR and spectrum width, tiles the sweep with overlapping chips and gives \
every gate the logistic of the logit map, maximum over chips. Velocity is dealiased with the \
region method and KDP is estimated from PHIDP when the file has none, both slightly different \
from the inputs the network was trained on. The network was trained on storm-centred samples \
(about 7 % tornadic) and its output is a likelihood on that distribution, not a calibrated \
frequency: the paper finds mid-range values over-confident and does not apply its isotonic \
calibration here. Skill figures belong to the benchmark chips, not to this full-scan output. \
It needs a WSR-88D-like volume with dual-polarisation and Doppler data at 0.5 and 0.9 degrees."""

NOTES_HEURISTIC = """\
This is the lighter rotation heuristic, NOT the machine-learning tornado probability: the \
TorNet CNN could not be run here (see the reason above). The rotation score is the LLSD \
azimuthal shear (Smith and Elmore 2004; Mahalik et al. 2019, radarx.retrieve.llsd) of the \
dealiased velocity, cyclonic rotation only by default, divided by 0.02 1/s and limited to 0-1, \
at gates with echo. Rotation objects are the couplets of radarx.retrieve.rotation_couplets \
(shear at or above the threshold, default 0.006 1/s, the value Veillette et al. 2025 apply to \
azimuthal shear in their baselines); the object score is the mean of shear/0.02 1/s and \
Vrot/20 m/s, each limited to 1. The scores and the Vrot classes are rules of thumb, not \
published thresholds and not probabilities. Range-folded or badly dealiased velocity, gust \
fronts, convergence lines and noise at weak echo all produce rotation; large distance from \
the radar blurs real vortices. Enable the ML path with 'pip install onnxruntime' and a TorNet \
ONNX model file."""


def ml_status(model_path: str = "") -> tuple[str | None, str]:
    """``(path, "")`` when the TorNet CNN can run offline, else ``(None, reason)``."""
    if importlib.util.find_spec("onnxruntime") is None:
        return None, "the onnxruntime package is not installed (pip install onnxruntime)"
    if model_path.strip():
        p = Path(model_path.strip()).expanduser()
        if not p.is_file():
            return None, f"the model file {p} does not exist"
        return str(p), ""
    try:
        from radarx.retrieve import _onnx_models

        cached = _onnx_models.cache_dir() / f"{DEFAULT_MODEL}.onnx"
    except Exception as exc:  # pooch missing, private API moved, ...
        return None, f"the radarx model cache cannot be located ({type(exc).__name__}: {exc})"
    if not cached.is_file():
        return None, (
            f"the TorNet ONNX model is not in the radarx cache ({cached}); radarx would "
            "download it from Hugging Face on first use, which this module never does. Put "
            "the model file there or enter its path in 'Model file'"
        )
    return str(cached), ""


@module(
    id="radarx_tornado_prob",
    title="Tornado Probability (radarx)",
    description=(
        "radarx.retrieve.tornado_probability: TorNet CNN tornado likelihood per gate "
        "(Veillette et al. 2025) when onnxruntime and the model are available; otherwise a "
        "labelled rotation-score heuristic from LLSD shear and rotation couplets. A "
        "research diagnostic, not a warning product."
    ),
    category="Hazards",
    requires=["VRADH"],
    optional=["DBZH", "ZDR", "RHOHV", "KDP", "PHIDP", "WRADH"],
    needs=A.NEEDS,
    scope="volume",
    params=[
        param(
            "method",
            "select",
            "auto",
            "Method",
            options=["auto", "ml", "heuristic"],
            help="auto = TorNet CNN when it can run, else the rotation heuristic",
        ),
        param(
            "model_path",
            "string",
            "",
            "Model file (ONNX)",
            help="Local TorNet ONNX file; empty = the model in the radarx cache",
        ),
        param(
            "min_probability",
            "number",
            0.2,
            "Object threshold (probability / score)",
            min=0.05,
            max=0.99,
            step=0.05,
            help="Regions at or above this form the listed objects",
        ),
        param(
            "max_range_km",
            "number",
            100.0,
            "Max range (km)",
            min=20,
            max=230,
            step=10,
            help="ML only. Limits the area the network scans (cost grows with range)",
        ),
        param(
            "shear_threshold",
            "number",
            0.006,
            "Shear threshold (1/s)",
            min=0.002,
            max=0.05,
            step=0.001,
            help="Heuristic only. LLSD azimuthal shear that starts a rotation object",
        ),
        param(
            "rotation",
            "select",
            "cyclonic",
            "Rotation",
            options=["cyclonic", "anticyclonic", "both"],
            help="Heuristic only",
        ),
        param(
            "min_dbz",
            "number",
            10.0,
            "Min reflectivity (dBZ, -30 = off)",
            min=-30,
            max=50,
            step=1,
            help="Heuristic only. Needs DBZH on the sweep",
        ),
        param(
            "dealias_first",
            "bool",
            True,
            "Dealias velocity first",
            help="Dealias the Doppler velocity before it enters the network / shear",
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
    model_path,
    min_probability,
    max_range_km,
    shear_threshold,
    rotation,
    min_dbz,
    dealias_first,
    nyquist_velocity,
):
    rt = A.rx()
    path, reason = ml_status(model_path)
    if method == "ml" and path is None:
        raise ModuleError(
            "The TorNet CNN cannot run: " + reason + ". Use method 'heuristic' (or 'auto') "
            "for the rotation-score fallback."
        )
    use_ml = path is not None and method in ("auto", "ml")
    if use_ml:
        X.need(rt, "tornado_probability", "The tornado probability")
        return _run_ml(
            ctx, rt, path, min_probability, max_range_km, dealias_first, nyquist_velocity
        )
    why = "" if method == "heuristic" else f"The TorNet CNN could not run: {reason}.\n"
    return _run_heuristic(
        ctx,
        rt,
        why,
        min_probability,
        shear_threshold,
        rotation,
        min_dbz,
        dealias_first,
        nyquist_velocity,
    )


# ---------------------------------------------------------------------------
# ML path
# ---------------------------------------------------------------------------


def _run_ml(ctx, rt, model_file, min_probability, max_range_km, dealias_first, nyquist):
    ds = X.selected_sweep(ctx)
    ctx.progress(5, "Reading the volume")
    vol = X.volume(
        ctx,
        any_of=("DBZH", "VRADH"),
        keep=("DBZH", "ZDR", "RHOHV", "KDP", "PHIDP", "VRADH", "WRADH"),
        progress=(5, 30),
    )
    kwargs: dict = {
        "model": model_file,  # a local path: radarx never downloads when given a file
        "dealias": bool(dealias_first),
        "max_range": float(max_range_km) * 1000.0,
    }
    if nyquist > 0:
        kwargs["nyquist_velocity"] = float(nyquist)
    ctx.progress(35, "Running the TorNet CNN")
    with A.captured_warnings() as warns, A.guard("Tornado probability"):
        out = rt.tornado_probability(vol.tree, **kwargs)

    prob_src = np.asarray(out["tornado_probability"].values, dtype=np.float32)
    az_src = np.asarray(out["azimuth"].values, dtype=np.float64)
    rng_src = np.asarray(out["range"].values, dtype=np.float64)
    shown = _remap_slant(prob_src, az_src, rng_src, ds)
    valid = np.isfinite(prob_src)
    regions = A.polar_regions(
        np.nan_to_num(prob_src, nan=0.0) >= float(min_probability),
        prob_src,
        az_src,
        rng_src,
        min_area_km2=0.5,
        max_regions=MAX_ROWS,
    )
    chip = np.asarray(out["chip_probability"].values, dtype=np.float64)
    peak = float(np.nanmax(prob_src)) if valid.any() else None
    res = Result()
    res.field(
        "Tornado probability (TorNet CNN)",
        shown,
        units="",
        vmin=0.0,
        vmax=1.0,
        cmap="turbo",
    )
    res.scalars(
        [
            {"label": "Method", "value": "TorNet CNN (ML)"},
            {"label": "Max probability", "value": peak},
            {
                "label": f"Gates ≥ {min_probability:g}",
                "value": int((np.nan_to_num(prob_src, nan=0.0) >= min_probability).sum()),
            },
            {"label": "Objects", "value": len(regions)},
            {"label": "Max chip probability", "value": float(chip.max()) if chip.size else None},
            {"label": "Chips run", "value": int(chip.size)},
            {"label": "Tilts", "value": "0.5° and 0.9°"},
            {"label": "radarx", "value": X.version()},
        ]
    )
    _objects_table(res, regions, "Tornado probability", "probability")
    notes = [NOTES_ML]
    if X.describe_skipped(vol):
        notes.insert(0, "Sweeps left out: " + X.describe_skipped(vol))
    res.text("\n".join(notes), title="About this method")
    return A.add_warnings(res, warns)


def _remap_slant(values, az_src, rng_src, ds):
    """Nearest-neighbour map of a (ray, slant range) product onto the sweep's gates."""
    tgt_az = np.asarray(ds["azimuth"].values, dtype=np.float64)
    tgt_rng = np.asarray(ds["range"].values, dtype=np.float64)
    ray, gap = A.nearest_ray(az_src, tgt_az)
    spacing = max(A.U.azimuth_spacing_deg(az_src), A.U.azimuth_spacing_deg(tgt_az))
    dr = float(np.median(np.diff(rng_src))) if rng_src.size > 1 else 250.0
    gate = np.clip(np.rint((tgt_rng - rng_src[0]) / dr).astype(np.int64), 0, rng_src.size - 1)
    ok_gate = (tgt_rng >= rng_src[0] - dr / 2) & (tgt_rng <= rng_src[-1] + dr / 2)
    out = np.asarray(values, dtype=np.float32)[np.ix_(ray, gate)]
    out[gap > 1.5 * spacing, :] = np.nan
    out[:, ~ok_gate] = np.nan
    return out


# ---------------------------------------------------------------------------
# heuristic path
# ---------------------------------------------------------------------------


def _run_heuristic(
    ctx, rt, why, min_score, shear_threshold, rotation, min_dbz, dealias_first, nyquist
):
    ds = X.selected_sweep(ctx, georeference=True)
    if "VRADH" not in ds.data_vars:
        raise ModuleError(
            "This sweep has no Doppler velocity (VRADH). Select a Doppler sweep (on NEXRAD "
            "split cuts velocity is on every second low-level sweep, e.g. sweep 1)."
        )
    notes = [why] if why else []
    llsd = X.need(rt, "llsd", "The rotation heuristic")
    couplets = X.need(rt, "rotation_couplets", "The rotation heuristic")
    with A.captured_warnings() as warns:
        field = "VRADH"
        if dealias_first:
            nyq, source = A.nyquist_velocity(ds, "VRADH", nyquist)
            ctx.progress(20, f"Dealiasing (Vn={nyq:g} m/s)")
            ds = ds.assign({"VRADH_dealiased": A.dealias(rt, ds, "VRADH", nyq)})
            field = "VRADH_dealiased"
            notes.append(f"Velocity dealiased with Nyquist {nyq:g} m/s ({source}).")
        use_dbz = min_dbz > -30 and "DBZH" in ds.data_vars
        if min_dbz > -30 and not use_dbz:
            notes.append("No reflectivity on this sweep: the minimum-dBZ condition was skipped.")
        ctx.progress(50, "Rotation shear")
        with A.guard("LLSD shear"):
            shear = A.polar_array(llsd(ds, field)["azimuthal_shear"], ds, "azimuthal_shear")
        ctx.progress(75, "Rotation objects")
        with A.guard("Rotation couplets"):
            found = couplets(
                ds,
                field,
                threshold=float(shear_threshold),
                rotation=rotation,
                min_dbz=float(min_dbz) if use_dbz else None,
            )

    signed = {"cyclonic": shear, "anticyclonic": -shear, "both": np.abs(shear)}[rotation]
    score = np.clip(signed / SHEAR_SATURATION, 0.0, 1.0).astype(np.float32)
    if use_dbz:
        score = np.where(np.nan_to_num(ds["DBZH"].values, nan=-99.0) >= min_dbz, score, np.nan)
    score = np.where(np.isfinite(signed), score, np.nan).astype(np.float32)

    n_total = int(found.sizes["couplet"])
    rows, features = [], []
    for i in range(min(n_total, MAX_ROWS)):
        sh = float(found["azimuthal_shear"].values[i])
        vrot = float(found["rotational_velocity"].values[i])
        obj_score = _object_score(abs(sh), vrot)
        cid = f"R{i + 1:02d}"
        az, rg = float(found["azimuth"].values[i]), float(found["range"].values[i])
        rows.append(
            {
                "id": cid,
                "azimuth": round(az, 1),
                "range_km": round(rg / 1000.0, 1),
                "shear": round(sh, 4),
                "vrot": round(vrot, 1) if math.isfinite(vrot) else None,
                "delta_v": round(float(found["delta_v"].values[i]), 1),
                "score": round(obj_score, 2),
                "klass": _vrot_class(vrot),
                "area_km2": round(float(found["area"].values[i]), 2),
            }
        )
        features.append(
            {
                "id": cid,
                "kind": "point",
                "frame": "polar",
                "coords": [[az, rg]],
                "label": f"{obj_score:.2f}",
                "color": "#ef4444" if sh >= 0 else "#3b82f6",
            }
        )
    above = [r for r in rows if r["score"] >= min_score]
    finite = np.isfinite(score)
    res = Result()
    res.field(
        "Rotation score (heuristic, not a probability)",
        score,
        units="",
        vmin=0.0,
        vmax=1.0,
        cmap="turbo",
    )
    res.scalars(
        [
            {"label": "Method", "value": "rotation heuristic (no ML)"},
            {
                "label": "Max rotation score",
                "value": float(np.nanmax(score)) if finite.any() else None,
            },
            {"label": "Rotation objects", "value": n_total},
            {"label": f"Objects with score ≥ {min_score:g}", "value": len(above)},
            {
                "label": "Strongest shear",
                "value": float(np.nanmax(np.abs(found["azimuthal_shear"].values)))
                if n_total
                else None,
                "unit": "1/s",
            },
            {
                "label": "Max Vrot",
                "value": float(np.nanmax(found["rotational_velocity"].values)) if n_total else None,
                "unit": "m/s",
            },
            {"label": "Shear threshold", "value": float(shear_threshold), "unit": "1/s"},
            {"label": "radarx", "value": X.version()},
        ]
    )
    res.table(
        f"Rotation objects ({n_total}"
        + (f", strongest {MAX_ROWS} shown)" if n_total > MAX_ROWS else ")"),
        [
            {"key": "id", "label": "Object"},
            {"key": "azimuth", "label": "Azimuth", "unit": "°", "digits": 1},
            {"key": "range_km", "label": "Range", "unit": "km", "digits": 1},
            {"key": "shear", "label": "Peak shear", "unit": "1/s", "digits": 4},
            {"key": "vrot", "label": "Vrot", "unit": "m/s", "digits": 1},
            {"key": "delta_v", "label": "Delta-V", "unit": "m/s", "digits": 1},
            {"key": "score", "label": "Rotation score", "digits": 2},
            {"key": "klass", "label": "Vrot class"},
            {"key": "area_km2", "label": "Area", "unit": "km²", "digits": 2},
        ],
        rows,
        id_key="id",
    )
    res.overlay("Rotation objects", features)
    res.text("\n".join([*notes, NOTES_HEURISTIC]), title="About this method")
    return A.add_warnings(res, warns)


def _object_score(shear: float, vrot: float) -> float:
    parts = [min(shear / SHEAR_SATURATION, 1.0)]
    if math.isfinite(vrot):
        parts.append(min(max(vrot, 0.0) / VROT_SATURATION, 1.0))
    return float(sum(parts) / len(parts))


def _vrot_class(vrot: float) -> str:
    if not math.isfinite(vrot):
        return "unknown"
    if vrot >= 20.0:
        return "strong"
    if vrot >= 15.0:
        return "moderate"
    if vrot >= 10.0:
        return "weak"
    return "marginal"


def _objects_table(res, regions, title, unit_label):
    rows, features = [], []
    for i, reg in enumerate(regions, start=1):
        oid = f"T{i:02d}"
        rows.append(
            {
                "id": oid,
                "azimuth": round(reg["centroid_az"], 1),
                "range_km": round(reg["centroid_range_m"] / 1000.0, 1),
                "peak": round(reg["peak"], 3),
                "mean": round(reg["mean"], 3),
                "area_km2": round(reg["area_km2"], 2),
            }
        )
        label = f"{reg['peak']:.2f}"
        if len(reg["outline"]) >= 3:
            features.append(polygon(oid, reg["outline"], label=label, color="#ef4444", fill=0.2))
        else:
            features.append(
                {
                    "id": oid,
                    "kind": "point",
                    "frame": "polar",
                    "coords": [[reg["centroid_az"], reg["centroid_range_m"]]],
                    "label": label,
                    "color": "#ef4444",
                }
            )
    res.table(
        f"{title} objects ({len(rows)})",
        [
            {"key": "id", "label": "Object"},
            {"key": "azimuth", "label": "Azimuth", "unit": "°", "digits": 1},
            {"key": "range_km", "label": "Range", "unit": "km", "digits": 1},
            {"key": "peak", "label": f"Peak {unit_label}", "digits": 3},
            {"key": "mean", "label": f"Mean {unit_label}", "digits": 3},
            {"key": "area_km2", "label": "Area", "unit": "km²", "digits": 2},
        ],
        rows,
        id_key="id",
    )
    res.overlay("Tornado probability objects", features)

"""Step catalog for processing pipelines (see :mod:`engine.pipeline`).

Every step is ``func(state, **params) -> StepOutcome | None`` on a
:class:`~engine.pipeline.WorkingState`. **Filters** only call ``state.restrict(keep)``;
**transforms** write new values into ``state.fields`` (and gap filling re-admits gates). A step
that does not apply to a sweep raises :class:`~engine.pipeline.StepSkippedError` (recorded, not
fatal).

The numerical work lives in small pure functions (the "kernels" above each step) that only use
numpy / scipy, so they can be copied verbatim into an exported script. ``radarx`` is used where it
has the method (de-aliasing, echo mask, noise correction) and a numpy fallback exists where it is
missing. Roles are looked up through :func:`engine.pipeline.find_variable` only.
"""

from __future__ import annotations

import importlib.util
import sys
import warnings
from pathlib import Path
from typing import Any

import numpy as np

from engine._lazy import LazyModule
from engine.pipeline import StepOutcome, StepSkippedError, WorkingState, step
from engine.plugins import param

ndimage = LazyModule("scipy.ndimage")  # imported on first use (~150 ms)

# ---------------------------------------------------------------------------
# Shared helper modules of the built-in plugin modules (loaded by path, like radarx_clean does)
# ---------------------------------------------------------------------------


def _load_helper(name: str, filename: str):
    if name not in sys.modules:
        path = Path(__file__).resolve().parent.parent / "modules" / filename
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:  # pragma: no cover
            raise ImportError(f"cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


A = _load_helper("xradar_module_radarx_adapter", "_radarx_adapter.py")
G = _load_helper("xradar_module_gapfill", "_gapfill.py")


def _have_radarx() -> bool:
    return importlib.util.find_spec("radarx") is not None


# ---------------------------------------------------------------------------
# Kernels (pure numpy / scipy; copied into exported scripts)
# ---------------------------------------------------------------------------


def neighbour_sums(values, valid, size, wrap):
    """``(sum, sum of squares, count)`` of the valid neighbours of every gate (centre excluded)."""
    mode = ("wrap" if wrap else "constant", "constant")
    area = float(size[0] * size[1])
    v = np.where(valid, values, 0.0).astype(np.float64)
    w = valid.astype(np.float64)
    s1 = ndimage.uniform_filter(v, size=size, mode=mode) * area - v
    s2 = ndimage.uniform_filter(v * v, size=size, mode=mode) * area - v * v
    n = np.rint(ndimage.uniform_filter(w, size=size, mode=mode) * area - w)
    return s1, s2, n


def reflectivity_texture(dbz, valid, wrap):
    """TDBZ (Hubbert et al. 2009): mean squared difference to the 8 neighbours (dBZ^2).

    NaN where fewer than 3 neighbours have data. Weather is smooth (small TDBZ), ground clutter
    and noise are not.
    """
    s1, s2, n = neighbour_sums(dbz, valid, (3, 3), wrap)
    z = np.where(valid, dbz, 0.0).astype(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        tdbz = (n * z * z - 2.0 * z * s1 + s2) / n
    return np.where(valid & (n >= 3), tdbz, np.nan)


def reflectivity_spin(dbz, valid, wrap, step_db=2.0):
    """SPIN (Steiner and Smith 2002): share of the 3x3 neighbourhood where the reflectivity
    gradient along the ray changes sign by more than ``step_db``. High for clutter, low for rain."""
    d = np.diff(np.where(valid, dbz, np.nan), axis=1)
    flip = np.zeros(dbz.shape, dtype=np.float64)
    with np.errstate(invalid="ignore"):
        inner = (
            (np.abs(d[:, :-1]) > step_db)
            & (np.abs(d[:, 1:]) > step_db)
            & (d[:, :-1] * d[:, 1:] < 0)
        )
    flip[:, 1:-1] = inner
    mode = ("wrap" if wrap else "constant", "constant")
    spin = ndimage.uniform_filter(flip, size=(3, 3), mode=mode)
    return np.where(valid, spin, np.nan)


def remove_small_regions(valid, min_size, wrap):
    """Boolean ``keep``: ``valid`` gates in 8-connected regions of at least ``min_size`` gates
    (regions touching across the 0/360 degree seam are joined when ``wrap``)."""
    if min_size <= 1 or not valid.any():
        return np.ones(valid.shape, dtype=bool)
    arr = np.pad(valid, ((1, 1), (0, 0)), mode="wrap" if wrap else "constant")
    labels, n = ndimage.label(arr, structure=np.ones((3, 3), dtype=int))
    sizes = np.bincount(labels[1:-1].ravel(), minlength=n + 1)
    big = sizes >= min_size
    big[0] = True
    return big[labels[1:-1]] | ~valid


def nan_median_filter(values, valid, size, wrap):
    """Median of the valid gates in a ``size = (rays, gates)`` window around every gate."""
    ky, kx = int(size[0]), int(size[1])
    arr = np.where(valid, values, np.nan).astype(np.float32)
    rows = ((ky // 2, ky // 2), (0, 0))
    if wrap:
        padded = np.pad(arr, rows, mode="wrap")
    else:
        padded = np.pad(arr, rows, mode="constant", constant_values=np.nan)
    padded = np.pad(padded, ((0, 0), (kx // 2, kx // 2)), mode="constant", constant_values=np.nan)
    out = np.full(arr.shape, np.nan, dtype=np.float32)
    step_rows = max(1, 400_000 // max(1, arr.shape[1] * ky * kx) * 8)
    for r0 in range(0, arr.shape[0], step_rows):
        block = padded[r0 : r0 + step_rows + ky - 1]
        win = np.lib.stride_tricks.sliding_window_view(block, (ky, kx))
        flat = win.reshape(win.shape[0], win.shape[1], -1)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            out[r0 : r0 + win.shape[0]] = np.nanmedian(flat, axis=-1)
    return out


def spikes(values, valid, size, wrap, threshold, n_sigma):
    """Isolated spikes: gates that differ from the mean of their valid neighbours by more than
    ``threshold`` (when 0: by more than ``n_sigma`` robust standard deviations of all such
    differences) AND from the median of those neighbours (so the neighbours of a spike, whose mean
    is pulled by it, are not flagged)."""
    s1, _, n = neighbour_sums(values, valid, size, wrap)
    with np.errstate(invalid="ignore", divide="ignore"):
        diff = np.where(valid & (n >= 2), np.abs(values - s1 / n), np.nan)
    thr = float(threshold)
    if thr <= 0:
        finite = diff[np.isfinite(diff)]
        if finite.size == 0:
            return np.zeros(valid.shape, dtype=bool)
        lo, hi = np.percentile(values[valid], [1, 99])
        thr = max(n_sigma * 1.4826 * float(np.median(finite)), 0.02 * float(hi - lo), 1e-6)
    with np.errstate(invalid="ignore"):
        cand = np.argwhere(np.isfinite(diff) & (diff > thr))
    out = np.zeros(valid.shape, dtype=bool)
    if len(cand) == 0:
        return out
    ky, kx = int(size[0]), int(size[1])
    dy, dx = np.meshgrid(np.arange(ky) - ky // 2, np.arange(kx) - kx // 2, indexing="ij")
    dy, dx = dy.ravel(), dx.ravel()
    centre = (dy == 0) & (dx == 0)
    rows = cand[:, 0:1] + dy[None, :]
    cols = cand[:, 1:2] + dx[None, :]
    inside = (cols >= 0) & (cols < values.shape[1])
    if wrap:
        rows = rows % values.shape[0]
    else:
        inside &= (rows >= 0) & (rows < values.shape[0])
    rows = np.clip(rows, 0, values.shape[0] - 1)
    cols = np.clip(cols, 0, values.shape[1] - 1)
    nb = np.where(inside & valid[rows, cols], values[rows, cols].astype(np.float64), np.nan)
    nb[:, centre] = np.nan
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        med = np.nanmedian(nb, axis=1)
    centre_val = values[cand[:, 0], cand[:, 1]]
    with np.errstate(invalid="ignore"):
        is_spike = np.abs(centre_val - med) > thr
    out[cand[is_spike, 0], cand[is_spike, 1]] = True
    return out


def unfold_velocity(vel, nyquist, wrap):
    """Built-in velocity de-aliasing without radarx: radial continuity along every ray, then a
    ray-to-ray offset so neighbouring rays agree; the absolute fold is chosen so the sweep mean is
    nearest zero. Fine for smooth fields, weaker than radarx's region / UNRAVEL methods."""
    out = np.array(vel, dtype=np.float64)
    width = 2.0 * nyquist
    n_az, n_rng = out.shape
    prev = np.full(n_az, np.nan)
    for j in range(n_rng):
        col = out[:, j]
        ok = np.isfinite(col)
        has = ok & np.isfinite(prev)
        k = np.where(has, np.rint((prev - col) / width), 0.0)
        col = col + k * width
        out[:, j] = np.where(ok, col, np.nan)
        prev = np.where(ok, col, prev)
    order = list(range(n_az))
    for pass_ in range(2 if wrap else 1):
        for i in order[1:] if pass_ == 0 else order[:1] + order[1:]:
            a = out[i - 1]
            b = out[i]
            both = np.isfinite(a) & np.isfinite(b)
            if both.sum() < 5:
                continue
            k = np.rint(np.median(a[both] - b[both]) / width)
            if k:
                out[i] = b + k * width
    ok = np.isfinite(out)
    if ok.any():
        shift = np.rint(-np.nanmean(out[ok]) / width) * width
        out = out + shift
    return out.astype(np.float32)


def simple_meteo_mask(dbz, rhohv, zdr, valid, wrap, rhohv_min=0.8, tdbz_max=60.0, zdr_max=6.0):
    """Built-in echo classification without radarx: RhoHV, |ZDR| and the texture of reflectivity."""
    keep = np.ones(valid.shape, dtype=bool)
    if rhohv is not None:
        keep &= ~(np.isfinite(rhohv) & (rhohv < rhohv_min))
    if zdr is not None:
        keep &= ~(np.isfinite(zdr) & (np.abs(zdr) > zdr_max))
    tdbz = reflectivity_texture(dbz, np.isfinite(dbz) & valid, wrap)
    keep &= ~(np.isfinite(tdbz) & (tdbz > tdbz_max))
    return keep


# ---------------------------------------------------------------------------
# radarx glue
# ---------------------------------------------------------------------------

_RADARX_ROLES = ("DBZH", "ZDR", "RHOHV", "PHIDP", "VRADH", "SNR")


def _radarx_dataset(state: WorkingState, roles=_RADARX_ROLES):
    """The sweep with canonical names, current (masked) field values, and site coordinates."""
    ds = A.prepare_sweep(state.ctx)
    new = {r: (("azimuth", "range"), state.masked(r)) for r in roles if r in state.fields}
    return ds.assign(new) if new else ds


SPLIT_CUT_ROLES = ("RHOHV", "ZDR", "PHIDP")  # dual-pol moments a Doppler cut of a split cut lacks
SPLIT_CUT_TOLERANCE_DEG = 0.1


def borrow_split_cut_fields(
    state: WorkingState, roles: list[str], datatree: Any, sweep: int, get_sweep_dataset
) -> list[str]:
    """Fill dual-pol roles a sweep lacks from the other cut of a split cut at the same elevation.

    NEXRAD scans each low elevation twice: a surveillance cut (reflectivity + dual-pol) and a
    Doppler cut (reflectivity + velocity, no RhoHV / ZDR). Filters that need RhoHV or ZDR can then
    still work on the Doppler cut: the companion sweep's field is mapped onto this sweep's grid
    (nearest ray and gate). Returns the notes describing what was borrowed.
    """
    from engine.pipeline import find_variable, polar_array
    from engine.reader import sorted_sweep_names

    wanted = [r for r in roles if r in SPLIT_CUT_ROLES and r not in state.fields]
    if not wanted:
        return []
    try:
        names = sorted_sweep_names(datatree)
        angles = [
            float(np.nanmean(np.asarray(datatree[n]["sweep_fixed_angle"].values))) for n in names
        ]
    except Exception:
        return []
    if not 0 <= sweep < len(names):
        return []
    notes: list[str] = []
    for role in wanted:
        for j, ang in enumerate(angles):
            if (
                j == sweep
                or not np.isfinite(ang)
                or abs(ang - angles[sweep]) > SPLIT_CUT_TOLERANCE_DEG
            ):
                continue
            if find_variable(datatree[names[j]].to_dataset(), role) is None:
                continue
            ds2 = get_sweep_dataset(j)
            name = find_variable(ds2, role)
            if name is None:
                continue
            src = polar_array(ds2[name])
            az2 = np.asarray(ds2["azimuth"].values, dtype=np.float64)
            rng2 = np.asarray(ds2["range"].values, dtype=np.float64)
            ray, gap = A.nearest_ray(az2, state.azimuth)
            order = np.argsort(rng2)
            pos = np.clip(np.searchsorted(rng2[order], state.range_m), 1, rng2.size - 1)
            left = np.abs(state.range_m - rng2[order][pos - 1]) <= np.abs(
                rng2[order][pos] - state.range_m
            )
            gate = order[np.where(left, pos - 1, pos)]
            out = src[np.ix_(ray, gate)].astype(np.float32)
            dr = float(np.median(np.diff(rng2))) if rng2.size > 1 else 250.0
            out[:, (state.range_m < rng2.min() - dr) | (state.range_m > rng2.max() + dr)] = np.nan
            out[gap > 1.5 * max(A.U.azimuth_spacing_deg(az2), 1e-3), :] = np.nan
            state.fields[role] = out
            state.borrowed[role] = j
            notes.append(f"{role} taken from sweep {j} (same elevation, split cut)")
            break
    return notes


def _wrap(state: WorkingState) -> bool:
    return bool(G.covers_full_circle(state.azimuth))


def _percent(n: int, base: int) -> str:
    return f"{100.0 * n / base:.1f} %" if base else "n/a"


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


@step(
    id="snr_gate",
    title="SNR gate",
    category="Noise",
    description="Remove gates whose signal-to-noise ratio is below the minimum (needs SNR).",
    optional=["SNR"],
    params=[param("min_snr", "number", 3.0, "Minimum SNR (dB)", min=-10, max=40, step=0.5)],
    script="""
        snr = fields.get("SNR")
        if snr is not None:
            keep &= ~(np.isfinite(snr) & (snr < {min_snr}))
    """,
)
def snr_gate(state: WorkingState, min_snr: float) -> StepOutcome:
    snr = state.fields.get("SNR")
    if snr is None:
        raise StepSkippedError("no SNR field on this sweep")
    removed = state.restrict(~(np.isfinite(snr) & (snr < min_snr)))
    return StepOutcome(notes=[f"{removed} gates below {min_snr:g} dB"])


@step(
    id="rhohv_gate",
    title="RhoHV gate",
    category="Meteo echo",
    description=(
        "Remove gates with a low copolar correlation coefficient (non-meteorological echo, "
        "noise). Needs RhoHV."
    ),
    optional=["RHOHV"],
    params=[param("min_rhohv", "number", 0.8, "Minimum RhoHV", min=0.0, max=1.0, step=0.01)],
    script="""
        rho = fields.get("RHOHV")
        if rho is not None:
            keep &= ~(np.isfinite(rho) & (rho < {min_rhohv}))
    """,
)
def rhohv_gate(state: WorkingState, min_rhohv: float) -> StepOutcome:
    rho = state.fields.get("RHOHV")
    if rho is None:
        raise StepSkippedError("no RhoHV field on this sweep")
    removed = state.restrict(~(np.isfinite(rho) & (rho < min_rhohv)))
    return StepOutcome(notes=[f"{removed} gates with RhoHV < {min_rhohv:g}"])


@step(
    id="echo_mask",
    title="Meteorological echo mask",
    category="Meteo echo",
    description=(
        "Remove non-meteorological echo (clutter, insects, noise) with radarx's fuzzy-logic echo "
        "mask (RhoHV, ZDR and the texture of ZDR / PhiDP / reflectivity). Without radarx a simple "
        "RhoHV / ZDR / texture rule is used."
    ),
    optional=["DBZH", "ZDR", "RHOHV", "PHIDP", "VRADH", "SNR"],
    packages=["radarx"],
    params=[
        param(
            "method",
            "select",
            "radarx",
            "Method",
            options=["radarx", "krause2016", "gourley2007"],
            help="radarx = radarx memberships; krause2016 / gourley2007 = published schemes.",
        ),
        param(
            "threshold",
            "number",
            0.0,
            "Score threshold (0 = default)",
            min=0,
            max=1,
            step=0.05,
            help="Higher removes more. Defaults: 0.6 (radarx), 0.8 (krause2016).",
        ),
        param("remove_speckle", "bool", True, "Also remove speckle (radarx min size)"),
        param("min_snr", "number", 3.0, "Min SNR when an SNR field exists (dB)", min=-10, max=30),
    ],
    kernels=[neighbour_sums, reflectivity_texture, simple_meteo_mask, G.covers_full_circle],
    script="""
        wrap = covers_full_circle(azimuth)
        if rt is not None and "DBZH" in fields:
            cls = rt.echo_mask(
                _rt_dataset(ds, fields, keep), method={method}
            )["ECHO_CLASS"].values
            keep &= ~((cls == 2) | ((cls == 3) & {remove_speckle}))
        elif "DBZH" in fields:
            keep &= simple_meteo_mask(
                fields["DBZH"], fields.get("RHOHV"), fields.get("ZDR"), keep, wrap
            )
    """,
)
def echo_mask(
    state: WorkingState, method: str, threshold: float, remove_speckle: bool, min_snr: float
) -> StepOutcome:
    if "DBZH" not in state.fields:
        raise StepSkippedError("no reflectivity field on this sweep")
    wrap = _wrap(state)
    if not _have_radarx():
        keep = simple_meteo_mask(
            state.masked("DBZH"),
            state.masked("RHOHV") if state.has("RHOHV") else None,
            state.masked("ZDR") if state.has("ZDR") else None,
            state.mask,
            wrap,
        )
        removed = state.restrict(keep)
        return StepOutcome(notes=[f"radarx not installed: simple rule used ({removed} gates)"])
    rt = A.rx()
    ds = _radarx_dataset(state)
    kwargs: dict[str, Any] = {"method": method}
    if method == "radarx":
        kwargs["min_snr"] = float(min_snr)
        if "SNR" in state.fields:
            kwargs["snr"] = "SNR"
    if threshold > 0 and method != "gourley2007":
        kwargs["threshold"] = float(threshold)
    with A.captured_warnings() as warns, A.guard("Echo classification"):
        out = rt.echo_mask(ds, **kwargs)
    cls = A.polar_array(out["ECHO_CLASS"], ds, "ECHO_CLASS")
    remove = cls == 2
    if remove_speckle:
        remove |= cls == 3
    removed = state.restrict(~remove)
    return StepOutcome(notes=[f"{method}: {removed} gates", *warns[:2]])


@step(
    id="clutter_filter",
    title="Ground clutter filter",
    category="Meteo echo",
    description=(
        "Remove ground clutter from the texture of reflectivity (TDBZ) and its SPIN, as in "
        "Hubbert et al. (2009) / Steiner and Smith (2002). With a velocity field only near-zero "
        "velocities are removed (clutter does not move). 'fuzzy' uses radarx's Gourley (2007) "
        "classifier instead."
    ),
    optional=["DBZH", "VRADH", "RHOHV"],
    packages=["radarx"],
    params=[
        param(
            "method",
            "select",
            "texture",
            "Method",
            options=["texture", "fuzzy"],
            help="texture = TDBZ / SPIN limits; fuzzy = radarx gourley2007 (also clear air).",
        ),
        param("tdbz_max", "number", 50.0, "Max texture TDBZ (dBZ^2)", min=1, max=500, step=1),
        param("spin_max", "number", 0.4, "Max SPIN (0-1)", min=0.05, max=1.0, step=0.05),
        param(
            "rule",
            "select",
            "both",
            "Clutter if texture and SPIN are",
            options=["both", "either"],
            help="'both' above their limits (conservative) or 'either' (aggressive).",
        ),
        param(
            "stationary_only",
            "bool",
            True,
            "Only near-zero velocity",
            help="When a velocity field exists, only remove gates with |V| below the limit.",
        ),
        param("max_abs_velocity", "number", 2.0, "Near-zero limit (m/s)", min=0, max=20, step=0.5),
        param(
            "max_range_km",
            "number",
            0.0,
            "Only within range (km, 0 = everywhere)",
            min=0,
            max=500,
            step=5,
        ),
    ],
    kernels=[neighbour_sums, reflectivity_texture, reflectivity_spin, G.covers_full_circle],
    script="""
        wrap = covers_full_circle(azimuth)
        z = fields.get("DBZH")
        if z is not None:
            zv = np.where(keep, z, np.nan)
            ok = np.isfinite(zv)
            hi_tex = reflectivity_texture(zv, ok, wrap) > {tdbz_max}
            hi_spin = reflectivity_spin(zv, ok, wrap) > {spin_max}
            clutter = (hi_tex & hi_spin) if {rule} == "both" else (hi_tex | hi_spin)
            vel = fields.get("VRADH")
            if {stationary_only} and vel is not None:
                clutter &= ~(np.isfinite(vel) & (np.abs(vel) > {max_abs_velocity}))
            if {max_range_km} > 0:
                clutter &= (range_m[None, :] <= {max_range_km} * 1000.0)
            keep &= ~np.nan_to_num(clutter, nan=0).astype(bool)
    """,
)
def clutter_filter(
    state: WorkingState,
    method: str,
    tdbz_max: float,
    spin_max: float,
    rule: str,
    stationary_only: bool,
    max_abs_velocity: float,
    max_range_km: float,
) -> StepOutcome:
    if "DBZH" not in state.fields:
        raise StepSkippedError("no reflectivity field to detect clutter on this sweep")
    wrap = _wrap(state)
    if method == "fuzzy":
        if not _have_radarx():
            raise StepSkippedError("radarx is not installed (use method 'texture')")
        rt = A.rx()
        ds = _radarx_dataset(state)
        with A.captured_warnings(), A.guard("Clutter classification"):
            out = rt.echo_mask(ds, method="gourley2007")
        clutter = A.polar_array(out["ECHO_CLASS"], ds, "ECHO_CLASS") == 2
    else:
        z = state.masked("DBZH")
        ok = np.isfinite(z)
        tdbz = reflectivity_texture(z, ok, wrap)
        spin = reflectivity_spin(z, ok, wrap)
        with np.errstate(invalid="ignore"):
            hi_tex, hi_spin = tdbz > tdbz_max, spin > spin_max
            clutter = (hi_tex & hi_spin) if rule == "both" else (hi_tex | hi_spin)
    if stationary_only and state.has("VRADH"):
        vel = state.fields["VRADH"]
        clutter &= ~(np.isfinite(vel) & (np.abs(vel) > max_abs_velocity))
    if max_range_km > 0:
        clutter &= state.range_m[None, :] <= max_range_km * 1000.0
    removed = state.restrict(~clutter)
    return StepOutcome(notes=[f"{removed} clutter gates ({method})"])


@step(
    id="biological_filter",
    title="Biological echo filter",
    category="Meteo echo",
    description=(
        "Remove birds / insects: weak echo (below the reflectivity limit) with a low RhoHV, a "
        "high ZDR or a wide Doppler spectrum. Rule based, uses whichever of RhoHV / ZDR / spectrum "
        "width exist. (radarx's MistNet network works on a whole volume and is not used here.)"
    ),
    optional=["DBZH", "RHOHV", "ZDR", "WRADH"],
    params=[
        param("max_dbz", "number", 25.0, "Only echo weaker than (dBZ)", min=-20, max=60, step=1),
        param("max_rhohv", "number", 0.8, "RhoHV below", min=0.3, max=1.0, step=0.01),
        param("min_zdr", "number", 3.0, "ZDR above (dB)", min=0, max=10, step=0.5),
        param(
            "min_width",
            "number",
            0.0,
            "Spectrum width above (m/s, 0 = off)",
            min=0,
            max=20,
            step=0.5,
        ),
        param("min_votes", "int", 1, "Signatures needed", min=1, max=3, step=1),
    ],
    script="""
        z = fields.get("DBZH")
        if z is not None:
            votes = np.zeros(z.shape, dtype=int)
            used = 0
            for role, test in (
                ("RHOHV", lambda a: a < {max_rhohv}),
                ("ZDR", lambda a: a > {min_zdr}),
                ("WRADH", lambda a: a > {min_width}),
            ):
                a = fields.get(role)
                if a is not None and (role != "WRADH" or {min_width} > 0):
                    votes += (np.isfinite(a) & test(a)).astype(int)
                    used += 1
            if used:
                keep &= ~(np.isfinite(z) & (z <= {max_dbz}) & (votes >= {min_votes}))
    """,
)
def biological_filter(
    state: WorkingState,
    max_dbz: float,
    max_rhohv: float,
    min_zdr: float,
    min_width: float,
    min_votes: int,
) -> StepOutcome:
    if "DBZH" not in state.fields:
        raise StepSkippedError("no reflectivity field to judge biological echo on this sweep")
    z = state.fields["DBZH"]
    votes = np.zeros(z.shape, dtype=np.int16)
    used: list[str] = []
    tests = (
        ("RHOHV", lambda a: a < max_rhohv, True),
        ("ZDR", lambda a: a > min_zdr, True),
        ("WRADH", lambda a: a > min_width, min_width > 0),
    )
    for role, test, enabled in tests:
        arr = state.fields.get(role)
        if arr is None or not enabled:
            continue
        with np.errstate(invalid="ignore"):
            votes += (np.isfinite(arr) & test(arr)).astype(np.int16)
        used.append(role)
    if not used:
        raise StepSkippedError("needs RhoHV, ZDR or spectrum width (none on this sweep)")
    with np.errstate(invalid="ignore"):
        bio = np.isfinite(z) & (z <= max_dbz) & (votes >= min(min_votes, len(used)))
    removed = state.restrict(~bio)
    return StepOutcome(notes=[f"{removed} biological gates (signatures: {', '.join(used)})"])


@step(
    id="speckle",
    title="Remove speckle",
    category="Cleanup",
    description="Remove small isolated regions of valid gates (8-connected, also across 0/360).",
    params=[param("min_size", "int", 10, "Minimum region size (gates)", min=2, max=2000, step=1)],
    kernels=[remove_small_regions, G.covers_full_circle],
    script="""
        wrap = covers_full_circle(azimuth)
        keep &= remove_small_regions(keep & np.isfinite(fields[role]), {min_size}, wrap)
    """,
)
def speckle(state: WorkingState, min_size: int) -> StepOutcome:
    keep = remove_small_regions(state.valid(), int(min_size), _wrap(state))
    removed = state.restrict(keep)
    return StepOutcome(notes=[f"{removed} gates in regions smaller than {min_size}"])


@step(
    id="despike",
    title="Despike",
    category="Cleanup",
    description=(
        "Remove single gates that differ strongly from the mean of their neighbours. The limit is "
        "set in the unit of the target, or (0) automatically from the scatter of the data."
    ),
    params=[
        param(
            "threshold", "number", 0.0, "Difference limit (target units, 0 = auto)", min=0, max=1000
        ),
        param("n_sigma", "number", 5.0, "Auto limit: robust sigmas", min=1, max=20, step=0.5),
        param("window", "select", 3, "Window (gates)", options=[3, 5]),
    ],
    kernels=[neighbour_sums, spikes, G.covers_full_circle],
    script="""
        wrap = covers_full_circle(azimuth)
        vals = np.where(keep, fields[role], np.nan)
        keep &= ~spikes(vals, np.isfinite(vals), ({window}, {window}), wrap, {threshold}, {n_sigma})
    """,
)
def despike(state: WorkingState, threshold: float, n_sigma: float, window: int) -> StepOutcome:
    vals = state.masked("target")
    win = int(window)
    bad = spikes(vals, np.isfinite(vals), (win, win), _wrap(state), threshold, n_sigma)
    removed = state.restrict(~bad)
    return StepOutcome(notes=[f"{removed} spikes"])


@step(
    id="range_clip",
    title="Value / range limits",
    category="Cleanup",
    description=(
        "Remove gates outside a value interval of the target and/or outside a range window. "
        "Use -1000 / 1000 (value) and 0 (max range) to leave a limit off."
    ),
    params=[
        param("min_value", "number", -1000.0, "Minimum value (-1000 = off)", min=-1000, max=1000),
        param("max_value", "number", 1000.0, "Maximum value (1000 = off)", min=-1000, max=1000),
        param("min_range_km", "number", 0.0, "Minimum range (km)", min=0, max=1000),
        param("max_range_km", "number", 0.0, "Maximum range (km, 0 = off)", min=0, max=1000),
    ],
    script="""
        vals = fields[role]
        keep &= ~(np.isfinite(vals) & ((vals < {min_value}) | (vals > {max_value})))
        keep &= range_m[None, :] >= {min_range_km} * 1000.0
        if {max_range_km} > 0:
            keep &= range_m[None, :] <= {max_range_km} * 1000.0
    """,
)
def range_clip(
    state: WorkingState,
    min_value: float,
    max_value: float,
    min_range_km: float,
    max_range_km: float,
) -> StepOutcome:
    vals = state.values
    with np.errstate(invalid="ignore"):
        keep = ~(np.isfinite(vals) & ((vals < min_value) | (vals > max_value)))
    keep &= state.range_m[None, :] >= min_range_km * 1000.0
    if max_range_km > 0:
        keep &= state.range_m[None, :] <= max_range_km * 1000.0
    removed = state.restrict(keep)
    return StepOutcome(notes=[f"{removed} gates outside the limits"])


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------


@step(
    id="dealias",
    title="Dealias velocity",
    category="Velocity",
    kind="transform",
    description=(
        "Unfold Doppler velocity beyond the Nyquist interval with radarx (region growing or "
        "UNRAVEL), or a built-in radial / azimuthal unfolding. Acts on the velocity field using "
        "only the gates kept so far, so put clutter and noise filters before it."
    ),
    needs=["VRADH"],
    transforms="VRADH",
    packages=["radarx"],
    params=[
        param(
            "method",
            "select",
            "region",
            "Method",
            options=["region", "unravel", "builtin"],
            help="region = radarx region growing; unravel = radarx (slower); builtin = numpy only.",
        ),
        param(
            "nyquist_velocity",
            "number",
            0.0,
            "Nyquist velocity (m/s, 0 = auto)",
            min=0,
            max=100,
            step=0.5,
            help="0 = from the file, else estimated from the largest valid |velocity|.",
        ),
        param("threshold", "number", 0.3, "Region threshold (x Vn)", min=0.05, max=0.9, step=0.05),
        param("max_speed", "number", 0.0, "Max plausible speed (m/s, 0 = off)", min=0, max=150),
        param("wind_speed", "number", 0.0, "Reference wind speed (m/s, 0 = none)", min=0, max=100),
        param("wind_from", "number", 270.0, "Reference wind from (deg)", min=0, max=360, step=5),
    ],
    kernels=[unfold_velocity, G.covers_full_circle],
    script="""
        nyq = {nyquist_velocity} or nyquist_of(ds, fields["VRADH"])
        vel = np.where(keep, fields["VRADH"], np.nan)
        if rt is not None and {method} != "builtin":
            work = _rt_dataset(ds, {"VRADH": fields["VRADH"]}, keep)
            kwargs = {"threshold": {threshold}} if {method} == "region" else {}
            res = rt.dealias_velocity(work, "VRADH", nyq, method={method}, **kwargs)
            out = res.transpose("azimuth", "range").values.astype("float32")
        else:
            out = unfold_velocity(vel, nyq, covers_full_circle(azimuth))
        fields["VRADH"] = np.where(np.isfinite(out), out, fields["VRADH"])
    """,
)
def dealias(
    state: WorkingState,
    method: str,
    nyquist_velocity: float,
    threshold: float,
    max_speed: float,
    wind_speed: float,
    wind_from: float,
) -> StepOutcome:
    raw = state.fields["VRADH"]
    # Nyquist from the user / the file / the largest raw |V| (estimated on the unmasked sweep).
    nyq, source = A.nyquist_velocity(A.prepare_sweep(state.ctx), "VRADH", nyquist_velocity)
    masked = state.masked("VRADH")
    if method != "builtin" and _have_radarx():
        rt = A.rx()
        ds = _radarx_dataset(state, roles=("VRADH",))
        wind = A.uniform_wind_profile(wind_speed, wind_from) if method == "region" else None
        with A.captured_warnings():
            da = A.dealias(
                rt,
                ds,
                "VRADH",
                nyq,
                threshold=threshold,
                max_speed=max_speed,
                method=method,
                wind_profile=wind,
            )
        out = A.polar_array(da, ds, "dealiased velocity")
        used = f"radarx {method}"
    else:
        out = unfold_velocity(masked, nyq, _wrap(state))
        used = "built-in" if method == "builtin" else "built-in (radarx not installed)"
    good = np.isfinite(out) & np.isfinite(masked)
    unfolded = good & (np.abs(out - masked) > nyq)
    state.fields["VRADH"] = np.where(np.isfinite(out), out, raw).astype(np.float32)
    return StepOutcome(
        modified=int(unfolded.sum()),
        notes=[f"{used}, Vn = {nyq:g} m/s ({source}); {int(unfolded.sum())} gates unfolded"],
        metrics={"nyquist": nyq},
    )


@step(
    id="smooth",
    title="Smooth",
    category="Cleanup",
    kind="transform",
    transforms="target",
    description=(
        "Light smoothing of the kept gates of the target: a median filter (robust, keeps edges) "
        "or a Gaussian. Removed gates stay removed and do not leak into the result."
    ),
    params=[
        param("method", "select", "median", "Method", options=["median", "gaussian"]),
        param("window", "int", 3, "Median window (gates, odd)", min=3, max=9, step=2),
        param("radius_km", "number", 0.3, "Gaussian radius (km)", min=0.1, max=5, step=0.1),
    ],
    kernels=[
        nan_median_filter,
        G.covers_full_circle,
        G.window_sigmas,
        G.normalized_gaussian,
        G.smooth_clean,
    ],
    script="""
        wrap = covers_full_circle(azimuth)
        ok = keep & np.isfinite(fields[role])
        if {method} == "median":
            sm = nan_median_filter(fields[role], ok, ({window}, {window}), wrap)
        else:
            r_mid = float(np.median(np.broadcast_to(range_m, ok.shape)[ok])) if ok.any() else 1000.0
            sigma = window_sigmas(range_m, azimuth, {radius_km}, r_mid)
            sm = smooth_clean(fields[role], ok, sigma=sigma, wrap_azimuth=wrap)
        fields[role] = np.where(ok, sm, fields[role])
    """,
)
def smooth(state: WorkingState, method: str, window: int, radius_km: float) -> StepOutcome:
    ok = state.valid()
    if not ok.any():
        raise StepSkippedError("no valid gates to smooth")
    values = state.values
    wrap = _wrap(state)
    if method == "median":
        sm = nan_median_filter(values, ok, (int(window), int(window)), wrap)
    else:
        r_mid = float(np.median(np.broadcast_to(state.range_m, ok.shape)[ok]))
        sigma = G.window_sigmas(state.range_m, state.azimuth, radius_km, r_mid)
        sm = G.smooth_clean(values, ok, sigma=sigma, wrap_azimuth=wrap)
    new = np.where(ok & np.isfinite(sm), sm, values).astype(np.float32)
    changed = int((ok & (np.abs(new - values) > 1e-6)).sum())
    state.fields[state.target] = new
    return StepOutcome(modified=changed, notes=[f"{method} smoothing changed {changed} gates"])


@step(
    id="gap_fill",
    title="Fill small gaps",
    category="Cleanup",
    kind="transform",
    transforms="target",
    description=(
        "Fill gates removed by earlier steps that are well surrounded by kept data (smooth "
        "distance-weighted interpolation). Large empty regions stay empty. Filled values are "
        "estimates; avoid for velocity unless it has been de-aliased."
    ),
    params=[
        param("radius_km", "number", 0.6, "Fill radius (km)", min=0.1, max=10, step=0.1),
        param(
            "min_coverage",
            "number",
            0.5,
            "Min kept share around a gap",
            min=0.1,
            max=1.0,
            step=0.05,
        ),
    ],
    kernels=[G.covers_full_circle, G.window_sigmas, G.normalized_gaussian, G.fill_gaps],
    script="""
        wrap = covers_full_circle(azimuth)
        have = keep & np.isfinite(fields[role])
        gaps = ~keep & np.isfinite(fields[role])
        if have.any() and gaps.any():
            r_mid = float(np.median(np.broadcast_to(range_m, have.shape)[have]))
            sigma = window_sigmas(range_m, azimuth, {radius_km}, r_mid)
            filled, ok = fill_gaps(
                fields[role], have, gaps, sigma=sigma, min_coverage={min_coverage},
                wrap_azimuth=wrap,
            )
            fields[role] = np.where(ok, filled, fields[role])
            keep |= ok
    """,
)
def gap_fill(state: WorkingState, radius_km: float, min_coverage: float) -> StepOutcome:
    have = state.valid()
    values = state.values
    gaps = ~state.mask & np.isfinite(values)
    if not have.any() or not gaps.any():
        return StepOutcome(notes=["nothing to fill"])
    r_mid = float(np.median(np.broadcast_to(state.range_m, have.shape)[have]))
    sigma = G.window_sigmas(state.range_m, state.azimuth, radius_km, r_mid)
    filled, ok = G.fill_gaps(
        values, have, gaps, sigma=sigma, min_coverage=min_coverage, wrap_azimuth=_wrap(state)
    )
    new = np.where(ok, filled, values).astype(np.float32)
    state.fields[state.target] = new
    state.mask |= ok
    n = int(ok.sum())
    return StepOutcome(filled=n, notes=[f"{n} gates filled"])


@step(
    id="noise_correction",
    title="Noise correction (ZDR / RhoHV)",
    category="Noise",
    kind="transform",
    transforms="target",
    description=(
        "Remove the noise bias from ZDR or RhoHV (Gourley et al. 2006) with radarx. Only valid for "
        "moments that still contain the noise (not already corrected). Needs an SNR field and a "
        "ZDR or RhoHV target."
    ),
    optional=["SNR", "ZDR"],
    packages=["radarx"],
    params=[param("min_snr", "number", 3.0, "Minimum SNR (dB)", min=-10, max=30, step=0.5)],
    script="""
        snr = fields.get("SNR")
        if rt is not None and snr is not None and role in ("ZDR", "RHOHV"):
            work = _rt_dataset(ds, fields, keep)
            if role == "ZDR":
                res = rt.correct_zdr_noise(work, zdr="ZDR", snr="SNR", min_snr={min_snr})
            else:
                res = rt.correct_rhohv_noise(
                    work, rhohv="RHOHV", snr="SNR", zdr="ZDR" in fields, min_snr={min_snr}
                )
            out = res.transpose("azimuth", "range").values.astype("float32")
            fields[role] = np.where(np.isfinite(out), out, fields[role])
    """,
)
def noise_correction(state: WorkingState, min_snr: float) -> StepOutcome:
    role = state.target
    if role not in ("ZDR", "RHOHV"):
        raise StepSkippedError(f"only applies to a ZDR or RhoHV target (target is {role})")
    if "SNR" not in state.fields:
        raise StepSkippedError("no SNR field on this sweep")
    if not _have_radarx():
        raise StepSkippedError("needs the radarx package")
    rt = A.rx()
    ds = _radarx_dataset(state)
    with A.captured_warnings() as warns, A.guard("Noise correction"):
        if role == "ZDR":
            res = rt.correct_zdr_noise(ds, zdr="ZDR", snr="SNR", min_snr=float(min_snr))
        else:
            res = rt.correct_rhohv_noise(
                ds,
                rhohv="RHOHV",
                snr="SNR",
                zdr=bool("ZDR" in state.fields),
                min_snr=float(min_snr),
            )
    out = A.polar_array(res, ds, role)
    old = state.values
    new = np.where(np.isfinite(out), out, old).astype(np.float32)
    changed = int((state.valid() & (np.abs(new - old) > 1e-6)).sum())
    state.fields[role] = new
    return StepOutcome(modified=changed, notes=[f"{changed} gates corrected", *warns[:1]])

"""Data-quality check of an opened radar volume ("smart open").

``check_tree`` (or :func:`check_reader` / :func:`check_volume`) looks at what is already in memory
and returns a report: a list of **findings** (what is in the file, what looks wrong, where, with
numbers), a **recommended QC recipe** built only from the existing pipeline steps, and a sensible
first view. Nothing is applied: the recipe is a suggestion the user runs with one click.

Every finding carries

* ``severity``: ``info`` (a fact), ``notice`` (worth knowing) or ``warning`` (act before analysing);
* ``evidence``: numbers, each labelled ``RETRIEVED`` (counted or measured directly in the data) or
  ``ESTIMATE`` (a model- or heuristic-based value; the method is in ``method``);
* ``where``: sweeps, azimuth interval and range interval of the problem;
* ``confidence`` (0-1, how sure the detector is, not how bad the problem is);
* ``method``: the technique with its literature reference.

Design limits, kept on purpose: single volume only (no persistence over time), no external data
(no terrain model, no sounding), no network. Cost is bounded: one sweep is held in memory at a
time and a wall-clock budget skips the optional heavy checks of the later sweeps (and says so).
See ``docs/DATA_CHECK.md`` for every check, its thresholds and its limits.
"""

# ruff: noqa: E501
# (the finding texts and method notes are prose; they read better unwrapped)
from __future__ import annotations

import contextlib
import logging
import math
import time
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import xarray as xr

from engine import varmap

logger = logging.getLogger(__name__)

REPORT_VERSION = 1
SEVERITIES = ("info", "notice", "warning")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# --- thresholds (documented in docs/DATA_CHECK.md; all are rules of thumb, not standards) --------
BUDGET_S = 4.0  # wall-clock budget; heavy per-sweep checks stop when it is used up
MAX_HEAVY_ELEVATION = 6.0  # deg: clutter, blockage, bio, ZDR, PHIDP, noise floor look at low cuts
MAX_CLUTTER_ELEVATION = 3.0
MAX_CLUTTER_RANGE_KM = (
    100.0  # beyond this a low beam is far above the ground: clutter is not searched
)
HEAVY_SWEEPS = 4  # at most this many sweeps per heavy check

COVERAGE_GAP_DEG = 10.0  # azimuth gap that counts as partial coverage
TIME_GAP_FACTOR = 5.0  # a ray interval above 5x the median ...
TIME_GAP_MIN_S = 1.0  # ... and above this many seconds is a gap

FOLD_NOTICE, FOLD_WARNING = 0.02, 0.10  # share of detected-signal gates carrying the folded code
CLUTTER_VEL = 1.0  # m/s: |V| below this and Z above CLUTTER_Z is "clutter-like"
CLUTTER_Z = 20.0
CLUTTER_CELL_FRACTION = 0.5
CLUTTER_WARNING_SHARE = 0.03  # share of echo gates inside clutter cells
BIO_MAX_DBZ, BIO_MAX_RHOHV, BIO_MIN_ZDR = (
    25.0,
    0.8,
    3.0,
)  # the pipeline's biological_filter defaults
BIO_NOTICE_SHARE = 0.15
SPECKLE_NOTICE_SHARE = 0.10
LOW_SNR_DB, LOW_SNR_NOTICE_SHARE = 3.0, 0.10
LOW_RHOHV, LOW_RHOHV_NOTICE_SHARE = 0.85, 0.10

ZDR_REFERENCE_DB = 0.3  # intrinsic ZDR of light rain at 20-25 dBZ (S-band), +- ZDR_REFERENCE_SIGMA
ZDR_REFERENCE_SIGMA = 0.1
ZDR_MIN_GATES, ZDR_MIN_BLOCKS, ZDR_MIN_RAYS = 3000, 300, 30
ZDR_Z_RANGE = (20.0, 25.0)
ZDR_RHOHV_MIN = 0.98
ZDR_MAX_HEIGHT_KM = 2.0  # stay well below a typical melting layer
ZDR_MAX_ELEVATION = 10.0
ZDR_RANGE_KM = (10.0, 110.0)

PHIDP_RHOHV_MIN = 0.9
PHIDP_NOISE_NOTICE, PHIDP_NOISE_WARNING = 8.0, 15.0  # deg
PHIDP_MIN_PAIRS = 2000
PHIDP_WRAP_NOTICE_SHARE = 0.01  # share of rays with a 360-degree wrap

# attenuation per degree of two-way PhiDP (dB/deg), by band
ATTEN_PER_DEG = {"C": 0.08, "X": 0.28}
ATTEN_NOTICE_DB, ATTEN_WARNING_DB = 3.0, 10.0

_ROLES = ("reflectivity", "velocity", "width", "zdr", "rhohv", "phidp", "snr")
_NAMES = {
    "reflectivity": "reflectivity",
    "velocity": "Doppler velocity",
    "width": "spectrum width",
    "zdr": "ZDR",
    "rhohv": "RhoHV",
    "phidp": "PhiDP",
    "snr": "SNR",
}
_LEGACY = {r: varmap.ROLES[r].legacy for r in varmap.ROLES}

EARTH_EFFECTIVE_RADIUS_M = 6_371_000.0 * 4.0 / 3.0


# ---------------------------------------------------------------------------
# Report objects
# ---------------------------------------------------------------------------


def ev(
    label: str, value: Any, unit: str = "", kind: str = "RETRIEVED", note: str | None = None
) -> dict[str, Any]:
    """One piece of evidence: a labelled number (RETRIEVED = measured, ESTIMATE = model based)."""
    if isinstance(value, (np.floating, float)):
        value = float(value) if math.isfinite(float(value)) else None
    elif isinstance(value, (np.integer,)):
        value = int(value)
    out: dict[str, Any] = {"label": label, "value": value, "unit": unit, "kind": kind}
    if note:
        out["note"] = note
    return out


@dataclass
class Finding:
    id: str
    title: str
    severity: str
    summary: str
    scope: str = "sweep"  # "file" or "sweep"
    sweeps: list[int] = field(default_factory=list)
    where: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.5
    method: str = ""
    per_sweep: list[dict[str, Any]] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)  # pipeline step ids this finding motivates
    targets: list[str] = field(default_factory=list)  # roles the steps apply to
    advice: str | None = None  # a next step that is not a pipeline step

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "severity": self.severity,
            "summary": self.summary,
            "scope": self.scope,
            "sweeps": list(self.sweeps),
            "where": self.where,
            "evidence": self.evidence,
            "confidence": round(float(self.confidence), 2),
            "method": self.method,
            "per_sweep": self.per_sweep,
            "steps": list(self.steps),
            "advice": self.advice,
        }


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------


def _wrap180(x: np.ndarray) -> np.ndarray:
    return (x + 180.0) % 360.0 - 180.0


def _mad_sigma(x: np.ndarray) -> float:
    """Robust standard deviation 1.4826 * median absolute deviation."""
    if x.size == 0:
        return float("nan")
    return float(1.4826 * np.median(np.abs(x - np.median(x))))


def circular_arc(angles: np.ndarray, coverage: float = 0.9) -> tuple[float, float]:
    """Shortest arc (start, end, degrees in [0, 360)) holding ``coverage`` of the angles."""
    a = np.sort(np.mod(np.asarray(angles, dtype=np.float64), 360.0))
    a = a[np.isfinite(a)]
    n = a.size
    if n == 0:
        return (0.0, 0.0)
    if n > 60000:
        a = a[:: math.ceil(n / 60000)]
        n = a.size
    k = max(1, math.ceil(coverage * n))
    if k >= n:
        gaps = np.diff(np.concatenate([a, [a[0] + 360.0]]))
        i = int(np.argmax(gaps))
        return (float(a[(i + 1) % n]), float(a[i]))
    ext = (np.roll(a, -(k - 1)) - a) % 360.0
    i = int(np.argmin(ext))
    return (float(a[i]), float(a[(i + k - 1) % n]))


def where_of(
    mask: np.ndarray,
    az: np.ndarray | None,
    rng_m: np.ndarray,
    coverage: float = 0.9,
) -> dict[str, Any] | None:
    """Azimuth arc and range interval (km) holding ``coverage`` of the flagged gates."""
    ia, ir = np.nonzero(mask)
    if ia.size == 0:
        return None
    r = rng_m[ir] / 1000.0
    lo, hi = np.percentile(r, [100 * (1 - coverage) / 2, 100 - 100 * (1 - coverage) / 2])
    out: dict[str, Any] = {"range_km": [round(float(lo), 1), round(float(hi), 1)]}
    if az is not None:
        a0, a1 = circular_arc(az[ia], coverage)
        width = (a1 - a0) % 360.0
        out["azimuth_deg"] = [0.0, 360.0] if width > 300.0 else [round(a0, 1), round(a1, 1)]
    return out


def _pct(x: float) -> float:
    return round(100.0 * float(x), 2)


def _beam_height_km(rng_m: np.ndarray, elev_deg: float) -> np.ndarray:
    """Beam centre height above the radar (km), standard 4/3 effective-Earth-radius model."""
    re = EARTH_EFFECTIVE_RADIUS_M
    s = math.sin(math.radians(elev_deg))
    return (np.sqrt(rng_m**2 + re**2 + 2.0 * rng_m * re * s) - re) / 1000.0


def _seconds(t: xr.DataArray) -> np.ndarray | None:
    """Ray times as float seconds (NaN for missing); ``None`` if they cannot be interpreted."""
    try:
        v = np.asarray(t.values)
        if np.issubdtype(v.dtype, np.datetime64):
            ns = v.astype("datetime64[ns]").astype("int64").astype(np.float64)
            ns[np.isnat(v)] = np.nan
            return ns / 1e9
        if np.issubdtype(v.dtype, np.timedelta64):
            return v.astype("timedelta64[ns]").astype("int64").astype(np.float64) / 1e9
        out = v.astype(np.float64)
        return out if out.ndim == 1 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# One sweep, loaded lazily and released after use
# ---------------------------------------------------------------------------


class _Sweep:
    """Fields of one sweep (masked float32 ``(rays, gates)``), loaded on first use."""

    def __init__(self, index: int, ds: xr.Dataset, overrides: Mapping[str, str] | None) -> None:
        from engine import rhi

        self.index = index
        self.ds = ds
        self.overrides = overrides
        self.elev = varmap.sweep_fixed_angle(ds)
        self.kind = "ppi"
        with contextlib.suppress(Exception):  # scan-type detection must never break the check
            self.kind = "rhi" if rhi.is_rhi(ds) else "ppi"
        self.names: dict[str, str] = {}
        for role in _ROLES:
            m = varmap.resolve(ds, role, overrides=overrides)
            if m is not None and ds[m.variable].ndim >= 2:
                self.names[role] = m.variable
        self.rng = (
            np.asarray(ds["range"].values, dtype=np.float64) if "range" in ds.coords else None
        )
        self.ray_dim: str | None = None
        for name in self.names.values():
            dims = [d for d in ds[name].dims if d != "range"]
            if dims:
                self.ray_dim = dims[0]
                break
        if self.ray_dim is None:
            for d in ds.dims:
                if d != "range":
                    self.ray_dim = str(d)
                    break
        self.n_rays = int(ds.sizes.get(self.ray_dim, 0)) if self.ray_dim else 0
        self.n_gates = int(ds.sizes.get("range", 0))
        self.az = self._ray_coord("azimuth")
        self._cache: dict[str, np.ndarray | None] = {}
        self._fold: dict[str, np.ndarray | None] = {}
        self._encoded: dict[str, bool] = {}

    def _ray_coord(self, name: str) -> np.ndarray | None:
        if name not in self.ds.variables or self.ray_dim is None:
            return None
        da = self.ds[name]
        if da.dims != (self.ray_dim,):
            return None
        return np.asarray(da.values, dtype=np.float64)

    @property
    def time_s(self) -> np.ndarray | None:
        if "time" not in self.ds.variables or self.ray_dim is None:
            return None
        da = self.ds["time"]
        return _seconds(da) if da.dims == (self.ray_dim,) else None

    def has(self, role: str) -> bool:
        return role in self.names

    def units(self, role: str) -> str:
        name = self.names.get(role)
        return str(self.ds[name].attrs.get("units", "")) if name else ""

    def field(self, role: str) -> np.ndarray | None:
        if role in self._cache:
            return self._cache[role]
        out = self._load(role)
        self._cache[role] = out
        return out

    def fold_mask(self, role: str) -> np.ndarray | None:
        """Gates carrying the Level-II style "range folded" code (needs the integer encoding)."""
        self.field(role)
        return self._fold.get(role)

    def _load(self, role: str) -> np.ndarray | None:
        from engine.reader import _no_echo_params

        name = self.names.get(role)
        if name is None:
            return None
        da = self.ds[name]
        extra = [d for d in da.dims if d not in (self.ray_dim, "range")]
        if extra:
            da = da.isel(dict.fromkeys(extra, 0))
        if self.ray_dim is None or "range" not in da.dims:
            return None
        da = da.transpose(self.ray_dim, "range")
        vals = np.asarray(da.values)
        params = _no_echo_params(da)
        self._encoded[role] = params is not None
        if params is not None:
            offset, scale = params
            tol = 0.25 * abs(scale)
            fold = np.abs(vals - (offset + scale)) <= tol
            nodata = np.abs(vals - offset) <= tol
            self._fold[role] = fold if fold.any() else np.zeros(0, dtype=bool)
            out = np.where(fold | nodata, np.float32(np.nan), vals).astype(np.float32)
            return out
        return np.array(vals, dtype=np.float32) if vals.dtype != np.float32 else vals

    def release(self) -> None:
        self._cache.clear()
        self._fold.clear()


# ---------------------------------------------------------------------------
# Band (frequency) of the radar
# ---------------------------------------------------------------------------


def radar_band(tree: xr.DataTree, ds: xr.Dataset | None, fmt: str | None) -> tuple[str | None, str]:
    """``("S"|"C"|"X"|None, how)`` from the file's frequency / wavelength, else the format."""
    freq = None
    holders = [tree.to_dataset()] + ([ds] if ds is not None else [])
    for h in holders:
        for key in ("radar_frequency", "frequency", "frequency_hz"):
            for src in (h.variables, h.attrs):
                if key in src:
                    try:
                        raw = src[key].values if hasattr(src[key], "values") else src[key]
                        val = float(np.nanmean(np.asarray(raw, dtype=float)))
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(val) and val > 0:
                        freq = val * (1e9 if val < 1e3 else 1e6 if val < 1e6 else 1.0)
                        break
            if freq:
                break
        if freq:
            break
        for key in ("radar_wavelength", "wavelength", "radar_wavelength_cm"):
            for src in (h.variables, h.attrs):
                if key in src:
                    try:
                        raw = src[key].values if hasattr(src[key], "values") else src[key]
                        w = float(np.nanmean(np.asarray(raw, dtype=float)))
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(w) and w > 0:
                        w_m = w / 100.0 if w > 1.0 else w
                        freq = 299_792_458.0 / w_m
                        break
            if freq:
                break
        if freq:
            break
    if freq:
        ghz = freq / 1e9
        band = "S" if 2 <= ghz < 4 else "C" if 4 <= ghz < 8 else "X" if 8 <= ghz < 12 else None
        return band, f"file frequency {ghz:.2f} GHz"
    if fmt and "nexrad" in fmt.lower():
        return "S", "assumed from the format (NEXRAD WSR-88D is S-band)"
    return None, "not stated in the file"


# ---------------------------------------------------------------------------
# Accumulator: raw per-sweep results, turned into findings after the loop
# ---------------------------------------------------------------------------


@dataclass
class _Acc:
    geometry: list[dict[str, Any]] = field(default_factory=list)
    timing: list[dict[str, Any]] = field(default_factory=list)
    velocity: list[dict[str, Any]] = field(default_factory=list)
    fold: list[dict[str, Any]] = field(default_factory=list)
    units: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    empty: list[dict[str, Any]] = field(default_factory=list)
    noise: list[dict[str, Any]] = field(default_factory=list)
    clutter: list[dict[str, Any]] = field(default_factory=list)
    blockage: list[dict[str, Any]] = field(default_factory=list)
    bio: list[dict[str, Any]] = field(default_factory=list)
    speckle: list[dict[str, Any]] = field(default_factory=list)
    snr: list[dict[str, Any]] = field(default_factory=list)
    rho: list[dict[str, Any]] = field(default_factory=list)
    zdr_blocks: list[np.ndarray] = field(default_factory=list)
    zdr_sweeps: list[dict[str, Any]] = field(default_factory=list)
    phidp: list[dict[str, Any]] = field(default_factory=list)
    atten: list[dict[str, Any]] = field(default_factory=list)
    roles: dict[str, list[int]] = field(default_factory=dict)
    not_checked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Per-sweep analyses
# ---------------------------------------------------------------------------


def _geometry(sw: _Sweep, acc: _Acc) -> None:
    info: dict[str, Any] = {
        "sweep": sw.index,
        "elevation": sw.elev,
        "kind": sw.kind,
        "n_rays": sw.n_rays,
        "n_gates": sw.n_gates,
    }
    if sw.kind == "ppi" and sw.az is not None and sw.n_rays >= 3:
        az = np.sort(np.mod(sw.az[np.isfinite(sw.az)], 360.0))
        if az.size >= 3:
            gaps = np.diff(np.concatenate([az, [az[0] + 360.0]]))
            i = int(np.argmax(gaps))
            spacing = float(np.median(gaps[gaps > 0])) if np.any(gaps > 0) else float("nan")
            info.update(
                largest_gap=float(gaps[i]),
                gap_from=float(az[i]),
                gap_to=float(az[(i + 1) % az.size]),
                spacing=spacing,
                coverage=float(360.0 - gaps[i] + spacing) if math.isfinite(spacing) else None,
            )
    acc.geometry.append(info)


def _timing(sw: _Sweep, acc: _Acc) -> None:
    t = sw.time_s
    if t is None or t.size < 3:
        return
    finite = np.isfinite(t)
    info: dict[str, Any] = {"sweep": sw.index, "n_rays": int(t.size), "n_nat": int((~finite).sum())}
    tt = t[finite]
    idx = np.nonzero(finite)[0]
    if tt.size >= 3:
        n_back = int((np.diff(tt) < 0).sum())
        info["n_backward_storage"] = n_back
        if n_back == 1:
            # rays stored from a fixed azimuth rather than from the start of the scan (as xradar
            # does for NEXRAD): one backward step, otherwise increasing -> a rotated, fine series
            k = int(np.nonzero(np.diff(tt) < 0)[0][0])
            rot = np.concatenate([tt[k + 1 :], tt[: k + 1]])
            if np.all(np.diff(rot) >= 0):
                info["stored_rotated"] = True
                n_back = 0
        if n_back > 0.05 * tt.size:
            info["stored_by_azimuth"] = (
                True  # sorted by azimuth, not by time: no fault, not flagged
            )
            n_back = 0
        else:
            info["n_backward"] = n_back
            if n_back:
                k = int(np.nonzero(np.diff(tt) < 0)[0][0])
                ray = int(idx[k + 1])
                info["first_backward_ray"] = ray
                if sw.az is not None:
                    info["backward_az"] = float(sw.az[ray] % 360)
        # everything else is judged on the time-sorted series, whatever the storage order
        order = np.argsort(tt, kind="stable")
        tt, idx = tt[order], idx[order]
        dt = np.diff(tt)
        pos = dt[dt > 0]
        med = float(np.median(pos)) if pos.size else float("nan")
        info.update(
            median_dt=med,
            n_duplicate=int((dt == 0).sum()),
            t_first=float(tt[0]),
            t_last=float(tt[-1]),
        )
        if math.isfinite(med):
            thr = max(TIME_GAP_FACTOR * med, TIME_GAP_MIN_S)
            gaps = np.nonzero(dt > thr)[0]
            if gaps.size:
                k = int(gaps[np.argmax(dt[gaps])])
                ray = int(idx[k])
                az_a = sw.az[ray] if sw.az is not None else None
                az_b = sw.az[int(idx[k + 1])] if sw.az is not None else None
                info.update(
                    n_gaps=int(gaps.size),
                    largest_gap_s=float(dt[k]),
                    gap_ray=ray,
                    gap_az=[
                        None if az_a is None else float(az_a % 360),
                        None if az_b is None else float(az_b % 360),
                    ],
                    lost_rays=float(dt[gaps].sum() / med),
                )
    acc.timing.append(info)


def _field_stats(sw: _Sweep, acc: _Acc) -> None:
    """Value ranges against the role's expectations, and fields that hold no valid gate."""
    for role in _ROLES:
        if not sw.has(role):
            continue
        a = sw.field(role)
        if a is None:
            continue
        fin = a[np.isfinite(a)]
        if fin.size == 0:
            acc.empty.append({"sweep": sw.index, "role": role, "variable": sw.names[role]})
            continue
        acc.roles.setdefault(role, []).append(sw.index)
        lo, p1, p99, hi = (
            np.percentile(fin, [0, 0.1, 99.9, 100])
            if fin.size > 50
            else (
                float(fin.min()),
                float(fin.min()),
                float(fin.max()),
                float(fin.max()),
            )
        )
        acc.units.setdefault(role, []).append(
            {
                "sweep": sw.index,
                "variable": sw.names[role],
                "units": sw.units(role),
                "min": float(lo),
                "max": float(hi),
                "p001": float(p1),
                "p999": float(p99),
                "n": int(fin.size),
            }
        )


def _range_folding(sw: _Sweep, acc: _Acc) -> None:
    for role in ("velocity", "reflectivity"):
        fold = sw.fold_mask(role)
        a = sw.field(role)
        if fold is None or a is None or fold.size == 0:
            continue
        n_fold = int(fold.sum())
        n_valid = int(np.isfinite(a).sum())
        n_sig = n_fold + n_valid
        if n_sig == 0:
            continue
        rec = {
            "sweep": sw.index,
            "role": role,
            "n_folded": n_fold,
            "n_valid": n_valid,
            "share_of_signal": n_fold / n_sig,
            "share_of_gates": n_fold / fold.size,
        }
        if n_fold and n_fold / n_sig >= FOLD_NOTICE:
            rec["where"] = where_of(fold, sw.az, sw.rng) if sw.rng is not None else None
        acc.fold.append(rec)


def _velocity(sw: _Sweep, acc: _Acc) -> None:
    from engine import multidoppler as md

    v = sw.field("velocity")
    if v is None:
        return
    n_valid = int(np.isfinite(v).sum())
    if n_valid < md.ALIAS_MIN_GATES:
        return
    nyq, src = md._sweep_nyquist_ms(sw.ds, v)
    if nyq is None:
        return
    jumps = md._jump_fraction(v, nyq)
    with np.errstate(invalid="ignore"):
        av = np.abs(v)
        max_abs = float(np.nanmax(av))
        edge = float((av >= 0.9 * nyq).sum() / n_valid)
    den = max(0, 2 * n_valid - v.shape[0] - v.shape[1])  # upper bound of valid pairs
    rec: dict[str, Any] = {
        "sweep": sw.index,
        "elevation": sw.elev,
        "nyquist": nyq,
        "source": src,
        "jump_fraction": jumps,
        "n_valid": n_valid,
        "max_abs": max_abs,
        "edge_fraction": edge,
        "approx_jumps": jumps * den,
    }
    if jumps >= 0.2 * md.ALIAS_JUMP_FRACTION and sw.rng is not None:
        m = np.zeros(v.shape, dtype=bool)
        with np.errstate(invalid="ignore"):
            for axis in (0, 1):
                d = np.abs(np.diff(v, axis=axis)) > 1.2 * nyq
                if axis == 0:
                    m[:-1] |= d
                    m[1:] |= d
                else:
                    m[:, :-1] |= d
                    m[:, 1:] |= d
        rec["where"] = where_of(m, sw.az, sw.rng)
        rec["n_jump_gates"] = int(m.sum())
    acc.velocity.append(rec)


def _noise_floor(sw: _Sweep, acc: _Acc) -> None:
    z = sw.field("reflectivity")
    if z is None or sw.rng is None or sw.n_gates < 50:
        return
    r_km = sw.rng / 1000.0
    ok = np.isfinite(z)
    if int(ok.sum()) < 5000:
        return
    edges = np.geomspace(max(r_km.min(), 10.0), r_km.max(), 17)
    centres, p2s = [], []
    idx = np.searchsorted(edges, r_km) - 1
    for b in range(len(edges) - 1):
        cols = np.nonzero(idx == b)[0]
        if cols.size == 0:
            continue
        vals = z[:, cols][ok[:, cols]]
        if vals.size < 200:
            continue
        centres.append(float(np.exp(np.mean(np.log(r_km[cols])))))
        p2s.append(float(np.percentile(vals, 2)))
    if len(centres) < 6:
        return
    c = np.array(centres)
    p = np.array(p2s)
    logr = np.log10(c)
    slope = float(np.polyfit(logr, p, 1)[0])
    z0 = float(np.median(p - 20.0 * logr))  # envelope at 1 km under the 20 log r law
    resid = p - (z0 + 20.0 * logr)
    acc.noise.append(
        {
            "sweep": sw.index,
            "elevation": sw.elev,
            "slope_db_per_decade": float(slope),
            "z1km": z0,
            "resid_mad": float(_mad_sigma(resid)),
            "n_bins": len(c),
            "r_min_km": float(c.min()),
            "r_max_km": float(c.max()),
            "mds50": z0 + 20.0 * math.log10(50.0),
            "mds100": z0 + 20.0 * math.log10(100.0),
        }
    )


def _clutter(sw: _Sweep, acc: _Acc) -> None:
    z, v = sw.field("reflectivity"), sw.field("velocity")
    if z is None or v is None or sw.az is None or sw.rng is None:
        return
    with np.errstate(invalid="ignore"):
        echo = np.isfinite(z) & np.isfinite(v)
        cl = echo & (z >= CLUTTER_Z) & (np.abs(v) <= CLUTTER_VEL)
    n_echo = int(np.isfinite(z).sum())
    if n_echo == 0:
        return
    n_cl = int(cl.sum())
    rec: dict[str, Any] = {
        "sweep": sw.index,
        "elevation": sw.elev,
        "n_echo": n_echo,
        "n_clutter_like": n_cl,
        "share_of_echo": n_cl / n_echo,
    }
    # persistence: clutter is anchored to the ground, so the same gates (a ring, or fixed patches)
    # stay clutter-like over many rays, unlike the zero-isodop line of real weather
    cl[:, sw.rng > MAX_CLUTTER_RANGE_KM * 1000.0] = False
    daz, drng = 5.0, 5000.0
    ia = (np.mod(sw.az, 360.0) // daz).astype(int)
    ir = (sw.rng // drng).astype(int)
    n_ia, n_ir = 72, int(ir.max()) + 1
    cell_cl = np.zeros((n_ia, n_ir))
    cell_ok = np.zeros((n_ia, n_ir))
    rows, cols = np.nonzero(cl)
    np.add.at(cell_cl, (ia[rows], ir[cols]), 1)
    rows, cols = np.nonzero(np.isfinite(z) & np.isfinite(v))
    np.add.at(cell_ok, (ia[rows], ir[cols]), 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        frac = np.where(cell_ok > 0, cell_cl / cell_ok, 0.0)
    cells = (frac >= CLUTTER_CELL_FRACTION) & (cell_cl >= 15)
    # a radial streak (an azimuth bin that is clutter-like at most of its ranges) is the zero-isodop
    # line of real weather, not ground: drop those azimuth bins
    has = cell_ok >= 15
    n_has = has.sum(axis=1)
    streak = (n_has >= 8) & (cells.sum(axis=1) > 0.5 * np.maximum(n_has, 1))
    rec["n_streak_azimuth_bins"] = int(streak.sum())
    cells[streak] = False
    if cells.any():
        gate_in_cell = cells[ia][:, ir]
        in_cells = cl & gate_in_cell
        n_in = int(in_cells.sum())
        ring_rows = np.nonzero(cells.sum(axis=0) >= n_ia // 2)[0]
        rec.update(
            n_in_cells=n_in,
            share_in_cells=n_in / n_echo,
            n_cells=int(cells.sum()),
            ring_range_km=(
                [
                    float(ring_rows.min() * drng / 1000.0),
                    float((ring_rows.max() + 1) * drng / 1000.0),
                ]
                if ring_rows.size
                else None
            ),
            where=where_of(in_cells, sw.az, sw.rng) if n_in else None,
            median_z=float(np.median(z[in_cells])) if n_in else None,
        )
    acc.clutter.append(rec)


def _blockage(sw: _Sweep, acc: _Acc) -> None:
    z = sw.field("reflectivity")
    if z is None or sw.az is None or sw.rng is None or sw.n_rays < 180:
        return
    geo = next((g for g in acc.geometry if g["sweep"] == sw.index), {})
    if (geo.get("coverage") or 0) < 350:
        return  # only a full circle has flanks on both sides of a wedge
    far = (sw.rng >= 20_000) & (sw.rng <= 150_000)
    if far.sum() < 20:
        return
    ok = np.isfinite(z[:, far])
    binsz = 1.0
    ia = (np.mod(sw.az, 360.0) // binsz).astype(int)
    nb = int(360 / binsz)
    cnt = np.bincount(ia, minlength=nb).astype(float)
    echo = np.bincount(ia, weights=ok.sum(axis=1), minlength=nb)
    with np.errstate(invalid="ignore", divide="ignore"):
        f = np.where(cnt > 0, echo / (cnt * ok.shape[1]), np.nan)
    if np.isnan(f).mean() > 0.2:
        return
    f = np.where(np.isnan(f), np.nanmedian(f), f)
    # flanks: running mean 8-30 deg away on both sides; blockage is a deficit inside a narrow wedge
    ext = np.concatenate([f[-45:], f, f[:45]])
    best: list[tuple[int, int]] = []
    flank_l = np.array([ext[i + 45 - 30 : i + 45 - 8].mean() for i in range(nb)])
    flank_r = np.array([ext[i + 45 + 8 : i + 45 + 30].mean() for i in range(nb)])
    flank = np.minimum(flank_l, flank_r)
    with np.errstate(invalid="ignore"):
        low = (flank >= 0.2) & (f <= 0.25 * flank)
    run = None
    for i in range(nb + 1):
        flag = bool(low[i % nb]) if i < nb else False
        if flag and run is None:
            run = i
        if not flag and run is not None:
            best.append((run, i - 1))
            run = None
    if len(best) > 1 and low[0] and low[-1]:  # wedge across north
        a, b = best.pop(0), best.pop()
        best.append((b[0], a[1] + nb))
    wedges = []
    for s, e in best:
        width = e - s + 1
        if 2 <= width <= 45:
            seg = np.arange(s, e + 1) % nb
            wedges.append(
                {
                    "azimuth_deg": [float(s % nb * binsz), float((e % nb + 1) * binsz)],
                    "width_deg": float(width * binsz),
                    "echo_share_inside": float(f[seg].mean()),
                    "echo_share_flanks": float(flank[seg].mean()),
                }
            )
    if wedges:
        acc.blockage.append(
            {"sweep": sw.index, "elevation": sw.elev, "wedges": wedges, "far_km": [20, 150]}
        )


def _echo_classes(sw: _Sweep, acc: _Acc) -> None:
    """Biological echo, isolated gates, low SNR, low RhoHV on this sweep."""
    z = sw.field("reflectivity")
    if z is None:
        return
    valid_z = np.isfinite(z)
    n_z = int(valid_z.sum())
    if n_z < 500:
        return
    rho, zdr, snr = sw.field("rhohv"), sw.field("zdr"), sw.field("snr")
    if rho is not None or zdr is not None:
        votes = np.zeros(z.shape, dtype=bool)
        with np.errstate(invalid="ignore"):
            if rho is not None:
                votes |= np.isfinite(rho) & (rho < BIO_MAX_RHOHV)
            if zdr is not None:
                votes |= np.isfinite(zdr) & (zdr > BIO_MIN_ZDR)
            bio = valid_z & (z <= BIO_MAX_DBZ) & votes
        n_bio = int(bio.sum())
        rec = {
            "sweep": sw.index,
            "elevation": sw.elev,
            "n_echo": n_z,
            "n_bio": n_bio,
            "share": n_bio / n_z,
            "signatures": [n for n, a in (("RhoHV", rho), ("ZDR", zdr)) if a is not None],
        }
        if n_bio and rec["share"] >= 0.02 and sw.rng is not None:
            rec["where"] = where_of(bio, sw.az, sw.rng)
            if rho is not None:
                rec["median_rhohv"] = float(np.nanmedian(np.where(bio, rho, np.nan)))
            rec["median_z"] = float(np.median(z[bio]))
        acc.bio.append(rec)
    if len(acc.speckle) < 2:
        p = np.pad(valid_z, 1, mode="constant")
        nb_cnt = np.zeros(z.shape, dtype=np.int8)
        for dy in (0, 1, 2):
            for dx in (0, 1, 2):
                if dy == 1 and dx == 1:
                    continue
                nb_cnt += p[dy : dy + z.shape[0], dx : dx + z.shape[1]]
        iso = valid_z & (nb_cnt == 0)
        acc.speckle.append(
            {
                "sweep": sw.index,
                "elevation": sw.elev,
                "n_valid": n_z,
                "n_isolated": int(iso.sum()),
                "share": float(iso.sum() / n_z),
                "where": where_of(iso, sw.az, sw.rng)
                if iso.sum() >= 50 and sw.rng is not None
                else None,
            }
        )
    if snr is not None:
        v = np.isfinite(snr)
        if v.sum() >= 500:
            low = v & (snr < LOW_SNR_DB)
            acc.snr.append(
                {
                    "sweep": sw.index,
                    "elevation": sw.elev,
                    "n_valid": int(v.sum()),
                    "n_low": int(low.sum()),
                    "share": float(low.sum() / v.sum()),
                    "where": where_of(low, sw.az, sw.rng)
                    if low.sum() >= 50 and sw.rng is not None
                    else None,
                }
            )
    if rho is not None and zdr is not None:
        both = np.isfinite(zdr) & np.isfinite(rho)
        if both.sum() >= 500:
            low = both & (rho < LOW_RHOHV)
            acc.rho.append(
                {
                    "sweep": sw.index,
                    "elevation": sw.elev,
                    "n_valid": int(both.sum()),
                    "n_low": int(low.sum()),
                    "share": float(low.sum() / both.sum()),
                    "where": where_of(low, sw.az, sw.rng)
                    if low.sum() >= 50 and sw.rng is not None
                    else None,
                }
            )


def _zdr_samples(sw: _Sweep, acc: _Acc) -> None:
    z, zdr, rho = sw.field("reflectivity"), sw.field("zdr"), sw.field("rhohv")
    if z is None or zdr is None or rho is None or sw.rng is None or sw.elev is None:
        return
    if not (0.0 <= sw.elev <= ZDR_MAX_ELEVATION):
        return
    km = sw.rng / 1000.0
    cols = (
        (km >= ZDR_RANGE_KM[0])
        & (km <= ZDR_RANGE_KM[1])
        & (_beam_height_km(sw.rng, sw.elev) <= ZDR_MAX_HEIGHT_KM)
    )
    if not cols.any():
        return
    with np.errstate(invalid="ignore"):
        sel = (
            np.isfinite(z)
            & np.isfinite(zdr)
            & np.isfinite(rho)
            & (z >= ZDR_Z_RANGE[0])
            & (z < ZDR_Z_RANGE[1])
            & (rho >= ZDR_RHOHV_MIN)
            & (np.abs(zdr) < 3.0)
        )
        sel &= cols[None, :]
        smooth = np.abs(zdr[:, 1:] - zdr[:, :-1]) < 1.0
    sel[:, :-1] &= smooth
    ia, ir = np.nonzero(sel)
    if ia.size == 0:
        acc.zdr_sweeps.append({"sweep": sw.index, "elevation": sw.elev, "n_gates": 0})
        return
    nblk = math.ceil(sw.n_gates / 5)
    key = ia.astype(np.int64) * nblk + ir // 5
    vals = zdr[ia, ir].astype(np.float64)
    inv = np.unique(key, return_inverse=True)[1]
    cnt = np.bincount(inv)
    sums = np.bincount(inv, weights=vals)
    keep = cnt >= 3
    block_means = sums[keep] / cnt[keep]
    acc.zdr_blocks.append(block_means)
    acc.zdr_sweeps.append(
        {
            "sweep": sw.index,
            "elevation": sw.elev,
            "n_gates": int(ia.size),
            "n_blocks": int(keep.sum()),
            "n_rays": int(np.unique(ia).size),
            "median_gate": float(np.median(vals)),
        }
    )


def _phidp(sw: _Sweep, acc: _Acc) -> None:
    phi, rho, z = sw.field("phidp"), sw.field("rhohv"), sw.field("reflectivity")
    if phi is None or rho is None or sw.rng is None:
        return
    with np.errstate(invalid="ignore"):
        sel = np.isfinite(phi) & np.isfinite(rho) & (rho >= PHIDP_RHOHV_MIN)
        if z is not None:
            sel &= np.isfinite(z) & (z >= 15.0)
    both = sel[:, :-1] & sel[:, 1:]
    n_pairs = int(both.sum())
    if n_pairs < PHIDP_MIN_PAIRS:
        acc.phidp.append({"sweep": sw.index, "elevation": sw.elev, "n_pairs": n_pairs})
        return
    d = np.where(both, phi[:, 1:] - phi[:, :-1], np.nan)
    wrapped = both & (np.abs(d) > 180.0)
    dw = _wrap180(d)[both]
    sigma = _mad_sigma(dw) / math.sqrt(2.0)
    rays_with_wrap = int(wrapped.any(axis=1).sum())
    # path-integrated PhiDP along each ray from the unwrapped gate-to-gate steps in rain
    steps = np.where(both, _wrap180(np.where(both, d, 0.0)), 0.0)
    dphi = steps.sum(axis=1)
    n_in = both.sum(axis=1)
    good = n_in >= 20
    rec: dict[str, Any] = {
        "sweep": sw.index,
        "elevation": sw.elev,
        "n_pairs": n_pairs,
        "sigma": float(sigma),
        "n_wrap_pairs": int(wrapped.sum()),
        "rays_with_wrap": rays_with_wrap,
        "n_rays": int(sw.n_rays),
        "share_rays_wrap": rays_with_wrap / max(1, sw.n_rays),
        "dphi_p95": float(np.percentile(dphi[good], 95)) if good.sum() >= 10 else None,
        "n_rain_rays": int(good.sum()),
    }
    if wrapped.any():
        wm = np.zeros(phi.shape, dtype=bool)
        wm[:, :-1] = wrapped
        rec["where"] = where_of(wm, sw.az, sw.rng)
    acc.phidp.append(rec)


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def _sev_by(value: float, notice: float, warning: float | None = None) -> str | None:
    if warning is not None and value >= warning:
        return "warning"
    return "notice" if value >= notice else None


def _fmt_where(w: dict[str, Any] | None) -> str:
    if not w:
        return ""
    parts = []
    if w.get("azimuth_deg"):
        a0, a1 = w["azimuth_deg"]
        parts.append("all azimuths" if (a0, a1) == (0.0, 360.0) else f"azimuth {a0:g}-{a1:g} deg")
    if w.get("range_km"):
        parts.append(f"range {w['range_km'][0]:g}-{w['range_km'][1]:g} km")
    return ", ".join(parts)


def _sweeps_text(sweeps: list[int], limit: int = 6) -> str:
    s = ", ".join(str(i) for i in sweeps[:limit])
    return f"sweep{'s' if len(sweeps) > 1 else ''} {s}" + (
        f" (+{len(sweeps) - limit} more)" if len(sweeps) > limit else ""
    )


def _f_scan_type(acc: _Acc, tree_info: dict[str, Any]) -> list[Finding]:
    out: list[Finding] = []
    geo = acc.geometry
    if not geo:
        return out
    n_ppi = sum(g["kind"] == "ppi" for g in geo)
    n_rhi = sum(g["kind"] == "rhi" for g in geo)
    elevs = [g["elevation"] for g in geo if g["kind"] == "ppi" and g["elevation"] is not None]
    if n_rhi and not n_ppi:
        title, summary = (
            "Range-height (RHI) scan",
            f"{n_rhi} RHI sweep(s): azimuth-based checks (coverage, blockage, clutter rings) are skipped.",
        )
    elif n_rhi:
        title, summary = (
            "Mixed scan types",
            f"{n_ppi} PPI and {n_rhi} RHI sweeps; azimuth-based checks use the PPI sweeps only.",
        )
    else:
        span = f", elevations {min(elevs):.1f}-{max(elevs):.1f} deg" if elevs else ""
        title, summary = "PPI volume scan", f"{n_ppi} PPI sweep(s){span}."
    out.append(
        Finding(
            id="scan_type",
            title=title,
            severity="info",
            summary=summary,
            scope="file",
            evidence=[ev("PPI sweeps", n_ppi), ev("RHI sweeps", n_rhi)]
            + (
                [
                    ev("lowest elevation", min(elevs), "deg"),
                    ev("highest elevation", max(elevs), "deg"),
                ]
                if elevs
                else []
            ),
            confidence=0.95 if tree_info.get("modes_known") else 0.8,
            method="Scan type from sweep_mode, else from how azimuth and elevation change along "
            "the rays.",
        )
    )
    # partial coverage
    partial = [g for g in geo if g["kind"] == "ppi" and g.get("largest_gap", 0) >= COVERAGE_GAP_DEG]
    if partial:
        full = [g for g in geo if g["kind"] == "ppi" and (g.get("coverage") or 0) >= 355]
        worst = max(partial, key=lambda g: g["largest_gap"])
        sev = "warning" if full else "notice"
        sweeps = [g["sweep"] for g in partial]
        where = {
            "azimuth_deg": [round(worst["gap_from"], 1), round(worst["gap_to"], 1)],
            "range_km": None,
            "sweeps": sweeps,
        }
        cov = worst.get("coverage")
        out.append(
            Finding(
                id="partial_coverage",
                title="Sector scan or missing rays" if not full else "Incomplete sweep(s)",
                severity=sev,
                summary=(
                    f"{_sweeps_text(sweeps)} cover{'s' if len(sweeps) == 1 else ''} about {cov:.0f} deg of azimuth; "
                    f"nothing between {worst['gap_from']:.0f} and {worst['gap_to']:.0f} deg."
                    + (
                        " Other sweeps are full circles, so rays were probably lost."
                        if full
                        else " Fine for an intentional sector scan."
                    )
                ),
                sweeps=sweeps,
                where=where,
                evidence=[
                    ev("azimuth coverage (worst sweep)", cov, "deg", "RETRIEVED"),
                    ev("largest azimuth gap", worst["largest_gap"], "deg"),
                    ev("median ray spacing", worst["spacing"], "deg"),
                    ev("full-circle sweeps in the file", len(full)),
                ],
                confidence=0.95,
                method=f"Largest gap in the sorted ray azimuths; a gap of at least {COVERAGE_GAP_DEG:g} deg counts. "
                "Warning when other sweeps of the same file are complete.",
                per_sweep=[
                    {
                        "sweep": g["sweep"],
                        "elevation": g["elevation"],
                        "coverage_deg": g.get("coverage"),
                    }
                    for g in partial
                ],
            )
        )
    return out


def _f_timing(acc: _Acc) -> list[Finding]:
    out: list[Finding] = []
    # many repeats = a coarse time stamp (whole seconds), not a fault; a few repeats are suspect
    coarse = [t for t in acc.timing if t.get("n_duplicate", 0) > 0.05 * t["n_rays"]]
    bad = [
        t
        for t in acc.timing
        if t.get("n_backward", 0) or 0 < t.get("n_duplicate", 0) <= 0.05 * t["n_rays"]
    ]
    if coarse:
        t = coarse[0]
        out.append(
            Finding(
                id="time_resolution",
                title="Coarse ray time stamps",
                severity="info",
                summary=f"Many rays share a time stamp ({_pct(t['n_duplicate'] / t['n_rays'])} % of the rays in sweep {t['sweep']}): the times are stored at a "
                "resolution coarser than the ray interval. Per-ray timing (motion, advection) is "
                "only as good as that resolution.",
                sweeps=[c["sweep"] for c in coarse],
                evidence=[
                    ev("repeated times", t["n_duplicate"]),
                    ev("rays", t["n_rays"]),
                    ev("median distinct interval", t.get("median_dt"), "s"),
                ],
                confidence=0.8,
                method="Share of rays whose time equals the previous ray's; above 5 % it is taken "
                "as the stored resolution.",
            )
        )
    if bad:
        nb = sum(t.get("n_backward", 0) for t in bad)
        nd = sum(t.get("n_duplicate", 0) for t in bad)
        worst = max(bad, key=lambda t: t.get("n_backward", 0) + t.get("n_duplicate", 0))
        az = worst.get("backward_az")
        where = {"sweeps": [t["sweep"] for t in bad]}
        if az is not None:
            where["azimuth_deg"] = [round(az, 1), round(az, 1)]
        out.append(
            Finding(
                id="time_order",
                title="Ray times not strictly increasing",
                severity="warning",
                summary=f"{nb} step(s) go back in time and {nd} ray(s) repeat the previous time ({_sweeps_text(where['sweeps'])}). "
                "Anything that uses ray time (motion, tracking, time-aware export) will be wrong.",
                sweeps=where["sweeps"],
                where=where,
                evidence=[
                    ev("backward steps", nb),
                    ev("duplicate times", nd),
                    ev("rays in worst sweep", worst["n_rays"]),
                ],
                confidence=0.95,
                method="First difference of the per-ray time coordinate: negative = unsorted, "
                "zero = duplicate.",
                per_sweep=[
                    {
                        "sweep": t["sweep"],
                        "backward": t.get("n_backward", 0),
                        "duplicate": t.get("n_duplicate", 0),
                    }
                    for t in bad
                ],
            )
        )
    gaps = [t for t in acc.timing if t.get("n_gaps")]
    if gaps:
        worst = max(gaps, key=lambda t: t["largest_gap_s"])
        lost = sum(t["lost_rays"] for t in gaps)
        total = sum(t["n_rays"] for t in gaps)
        sev = "warning" if lost / max(1, total) >= 0.05 else "notice"
        where = {"sweeps": [t["sweep"] for t in gaps]}
        if worst.get("gap_az") and None not in worst["gap_az"]:
            where["azimuth_deg"] = [round(worst["gap_az"][0], 1), round(worst["gap_az"][1], 1)]
        out.append(
            Finding(
                id="time_gap",
                title="Gap in the ray times",
                severity=sev,
                summary=f"A {worst['largest_gap_s']:.1f} s pause between rays in sweep {worst['sweep']} (after ray {worst['gap_ray']}); "
                f"about {worst['lost_rays']:.0f} rays are missing there.",
                sweeps=where["sweeps"],
                where=where,
                evidence=[
                    ev("largest gap", worst["largest_gap_s"], "s"),
                    ev("median ray interval", worst["median_dt"], "s"),
                    ev("rays missing (gap / median interval)", worst["lost_rays"], "", "ESTIMATE"),
                    ev("gaps in file", sum(t["n_gaps"] for t in gaps)),
                ],
                confidence=0.85,
                method=f"A ray interval above max({TIME_GAP_FACTOR:g} x median, {TIME_GAP_MIN_S:g} s) is a gap; missing rays = gap / median interval.",
            )
        )
    nat = [t for t in acc.timing if t.get("n_nat")]
    if nat:
        out.append(
            Finding(
                id="time_missing",
                title="Rays without a time",
                severity="notice",
                summary=f"{sum(t['n_nat'] for t in nat)} ray(s) have no valid time ({_sweeps_text([t['sweep'] for t in nat])}).",
                sweeps=[t["sweep"] for t in nat],
                evidence=[ev("rays without time", sum(t["n_nat"] for t in nat))],
                confidence=0.95,
                method="Count of NaT / NaN in the per-ray time coordinate.",
            )
        )
    # sweeps that start before the previous one ended
    starts = sorted((t["t_first"], t["sweep"]) for t in acc.timing if "t_first" in t)
    ordered = [s for _, s in starts]
    if ordered != sorted(ordered) and len(ordered) > 2:
        out.append(
            Finding(
                id="sweep_time_order",
                title="Sweeps are not in time order",
                severity="notice",
                summary="The sweep numbers do not follow the sweeps' start times.",
                sweeps=ordered,
                evidence=[ev("sweeps by start time", " ".join(map(str, ordered[:12])))],
                confidence=0.9,
                method="Start time of each sweep compared with its index.",
            )
        )
    return out


def _f_fields(acc: _Acc, n_sweeps: int) -> list[Finding]:
    out: list[Finding] = []
    roles = acc.roles
    have = lambda r: bool(roles.get(r))  # noqa: E731

    def add(
        fid: str,
        title: str,
        sev: str,
        summary: str,
        sweeps: list[int],
        advice: str | None = None,
        conf: float = 0.9,
    ) -> None:
        out.append(
            Finding(
                id=fid,
                title=title,
                severity=sev,
                summary=summary,
                scope="file",
                sweeps=sweeps,
                evidence=[ev("sweeps with the field", len(sweeps)), ev("sweeps in file", n_sweeps)],
                confidence=conf,
                method="Each role is resolved on every sweep through the variable mapping (CF "
                "name, aliases, patterns; docs/VARMAP.md).",
                advice=advice,
            )
        )

    if not have("reflectivity") and n_sweeps:
        add(
            "no_reflectivity",
            "No reflectivity field",
            "warning",
            "No variable resolves to reflectivity: most QC steps, the noise-floor and clutter "
            "checks need it. If it exists under another name, map it under Variable mapping.",
            [],
            "Open Variable mapping in the data inspector and assign the reflectivity variable.",
        )
    vel, wid = roles.get("velocity", []), roles.get("width", [])
    if vel and not wid:
        add(
            "velocity_without_width",
            "Velocity without spectrum width",
            "notice",
            "Doppler velocity is present but spectrum width is not, so noisy velocity cannot be "
            "screened by width.",
            vel,
        )
    if wid and not vel:
        add(
            "width_without_velocity",
            "Spectrum width without velocity",
            "notice",
            "Spectrum width is present but no velocity field resolves; check the variable mapping.",
            wid,
        )
    if have("zdr") and not have("rhohv"):
        add(
            "zdr_without_rhohv",
            "ZDR without RhoHV",
            "warning",
            "ZDR is present but there is no correlation coefficient: non-meteorological echo "
            "cannot be separated and the ZDR offset check is impossible.",
            roles["zdr"],
            "Map a RhoHV variable if the file has one under another name.",
        )
    if have("phidp") and not have("rhohv"):
        add(
            "phidp_without_rhohv",
            "PhiDP without RhoHV",
            "notice",
            "PhiDP is present without RhoHV, so its noise cannot be screened and KDP will be less "
            "reliable.",
            roles["phidp"],
        )
    dual = set(roles.get("zdr", [])) | set(roles.get("rhohv", []))
    elev = {g["sweep"]: g["elevation"] for g in acc.geometry}
    pairs = [
        (d, v)
        for d in sorted(dual - set(vel))
        for v in vel
        if v not in dual
        and None not in (elev.get(d), elev.get(v))
        and abs(elev[d] - elev[v]) <= 0.3
    ]
    if pairs:
        lone = {p[0] for p in pairs} | {p[1] for p in pairs}
        out.append(
            Finding(
                id="split_cut",
                title="Split cuts: velocity and dual-pol on separate sweeps",
                severity="info",
                summary=f"{len(pairs)} elevation(s) are scanned twice: dual-pol on one sweep (e.g. {pairs[0][0]}), velocity on the next (e.g. {pairs[0][1]}). "
                "Typical of NEXRAD split cuts; tools pair the sweeps by elevation automatically.",
                scope="file",
                sweeps=sorted(lone),
                evidence=[
                    ev("paired cuts", len(pairs)),
                    ev("sweeps with velocity", len(vel)),
                    ev("sweeps with dual-pol", len(dual)),
                ],
                confidence=0.9,
                method="Sweeps of the same fixed angle (within 0.3 deg) where one "
                "has only velocity and the other only dual-pol moments "
                "(docs/VARMAP.md, paired sweeps).",
            )
        )
    if acc.empty:
        by: dict[str, list[int]] = {}
        for e in acc.empty:
            by.setdefault(e["variable"], []).append(e["sweep"])
        # an empty field next to a sweep of the same elevation that has it is split-cut padding
        padding = all(
            any(
                o != e["sweep"]
                and o in roles.get(e["role"], [])
                and None not in (elev.get(o), elev.get(e["sweep"]))
                and abs(elev[o] - elev[e["sweep"]]) <= 0.3
                for o in roles.get(e["role"], [])
            )
            for e in acc.empty
            if e["role"] != "any"
        )
        out.append(
            Finding(
                id="empty_field",
                title="Fields with no valid gate",
                severity="info" if padding else "notice",
                summary="; ".join(
                    f"{v} is entirely missing on {_sweeps_text(s)}" for v, s in list(by.items())[:3]
                )
                + (
                    ". Each has a partner sweep at the same elevation: split-cut padding, nothing "
                    "is wrong."
                    if padding
                    else "."
                ),
                sweeps=sorted({i for s in by.values() for i in s}),
                evidence=[ev("empty (field, sweep) pairs", len(acc.empty))],
                confidence=0.9,
                method="A listed variable with every gate NaN after decoding; "
                "benign when a sweep within 0.3 deg elevation has the "
                "field.",
            )
        )
    return out


def _f_units(acc: _Acc) -> list[Finding]:
    out: list[Finding] = []

    def add(role: str, sev: str, why: str, recs: list[dict[str, Any]], expect: str) -> None:
        worst = recs[0]
        out.append(
            Finding(
                id=f"units_{role}",
                title=f"{_NAMES[role].capitalize()}: unexpected values or units",
                severity=sev,
                summary=f"{worst['variable']}: {why}",
                sweeps=sorted({r["sweep"] for r in recs}),
                evidence=[
                    ev("declared units", worst["units"] or "(none)"),
                    ev("smallest value", worst["min"]),
                    ev("largest value", worst["max"]),
                    ev("0.1 / 99.9 percentile", f"{worst['p001']:.3g} / {worst['p999']:.3g}"),
                ],
                confidence=0.7,
                method=f"0.1 and 99.9 percentiles compared with what {_NAMES[role]} normally holds ({expect}); a unit string of a different class is also flagged (docs/VARMAP.md unit classes).",
                advice="Check the unit/scale in the file; the app does not rescale anything on "
                "its own.",
            )
        )

    for role, expect, classes, lo, hi, hard_lo, hard_hi in (
        ("reflectivity", "about -40 to 90 dBZ", ("dbz", "db"), -60.0, 95.0, -100.0, 120.0),
        (
            "velocity",
            "within the Nyquist velocity, tens of m/s",
            ("speed",),
            -120.0,
            120.0,
            -300.0,
            300.0,
        ),
        ("width", "0 to ~30 m/s", ("speed",), -0.01, 40.0, -1.0, 80.0),
        ("zdr", "about -8 to 12 dB", ("db",), -10.0, 14.0, -30.0, 40.0),
        ("rhohv", "0 to 1", ("none", "unitless"), -0.02, 1.08, -0.5, 1.5),
    ):
        recs = acc.units.get(role) or []
        if not recs:
            continue
        bad = [r for r in recs if r["p001"] < lo or r["p999"] > hi]
        if bad:
            worst = max(bad, key=lambda r: max(r["p999"] - hi, lo - r["p001"]))
            hard = worst["p001"] < hard_lo or worst["p999"] > hard_hi
            what = f"values (0.1 to 99.9 percentile) {worst['p001']:.3g} to {worst['p999']:.3g} {worst['units']}".strip()
            add(
                role,
                "warning" if hard or (role == "rhohv" and worst["p999"] > 1.2) else "notice",
                f"{what} are outside the usual range ({expect}).",
                [worst, *bad],
                expect,
            )
            continue
        cls = varmap.unit_class(recs[0]["units"])
        if recs[0]["units"] and cls is not None and cls not in classes and role != "rhohv":
            add(
                role,
                "notice",
                f"units '{recs[0]['units']}' are not what {_NAMES[role]} normally has.",
                recs,
                expect,
            )
    phi = acc.units.get("phidp") or []
    if phi:
        r = phi[0]
        if 0 < r["max"] <= 2 * math.pi + 0.1 and r["max"] - r["min"] > 0.5:
            add(
                "phidp",
                "notice",
                f"values only reach {r['max']:.2f}: the field may be in radians, not degrees.",
                phi,
                "0-360 deg",
            )
        elif r["max"] > 360.5 or r["min"] < -180.5:
            add(
                "phidp",
                "notice",
                f"values {r['min']:.0f} to {r['max']:.0f} deg are outside one turn.",
                phi,
                "0-360 deg",
            )
    # Nyquist vs the largest velocity
    over = [v for v in acc.velocity if v["source"] == "file" and v["max_abs"] > 1.05 * v["nyquist"]]
    if over:
        w = max(over, key=lambda v: v["max_abs"] / v["nyquist"])
        out.append(
            Finding(
                id="nyquist_mismatch",
                title="Velocities above the stated Nyquist velocity",
                severity="warning",
                summary=f"Sweep {w['sweep']} holds |V| up to {w['max_abs']:.1f} m/s but the file says the Nyquist velocity is {w['nyquist']:.1f} m/s: "
                "the stated Nyquist velocity or the velocity unit is wrong, or the data were "
                "already de-aliased.",
                sweeps=[v["sweep"] for v in over],
                evidence=[
                    ev("largest |V|", w["max_abs"], "m/s"),
                    ev("stated Nyquist velocity", w["nyquist"], "m/s"),
                ],
                confidence=0.8,
                method="Radial velocity cannot exceed the Nyquist velocity (Doviak and Zrnic "
                "1993) unless it was unfolded afterwards.",
            )
        )
    return out


def _f_velocity(acc: _Acc) -> list[Finding]:
    from engine import multidoppler as md

    recs = acc.velocity
    if not recs:
        return []
    thr = md.ALIAS_JUMP_FRACTION
    flagged = [r for r in recs if r["jump_fraction"] >= 0.2 * thr and r["approx_jumps"] >= 50]
    if not flagged:
        return []
    worst = max(flagged, key=lambda r: r["jump_fraction"])
    sev = "warning" if worst["jump_fraction"] >= thr else "notice"
    base = 0.85 if worst["source"] == "file" else 0.65
    conf = base * (1.0 if worst["approx_jumps"] >= 50 else 0.7)
    sweeps = [r["sweep"] for r in flagged]
    nyq_txt = f"{worst['nyquist']:.1f} m/s, " + (
        "from the file" if worst["source"] == "file" else "estimated from the largest |V|"
    )
    return [
        Finding(
            id="velocity_aliasing",
            title="Velocity looks aliased (folded)",
            severity=sev,
            summary=f"{_sweeps_text(sweeps)}: {_pct(worst['jump_fraction'])} % of gate-to-gate velocity steps exceed 1.2 x the Nyquist velocity "
            f"(Nyquist {nyq_txt}); worst on sweep {worst['sweep']}. De-alias before computing winds, shear or divergence.",
            sweeps=sweeps,
            where={**(worst.get("where") or {}), "sweeps": sweeps},
            evidence=[
                ev(
                    "steps above 1.2 x Nyquist",
                    _pct(worst["jump_fraction"]),
                    "%",
                    "RETRIEVED",
                    f"threshold for warning: {_pct(thr)} %",
                ),
                ev(
                    "Nyquist velocity",
                    worst["nyquist"],
                    "m/s",
                    "RETRIEVED" if worst["source"] == "file" else "ESTIMATE",
                    "from the file"
                    if worst["source"] == "file"
                    else "largest |V| rounded up to 0.5 m/s",
                ),
                ev("gates with |V| >= 0.9 Nyquist", _pct(worst["edge_fraction"]), "%"),
                ev("valid velocity gates", worst["n_valid"]),
            ],
            confidence=conf,
            method="Share of gate-to-gate velocity steps (along azimuth and range) above 1.2 "
            "Nyquist velocities, the same detector the multi-Doppler "
            "retrieval uses (a folded field jumps by about 2 Nyquist velocities across fold "
            "boundaries; Eilts and Smith 1990, Doviak and Zrnic 1993). "
            "With no Nyquist value in the file it is estimated from the largest |V|, which is "
            "exact for aliased data and too small otherwise.",
            per_sweep=[
                {
                    "sweep": r["sweep"],
                    "elevation": r["elevation"],
                    "jump_percent": _pct(r["jump_fraction"]),
                    "nyquist": r["nyquist"],
                }
                for r in flagged
            ],
            steps=["dealias"],
            targets=["velocity"],
        )
    ]


def _f_folding(acc: _Acc) -> list[Finding]:
    flagged = [r for r in acc.fold if r["n_folded"] and r["share_of_signal"] >= FOLD_NOTICE]
    if not flagged:
        return []
    worst = max(flagged, key=lambda r: r["share_of_signal"])
    sev = "warning" if worst["share_of_signal"] >= FOLD_WARNING else "notice"
    where = {**(worst.get("where") or {}), "sweeps": sorted({r["sweep"] for r in flagged})}
    return [
        Finding(
            id="range_folding",
            title="Range-folded gates",
            severity=sev,
            summary=f"Sweep {worst['sweep']} ({_NAMES[worst['role']]}): {_pct(worst['share_of_signal'])} % of the gates with a signal are range-folded "
            f"(second-trip echo hides the true value); {_fmt_where(worst.get('where'))}.",
            sweeps=where["sweeps"],
            where=where,
            evidence=[
                ev("range-folded gates", worst["n_folded"]),
                ev("share of gates with a signal", _pct(worst["share_of_signal"]), "%"),
                ev("share of all gates", _pct(worst["share_of_gates"]), "%"),
            ],
            confidence=0.9,
            method="Count of gates carrying the Level II 'range folded' code (value code 1) "
            "before it is masked; folded gates have no measurement "
            "(Doviak and Zrnic 1993; NEXRAD ICD 2620010). Only integer-encoded files carry the "
            "code, so other formats are not checked.",
            per_sweep=[
                {
                    "sweep": r["sweep"],
                    "variable": r["role"],
                    "folded_percent": _pct(r["share_of_signal"]),
                }
                for r in flagged[:12]
            ],
            advice="The folded gates are already masked and stay empty: nothing in the data can "
            "recover them. Use the other cut (lower PRF / SZ-2 sweep) if the volume has "
            "one.",
        )
    ]


def _f_noise(acc: _Acc) -> list[Finding]:
    good = [
        n
        for n in acc.noise
        if 10 <= n["slope_db_per_decade"] <= 30 and n["resid_mad"] <= 4.0 and n["n_bins"] >= 8
    ]
    if not good:
        return []
    n = min(good, key=lambda g: g["sweep"])
    sev = "notice" if n["mds50"] > 10.0 else "info"
    return [
        Finding(
            id="noise_floor",
            title="Minimum detectable reflectivity",
            severity=sev,
            summary=f"Weakest echoes follow the 20 log r noise law: about {n['mds50']:.0f} dBZ at 50 km and {n['mds100']:.0f} dBZ at 100 km (sweep {n['sweep']}). "
            "Echo weaker than this is not in the data."
            + (
                " That is high: weak precipitation and clear-air echo are cut off."
                if sev == "notice"
                else ""
            ),
            sweeps=[n["sweep"]],
            where={
                "sweeps": [n["sweep"]],
                "range_km": [round(n["r_min_km"], 1), round(n["r_max_km"], 1)],
            },
            evidence=[
                ev(
                    "envelope at 50 km",
                    n["mds50"],
                    "dBZ",
                    "ESTIMATE",
                    "upper bound of the true minimum detectable signal",
                ),
                ev("envelope at 100 km", n["mds100"], "dBZ", "ESTIMATE"),
                ev(
                    "fitted slope",
                    n["slope_db_per_decade"],
                    "dB/decade",
                    "RETRIEVED",
                    "20 expected",
                ),
                ev("scatter around the law (1.48 MAD)", n["resid_mad"], "dB"),
            ],
            confidence=0.6,
            method="2nd percentile of reflectivity in 16 log-spaced range bins from 10 km; for a "
            "noise-limited receiver the weakest detectable echo grows as "
            "20 log10(r) (radar equation, Doviak and Zrnic 1993). Accepted only if the envelope "
            "(10 km outwards) has a slope of 20 +- 10 dB/decade and a scatter below 4 dB. "
            "Thresholding in the signal processor (SNR gate) is included, and the true noise "
            "floor is lower or equal.",
        )
    ]


def _f_clutter(acc: _Acc) -> list[Finding]:
    flagged = [
        c for c in acc.clutter if c.get("n_in_cells") and c.get("share_in_cells", 0) >= 0.002
    ]
    if not flagged:
        return []
    worst = max(flagged, key=lambda c: c["share_in_cells"])
    sev = "warning" if worst["share_in_cells"] >= CLUTTER_WARNING_SHARE else "notice"
    ring = worst.get("ring_range_km")
    shape = f"a ring at {ring[0]:g}-{ring[1]:g} km" if ring else "fixed patches"
    where = {**(worst.get("where") or {}), "sweeps": [c["sweep"] for c in flagged]}
    return [
        Finding(
            id="ground_clutter",
            title="Ground clutter",
            severity=sev,
            summary=f"Sweep {worst['sweep']}: {_pct(worst['share_in_cells'])} % of the echo gates are persistent zero-velocity strong echo ({shape}); "
            f"{_fmt_where(worst.get('where'))}.",
            sweeps=where["sweeps"],
            where=where,
            evidence=[
                ev("clutter-like gates in persistent cells", worst["n_in_cells"]),
                ev("share of echo gates", _pct(worst["share_in_cells"]), "%"),
                ev("median reflectivity there", worst.get("median_z"), "dBZ"),
                ev(
                    "clutter-like gates anywhere (|V| <= 1 m/s, Z >= 20 dBZ)",
                    _pct(worst["share_of_echo"]),
                    "%",
                ),
            ],
            confidence=0.7 if ring else 0.55,
            method=f"Gates with |V| <= {CLUTTER_VEL:g} m/s and Z >= {CLUTTER_Z:g} dBZ are counted per 5 deg x 5 km cell; cells where at least "
            f"{int(CLUTTER_CELL_FRACTION * 100)} % of the gates qualify are 'persistent' (ground does not move; a zero-isodop line of real weather is one or two rays wide "
            "and fails the test). After Hubbert et al. 2009 and Steiner and Smith 2002. Calm-wind "
            "widespread rain can look the same.",
            per_sweep=[
                {
                    "sweep": c["sweep"],
                    "elevation": c["elevation"],
                    "percent": _pct(c.get("share_in_cells", 0)),
                }
                for c in flagged
            ],
            steps=["clutter_filter"],
            targets=["velocity", "reflectivity"],
        )
    ]


def _f_blockage(acc: _Acc) -> list[Finding]:
    if not acc.blockage:
        return []
    first = acc.blockage[0]
    w = max(first["wedges"], key=lambda x: x["echo_share_flanks"] - x["echo_share_inside"])
    sweeps = [b["sweep"] for b in acc.blockage]
    repeat = len(acc.blockage) > 1 and any(
        abs(a["azimuth_deg"][0] - w["azimuth_deg"][0]) <= 5
        for b in acc.blockage[1:]
        for a in b["wedges"]
    )
    return [
        Finding(
            id="beam_blockage",
            title="Possible beam blockage (azimuth deficit)",
            severity="notice",
            summary=f"Sweep {first['sweep']}: a {w['width_deg']:.0f} deg wedge at azimuth {w['azimuth_deg'][0]:g}-{w['azimuth_deg'][1]:g} deg has "
            f"{_pct(w['echo_share_inside'])} % echo at 20-150 km while the flanks have {_pct(w['echo_share_flanks'])} %."
            + (" Seen on more than one sweep." if repeat else "")
            + " A gap in the precipitation looks the same; no terrain model was used.",
            sweeps=sweeps,
            where={"azimuth_deg": w["azimuth_deg"], "range_km": first["far_km"], "sweeps": sweeps},
            evidence=[
                ev("echo share inside the wedge", _pct(w["echo_share_inside"]), "%"),
                ev("echo share on the flanks (8-30 deg away)", _pct(w["echo_share_flanks"]), "%"),
                ev("wedge width", w["width_deg"], "deg"),
            ],
            confidence=0.55 if repeat else 0.4,
            method="Per-degree share of gates with echo between 20 and 150 km, compared with the "
            "flanks 8-30 deg away on both sides: a narrow wedge (2-45 deg) "
            "with at most a quarter of the flank echo is flagged. A proxy only (partial blockage: "
            "Bech et al. 2003); confirm with a terrain-based "
            "blockage map (radarx beam-blockage module) or clear-air maps.",
            per_sweep=[
                {
                    "sweep": b["sweep"],
                    "elevation": b["elevation"],
                    "wedges": [x["azimuth_deg"] for x in b["wedges"]],
                }
                for b in acc.blockage
            ],
        )
    ]


def _f_bio(acc: _Acc) -> list[Finding]:
    recs = [b for b in acc.bio if b["share"] >= 0.02]
    if not recs:
        return []
    worst = max(recs, key=lambda b: b["share"])
    sev = "notice" if worst["share"] >= BIO_NOTICE_SHARE else "info"
    sweeps = [b["sweep"] for b in recs]
    steps = ["biological_filter"] if sev == "notice" else []
    sig = " and ".join(worst["signatures"])
    return [
        Finding(
            id="biological_echo",
            title="Biological / clear-air echo",
            severity=sev,
            summary=f"Sweep {worst['sweep']}: {_pct(worst['share'])} % of the echo gates are weak (<= {BIO_MAX_DBZ:g} dBZ) with {sig} signatures of birds, insects "
            f"or clear-air scatter; {_fmt_where(worst.get('where'))}.",
            sweeps=sweeps,
            where={**(worst.get("where") or {}), "sweeps": sweeps},
            evidence=[
                ev("candidate gates", worst["n_bio"]),
                ev(
                    "share of echo gates",
                    _pct(worst["share"]),
                    "%",
                    "ESTIMATE",
                    "what the biological_filter step would remove at its defaults",
                ),
                ev("median reflectivity", worst.get("median_z"), "dBZ"),
                ev("median RhoHV", worst.get("median_rhohv"), ""),
            ],
            confidence=0.5,
            method=f"Reflectivity <= {BIO_MAX_DBZ:g} dBZ with RhoHV < {BIO_MAX_RHOHV:g} or ZDR > {BIO_MIN_ZDR:g} dB: the rule of the pipeline's biological filter "
            "(Zrnic and Ryzhkov 1998; Gourley et al. 2007). Low RhoHV also occurs in noisy weak "
            "precipitation, so this is an upper bound.",
            per_sweep=[
                {"sweep": b["sweep"], "elevation": b["elevation"], "percent": _pct(b["share"])}
                for b in recs
            ],
            steps=steps,
            targets=["velocity", "reflectivity", "zdr"],
        )
    ]


def _f_speckle(acc: _Acc) -> list[Finding]:
    recs = acc.speckle
    if not recs:
        return []
    worst = max(recs, key=lambda r: r["share"])
    if worst["share"] < SPECKLE_NOTICE_SHARE:
        return []
    return [
        Finding(
            id="speckle",
            title="Isolated single gates (speckle)",
            severity="notice",
            summary=f"Sweep {worst['sweep']}: {_pct(worst['share'])} % of the valid reflectivity gates have no valid neighbour; "
            "these are mostly noise or point targets.",
            sweeps=[r["sweep"] for r in recs],
            where=worst.get("where"),
            evidence=[
                ev("isolated gates", worst["n_isolated"]),
                ev("share of valid gates", _pct(worst["share"]), "%"),
            ],
            confidence=0.7,
            method="A valid gate with none of its 8 neighbours valid (azimuth edges not wrapped). "
            "The pipeline's 'Remove speckle' step removes small regions (Steiner and Smith "
            "2002).",
            steps=["speckle"],
            targets=["velocity", "reflectivity"],
        )
    ]


def _f_snr(acc: _Acc) -> list[Finding]:
    recs = [r for r in acc.snr if r["share"] >= LOW_SNR_NOTICE_SHARE]
    if not recs:
        return []
    worst = max(recs, key=lambda r: r["share"])
    return [
        Finding(
            id="low_snr",
            title="Many low-SNR gates",
            severity="notice",
            summary=f"Sweep {worst['sweep']}: {_pct(worst['share'])} % of the gates have SNR below {LOW_SNR_DB:g} dB; their values are noise dominated.",
            sweeps=[r["sweep"] for r in recs],
            where=worst.get("where"),
            evidence=[
                ev("gates below the SNR limit", worst["n_low"]),
                ev("share of gates with SNR", _pct(worst["share"]), "%"),
            ],
            confidence=0.8,
            method=f"Share of gates with SNR < {LOW_SNR_DB:g} dB; velocity, width and polarimetric moments degrade quickly below a few dB (Doviak and Zrnic 1993).",
            steps=["snr_gate"],
            targets=["velocity", "reflectivity", "zdr"],
        )
    ]


def _f_rho(acc: _Acc) -> list[Finding]:
    recs = [r for r in acc.rho if r["share"] >= LOW_RHOHV_NOTICE_SHARE]
    if not recs:
        return []
    worst = max(recs, key=lambda r: r["share"])
    return [
        Finding(
            id="low_rhohv",
            title="Many gates with low RhoHV",
            severity="notice",
            summary=f"Sweep {worst['sweep']}: {_pct(worst['share'])} % of the ZDR gates have RhoHV below {LOW_RHOHV:g}; ZDR there describes non-meteorological or mixed scatterers.",
            sweeps=[r["sweep"] for r in recs],
            where=worst.get("where"),
            evidence=[
                ev("gates with RhoHV < limit", worst["n_low"]),
                ev("share of ZDR gates", _pct(worst["share"]), "%"),
            ],
            confidence=0.7,
            method=f"Share of valid ZDR gates with RhoHV < {LOW_RHOHV:g}; precipitation has RhoHV above about 0.9, ground clutter, insects and birds well below (Zrnic and Ryzhkov 1998).",
            steps=["rhohv_gate"],
            targets=["zdr"],
        )
    ]


def _f_zdr(acc: _Acc) -> list[Finding]:
    if not acc.zdr_sweeps:
        return []
    n_gates = sum(s["n_gates"] for s in acc.zdr_sweeps)
    n_rays = sum(s.get("n_rays", 0) for s in acc.zdr_sweeps)
    blocks = np.concatenate(acc.zdr_blocks) if acc.zdr_blocks else np.zeros(0)
    sweeps = [s["sweep"] for s in acc.zdr_sweeps]
    method = (
        f"Light-rain reference: gates with Z in {ZDR_Z_RANGE[0]:g}-{ZDR_Z_RANGE[1]:g} dBZ, RhoHV >= {ZDR_RHOHV_MIN:g}, 10-110 km, beam below {ZDR_MAX_HEIGHT_KM:g} km "
        f"(under a typical melting layer) and elevation <= {ZDR_MAX_ELEVATION:g} deg have an intrinsic ZDR of about {ZDR_REFERENCE_DB:g} dB "
        f"(+- {ZDR_REFERENCE_SIGMA:g} dB systematic; Ryzhkov et al. 2005, Bringi and Chandrasekar 2001). Offset = median of 5-gate block means - reference. "
        f"Standard error = 1.2533 x 1.4826 MAD / sqrt(blocks); total 1-sigma adds the reference uncertainty. Refused below {ZDR_MIN_GATES} gates, "
        f"{ZDR_MIN_BLOCKS} blocks or {ZDR_MIN_RAYS} rays. S-band reference: at C/X band differential attenuation and resonance bias it."
    )
    if n_gates < ZDR_MIN_GATES or blocks.size < ZDR_MIN_BLOCKS or n_rays < ZDR_MIN_RAYS:
        return [
            Finding(
                id="zdr_offset",
                title="ZDR offset: not estimated",
                severity="info",
                summary=f"Only {n_gates} light-rain gates ({blocks.size} blocks, {n_rays} rays) qualify; at least {ZDR_MIN_GATES} gates, {ZDR_MIN_BLOCKS} blocks and "
                f"{ZDR_MIN_RAYS} rays are needed for an estimate, so none is given.",
                sweeps=sweeps,
                evidence=[
                    ev("qualifying gates", n_gates),
                    ev("independent blocks", int(blocks.size)),
                    ev("rays", n_rays),
                ],
                confidence=0.0,
                method=method,
            )
        ]
    med = float(np.median(blocks))
    sig = _mad_sigma(blocks)
    se = max(1.2533 * sig / math.sqrt(blocks.size), 0.01)
    tot = math.sqrt(se**2 + ZDR_REFERENCE_SIGMA**2)
    offset = med - ZDR_REFERENCE_DB
    a = abs(offset)
    if a >= 0.25 and a >= 2 * tot:
        sev = "warning"
    elif a >= 0.1 and a >= tot:
        sev = "notice"
    else:
        sev = "info"
    verdict = {
        "warning": "Subtract it before hydrometeor classification or rain rates.",
        "notice": "Small but may bias rain rates.",
        "info": "Consistent with no offset within the uncertainty.",
    }[sev]
    return [
        Finding(
            id="zdr_offset",
            title="ZDR calibration offset",
            severity=sev,
            summary=f"ZDR in light rain is {offset:+.2f} +- {tot:.2f} dB from the reference ({med:.2f} dB measured vs {ZDR_REFERENCE_DB:g} dB expected, "
            f"{n_gates} gates, {_sweeps_text(sweeps)}). {verdict}",
            sweeps=sweeps,
            where={"sweeps": sweeps, "range_km": list(ZDR_RANGE_KM)},
            evidence=[
                ev("ZDR offset", round(offset, 3), "dB", "ESTIMATE", "light-rain method"),
                ev(
                    "1-sigma uncertainty",
                    round(tot, 3),
                    "dB",
                    "ESTIMATE",
                    f"standard error {se:.3f} dB and reference +-{ZDR_REFERENCE_SIGMA:g} dB",
                ),
                ev("measured median ZDR", med, "dB"),
                ev("reference ZDR", ZDR_REFERENCE_DB, "dB", "ESTIMATE"),
                ev("qualifying gates", n_gates),
                ev("independent blocks", int(blocks.size)),
                ev("rays", n_rays),
                ev("spread (1.48 MAD of blocks)", sig, "dB"),
            ],
            confidence=min(0.8, 0.4 + 0.4 * min(1.0, blocks.size / 2000.0)),
            method=method,
            per_sweep=[
                {k: s[k] for k in ("sweep", "elevation", "n_gates") if k in s}
                for s in acc.zdr_sweeps
            ],
            advice=f"Correct ZDR by {-offset:+.2f} dB (Modules > ZDR calibration) before hydrometeor classification or rainfall."
            if sev != "info"
            else None,
        )
    ]


def _f_phidp(acc: _Acc, band: str | None, band_how: str) -> list[Finding]:
    out: list[Finding] = []
    recs = [p for p in acc.phidp if "sigma" in p]
    if not recs:
        return out
    worst = max(recs, key=lambda p: p["sigma"])
    sev = _sev_by(worst["sigma"], PHIDP_NOISE_NOTICE, PHIDP_NOISE_WARNING)
    sweeps = [p["sweep"] for p in recs]
    if sev:
        out.append(
            Finding(
                id="phidp_noise",
                title="Noisy PhiDP",
                severity=sev,
                summary=f"Sweep {worst['sweep']}: gate-to-gate PhiDP noise is about {worst['sigma']:.1f} deg in good rain gates (RhoHV >= {PHIDP_RHOHV_MIN:g}); "
                "KDP from the raw field will be too noisy to use.",
                sweeps=sweeps,
                evidence=[
                    ev(
                        "PhiDP noise",
                        worst["sigma"],
                        "deg",
                        "ESTIMATE",
                        f"thresholds: notice {PHIDP_NOISE_NOTICE:g}, warning {PHIDP_NOISE_WARNING:g} deg (rules of thumb)",
                    ),
                    ev("gate pairs used", worst["n_pairs"]),
                ],
                confidence=0.6,
                method="1.4826 x MAD of the wrapped gate-to-gate PhiDP differences / sqrt(2), in "
                "gates with RhoHV >= 0.9 and Z >= 15 dBZ (white-noise assumption; "
                "Hubbert and Bringi 1995). Typical good-rain values are a few degrees; thresholds "
                "here are rules of thumb.",
                advice="Filter PhiDP before KDP (Modules > KDP estimation).",
            )
        )
    wrap = [p for p in recs if p["share_rays_wrap"] >= PHIDP_WRAP_NOTICE_SHARE]
    if wrap:
        w = max(wrap, key=lambda p: p["share_rays_wrap"])
        out.append(
            Finding(
                id="phidp_unfold",
                title="PhiDP wraps around 360 deg",
                severity="notice",
                summary=f"Sweep {w['sweep']}: {_pct(w['share_rays_wrap'])} % of the rays jump by more than 180 deg between neighbouring gates; PhiDP needs unfolding before KDP.",
                sweeps=[p["sweep"] for p in wrap],
                where={**(w.get("where") or {}), "sweeps": [p["sweep"] for p in wrap]},
                evidence=[
                    ev("rays with a wrap", w["rays_with_wrap"]),
                    ev("share of rays", _pct(w["share_rays_wrap"]), "%"),
                    ev("wrapped gate pairs", w["n_wrap_pairs"]),
                ],
                confidence=0.8,
                method="Gate-to-gate PhiDP steps above 180 deg in gates with RhoHV >= 0.9 are "
                "360-degree wraps (a physical change that large between adjacent gates "
                "does not occur).",
                advice="Unfold PhiDP before computing KDP.",
            )
        )
    # attenuation hint
    dphi = [p for p in recs if p.get("dphi_p95") is not None]
    if dphi and band in ATTEN_PER_DEG:
        p = max(dphi, key=lambda q: q["dphi_p95"])
        gamma = ATTEN_PER_DEG[band]
        pia = gamma * max(0.0, p["dphi_p95"])
        sev = _sev_by(pia, ATTEN_NOTICE_DB, ATTEN_WARNING_DB)
        if sev:
            out.append(
                Finding(
                    id="attenuation",
                    title="Rain attenuation likely",
                    severity=sev,
                    summary=f"{band}-band ({band_how}): total PhiDP increase along rays reaches {p['dphi_p95']:.0f} deg (95th percentile), which implies roughly "
                    f"{pia:.0f} dB of two-way attenuation of reflectivity at the far end of those rays (sweep {p['sweep']}).",
                    sweeps=[q["sweep"] for q in dphi],
                    evidence=[
                        ev("PhiDP increase, 95th pct of rays", p["dphi_p95"], "deg"),
                        ev(
                            "path-integrated attenuation",
                            pia,
                            "dB",
                            "ESTIMATE",
                            f"{gamma:g} dB per degree at {band} band; factor-of-two uncertain",
                        ),
                        ev("radar band", band, "", "RETRIEVED", band_how),
                    ],
                    confidence=0.5,
                    method=f"Sum of unwrapped gate-to-gate PhiDP steps in rain gates (RhoHV >= 0.9, Z >= 15 dBZ) per ray, times {gamma:g} dB/deg "
                    "("
                    + (
                        "Bringi et al. 1990, Carey et al. 2000"
                        if band == "C"
                        else "Park et al. 2005"
                    )
                    + "). The ratio varies with drop size distribution and "
                    "temperature.",
                    advice="Apply an attenuation correction (ZPHI / PhiDP-based) before "
                    "quantitative use of reflectivity and ZDR.",
                )
            )
    return out


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------

_STEP_ORDER = (
    "snr_gate",
    "rhohv_gate",
    "echo_mask",
    "clutter_filter",
    "biological_filter",
    "dealias",
    "speckle",
)
_RECIPE_INFO = {
    "velocity": ("VRADH", "Doppler velocity: de-alias and clean"),
    "reflectivity": ("DBZH", "Reflectivity: clean"),
    "zdr": ("ZDR", "ZDR: clean"),
}
_STEP_WHY = {
    "snr_gate": "Remove noise-dominated gates first so the later steps see a cleaner field.",
    "rhohv_gate": "Drop gates whose correlation says they are not rain-like.",
    "clutter_filter": "Remove persistent ground echo before de-aliasing so it cannot anchor the "
    "unfolding.",
    "biological_filter": "Remove weak bird / insect echo that would otherwise pass as weather.",
    "dealias": "Unfold the aliased velocity (needs the clutter and bio filters first).",
    "speckle": "Drop isolated single gates left over.",
}


def _recommend_recipes(
    findings: list[Finding], roles: dict[str, list[int]]
) -> list[dict[str, Any]]:
    from engine import pipeline

    out: list[dict[str, Any]] = []
    for role, (target, name) in _RECIPE_INFO.items():
        if not roles.get(role):
            continue
        picked: dict[str, list[Finding]] = {}
        for f in findings:
            if f.severity == "info" or role not in f.targets:
                continue
            for s in f.steps:
                picked.setdefault(s, []).append(f)
        if role == "velocity" and "dealias" in picked and "clutter_filter" not in picked:
            pass  # de-aliasing alone is a valid recipe
        if not picked:
            continue
        steps = [s for s in _STEP_ORDER if s in picked]
        reasons = [
            {
                "step_id": s,
                "reason": f"{_STEP_WHY.get(s, '')} Because: {picked[s][0].summary}".strip(),
                "findings": [f.id for f in picked[s]],
            }
            for s in steps
        ]
        try:
            recipe = pipeline.parse_recipe(
                {
                    "name": f"Data check: {name}",
                    "description": "Suggested from the data check of this file: "
                    + ", ".join(sorted({f.title.lower() for fs in picked.values() for f in fs})),
                    "target": target,
                    "steps": [{"step_id": s, "params": {}} for s in steps],
                }
            )
        except Exception as exc:  # a catalog change must never break the report
            logger.warning("data-check recipe for %s is invalid: %s", role, exc)
            continue
        out.append(
            {"role": role, "recipe": recipe.to_dict(), "reasons": reasons, "sweeps": roles[role]}
        )
    return out


def _elev_key(e: float | None) -> float:
    return 99.0 if e is None else float(e)


def _first_view(acc: _Acc, sweeps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The sweep, variable and colour scale to look at first."""
    ppi = {g["sweep"]: g for g in acc.geometry}
    order = ["reflectivity", "velocity", "zdr", "rhohv", "phidp", "width", "snr"]
    for role in order:
        idx = [
            i for i in acc.roles.get(role, []) if ppi.get(i, {}).get("kind") == "ppi"
        ] or acc.roles.get(role, [])
        if not idx:
            continue
        sweep = min(
            idx,
            key=lambda i: (
                ppi.get(i, {}).get("elevation")
                if ppi.get(i, {}).get("elevation") is not None
                else 99,
                i,
            ),
        )
        u = next((r for r in acc.units.get(role, []) if r["sweep"] == sweep), None)
        view: dict[str, Any] = {
            "role": role,
            "variable": u["variable"] if u else _LEGACY.get(role, role),
            "sweep": sweep,
        }
        reason = f"the lowest sweep with {_NAMES[role]}"
        if role == "reflectivity" and u:
            lo = max(-30.0, math.floor(u["p001"] / 5.0) * 5.0)
            hi = min(85.0, math.ceil(u["p999"] / 5.0) * 5.0)
            view["scale"] = {
                "vmin": lo,
                "vmax": hi,
                "unit": "dBZ",
                "note": "0.1 to 99.9 percentile of the sweep, rounded to 5 dBZ",
            }
        elif role == "velocity":
            nyq = next((v["nyquist"] for v in acc.velocity if v["sweep"] == sweep), None)
            if nyq:
                view["scale"] = {
                    "vmin": -float(nyq),
                    "vmax": float(nyq),
                    "unit": "m/s",
                    "note": "symmetric about zero, +-Nyquist velocity (diverging colours)",
                }
        view["reason"] = reason
        view["also"] = []
        if role == "reflectivity":
            vi = list(acc.roles.get("velocity", []))
            if vi:
                s2 = min(vi, key=lambda i: (_elev_key(ppi.get(i, {}).get("elevation")), i))
                u2 = next((r for r in acc.units.get("velocity", []) if r["sweep"] == s2), None)
                view["also"].append(
                    {"role": "velocity", "variable": u2["variable"] if u2 else "VRADH", "sweep": s2}
                )
        return view
    return None


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _sweep_datasets(tree: xr.DataTree) -> list[tuple[int, xr.Dataset | None]]:
    from engine.reader import _is_placeholder, sorted_sweep_names

    out: list[tuple[int, xr.Dataset | None]] = []
    for i, name in enumerate(sorted_sweep_names(tree)):
        ds = tree[name].to_dataset()
        out.append((i, None if _is_placeholder(ds) else ds))
    return out


#: Pixel checks that only look at the low sweeps: (name, function, highest elevation, max sweeps)
_HEAVY = (
    ("noise", _noise_floor, MAX_HEAVY_ELEVATION, 2),
    ("clutter", _clutter, MAX_CLUTTER_ELEVATION, 3),
    ("blockage", _blockage, MAX_HEAVY_ELEVATION, 2),
    ("echo", _echo_classes, MAX_HEAVY_ELEVATION, 3),
    ("zdr", _zdr_samples, MAX_HEAVY_ELEVATION, 6),
    ("phidp", _phidp, MAX_HEAVY_ELEVATION, 3),
)


def check_tree(
    tree: xr.DataTree,
    *,
    path: str | None = None,
    fmt: str | None = None,
    overrides: Mapping[str, str] | None = None,
    budget_s: float = BUDGET_S,
) -> dict[str, Any]:
    """Run every check on an opened volume and return the report (JSON-serialisable dict)."""
    t0 = time.perf_counter()
    acc = _Acc()
    sweeps = _sweep_datasets(tree)
    n_heavy: dict[str, int] = {}
    first_ds: xr.Dataset | None = None
    skipped_budget: list[int] = []
    skipped_arco = [i for i, ds in sweeps if ds is None]
    if skipped_arco:
        acc.not_checked.append(
            f"sweeps {skipped_arco[:8]} are not loaded yet (remote data is never fetched by the check)"
        )
    modes_known = False

    def run(fn, *a) -> None:
        try:
            fn(*a)
        except Exception as exc:  # one failing check must not hide the others
            logger.warning(
                "data check %s failed: %s", getattr(fn, "__name__", fn), exc, exc_info=True
            )
            acc.errors.append(f"{getattr(fn, '__name__', 'check')}: {type(exc).__name__}: {exc}")

    with warnings.catch_warnings(), np.errstate(all="ignore"):
        warnings.simplefilter("ignore")
        for i, ds in sweeps:
            if ds is None:
                continue
            if first_ds is None:
                first_ds = ds
            sw = _Sweep(i, ds, overrides)
            modes_known = modes_known or "sweep_mode" in ds.variables
            if sw.n_rays == 0 or sw.n_gates == 0:
                acc.geometry.append(
                    {
                        "sweep": i,
                        "elevation": sw.elev,
                        "kind": sw.kind,
                        "n_rays": sw.n_rays,
                        "n_gates": sw.n_gates,
                    }
                )
                acc.empty.append(
                    {"sweep": i, "role": "any", "variable": "(sweep has no rays or gates)"}
                )
                continue
            run(_geometry, sw, acc)
            run(_timing, sw, acc)
            over = (time.perf_counter() - t0) > budget_s
            if over:
                skipped_budget.append(i)
                sw.release()
                continue
            run(_field_stats, sw, acc)
            run(_range_folding, sw, acc)
            run(_velocity, sw, acc)
            low = sw.kind == "ppi" and sw.elev is not None and sw.elev <= MAX_HEAVY_ELEVATION
            if low:
                for name, fn, max_elev, cap in _HEAVY:
                    if sw.elev <= max_elev and n_heavy.get(name, 0) < cap:
                        n_heavy[name] = n_heavy.get(name, 0) + 1
                        run(fn, sw, acc)
            sw.release()
    if skipped_budget:
        acc.not_checked.append(
            f"sweeps {skipped_budget[:8]}: the time budget ({budget_s:g} s) ran out before their pixel checks"
        )

    n_sweeps = len(sweeps)
    band, band_how = radar_band(tree, first_ds, fmt)
    findings: list[Finding] = []
    for build in (
        lambda: _f_scan_type(acc, {"modes_known": modes_known}),
        lambda: _f_timing(acc),
        lambda: _f_fields(acc, n_sweeps),
        lambda: _f_units(acc),
        lambda: _f_velocity(acc),
        lambda: _f_folding(acc),
        lambda: _f_noise(acc),
        lambda: _f_clutter(acc),
        lambda: _f_blockage(acc),
        lambda: _f_bio(acc),
        lambda: _f_speckle(acc),
        lambda: _f_snr(acc),
        lambda: _f_rho(acc),
        lambda: _f_zdr(acc),
        lambda: _f_phidp(acc, band, band_how),
    ):
        try:
            findings.extend(build())
        except Exception as exc:
            logger.warning("data check report step failed: %s", exc, exc_info=True)
            acc.errors.append(f"report: {type(exc).__name__}: {exc}")
    findings.sort(key=lambda f: (-_RANK[f.severity], -f.confidence, f.id))

    # what could not be assessed (stated, never guessed)
    present = set(acc.roles)
    if "velocity" in present and not acc.fold:
        acc.not_checked.append("range folding: this file has no Level II style range-folded code")
    if "rhohv" not in present and "zdr" not in present:
        acc.not_checked.append("biological echo and ZDR offset: need RhoHV / ZDR")
    elif not acc.zdr_sweeps and {"zdr", "rhohv", "reflectivity"} <= present:
        acc.not_checked.append(
            "ZDR offset: no sweep at low elevation with ZDR, RhoHV and reflectivity"
        )
    if band is None:
        acc.not_checked.append("attenuation: the radar band is not stated in the file")
    elif band == "S":
        acc.not_checked.append("attenuation: S band, negligible in rain, not assessed")

    counts = {s: sum(f.severity == s for f in findings) for s in SEVERITIES}
    advice = [
        {"id": f.id, "text": f.advice, "severity": f.severity}
        for f in findings
        if f.advice and f.severity != "info"
    ]
    report = {
        "version": REPORT_VERSION,
        "file": path,
        "format": fmt,
        "n_sweeps": n_sweeps,
        "band": band,
        "counts": counts,
        "findings": [f.to_dict() for f in findings],
        "recommendation": {
            "recipes": _recommend_recipes(findings, acc.roles),
            "first_view": _first_view(acc, []),
            "advice": advice,
        },
        "not_checked": acc.not_checked,
        "errors": acc.errors,
        "elapsed_ms": round(1000.0 * (time.perf_counter() - t0), 1),
        "note": "Nothing was changed. RETRIEVED = measured in the data; ESTIMATE = model-based, "
        "see each finding's method.",
    }
    return report


def check_reader(
    reader: Any, *, overrides: Mapping[str, str] | None = None, budget_s: float = BUDGET_S
) -> dict[str, Any]:
    """:func:`check_tree` on an opened :class:`engine.reader.RadarReader`."""
    tree = reader.datatree
    if tree is None:
        raise RuntimeError("No file is open")
    return check_tree(
        tree,
        path=getattr(reader, "_path", None),
        fmt=getattr(reader, "_format", None),
        overrides=overrides,
        budget_s=budget_s,
    )


def check_volume(vol: Any, *, budget_s: float = BUDGET_S) -> dict[str, Any]:
    """:func:`check_tree` on an :class:`engine.api.Volume` (uses its variable mapping)."""
    return check_reader(vol._reader, overrides=vol.mapping, budget_s=budget_s)

"""Range-height indicator (RHI) support: detection, ray ordering and beam geometry.

An RHI sweep keeps the azimuth fixed and moves the antenna in elevation, so its rays differ in
*elevation* rather than azimuth. Drawn as a PPI it collapses onto one line. This module

* decides whether a sweep dataset is an RHI (:func:`is_rhi`), reliably: the file's ``sweep_mode``
  wins, and only when it is missing or unknown a constant-azimuth / varying-elevation heuristic
  is used;
* describes it for the file schema (:func:`describe_sweep`: azimuth, elevation span, ray count);
* orders its rays by elevation and drops duplicated elevations (:func:`rhi_rays`). Real RHI files
  are often not stored in elevation order (CPOL stitches several passes; NPOL interleaves them) and
  pseudo-RHIs cut from a volume carry one ray per volume tilt, sometimes two at nearly the same
  elevation (split cuts);
* converts (slant range, elevation) to (ground range, height) with the standard 4/3 effective Earth
  radius beam model (:func:`beam_geometry`), the same model as ``engine.volume`` (not imported
  from there: ``engine.reader`` imports this module).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import numpy as np
import xarray as xr

from engine import valuemask

#: Effective Earth radius of the standard 4/3 beam model (same value as ``engine.volume``), metres.
EFFECTIVE_RADIUS_M: float = 6_371_000.0 * 4.0 / 3.0

#: ``sweep_mode`` values (normalised: lower case, ``_`` separated) that mean an RHI scan.
RHI_MODES = frozenset({"rhi", "manual_rhi", "sector_rhi", "elevation_surveillance", "sunscan_rhi"})
#: ``sweep_mode`` values that are known *not* to be an RHI (the heuristic is skipped for them).
KNOWN_NON_RHI_MODES = frozenset(
    {
        "azimuth_surveillance",
        "ppi",
        "sector",
        "coplane",
        "manual_ppi",
        "sunscan",
        "pointing",
        "vertical_pointing",
        "idle",
        "calibration",
    }
)

#: Rays closer than this in elevation (deg) are the same elevation: only one is kept.
ELEVATION_TIE_DEG = 0.03
# Heuristic (only when the file states no usable sweep_mode): at least this many rays ...
_MIN_RAYS = 5
# ... that sweep through at least this much elevation ...
_MIN_ELEV_SPAN_DEG = 3.0
# ... while the azimuth barely moves.
_MAX_AZ_SPAN_DEG = 1.5


@dataclass(frozen=True)
class RhiRays:
    """The rays of an RHI sweep in display order (ascending elevation)."""

    #: Indices into the sweep's ray axis, one per kept ray.
    order: np.ndarray
    #: Elevation of every kept ray, degrees, ascending.
    elevation: np.ndarray
    #: Azimuth of every kept ray, degrees.
    azimuth: np.ndarray
    #: The azimuth plane of the RHI, degrees in [0, 360).
    plane_azimuth: float


def _wrap180(d: np.ndarray | float) -> np.ndarray | float:
    return (np.asarray(d, dtype=np.float64) + 180.0) % 360.0 - 180.0


def circular_span(azimuth: np.ndarray) -> float:
    """Smallest arc (degrees) that contains every azimuth (0 for a constant azimuth)."""
    az = np.sort(np.mod(np.asarray(azimuth, dtype=np.float64), 360.0))
    az = az[np.isfinite(az)]
    if az.size < 2:
        return 0.0
    gaps = np.diff(np.concatenate([az, [az[0] + 360.0]]))
    return float(360.0 - gaps.max())


def circular_median(azimuth: np.ndarray) -> float:
    """Median azimuth (degrees, [0, 360)) that is safe across the 0/360 seam."""
    az = np.asarray(azimuth, dtype=np.float64)
    az = az[np.isfinite(az)]
    if az.size == 0:
        return 0.0
    ref = float(np.arctan2(np.sin(np.deg2rad(az)).mean(), np.cos(np.deg2rad(az)).mean()))
    ref = np.rad2deg(ref)
    return float((ref + np.median(_wrap180(az - ref))) % 360.0)


def sweep_mode_of(ds: xr.Dataset) -> str | None:
    """The sweep's ``sweep_mode`` as a normalised string (``"rhi"``), or ``None`` if not stated."""
    raw: Any = None
    if "sweep_mode" in ds.variables:
        raw = ds.variables["sweep_mode"].values
    elif "sweep_mode" in ds.attrs:
        raw = ds.attrs["sweep_mode"]
    if raw is None:
        return None
    try:
        arr = np.asarray(raw)
        if arr.size == 0:
            return None
        raw = arr.flat[0]
    except Exception:  # pragma: no cover - exotic dtype
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    return text or None


def _ray_angles(ds: xr.Dataset) -> tuple[np.ndarray, np.ndarray] | None:
    """Per-ray ``(azimuth, elevation)`` as float64 arrays of equal length, or ``None``."""
    variables = ds.variables
    if "azimuth" not in variables or "elevation" not in variables:
        return None
    az = np.asarray(variables["azimuth"].values, dtype=np.float64).ravel()
    el = np.asarray(variables["elevation"].values, dtype=np.float64).ravel()
    if az.size == 0 or az.size != el.size:
        return None
    return az, el


def is_rhi(ds: xr.Dataset) -> bool:
    """True when *ds* (one sweep) is a range-height scan."""
    mode = sweep_mode_of(ds)
    if mode in RHI_MODES:
        return True
    if mode in KNOWN_NON_RHI_MODES:
        return False
    angles = _ray_angles(ds)
    if angles is None:
        return False
    az, el = angles
    ok = np.isfinite(az) & np.isfinite(el)
    if int(ok.sum()) < _MIN_RAYS:
        return False
    az_span = circular_span(az[ok])
    el_span = float(np.ptp(el[ok]))
    return az_span <= _MAX_AZ_SPAN_DEG and el_span >= _MIN_ELEV_SPAN_DEG and el_span > 3.0 * az_span


def plane_azimuth(ds: xr.Dataset, azimuth: np.ndarray) -> float:
    """Azimuth of the RHI plane: the sweep's fixed angle when it lies among the rays' azimuths, else
    their median.

    CfRadial stores the target azimuth as ``sweep_fixed_angle`` (CPOL: 135 = the ray azimuth). Some
    writers put an elevation there instead (NPOL: 29.9 for a plane at 243), which would be nonsense
    as an azimuth, so it is only trusted when the rays actually point there.
    """
    az = np.asarray(azimuth, dtype=np.float64)
    az = az[np.isfinite(az)]
    if az.size == 0:
        return 0.0
    fixed: float | None = None
    if "sweep_fixed_angle" in ds.attrs:
        fixed = float(ds.attrs["sweep_fixed_angle"])
    elif "sweep_fixed_angle" in ds.variables:
        fixed = float(np.asarray(ds.variables["sweep_fixed_angle"].values).flat[0])
    if fixed is not None and np.isfinite(fixed):
        median = circular_median(az)
        spread = float(np.abs(_wrap180(az - median)).max())
        if abs(float(_wrap180(fixed - median))) <= spread + 0.1:
            return fixed % 360.0
    return circular_median(az)


def rhi_rays(ds: xr.Dataset) -> RhiRays | None:
    """Display order of the rays of an RHI sweep, or ``None`` when *ds* is not an RHI.

    Rays are sorted by ascending elevation; rays without a finite angle are dropped; rays whose
    elevations are within :data:`ELEVATION_TIE_DEG` of each other collapse to the one pointing
    closest to the plane azimuth.
    """
    if not is_rhi(ds):
        return None
    angles = _ray_angles(ds)
    if angles is None:
        return None
    az, el = angles
    good = np.flatnonzero(np.isfinite(az) & np.isfinite(el))
    if good.size == 0:
        return None
    plane = plane_azimuth(ds, az[good])
    off = np.abs(_wrap180(az[good] - plane))
    # Ascending elevation; among equals the ray nearest the plane first.
    order = good[np.lexsort((off, el[good]))]
    keep = [int(order[0])]
    for idx in order[1:]:
        if el[idx] - el[keep[-1]] > ELEVATION_TIE_DEG:
            keep.append(int(idx))
    sel = np.asarray(keep, dtype=np.intp)
    return RhiRays(
        order=sel,
        elevation=el[sel],
        azimuth=az[sel] % 360.0,
        plane_azimuth=float(plane),
    )


def describe_sweep(ds: xr.Dataset) -> dict[str, Any]:
    """Schema fields of one sweep: ``{"kind": "ppi"}`` or the RHI plane and elevation span."""
    try:
        rays = rhi_rays(ds)
    except Exception:
        rays = None
    if rays is None:
        return {"kind": "ppi"}
    return {
        "kind": "rhi",
        "azimuth": round(rays.plane_azimuth, 3),
        "elevation_min": round(float(rays.elevation[0]), 3),
        "elevation_max": round(float(rays.elevation[-1]), 3),
        "n_rays": int(rays.order.size),
    }


def beam_geometry(
    range_m: np.ndarray | float,
    elevation_deg: np.ndarray | float,
    radar_height_m: float = 0.0,
    re: float = EFFECTIVE_RADIUS_M,
) -> tuple[np.ndarray, np.ndarray]:
    """``(ground_range_m, height_m)`` of beam-centre points (4/3 effective Earth radius).

    *height_m* is above sea level when *radar_height_m* is the site altitude, above the radar for 0.
    """
    r = np.asarray(range_m, dtype=np.float64)
    el = np.deg2rad(np.asarray(elevation_deg, dtype=np.float64))
    height = np.sqrt(r * r + re * re + 2.0 * r * re * np.sin(el)) - re
    ground = re * np.arcsin(np.clip(r * np.cos(el) / (re + height), -1.0, 1.0))
    return ground, height + radar_height_m


def ray_bounds(angles: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Lower and upper elevation bound of every ray's cell, for ascending *angles*.

    Same rule as the viewer (``rhiGeometry.ts``): a cell reaches half-way to each neighbour; a gap
    wider than ``max(1.5 steps, min(10 deg, 8 steps))`` (a lost block of rays) is not bridged and
    each side gets half a typical step, so the missing angles stay empty.
    """
    a = np.asarray(angles, dtype=np.float64)
    n = a.size
    if n == 0:
        return np.zeros(0), np.zeros(0)
    if n == 1:
        return a - 0.5, a + 0.5
    d = np.diff(a)
    pos = d[d > 0]
    step = float(np.median(pos)) if pos.size else 1.0
    limit = max(1.5 * step, min(10.0, 8.0 * step))
    bridged = d <= limit
    half_up = np.where(bridged, d / 2.0, step / 2.0)  # reach towards the next ray, per ray 0..n-2
    lo = np.empty(n)
    hi = np.empty(n)
    hi[:-1] = a[:-1] + half_up
    hi[-1] = a[-1] + step / 2.0
    lo[1:] = a[1:] - half_up
    lo[0] = a[0] - step / 2.0
    return lo, hi


def contiguous_mesh(angles: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(edges, values)`` for ``pcolormesh``: ``len(edges) == len(values) + 1``.

    *values* is ``(n_rays, n_gates)`` in ascending-angle order. Holes between rays that are not
    bridged get an all-NaN row, so they are left blank instead of smeared.
    """
    lo, hi = ray_bounds(angles)
    n = lo.size
    if n == 0:
        return np.zeros(1), values
    edges = [lo[0]]
    rows: list[np.ndarray] = []
    nan_row = np.full(values.shape[1:], np.nan, dtype=values.dtype)
    for i in range(n):
        if i > 0 and lo[i] > hi[i - 1] + 1e-9:
            rows.append(nan_row)  # blank row from the previous ray's upper bound to this lower one
            edges.append(lo[i])
        rows.append(values[i])
        edges.append(hi[i])
    return np.asarray(edges), np.asarray(rows)


# ---------------------------------------------------------------------------
# Noise cleaning (the same rules as the viewer's "Noise & smoothing" panel)
# ---------------------------------------------------------------------------

_QUALITY_PATTERNS: dict[str, re.Pattern[str]] = {
    "sqi": re.compile(r"^(sqi|ncp|signal_?quality|normalized_coherent_power)", re.I),
    "snr": re.compile(r"^(snr|signal_to_noise)", re.I),
    "rhohv": re.compile(r"(^|_)rhohv|^rho_?hv|cross_correlation|correlation_coeff", re.I),
}
_QUALITY_UNIT = {"sqi": "", "snr": " dB", "rhohv": ""}


@dataclass(frozen=True)
class RhiClean:
    """What is hidden from an RHI figure: ``None`` / ``0`` switch a step off, ``raw`` all."""

    raw: bool = False
    sqi: float | None = 0.4
    snr: float | None = 3.0
    rhohv: float | None = 0.7
    despeckle: int = 8
    mask: dict[str, Any] | None = None


def parse_clean(options: Any) -> RhiClean:
    """``RhiClean`` from an export option: ``None`` = defaults, ``False`` / ``"raw"`` = raw data,
    or a dict with any of ``raw``, ``sqi``, ``snr``, ``rhohv``, ``despeckle`` and ``mask``."""
    if options is None or options is True:
        return RhiClean()
    if options is False or options == "raw":
        return RhiClean(raw=True)
    if not isinstance(options, dict):
        raise ValueError("rhi_clean must be a dict, a boolean or 'raw'")
    d = RhiClean()

    def limit(key: str, default: float | None) -> float | None:
        if key not in options:
            return default
        v = options[key]
        if v is None or v is False:
            return None
        return float(v)

    return RhiClean(
        raw=bool(options.get("raw", False)),
        sqi=limit("sqi", d.sqi),
        snr=limit("snr", d.snr),
        rhohv=limit("rhohv", d.rhohv),
        despeckle=int(options.get("despeckle", d.despeckle) or 0),
        mask=options.get("mask") if isinstance(options.get("mask"), dict) else None,
    )


def find_quality_fields(names: list[str], exclude: str = "") -> dict[str, str]:
    """The variable to use for each quality kind (``sqi``, ``snr``, ``rhohv``): first by name."""
    out: dict[str, str] = {}
    for name in names:
        if name == exclude:
            continue
        for kind, pattern in _QUALITY_PATTERNS.items():
            if kind not in out and pattern.search(name):
                out[kind] = name
                break
    return out


def _mask_hidden(values: np.ndarray, mask: dict[str, Any]) -> np.ndarray | None:
    """Boolean array of the values a value mask hides (the rules live in :mod:`engine.valuemask`)."""
    return valuemask.hidden(values, mask)


def _fmt(v: float) -> str:
    return f"{v:g}"


def clean_values(
    values: np.ndarray,
    quality: dict[str, tuple[str, np.ndarray]],
    spec: RhiClean,
    *,
    units: str = "",
) -> tuple[np.ndarray, str]:
    """Hide noise in an RHI sweep. Returns ``(values with NaN where hidden, caption)``.

    *values* and the quality arrays are ``(n_rays, n_gates)``; *quality* maps ``"sqi"`` / ``"snr"``
    / ``"rhohv"`` to ``(variable name, array)``. Steps: quality masks, the value mask, then
    despeckle
    (8-connected groups of fewer than ``spec.despeckle`` gates are removed).
    """
    out = np.array(values, dtype=np.float64, copy=True)
    before = int(np.isfinite(out).sum())
    if spec.raw:
        return out, "Raw data: no masking or despeckling."
    applied: list[str] = []
    missing: list[str] = []
    n_quality = 0
    for kind in ("sqi", "snr", "rhohv"):
        limit = getattr(spec, kind)
        if limit is None:
            continue
        found = quality.get(kind)
        if found is None:
            missing.append(kind.upper())
            continue
        name, q = found
        with np.errstate(invalid="ignore"):
            out[np.isfinite(q) & (q < limit)] = np.nan
        applied.append(f"{name} \u2265 {_fmt(limit)}{_QUALITY_UNIT[kind]}")
        n_quality += 1
    if spec.mask:
        hidden = _mask_hidden(out, spec.mask)
        if hidden is not None:
            out[hidden] = np.nan
            applied.append(f"value mask ({spec.mask.get('mode')} {units})".strip())
    if spec.despeckle and spec.despeckle > 1:
        from scipy import ndimage

        valid = np.isfinite(out)
        labels, n = ndimage.label(valid, structure=np.ones((3, 3), dtype=int))
        if n:
            sizes = np.bincount(labels.ravel())
            small = sizes < spec.despeckle
            small[0] = False
            out[small[labels]] = np.nan
        applied.append(f"despeckled (< {spec.despeckle} gates)")
    after = int(np.isfinite(out).sum())
    parts: list[str] = []
    if applied:
        text = "Cleaned: " + ", ".join(applied)
        pct = round(100 * (before - after) / before) if before else 0
        if pct > 0:
            text += f" (hid {pct}% of gates)"
        parts.append(text)
    if n_quality == 0 and missing:
        parts.append(f"no {'/'.join(missing)} field in this sweep")
    return out, "  \u00b7  ".join(parts) if parts else "No masking applied."

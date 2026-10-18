"""Universal variable mapping: which file variable plays which radar *role*.

Radar files name the same physical field differently (``DBZH``, ``DBZ``, ``reflectivity``,
``corrected_reflectivity_horizontal``, ``DBZHCC`` ...). This module is the single source of
truth for that: one registry of field **roles** (reflectivity, zdr, phidp, kdp, rhohv,
velocity, width, snr, ncp), each with CF ``standard_name`` s, accepted unit classes, name
aliases and regex patterns, plus helpers to resolve a role on a sweep and across sweeps.

Resolution order for a role on one sweep dataset (first hit wins; confidence in brackets):

1. **override** - the user's mapping for this file (1.0)
2. **cf** - ``standard_name`` (+ compatible ``units``) match (0.95)
3. **alias** - exact variable name from the role's alias list (0.9)
4. **pattern** - regular expression on the variable name (0.6)

Inside steps 2-4 raw names are tried before clutter-corrected / qc'd ones (``DBZHCC``,
``ZDRC``, dealiased velocity ...); the corrected variants are only a fallback, unless
``prefer_corrected=True`` is passed. Flag / mask companions (``DBZHC_F``, ``*_FLAG``) never
match a pattern.

Cross-sweep: NEXRAD split cuts keep velocity on a second sweep at the same elevation.
:func:`resolve_in_tree` can look for the role on the sweep with the nearest fixed angle
(within a tolerance, 0.3 deg by default) and :func:`align_to_sweep` resamples that field onto
the requested sweep's ``(azimuth, range)`` grid (nearest ray, nearest gate).

This file imports nothing from the rest of the engine (``engine.plugins`` imports *it*).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

logger = logging.getLogger(__name__)

DEFAULT_TOLERANCE_DEG = 0.3

# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

# Free-text ``units`` attribute -> coarse unit class. Only a *known, different* class vetoes a
# CF match; empty / unknown units are accepted (files are sloppy about them).
_UNIT_CLASS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dbz", re.compile(r"^dbz$")),
    ("db", re.compile(r"^db$")),
    ("deg/km", re.compile(r"^(?:deg(?:rees?)?|°)(?:/km|km-1|\.km-1|perkm)$")),
    ("deg", re.compile(r"^(?:deg(?:rees?)?(?:_?[a-z]*)?|°)$")),
    ("speed", re.compile(r"^(?:m/s|ms-1|m\.s-1|m\*s-1|mps|meters?per(?:second|seconds|sec))$")),
    ("none", re.compile(r"^(?:1|-|unitless|none|ratio|dimensionless|%)$")),
)


def unit_class(units: object) -> str | None:
    """Coarse class (``dbz``, ``db``, ``deg``, ``deg/km``, ``speed``, ``none``) or ``None``."""
    text = re.sub(r"\s+", "", str(units or "")).lower().replace("_", "")
    if not text:
        return None  # no units given: unknown, never a veto
    for cls, pat in _UNIT_CLASS_PATTERNS:
        if pat.match(text):
            return cls
    return None


# ---------------------------------------------------------------------------
# Role registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Role:
    """One physical field a module can ask for."""

    key: str  # "velocity"
    legacy: str  # canonical legacy name modules use: "VRADH"
    label: str  # "velocity"
    standard_names: tuple[str, ...]
    units: tuple[str, ...]  # accepted unit classes (see :func:`unit_class`)
    aliases: tuple[str, ...]  # raw names, in preference order
    corrected_aliases: tuple[str, ...] = ()  # clutter-corrected / qc'd / dealiased names
    patterns: tuple[str, ...] = ()  # full-match, case-insensitive
    corrected_patterns: tuple[str, ...] = ()

    @property
    def all_aliases(self) -> tuple[str, ...]:
        return self.aliases + self.corrected_aliases

    def is_corrected(self, name: str) -> bool:
        low = name.lower()
        if any(low == a.lower() for a in self.corrected_aliases):
            return True
        return any(re.fullmatch(p, name, re.IGNORECASE) for p in self.corrected_patterns)


ROLES: dict[str, Role] = {
    r.key: r
    for r in (
        Role(
            "reflectivity",
            "DBZH",
            "reflectivity",
            standard_names=(
                "radar_equivalent_reflectivity_factor_h",
                "equivalent_reflectivity_factor",
                "radar_equivalent_reflectivity_factor",
            ),
            units=("dbz", "db"),
            aliases=(
                "DBZH",
                "DBZ",
                "reflectivity",
                "Z",
                "corrected_reflectivity_horizontal",
                "TH",
            ),
            corrected_aliases=("DBZHCC", "DBZHC"),
            patterns=(
                r"dbz_?h?",
                r"zh",
                r"(?:radar_)?(?:equivalent_)?reflectivity(?:_factor)?(?:_h+)?",
            ),
            corrected_patterns=(
                r"dbz_?h?c+",
                r"dbz_?h?_?(?:corr\w*|qc\w*|clean\w*|filt\w*)",
                r"(?:corrected|cleaned|qc|clutter_?corrected)_(?:radar_)?"
                r"(?:equivalent_)?reflectivity\w*",
            ),
        ),
        Role(
            "zdr",
            "ZDR",
            "differential reflectivity (ZDR)",
            standard_names=("radar_differential_reflectivity_hv",),
            units=("db",),
            aliases=("ZDR", "differential_reflectivity", "Zdr"),
            corrected_aliases=("ZDRC",),
            patterns=(r"zdr_?[a-z]?", r"differential_reflectivity\w*"),
            corrected_patterns=(r"zdr_?(?:corr\w*|qc\w*|clean\w*|filt\w*|cal\w*)",),
        ),
        Role(
            "phidp",
            "PHIDP",
            "differential phase (PHIDP)",
            standard_names=("radar_differential_phase_hv",),
            units=("deg",),
            aliases=("PHIDP", "differential_phase", "PhiDP", "UPHIDP"),
            patterns=(r"phi_?dp_?[a-z]?", r"differential_phase\w*"),
            corrected_patterns=(r"phi_?dp_?(?:corr\w*|filt\w*|smooth\w*|unfold\w*|f)",),
        ),
        Role(
            "kdp",
            "KDP",
            "specific differential phase (KDP)",
            standard_names=("radar_specific_differential_phase_hv",),
            units=("deg/km",),
            aliases=("KDP", "specific_differential_phase"),
            patterns=(r"kdp_?[a-z]?", r"specific_differential_phase\w*"),
            corrected_patterns=(r"kdp_?(?:corr\w*|filt\w*|smooth\w*)",),
        ),
        Role(
            "rhohv",
            "RHOHV",
            "cross-correlation (RHOHV)",
            standard_names=("radar_correlation_coefficient_hv",),
            units=("none",),
            aliases=("RHOHV", "cross_correlation_ratio", "RhoHV", "CCORH"),
            corrected_aliases=("RHOHVC", "RHOHV_NC"),
            patterns=(r"rho_?hv_?[a-z]?", r"(?:co|cross_?)correlation\w*"),
            corrected_patterns=(r"rho_?hv_?(?:c|corr\w*|nc|noise\w*|filt\w*)",),
        ),
        Role(
            "velocity",
            "VRADH",
            "velocity",
            standard_names=(
                "radial_velocity_of_scatterers_away_from_instrument_h",
                "radial_velocity_of_scatterers_away_from_instrument",
                "radial_velocity",
            ),
            units=("speed",),
            aliases=("VRADH", "velocity", "V", "radial_velocity", "VEL", "VRAD", "VELH"),
            corrected_aliases=("VRADDH", "corrected_velocity", "dealiased_velocity", "VELC"),
            patterns=(
                r"vrad_?h?",
                r"vel(?:ocity)?_?h?",
                r"(?:radial|doppler)_velocity(?:_h)?",
            ),
            corrected_patterns=(
                r"vradd\w*",
                r"(?:corrected|dealiased|unfolded)_(?:radial_|doppler_)?(?:vel\w*|vrad\w*)",
                r"vel(?:ocity)?_?(?:corr\w*|dealias\w*|unfold\w*)",
            ),
        ),
        Role(
            "width",
            "WRADH",
            "spectrum width",
            standard_names=(
                "radar_doppler_spectrum_width_h",
                "radar_doppler_spectrum_width",
                "doppler_spectrum_width",
            ),
            units=("speed",),
            aliases=("WRADH", "WIDTH", "spectrum_width", "WRAD"),
            patterns=(r"wrad_?h?", r"width", r"(?:doppler_)?spectrum_width\w*"),
        ),
        Role(
            "snr",
            "SNR",
            "signal-to-noise ratio",
            standard_names=(
                "signal_to_noise_ratio",
                "radar_signal_to_noise_ratio_h",
                "radar_signal_to_noise_ratio",
            ),
            units=("db",),
            aliases=("SNR", "SNRH", "SNRHC", "SNRV", "SNRVC", "snr"),
            patterns=(r"snr_?[hv]?c*", r"signal_to_noise\w*"),
        ),
        Role(
            "ncp",
            "NCP",
            "coherent power (NCP / SQI)",
            standard_names=(
                "normalized_coherent_power",
                "radar_normalized_coherent_power",
                "signal_quality_index",
            ),
            units=("none",),
            aliases=("NCP", "normalized_coherent_power", "SQI", "SQIH"),
            patterns=(r"ncp_?[a-z]?", r"sqi_?[hv]?", r"signal_quality\w*"),
        ),
    )
}

# Flag / mask companions are never a field (``DBZHC_F`` is a flag-filtered copy, not the moment).
_NOT_A_FIELD = re.compile(r"(?:_F|_FLAGS?|_MASK|_QC|_COUNT)$", re.IGNORECASE)

# Name -> role key. Case-sensitive first, lower-case as a fallback ("dbzh" -> reflectivity).
_NAME_TO_ROLE: dict[str, str] = {}
_LOWER_TO_ROLE: dict[str, str] = {}
_STD_TO_ROLE: dict[str, str] = {}
for _r in ROLES.values():
    for _n in (_r.key, _r.legacy, *_r.all_aliases):
        _NAME_TO_ROLE.setdefault(_n, _r.key)
        _LOWER_TO_ROLE.setdefault(_n.lower(), _r.key)
    for _s in _r.standard_names:
        _STD_TO_ROLE.setdefault(_s, _r.key)


def role_for_name(name: str) -> Role | None:
    """The role a role key, legacy name (``VRADH``) or known alias (``VEL``) stands for."""
    if not isinstance(name, str):
        return None
    key = _NAME_TO_ROLE.get(name) or _LOWER_TO_ROLE.get(name.lower())
    return ROLES[key] if key else None


def legacy_alias_table() -> dict[str, list[str]]:
    """``{"DBZH": [aliases...]}``: the deprecated ``VAR_ALIASES`` shape (kept for old imports)."""
    return {
        r.legacy: [r.legacy, *[a for a in r.all_aliases if a != r.legacy]] for r in ROLES.values()
    }


def describe_roles() -> list[dict[str, Any]]:
    """JSON-safe registry description (for the UI)."""
    return [
        {
            "role": r.key,
            "legacy": r.legacy,
            "label": r.label,
            "standard_names": list(r.standard_names),
            "units": list(r.units),
            "aliases": list(r.aliases),
            "corrected_aliases": list(r.corrected_aliases),
        }
        for r in ROLES.values()
    ]


# ---------------------------------------------------------------------------
# Resolution on one dataset
# ---------------------------------------------------------------------------

KIND_CONFIDENCE = {"override": 1.0, "cf": 0.95, "exact": 0.95, "alias": 0.9, "pattern": 0.6}


@dataclass
class Match:
    """Outcome of a successful resolution."""

    role: str | None  # role key; None when an unregistered variable name matched itself
    variable: str
    kind: str  # override | cf | exact | alias | pattern
    confidence: float
    sweep: int | None = None  # set (!= requested sweep) when taken from a paired sweep
    corrected: bool = False  # a clutter-corrected / qc'd variant was used
    angle_diff: float | None = None  # |fixed-angle difference| to the paired sweep, degrees

    @property
    def paired(self) -> bool:
        return self.sweep is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "variable": self.variable,
            "kind": self.kind,
            "confidence": self.confidence,
            "sweep": self.sweep,
            "corrected": self.corrected,
            "angle_diff": self.angle_diff,
        }


class VarMapError(Exception):
    """Bad variable-mapping input (unknown role, malformed mapping)."""


def normalize_overrides(mapping: Mapping[str, Any] | None) -> dict[str, str]:
    """``{role-or-legacy-name: variable}`` -> ``{role_key: variable}``; blanks are dropped.

    Raises :class:`VarMapError` for an unknown role.
    """
    out: dict[str, str] = {}
    for name, var in (mapping or {}).items():
        role = role_for_name(str(name))
        if role is None:
            raise VarMapError(f"Unknown role '{name}'. Roles: {', '.join(ROLES)}")
        if var is None or str(var).strip() in ("", "auto"):
            continue
        out[role.key] = str(var)
    return out


def _is_field(da: xr.DataArray) -> bool:
    return da.ndim >= 2


def _veto(da: xr.DataArray, role: Role) -> bool:
    """True if the variable's own ``standard_name`` says it is a *different* known role."""
    std = da.attrs.get("standard_name")
    other = _STD_TO_ROLE.get(std) if isinstance(std, str) else None
    return other is not None and other != role.key


def _cf_ok(da: xr.DataArray, role: Role) -> bool:
    std = da.attrs.get("standard_name")
    if std not in role.standard_names:
        return False
    cls = unit_class(da.attrs.get("units"))
    return cls is None or cls in role.units


def _pick(ds: xr.Dataset, role: Role, names: Iterable[str], step: str) -> tuple[str, str] | None:
    """Best variable of *names* for *step* (cf | alias | pattern) -> ``(name, kind)``."""
    present = [str(n) for n in names]
    if step == "cf":
        hits = [n for n in present if _is_field(ds[n]) and _cf_ok(ds[n], role)]
        if not hits:
            return None
        order = {a.lower(): i for i, a in enumerate(role.all_aliases)}
        hits.sort(key=lambda n: (order.get(n.lower(), len(order)), n))
        return hits[0], "cf"
    if step == "alias":
        for alias in role.aliases + role.corrected_aliases:
            if alias in present and not _veto(ds[alias], role):
                return alias, "alias"
        lowered = {n.lower(): n for n in present}
        for alias in role.aliases + role.corrected_aliases:
            hit = lowered.get(alias.lower())
            if hit is not None and not _veto(ds[hit], role):
                return hit, "alias"
        return None
    for pat in role.patterns + role.corrected_patterns:
        rx = re.compile(pat, re.IGNORECASE)
        hits = sorted(
            n
            for n in present
            if rx.fullmatch(n)
            and not _NOT_A_FIELD.search(n)
            and _is_field(ds[n])
            and not _veto(ds[n], role)
        )
        if hits:
            return hits[0], "pattern"
    return None


def _resolve_role(
    ds: xr.Dataset, role: Role, overrides: Mapping[str, str] | None, prefer_corrected: bool
) -> Match | None:
    names = [str(n) for n in ds.data_vars]
    ov = (overrides or {}).get(role.key)
    if ov and ov in ds.data_vars:
        return Match(role.key, ov, "override", KIND_CONFIDENCE["override"])
    groups = (True, False) if prefer_corrected else (False, True)
    for step in ("cf", "alias", "pattern"):
        for corrected in groups:
            pool = [n for n in names if role.is_corrected(n) == corrected]
            hit = _pick(ds, role, pool, step)
            if hit is not None:
                name, kind = hit
                return Match(role.key, name, kind, KIND_CONFIDENCE[kind], corrected=corrected)
    return None


def resolve(
    ds: xr.Dataset,
    name: str,
    *,
    overrides: Mapping[str, str] | None = None,
    prefer_corrected: bool = False,
) -> Match | None:
    """Resolve *name* (role key, legacy name such as ``VRADH``, alias, or plain variable) on *ds*.

    An alias that is itself a variable of *ds* (and is not the role's canonical / key name)
    resolves to that variable, as before; a user override for the role wins over everything.
    ``None`` when nothing matches.
    """
    role = role_for_name(name)
    if role is None:
        if name in ds.data_vars:
            return Match(None, name, "exact", KIND_CONFIDENCE["exact"])
        return None
    ov = (overrides or {}).get(role.key)
    if ov and ov in ds.data_vars:
        return Match(role.key, ov, "override", KIND_CONFIDENCE["override"])
    if name in ds.data_vars and name not in (role.key, role.legacy):
        return Match(
            role.key, name, "alias", KIND_CONFIDENCE["alias"], corrected=role.is_corrected(name)
        )
    return _resolve_role(ds, role, overrides, prefer_corrected)


def find_in(
    ds: xr.Dataset,
    name: str,
    *,
    overrides: Mapping[str, str] | None = None,
    prefer_corrected: bool = False,
) -> xr.DataArray | None:
    """The DataArray :func:`resolve` picks for *name* on *ds*, or ``None``."""
    m = resolve(ds, name, overrides=overrides, prefer_corrected=prefer_corrected)
    return ds[m.variable] if m is not None else None


def resolve_all(
    ds: xr.Dataset, overrides: Mapping[str, str] | None = None
) -> dict[str, Match | None]:
    return {key: resolve(ds, key, overrides=overrides) for key in ROLES}


def standardize_dataset(ds: xr.Dataset, overrides: Mapping[str, str] | None = None) -> xr.Dataset:
    """Rename each role's resolved variable to its legacy canonical name (``DBZH``, ``VRADH``...).

    Variables whose canonical name already exists are left alone; the input is not changed.
    """
    rename: dict[str, str] = {}
    taken = {str(v) for v in ds.data_vars}
    for role in ROLES.values():
        if role.legacy in taken:
            continue
        m = resolve(ds, role.key, overrides=overrides)
        if m is not None and m.variable not in rename:
            rename[m.variable] = role.legacy
    return ds.rename(rename) if rename else ds


# ---------------------------------------------------------------------------
# Sweeps: fixed angles, pairing, alignment
# ---------------------------------------------------------------------------


def sweep_fixed_angle(ds: xr.Dataset) -> float | None:
    """Fixed elevation angle (deg) of a sweep dataset, or ``None`` if it cannot be told."""
    for src in (ds.attrs, ds.data_vars, ds.coords):
        if "sweep_fixed_angle" in src:
            try:
                val = float(
                    np.asarray(
                        getattr(src["sweep_fixed_angle"], "values", src["sweep_fixed_angle"])
                    )
                )
            except (TypeError, ValueError):
                continue
            if np.isfinite(val):
                return val
    if "elevation" in ds.variables:
        el = np.asarray(ds["elevation"].values, dtype=float)
        if el.size and np.isfinite(el).any():
            return float(np.nanmean(el))
    return None


def _sweep_nodes(tree: xr.DataTree) -> list[xr.DataTree]:
    from engine.reader import sorted_sweep_names  # lazy: keeps this module import-light

    return [tree[n] for n in sorted_sweep_names(tree)]


def sweep_datasets(tree: xr.DataTree) -> list[xr.Dataset]:
    """Cheap (unmasked, lazy) per-sweep datasets, used to look at names / attrs / angles only."""
    return [n.to_dataset() for n in _sweep_nodes(tree)]


def find_paired_sweep(
    tree: xr.DataTree,
    sweep: int,
    name: str,
    *,
    overrides: Mapping[str, str] | None = None,
    prefer_corrected: bool = False,
    tolerance: float = DEFAULT_TOLERANCE_DEG,
    datasets: list[xr.Dataset] | None = None,
) -> Match | None:
    """Match *name* on the sweep with the nearest fixed angle (``|diff| <= tolerance``).

    The requested sweep itself is skipped. Ties go to the closest sweep index, then the lower.
    The returned :class:`Match` has ``sweep`` (the paired sweep) and ``angle_diff`` set.
    """
    dss = datasets if datasets is not None else sweep_datasets(tree)
    if not 0 <= sweep < len(dss):
        return None
    here = sweep_fixed_angle(dss[sweep])
    if here is None:
        return None
    best: tuple[tuple[float, int, int], Match] | None = None
    for j, ds in enumerate(dss):
        if j == sweep:
            continue
        ang = sweep_fixed_angle(ds)
        if ang is None or abs(ang - here) > tolerance + 1e-9:
            continue
        m = resolve(ds, name, overrides=overrides, prefer_corrected=prefer_corrected)
        if m is None:
            continue
        key = (abs(ang - here), abs(j - sweep), j)
        if best is None or key < best[0]:
            m.sweep, m.angle_diff = j, abs(ang - here)
            best = (key, m)
    return best[1] if best else None


def resolve_in_tree(
    tree: xr.DataTree,
    sweep: int,
    name: str,
    *,
    overrides: Mapping[str, str] | None = None,
    prefer_corrected: bool = False,
    allow_paired: bool = False,
    tolerance: float = DEFAULT_TOLERANCE_DEG,
    datasets: list[xr.Dataset] | None = None,
) -> Match | None:
    """Resolve *name* on *sweep*; with ``allow_paired`` fall back to the paired sweep."""
    dss = datasets if datasets is not None else sweep_datasets(tree)
    if not 0 <= sweep < len(dss):
        return None
    m = resolve(dss[sweep], name, overrides=overrides, prefer_corrected=prefer_corrected)
    if m is not None or not allow_paired:
        return m
    return find_paired_sweep(
        tree,
        sweep,
        name,
        overrides=overrides,
        prefer_corrected=prefer_corrected,
        tolerance=tolerance,
        datasets=dss,
    )


def nearest_ray(src_az: np.ndarray, query_az: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Index of the closest ray of ``src_az`` for each ``query_az`` (circular) and the gap (deg)."""
    src = np.mod(np.asarray(src_az, dtype=np.float64), 360.0)
    order = np.argsort(src)
    s = src[order]
    q = np.mod(np.asarray(query_az, dtype=np.float64), 360.0)
    hi = np.searchsorted(s, q) % s.size
    lo = (hi - 1) % s.size
    d_lo = np.abs((q - s[lo] + 180.0) % 360.0 - 180.0)
    d_hi = np.abs((q - s[hi] + 180.0) % 360.0 - 180.0)
    pick_hi = d_hi < d_lo
    idx = np.where(pick_hi, hi, lo)
    return order[idx], np.where(pick_hi, d_hi, d_lo)


def align_to_sweep(da: xr.DataArray, src_ds: xr.Dataset, tgt_ds: xr.Dataset) -> xr.DataArray:
    """Resample *da* (a field of *src_ds*) onto *tgt_ds*'s ``(azimuth, range)`` grid.

    Nearest ray (circular) and nearest gate. Target rays farther than ~1.5 ray spacings from
    any source ray, and gates beyond the source's range coverage, are NaN.
    """
    ray_dims = [d for d in da.dims if d != "range"]
    if len(ray_dims) != 1 or "range" not in da.dims:
        raise VarMapError(f"cannot align '{da.name}' with dimensions {da.dims}")
    vals = np.asarray(da.transpose(ray_dims[0], "range").values)
    if vals.dtype.kind not in "fc":
        vals = vals.astype(np.float32)
    src_az = np.asarray(src_ds["azimuth"].values, dtype=np.float64)
    src_rng = np.asarray(src_ds["range"].values, dtype=np.float64)
    tgt_az = np.asarray(tgt_ds["azimuth"].values, dtype=np.float64)
    tgt_rng = np.asarray(tgt_ds["range"].values, dtype=np.float64)

    ray, gap = nearest_ray(src_az, tgt_az)
    if src_az.size > 1:
        step = np.abs(np.diff(np.sort(np.mod(src_az, 360.0))))
        step = step[step > 1e-6]
        max_gap = max(1.0, 1.5 * float(np.median(step))) if step.size else 1.0
    else:
        max_gap = 1.0
    hi = np.clip(np.searchsorted(src_rng, tgt_rng), 1, max(src_rng.size - 1, 1))
    lo = hi - 1 if src_rng.size > 1 else np.zeros_like(hi)
    gate = np.where(np.abs(tgt_rng - src_rng[lo]) <= np.abs(src_rng[hi] - tgt_rng), lo, hi)
    spacing = float(np.median(np.diff(src_rng))) if src_rng.size > 1 else 0.0
    inside = (tgt_rng >= src_rng[0] - spacing / 2) & (tgt_rng <= src_rng[-1] + spacing / 2)

    out = vals[ray][:, gate]
    out = np.where(inside[None, :] & (gap <= max_gap)[:, None], out, np.nan)
    ray_dim = tgt_ds["azimuth"].dims[0] if tgt_ds["azimuth"].dims else "azimuth"
    coords = {ray_dim: tgt_ds[ray_dim].values if ray_dim in tgt_ds.coords else tgt_az}
    coords["range"] = tgt_ds["range"].values
    return xr.DataArray(
        out, dims=(ray_dim, "range"), coords=coords, name=da.name, attrs=dict(da.attrs)
    )


# ---------------------------------------------------------------------------
# Error text
# ---------------------------------------------------------------------------


def _short(items: Iterable[str], limit: int = 8) -> str:
    items = list(items)
    more = f", ... (+{len(items) - limit})" if len(items) > limit else ""
    return ", ".join(items[:limit]) + more


def _var_list(ds: xr.Dataset) -> str:
    names = [str(v) for v, da in ds.data_vars.items() if da.ndim >= 2]
    return ", ".join(names) if names else "none"


def _sweep_hint(
    tree: xr.DataTree,
    sweep: int,
    name: str,
    role: Role,
    overrides: Mapping[str, str] | None,
    tolerance: float,
    datasets: list[xr.Dataset] | None,
) -> str:
    """Where else in the volume the role exists (paired sweep first), as a sentence."""
    dss = datasets if datasets is not None else sweep_datasets(tree)
    paired = find_paired_sweep(
        tree, sweep, name, overrides=overrides, tolerance=tolerance, datasets=dss
    )
    if paired is not None:
        return (
            f" Sweep {paired.sweep} (same elevation, within {tolerance:g} deg) has "
            f"{paired.variable}: select that sweep, or use a module that accepts paired sweeps."
        )
    others = [
        f"{j} ({m.variable})"
        for j, d in enumerate(dss)
        if j != sweep and (m := resolve(d, name, overrides=overrides)) is not None
    ]
    return f" Other sweeps with {role.label}: {_short(others, 4)}." if others else ""


def explain_missing(
    name: str,
    ds: xr.Dataset,
    *,
    sweep: int | None = None,
    tree: xr.DataTree | None = None,
    overrides: Mapping[str, str] | None = None,
    tolerance: float = DEFAULT_TOLERANCE_DEG,
    datasets: list[xr.Dataset] | None = None,
) -> str:
    """User-facing text for "role *name* not found": what was searched, what exists, hints."""
    role = role_for_name(name)
    where = f"sweep {sweep}" if sweep is not None else "this sweep"
    if role is None:
        return f"No variable '{name}' found; variables on {where}: {_var_list(ds)}"
    searched = (
        f"CF standard_name {role.standard_names[0]}, "
        f"names {_short(role.aliases, 6)} and name patterns"
    )
    ov = (overrides or {}).get(role.key)
    head = f"No {role.label} field found on {where}"
    if ov:
        head += f" (your mapping '{ov}' is not on this sweep)"
    text = f"{head} (searched {searched}). "
    text += f"Variables on {where}: {_var_list(ds)}."
    if tree is not None and sweep is not None:
        try:
            text += _sweep_hint(tree, sweep, name, role, overrides, tolerance, datasets)
        except Exception:  # a hint must never mask the real error
            logger.debug("sweep hint failed", exc_info=True)
    text += " You can map a variable to this role under 'Variable mapping'."
    return text


# ---------------------------------------------------------------------------
# Per-file overrides (memory + persistence)
# ---------------------------------------------------------------------------


def store_path() -> Path:
    env = os.environ.get("XRADAR_VARMAP_FILE")
    return Path(env).expanduser() if env else Path.home() / ".xradar-desktop" / "variable_maps.json"


def _read_store() -> dict[str, dict[str, str]]:
    try:
        raw = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(p): {str(k): str(v) for k, v in m.items()}
        for p, m in raw.items()
        if isinstance(m, dict)
    }


def load_saved(path: str | None) -> dict[str, str]:
    """Persisted overrides for a file path (empty when none / unreadable)."""
    if not path:
        return {}
    try:
        return normalize_overrides(_read_store().get(os.path.abspath(path), {}))
    except VarMapError:
        return {}


def save_persisted(path: str | None, mapping: Mapping[str, str]) -> None:
    """Persist overrides for a file path; an empty mapping removes the entry. Never raises."""
    if not path:
        return
    key = os.path.abspath(path)
    try:
        data = _read_store()
        if mapping:
            data[key] = dict(mapping)
        else:
            data.pop(key, None)
        target = store_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:  # persistence is a convenience, never a failure
        logger.warning("Could not save variable mappings: %s", exc)


@dataclass
class OverrideStore:
    """In-memory per-``file_id`` overrides, seeded from / written through to the JSON store."""

    _maps: dict[str, dict[str, str]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, file_id: str | None, path: str | None = None) -> dict[str, str]:
        key = file_id or ""
        with self._lock:
            if key not in self._maps:
                self._maps[key] = load_saved(path)
            return dict(self._maps[key])

    def update(
        self,
        file_id: str | None,
        path: str | None,
        mapping: Mapping[str, Any],
        *,
        replace: bool = False,
    ) -> dict[str, str]:
        """Merge (or replace with) *mapping*; a blank / ``"auto"`` value clears that role."""
        cleared = {
            role_for_name(str(k)).key  # type: ignore[union-attr]
            for k, v in mapping.items()
            if role_for_name(str(k)) is not None and (v is None or str(v).strip() in ("", "auto"))
        }
        new = normalize_overrides(mapping)  # raises VarMapError for unknown roles
        current = {} if replace else self.get(file_id, path)
        current = {k: v for k, v in current.items() if k not in cleared}
        current.update(new)
        with self._lock:
            self._maps[file_id or ""] = current
        save_persisted(path, current)
        return dict(current)

    def drop(self, file_id: str | None) -> None:
        with self._lock:
            self._maps.pop(file_id or "", None)


# ---------------------------------------------------------------------------
# Whole-file summary (get_variable_mapping)
# ---------------------------------------------------------------------------


def mapping_report(
    tree: xr.DataTree,
    sweep: int = 0,
    overrides: Mapping[str, str] | None = None,
    *,
    tolerance: float = DEFAULT_TOLERANCE_DEG,
) -> dict[str, Any]:
    """Resolved role -> variable for *sweep* (paired-sweep fallback included) + dropdown options."""
    dss = sweep_datasets(tree)
    if not dss:
        return {"sweep": sweep, "n_sweeps": 0, "roles": [], "variables": []}
    sweep = min(max(int(sweep), 0), len(dss) - 1)
    variables = sorted({str(v) for d in dss for v, da in d.data_vars.items() if da.ndim >= 2})
    roles = []
    for r in ROLES.values():
        m = resolve_in_tree(
            tree,
            sweep,
            r.key,
            overrides=overrides,
            allow_paired=True,
            tolerance=tolerance,
            datasets=dss,
        )
        roles.append(
            {
                "role": r.key,
                "legacy": r.legacy,
                "label": r.label,
                "override": (overrides or {}).get(r.key),
                "variable": m.variable if m else None,
                "kind": m.kind if m else None,
                "confidence": m.confidence if m else 0.0,
                "sweep": m.sweep if m else None,
                "corrected": bool(m.corrected) if m else False,
            }
        )
    return {
        "sweep": sweep,
        "n_sweeps": len(dss),
        "tolerance": tolerance,
        "roles": roles,
        "variables": variables,
    }

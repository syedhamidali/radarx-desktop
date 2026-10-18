"""Helpers shared by the second batch of ``radarx_*`` modules.

Files starting with ``_`` are skipped by module discovery. This file complements
``_radarx_adapter.py`` (which it imports by path) with what the *volume* modules (KDP column,
VVP winds, melting layer, QVP, tornado probability) need:

* :func:`site_scalars` - the radar site as three scalars. Mobile and some CfRadial files store
  latitude/longitude/altitude per ray (with NaN padding); radarx wants scalars.
* :func:`volume` - a ``DataTree`` of the sweeps, loaded through the same loader the server gives
  the module (so reserved "no echo" / "range folded" codes of Level II files are NaN, not
  -33 dBZ), with canonical variable names, the site as scalar coordinates and optionally KDP
  estimated from PHIDP.
* small unit/wind helpers.

``radarx`` is never imported at module level.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import xarray as xr

from engine.plugins import ModuleError


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

SITE_KEYS = ("latitude", "longitude", "altitude")
MS_TO_KT = 1.0 / 0.514444


# ---------------------------------------------------------------------------
# radar site
# ---------------------------------------------------------------------------


def _scalar(values) -> float | None:
    arr = np.asarray(values, dtype=np.float64).ravel()
    arr = arr[np.isfinite(arr) & (arr > -9000)]  # NaN and the -9999 fill value
    return float(np.median(arr)) if arr.size else None


def site_scalars(tree: xr.DataTree) -> dict[str, float]:
    """Radar latitude/longitude/altitude as floats (median of valid per-ray values)."""
    root = tree.root
    out: dict[str, float] = {}
    for key in SITE_KEYS:
        src = root.variables if key in root.variables else None
        if src is None:
            for name in A.sweep_names(tree):
                node = tree[name]
                if key in node.variables:
                    src = node.variables
                    break
        if src is None:
            continue
        val = _scalar(src[key].values)
        if val is not None:
            out[key] = val
    return out


def version() -> str:
    """Installed radarx version (package metadata; the package has no ``__version__``)."""
    try:
        from importlib.metadata import version as _version

        return _version("radarx")
    except Exception:
        return A.radarx_version()


def need(rt, name: str, what: str):
    """``radarx.retrieve.<name>`` or a ModuleError that says the installed radarx is too old."""
    func = getattr(rt, name, None)
    if func is None:
        raise ModuleError(
            f"{what} needs radarx.retrieve.{name}, which the installed radarx {version()} does "
            "not have. Upgrade radarx (pip install -U radarx) to use this module."
        )
    return func


def radar_band(tree: xr.DataTree, ds: xr.Dataset | None = None) -> str | None:
    """``"S"``, ``"C"`` or ``"X"`` from the radar frequency in the file, else None."""
    sources = [tree.root.to_dataset()]
    if ds is not None:
        sources.append(ds)
    for src in sources:
        for key in ("frequency", "radar_frequency"):
            if key in src.variables:
                vals = np.asarray(src[key].values, dtype=np.float64).ravel()
                vals = vals[np.isfinite(vals) & (vals > 0)]
                if vals.size:
                    f = float(np.median(vals))
                    f = f * 1e9 if f < 1e3 else f  # GHz or Hz
                    if 2e9 <= f < 4e9:
                        return "S"
                    if 4e9 <= f < 8e9:
                        return "C"
                    if 8e9 <= f < 12.5e9:
                        return "X"
    return None


def with_site(ds: xr.Dataset, site: dict[str, float]) -> xr.Dataset:
    """``ds`` with the site as scalar coordinates (replacing per-ray arrays)."""
    return ds.drop_vars([k for k in SITE_KEYS if k in ds.variables]).assign_coords(**site)


# ---------------------------------------------------------------------------
# volumes
# ---------------------------------------------------------------------------


@dataclass
class Volume:
    """A prepared volume: sweeps that hold the requested fields, standardised."""

    tree: xr.DataTree
    names: list[str]  # kept sweep names, in volume order
    indices: list[int]  # their index in the file (the sweep numbers the UI uses)
    skipped: list[str] = field(default_factory=list)  # "sweep_3: no ZDR" ...
    kdp_estimated: list[str] = field(default_factory=list)
    site: dict[str, float] = field(default_factory=dict)

    def sweep(self, name: str) -> xr.Dataset:
        return self.tree[name].to_dataset()


def sweep_loader(ctx):
    """Callable ``i -> Dataset`` that loads sweep ``i`` the way the server does (masked)."""
    loader = getattr(ctx, "_get_ds", None)
    if callable(loader):
        return loader
    names = A.sweep_names(ctx.datatree)
    return lambda i: ctx.datatree[names[i]].to_dataset()


def elevation_of(ds: xr.Dataset) -> float:
    return A.sweep_elevation(ds)


def volume(
    ctx,
    *,
    require: tuple[str, ...] = (),
    keep: tuple[str, ...] = (),
    any_of: tuple[str, ...] = (),
    estimate_kdp: bool = False,
    band: str = "S",
    elevation_range: tuple[float, float] | None = None,
    progress: tuple[int, int] | None = None,
) -> Volume:
    """Load the sweeps of the open volume that hold every field in ``require``.

    Fields are renamed to canonical names (``DBZ`` -> ``DBZH`` ...). ``keep`` adds optional
    fields that are kept when present; ``any_of`` skips sweeps that hold none of the listed
    fields. With ``estimate_kdp`` a sweep with PHIDP but no KDP gets
    KDP from ``radarx.retrieve.estimate_kdp``. Only the requested 2-D fields are kept, which
    keeps a 17-sweep volume small. ``progress=(lo, hi)`` reports a percentage range to ``ctx``.
    """
    rt = A.rx() if estimate_kdp else None
    tree = ctx.datatree
    all_names = A.sweep_names(tree)
    site = site_scalars(tree)
    load = sweep_loader(ctx)
    want = tuple(dict.fromkeys((*require, *any_of, *keep)))
    nodes: dict[str, xr.Dataset] = {}
    kept_names: list[str] = []
    kept_idx: list[int] = []
    skipped: list[str] = []
    est: list[str] = []
    for i, name in enumerate(all_names):
        if progress:
            ctx.progress(
                progress[0] + (progress[1] - progress[0]) * i // max(len(all_names), 1),
                f"Preparing {name}",
            )
        ds = A.standardize(load(i))
        if "azimuth" not in ds.coords or "range" not in ds.coords:
            skipped.append(f"{name}: not a polar sweep")
            continue
        if elevation_range is not None:
            el = elevation_of(ds)
            if not elevation_range[0] <= el <= elevation_range[1]:
                continue
        if estimate_kdp and "KDP" not in ds.data_vars and "PHIDP" in ds.data_vars:
            sub = ds[[v for v in ("PHIDP", "RHOHV", "DBZH") if v in ds.data_vars]]
            try:
                sub = with_site(sub, site)
                sub, did = A.with_kdp(rt, sub, band=band)
            except ModuleError as exc:
                skipped.append(f"{name}: {exc}")
                continue
            if did:
                ds = ds.assign(KDP=sub["KDP"])
                est.append(name)
        if any(r not in ds.data_vars for r in require):
            continue  # e.g. the Doppler-only cuts of NEXRAD: expected, not worth a message
        if any_of and not any(v in ds.data_vars for v in any_of):
            continue  # e.g. the Doppler-only cuts of NEXRAD: expected, not worth a message
        names2d = [v for v in want if v in ds.data_vars]
        scalars = [v for v in ds.data_vars if ds[v].ndim < 2]
        ds = with_site(ds[names2d + scalars].load(), site)
        nodes[f"/{name}"] = ds
        kept_names.append(name)
        kept_idx.append(i)
    root = xr.Dataset(coords={k: np.float64(v) for k, v in site.items()})
    tree_out = xr.DataTree.from_dict({"/": root, **nodes})
    return Volume(tree_out, kept_names, kept_idx, skipped, est, site)


def describe_skipped(vol: Volume, limit: int = 6) -> str:
    if not vol.skipped:
        return ""
    shown = "; ".join(vol.skipped[:limit])
    more = f" (+{len(vol.skipped) - limit} more)" if len(vol.skipped) > limit else ""
    return f"{shown}{more}"


def selected_sweep(ctx, *, georeference: bool = False) -> xr.Dataset:
    """The selected sweep, standardised, with scalar site coordinates."""
    ds = A.standardize(ctx.dataset)
    if "azimuth" not in ds.coords or "range" not in ds.coords:
        raise ModuleError("This sweep has no azimuth/range coordinates (not a polar PPI)")
    ds = with_site(ds, site_scalars(ctx.datatree))
    return A.georeference_sweep(ds) if georeference else ds


def beam_height_asl(
    range_m: np.ndarray, elevation_deg: float, altitude_m: float, k_e: float = 4.0 / 3.0
) -> np.ndarray:
    """Beam-centre height above sea level, effective Earth radius model (Doviak & Zrnic 2.28b)."""
    reff = k_e * 6371000.0
    a = reff + float(altitude_m)
    r = np.asarray(range_m, dtype=np.float64)
    sin_el = math.sin(math.radians(float(elevation_deg)))
    return np.sqrt(r * r + a * a + 2.0 * r * a * sin_el) - reff


def profile_rows(profile: xr.Dataset, names: list[str], digits: int = 2) -> list[dict]:
    """Rows ``{height_km, <name>...}`` of a 1-D ``height`` profile (NaN -> None)."""
    h = np.asarray(profile["height"].values, dtype=np.float64)
    cols = {n: np.asarray(profile[n].values, dtype=np.float64).reshape(-1) for n in names}
    rows = []
    for i in range(h.size):
        row: dict = {"height_asl_km": round(float(h[i]) / 1000.0, 3)}
        have = False
        for n, arr in cols.items():
            val = arr[i]
            row[n] = round(float(val), digits) if np.isfinite(val) else None
            have = have or np.isfinite(val)
        if have:
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# winds
# ---------------------------------------------------------------------------


def wind_dir_from(u: float, v: float) -> float:
    """Meteorological direction (degrees the wind blows *from*) of an (east, north) wind."""
    return float((math.degrees(math.atan2(-u, -v))) % 360.0)


def compass(deg: float) -> str:
    pts = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW"]
    pts += ["NW", "NNW"]
    return pts[int((deg % 360.0) / 22.5 + 0.5) % 16]


def interp_valid(height: np.ndarray, values: np.ndarray, at: float) -> float:
    """Linear interpolation at ``at`` using the finite samples only (NaN outside their span)."""
    ok = np.isfinite(values) & np.isfinite(height)
    if ok.sum() < 2 or at < height[ok].min() or at > height[ok].max():
        return float("nan")
    return float(np.interp(at, height[ok], values[ok]))

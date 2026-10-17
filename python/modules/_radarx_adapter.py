"""Shared glue between the desktop module SDK and the optional ``radarx`` package.

Files starting with ``_`` are skipped by module discovery. Every ``radarx_*.py`` module pulls
this file in by path (see ``_load_adapter`` in each module), exactly as ``zdr_arc.py`` does
with ``_radar_utils.py``.

Rules this file follows:

* ``radarx`` is **never** imported at module level. A module whose pip package is missing is
  listed as unavailable by the SDK (``needs=["radarx"]``); only code that runs after that
  check may import it, through :func:`rx`.
* radarx functions want an ``xarray.Dataset`` sweep (or a ``DataTree`` volume) with the radar
  site coordinates (``latitude``/``longitude``/``altitude``) that the desktop reader keeps
  on the tree root only; :func:`prepare_sweep` puts them back.
* Gridded results must have exactly the selected sweep's ``(n_azimuth, n_range)`` shape.
  Volume products live on another grid, so :func:`remap_polar` and :func:`sample_cartesian`
  map them onto the selected sweep (nearest neighbour).
"""

from __future__ import annotations

import contextlib
import importlib.util
import math
import sys
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from engine.plugins import VAR_ALIASES, ModuleError


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

NEEDS = ["radarx"]
NEXRAD_VELOCITY_FLAG = 63.0  # |v| above this is a Level II range-fold / no-data code


# ---------------------------------------------------------------------------
# radarx access
# ---------------------------------------------------------------------------


def rx():
    """``radarx.retrieve`` (imported lazily; raises a ModuleError if it is not installed)."""
    try:
        import radarx.retrieve as retrieve
    except ImportError as exc:  # pragma: no cover - guarded by needs=["radarx"]
        raise ModuleError(
            "This module needs the Python package radarx (pip install radarx)"
        ) from exc
    return retrieve


def radarx_version() -> str:
    try:
        import radarx

        return str(getattr(radarx, "__version__", "?"))
    except ImportError:  # pragma: no cover
        return "?"


@contextlib.contextmanager
def guard(what: str):
    """Turn radarx's KeyError/ValueError/TypeError into a user-facing :class:`ModuleError`."""
    try:
        yield
    except ModuleError:
        raise
    except KeyError as exc:
        msg = exc.args[0] if exc.args else exc
        raise ModuleError(f"{what}: {msg}") from exc
    except (ValueError, TypeError, ImportError) as exc:
        raise ModuleError(f"{what}: {exc}") from exc


@contextlib.contextmanager
def captured_warnings():
    """Collect radarx's Python warnings (e.g. 'no reference wind') so they can be shown."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out: list[str] = []
        yield out
    seen: set[str] = set()
    for w in caught:
        text = str(w.message).strip().replace("\n", " ")
        if text and text not in seen:
            seen.add(text)
            out.append(text)


def add_warnings(res, messages: list[str]):
    """Append a 'Warnings from radarx' text block if there is anything to say."""
    if messages:
        res.text("\n".join(f"- {m}" for m in messages), title="Warnings from radarx")
    return res


# ---------------------------------------------------------------------------
# sweep / volume preparation
# ---------------------------------------------------------------------------


def sweep_names(tree: xr.DataTree) -> list[str]:
    """``sweep_*`` children of a volume in numeric order."""

    def key(name: str) -> tuple[int, str]:
        digits = "".join(ch for ch in name if ch.isdigit())
        return (int(digits) if digits else 10**9, name)

    return sorted((n for n in tree.children if n.startswith("sweep")), key=key)


def selected_name(ctx) -> str:
    names = sweep_names(ctx.datatree)
    if not 0 <= ctx.sweep < len(names):
        raise ModuleError(f"Sweep {ctx.sweep} is not in this volume ({len(names)} sweeps)")
    return names[ctx.sweep]


def site_coords(tree: xr.DataTree) -> dict[str, Any]:
    root = tree.root
    out = {}
    for key in ("latitude", "longitude", "altitude"):
        if key in root.variables:
            out[key] = root[key].values
        elif key in root.coords:  # pragma: no cover - same thing on some xarray versions
            out[key] = root.coords[key].values
    return out


def standardize(ds: xr.Dataset) -> xr.Dataset:
    """Rename alias variables (``DBZ``, ``differential_reflectivity``, ...) to canonical names.

    radarx looks fields up by a fixed list of names; the desktop SDK accepts more aliases.
    Only data variables whose canonical name is absent are renamed; the input is not changed.
    """
    rename = {}
    for canon, aliases in VAR_ALIASES.items():
        if canon in ds.data_vars:
            continue
        for alias in aliases:
            if alias in ds.data_vars:
                rename[alias] = canon
                break
    return ds.rename(rename) if rename else ds


def prepare_sweep(ctx, *, georeference: bool = False) -> xr.Dataset:
    """The selected sweep as an in-memory Dataset with radar site coordinates.

    With ``georeference=True`` also adds the 2-D ``x``/``y``/``z`` gate coordinates that
    ``rotation_couplets`` needs (``xradar``'s AEQD georeferencing).
    """
    ds = ctx.dataset
    if "azimuth" not in ds.coords or "range" not in ds.coords:
        raise ModuleError("This sweep has no azimuth/range coordinates (not a polar PPI)")
    ds = standardize(ds)
    site = site_coords(ctx.datatree)
    missing = {k: v for k, v in site.items() if k not in ds.coords}
    if missing:
        ds = ds.assign_coords(**missing)
    if georeference:
        ds = georeference_sweep(ds)
    return ds


def georeference_sweep(ds: xr.Dataset) -> xr.Dataset:
    if {"x", "y"} <= set(ds.coords):
        return ds
    try:
        return ds.xradar.georeference()
    except Exception:
        az = np.deg2rad(ds["azimuth"].values.astype(np.float64))[:, None]
        el = np.deg2rad(np.broadcast_to(ds["elevation"].values, ds["azimuth"].shape))[:, None]
        r = ds["range"].values.astype(np.float64)[None, :]
        s = r * np.cos(el)
        return ds.assign_coords(
            x=(("azimuth", "range"), s * np.sin(az)),
            y=(("azimuth", "range"), s * np.cos(az)),
        )


def polar_array(values: Any, ds: xr.Dataset, name: str = "result") -> np.ndarray:
    """``values`` (DataArray or ndarray) as float32 ``(n_azimuth, n_range)`` of the sweep."""
    if isinstance(values, xr.DataArray):
        ray = [d for d in values.dims if d != "range"]
        if len(ray) != 1 or "range" not in values.dims:
            raise ModuleError(f"{name}: unexpected dimensions {values.dims}")
        values = values.transpose(ray[0], "range").values
    arr = np.asarray(values, dtype=np.float32)
    want = (ds.sizes["azimuth"], ds.sizes["range"])
    if arr.shape != want:
        raise ModuleError(f"{name}: shape {arr.shape} does not match the sweep grid {want}")
    return arr


def nyquist_velocity(ds: xr.Dataset, field: str, override: float) -> tuple[float, str]:
    """Nyquist velocity (m/s) and where it came from.

    ``override > 0`` wins; then the file (``nyquist_velocity`` variable/coordinate/attribute);
    otherwise it is *estimated* as the largest valid |velocity| of the sweep, which equals the
    Nyquist velocity when the data are aliased and under-estimates it otherwise.
    """
    if override and override > 0:
        return float(override), "user"
    for src in (ds.data_vars, ds.coords):
        if "nyquist_velocity" in src:
            vals = np.asarray(ds["nyquist_velocity"].values, dtype=float)
            vals = vals[np.isfinite(vals) & (vals > 0)]
            if vals.size:
                return float(np.median(vals)), "file"
    attr = ds[field].attrs.get("nyquist_velocity") or ds.attrs.get("nyquist_velocity")
    if attr:
        return float(attr), "file"
    v = np.abs(np.asarray(ds[field].values, dtype=np.float64))
    v = v[np.isfinite(v) & (v < NEXRAD_VELOCITY_FLAG)]
    if v.size < 100 or float(v.max()) < 5.0:
        raise ModuleError(
            "Cannot determine the Nyquist velocity (not in the file and too few valid "
            "velocities to estimate it). Enter it in the 'Nyquist velocity' parameter."
        )
    return float(math.ceil(v.max() * 2.0) / 2.0), "estimated from max |V|"


def dealias(
    rt,
    ds: xr.Dataset,
    field: str,
    nyq: float,
    *,
    threshold: float = 0.3,
    max_speed: float = 0.0,
    method: str = "region",
    alpha: float = 0.8,
    wind_profile: xr.Dataset | None = None,
):
    """Run ``radarx.retrieve.dealias_velocity`` on one sweep and return the DataArray."""
    kwargs: dict[str, Any] = {"method": method}
    if method == "region":
        kwargs.update(threshold=threshold)
        if max_speed and max_speed > 0:
            kwargs["max_speed"] = float(max_speed)
        if wind_profile is not None:
            kwargs["wind_profile"] = wind_profile
    else:
        kwargs["alpha"] = alpha
    with guard("Velocity dealiasing"):
        return rt.dealias_velocity(ds, field, float(nyq), **kwargs)


def uniform_wind_profile(speed: float, from_deg: float) -> xr.Dataset | None:
    """Height-independent wind (u east, v north) as a radarx ``wind_profile``; None if calm."""
    if not speed or speed <= 0:
        return None
    ang = math.radians(from_deg)
    u, v = -speed * math.sin(ang), -speed * math.cos(ang)  # meteorological 'from' convention
    height = np.array([0.0, 20000.0])
    return xr.Dataset(
        {"u": ("height", np.full(2, u)), "v": ("height", np.full(2, v))},
        coords={"height": height},
    )


def with_kdp(rt, ds: xr.Dataset, **kwargs) -> tuple[xr.Dataset, bool]:
    """Dataset with ``KDP`` (estimated from PHIDP by radarx if absent) and an estimated flag."""
    for name in ("KDP", "specific_differential_phase", "corrected_specific_differential_phase"):
        if name in ds.data_vars:
            return ds, False
    if not any(n in ds.data_vars for n in ("PHIDP", "UPHIDP", "differential_phase")):
        return ds, False
    with guard("KDP estimation"):
        out = rt.estimate_kdp(ds, **kwargs)
    return ds.assign(KDP=out["KDP"], PHIDP_processed=out["PHIDP_processed"]), True


def meteo_mask(rt, ds: xr.Dataset) -> xr.DataArray:
    """radarx fuzzy-logic echo mask (True = meteorological) for ``ds``."""
    with guard("Echo mask"):
        return rt.echo_mask(ds)["METEO_MASK"]


# ---------------------------------------------------------------------------
# polar <-> polar / cartesian mapping
# ---------------------------------------------------------------------------


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


def ground_range_m(range_m: np.ndarray, elevation_deg: float, altitude_m: float = 0.0):
    """Ground distance of each gate using radarx's 4/3-Earth geometry."""
    from radarx.fundamentals.geometry import ground_range

    return np.asarray(
        ground_range(np.asarray(range_m, dtype=np.float64), elevation_deg, radar_height=altitude_m),
        dtype=np.float64,
    )


def sweep_elevation(ds: xr.Dataset) -> float:
    if "sweep_fixed_angle" in ds.variables:
        val = float(np.asarray(ds["sweep_fixed_angle"].values).ravel()[0])
        if math.isfinite(val):
            return val
    return float(np.nanmedian(ds["elevation"].values))


def site_altitude(ds: xr.Dataset) -> float:
    return float(ds["altitude"].values) if "altitude" in ds.coords else 0.0


def remap_polar(
    values: np.ndarray,
    src_azimuth: np.ndarray,
    src_ground_m: np.ndarray,
    ds: xr.Dataset,
) -> np.ndarray:
    """Nearest-neighbour map of a polar product (rays x ground range) onto the sweep ``ds``.

    ``src_ground_m`` is the product's ground range per range bin (radarx volume products carry
    it as the ``ground_range`` coordinate). The target gate's ground range is computed from
    its slant range and elevation. Gates outside the product's coverage become NaN.
    """
    values = np.asarray(values, dtype=np.float32)
    tgt_az = np.asarray(ds["azimuth"].values, dtype=np.float64)
    tgt_rng = np.asarray(ds["range"].values, dtype=np.float64)
    tgt_ground = ground_range_m(tgt_rng, sweep_elevation(ds), site_altitude(ds))
    src_ground = np.asarray(src_ground_m, dtype=np.float64)
    if (
        values.shape == (tgt_az.size, tgt_rng.size)
        and src_azimuth.size == tgt_az.size
        and np.allclose(src_azimuth, tgt_az, atol=1e-6)
        and np.allclose(src_ground, tgt_ground, rtol=0, atol=1.0)
    ):
        return values
    ray, gap = nearest_ray(src_azimuth, tgt_az)
    spacing = max(U.azimuth_spacing_deg(src_azimuth), U.azimuth_spacing_deg(tgt_az))
    order = np.argsort(src_ground)
    sg = src_ground[order]
    pos = np.clip(np.searchsorted(sg, tgt_ground), 1, sg.size - 1)
    left_closer = np.abs(tgt_ground - sg[pos - 1]) <= np.abs(sg[pos] - tgt_ground)
    gate = order[np.where(left_closer, pos - 1, pos)]
    dr = float(np.median(np.diff(sg))) if sg.size > 1 else 250.0
    gate_ok = (tgt_ground >= sg[0] - dr) & (tgt_ground <= sg[-1] + dr)
    out = values[np.ix_(ray, gate)]
    out[gap > 1.5 * spacing, :] = np.nan
    out[:, ~gate_ok] = np.nan
    return out


def sample_cartesian(
    grid_yx: np.ndarray,
    x_axis: np.ndarray,
    y_axis: np.ndarray,
    ds: xr.Dataset,
    *,
    flat: bool = False,
) -> np.ndarray:
    """Sample a regular ``(y, x)`` grid (metres east/north of the radar) at every polar gate."""
    az = np.deg2rad(np.asarray(ds["azimuth"].values, dtype=np.float64))[:, None]
    rng = np.asarray(ds["range"].values, dtype=np.float64)
    el = sweep_elevation(ds)
    s = rng * math.cos(math.radians(el)) if flat else ground_range_m(rng, el, site_altitude(ds))
    x = s[None, :] * np.sin(az)
    y = s[None, :] * np.cos(az)
    x0, y0 = float(x_axis[0]), float(y_axis[0])
    dx, dy = float(x_axis[1] - x_axis[0]), float(y_axis[1] - y_axis[0])
    ix = np.rint((x - x0) / dx).astype(np.int64)
    iy = np.rint((y - y0) / dy).astype(np.int64)
    ok = (ix >= 0) & (ix < x_axis.size) & (iy >= 0) & (iy < y_axis.size)
    out = np.full(x.shape, np.nan, dtype=np.float32)
    out[ok] = np.asarray(grid_yx, dtype=np.float32)[iy[ok], ix[ok]]
    return out


def sweep_to_cartesian(
    fields: dict[str, np.ndarray],
    ds: xr.Dataset,
    spacing_m: float,
    max_range_m: float,
) -> xr.Dataset:
    """Resample polar fields of one PPI to a regular x/y grid (nearest neighbour).

    The grid is centred on the radar, ``spacing_m`` apart, out to ``max_range_m``. Cells outside
    the sweep (beyond its last gate or in an azimuth gap) are NaN. Uses a flat slant-range to
    ground-range conversion (``r = s / cos(el)``), the inverse of ``sample_cartesian(flat=True)``.
    """
    azimuth = np.asarray(ds["azimuth"].values, dtype=np.float64)
    rng = np.asarray(ds["range"].values, dtype=np.float64)
    el = math.radians(sweep_elevation(ds))
    r_limit = min(float(max_range_m), float(rng[-1]) * math.cos(el))
    n = math.floor(r_limit / spacing_m)
    axis = (np.arange(-n, n + 1) * spacing_m).astype(np.float64)
    xx, yy = np.meshgrid(axis, axis)
    s = np.hypot(xx, yy)
    bearing = np.mod(np.degrees(np.arctan2(xx, yy)), 360.0)
    ray, gap = nearest_ray(azimuth, bearing.ravel())
    ray = ray.reshape(bearing.shape)
    gap = gap.reshape(bearing.shape)
    dr = float(np.median(np.diff(rng))) if rng.size > 1 else 250.0
    gate = np.rint((s / math.cos(el) - rng[0]) / dr).astype(np.int64)
    ok = (gate >= 0) & (gate < rng.size) & (gap <= 1.5 * U.azimuth_spacing_deg(azimuth))
    gate = np.clip(gate, 0, rng.size - 1)
    data = {}
    for name, arr in fields.items():
        sampled = np.asarray(arr, dtype=np.float32)[ray, gate]
        sampled[~ok] = np.nan
        data[name] = (("y", "x"), sampled)
    return xr.Dataset(data, coords={"y": axis, "x": axis})


# ---------------------------------------------------------------------------
# object extraction on polar grids
# ---------------------------------------------------------------------------


def label_polar(mask: np.ndarray, wrap: bool) -> tuple[np.ndarray, int]:
    """8-connected labels; regions touching across the 0/360 seam are merged when ``wrap``."""
    from scipy import ndimage

    labels, n = ndimage.label(mask, structure=np.ones((3, 3), dtype=int))
    if not (wrap and n and mask[0].any() and mask[-1].any()):
        return labels, n
    parent = np.arange(n + 1)

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    first, last = labels[0], labels[-1]
    for la, lb in ((last, first), (last[:-1], first[1:]), (last[1:], first[:-1])):
        keep = (la > 0) & (lb > 0)
        for a, b in np.unique(np.column_stack([la[keep], lb[keep]]), axis=0):
            ra, rb = find(int(a)), find(int(b))
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    mapping = np.array([find(i) for i in range(n + 1)])
    return mapping[labels], n


def polar_regions(
    mask: np.ndarray,
    values: np.ndarray,
    azimuth: np.ndarray,
    range_m: np.ndarray,
    *,
    min_area_km2: float = 0.0,
    max_regions: int = 50,
) -> list[dict[str, Any]]:
    """Connected regions of ``mask`` with area, peak, centroid and an outline (polar polygon).

    Regions are sorted by their peak value (largest first). ``outline`` is
    ``[[azimuth_deg, range_m], ...]`` (convex hull of the boundary gates, so crescents are
    over-filled) or an empty list for degenerate regions.
    """
    from scipy import ndimage

    wrap = U.covers_full_circle(azimuth)
    labels, n = label_polar(mask, wrap)
    if n == 0:
        return []
    dr = float(np.median(np.diff(range_m))) if range_m.size > 1 else 250.0
    dtheta = math.radians(U.azimuth_spacing_deg(azimuth))
    gate_km2 = np.maximum(range_m, dr / 2.0) * dtheta * dr / 1e6
    objects = ndimage.find_objects(labels)
    found = []
    for lab, sl in enumerate(objects, start=1):
        if sl is None:
            continue
        sub = labels[sl] == lab
        ai, ri = np.nonzero(sub)
        ai_g, ri_g = ai + sl[0].start, ri + sl[1].start
        area = float(gate_km2[ri_g].sum())
        if area < min_area_km2:
            continue
        vals = values[ai_g, ri_g]
        finite = np.isfinite(vals)
        w = gate_km2[ri_g]
        x, y = U.polar_to_xy(azimuth[ai_g], range_m[ri_g])
        wsum = w.sum()
        cx, cy = float((w * x).sum() / wsum), float((w * y).sum() / wsum)
        eroded = ndimage.binary_erosion(sub)
        bi, br = np.nonzero(sub & ~eroded)
        if bi.size == 0:
            bi, br = ai, ri
        outline = U.order_polygon(azimuth[bi + sl[0].start], range_m[br + sl[1].start], hull=True)
        peak = float(np.nanmax(vals)) if finite.any() else float("nan")
        found.append(
            {
                "area_km2": area,
                "gates": int(sub.sum()),
                "peak": peak,
                "mean": float(np.nanmean(vals)) if finite.any() else float("nan"),
                "centroid_az": float(np.degrees(math.atan2(cx, cy)) % 360.0),
                "centroid_range_m": float(math.hypot(cx, cy)),
                "outline": outline,
                "x_m": cx,
                "y_m": cy,
            }
        )
    found.sort(key=lambda f: (-(f["peak"] if math.isfinite(f["peak"]) else -1e30), -f["area_km2"]))
    return found[:max_regions]


def hull_xy(x: np.ndarray, y: np.ndarray, pad: float = 0.0) -> list[list[float]]:
    """Convex hull of x/y points (metres) as a closed ring ``[[x, y], ...]`` (empty if < 3 pts)."""
    pts = np.unique(np.round(np.column_stack([x, y]), 1), axis=0)
    if pad:
        offs = np.array([[pad, pad], [pad, -pad], [-pad, pad], [-pad, -pad]])
        pts = np.unique(np.concatenate([pts + o for o in offs]), axis=0)
    if len(pts) < 3:
        return []
    try:
        from scipy.spatial import ConvexHull

        hull = ConvexHull(pts)
    except Exception:
        return []
    return [[float(pts[i, 0]), float(pts[i, 1])] for i in hull.vertices]


def xy_to_polar(x_m: float, y_m: float) -> tuple[float, float]:
    """(azimuth deg, range m) of a point given metres east/north of the radar."""
    return float(math.degrees(math.atan2(x_m, y_m)) % 360.0), float(math.hypot(x_m, y_m))


# ---------------------------------------------------------------------------
# presentation helpers
# ---------------------------------------------------------------------------

# Colours keyed by radarx HID flag_meanings (all methods and bands).
HID_COLORS = {
    "drizzle": "#a5f3fc",
    "rain": "#3b82f6",
    "light_and_moderate_rain": "#3b82f6",
    "heavy_rain": "#1e3a8a",
    "big_drops": "#22d3ee",
    "dry_snow": "#e5e7eb",
    "aggregates": "#e5e7eb",
    "wet_snow": "#a855f7",
    "ice_crystals": "#9ca3af",
    "crystals": "#9ca3af",
    "plates": "#d1d5db",
    "dendrites": "#f5f5f4",
    "vertically_aligned_ice": "#fb7185",
    "graupel": "#f59e0b",
    "low_density_graupel": "#fbbf24",
    "high_density_graupel": "#f97316",
    "hail": "#dc2626",
    "rain_hail_mixture": "#be123c",
    "freezing_rain": "#14b8a6",
    "freezing_frozen_rain": "#14b8a6",
}
_FALLBACK = ["#84cc16", "#06b6d4", "#d946ef", "#eab308", "#64748b", "#f43f5e", "#10b981"]


def pretty(meaning: str) -> str:
    text = meaning.replace("_", " ")
    return text[:1].upper() + text[1:]


def hid_categories(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    """``categories`` for ``Result.field`` from the ``flag_values``/``flag_meanings`` of HID."""
    values = [int(v) for v in np.atleast_1d(attrs.get("flag_values", []))]
    meanings = str(attrs.get("flag_meanings", "")).split()
    cats = []
    for i, (val, meaning) in enumerate(zip(values, meanings, strict=False)):
        cats.append(
            {
                "value": val,
                "label": pretty(meaning),
                "color": HID_COLORS.get(meaning, _FALLBACK[i % len(_FALLBACK)]),
            }
        )
    return cats


def symmetric_limit(arr: np.ndarray, pct: float = 99.5, floor: float = 1e-6) -> float:
    """Symmetric colour-scale half-range from the ``pct`` percentile of |arr|."""
    finite = np.abs(arr[np.isfinite(arr)])
    if not finite.size:
        return floor
    return max(float(np.percentile(finite, pct)), floor)


def fraction(mask: np.ndarray, base: np.ndarray | None = None) -> float:
    denom = int(base.sum()) if base is not None else mask.size
    return float(mask.sum()) / denom if denom else 0.0

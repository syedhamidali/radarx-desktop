"""Multi-radar mosaic engine built on ``radarx.grid.grid_radars``.

``build_mosaic`` turns several (or one) opened radar volumes into a single 2-D product
on a Web-Mercator-uniform grid, ready to be draped on a map as one image:

1. every volume is prepared (alias renaming, in-memory sweeps, site, volume time);
2. a shared extent is chosen (union of the radars' coverage discs, or an explicit lon/lat box)
   and an azimuthal-equidistant grid is laid over it;
3. ``radarx.grid.grid_radars`` cone-grids all radars onto it and merges them (weights from
   range, beam elevation and time) -- in horizontal bands so the memory stays bounded;
4. each band is reduced to 2-D (``cappi``, ``composite`` or ``echo_top``) and per-radar
   statistics (coverage, merge share) are accumulated;
5. the AEQD product is resampled to a grid that is uniform in Web-Mercator (x, y), coloured
   through a 256-entry look-up table and encoded as an RGBA PNG whose four lon/lat corners
   are exact for a Mapbox/MapLibre ``image`` source.

The float grids are kept in a small LRU (:data:`MOSAICS`) so the UI can probe and export a
mosaic without recomputing it. The merged 3-D field itself (every level, not only the reduced
product) is also kept, quantised to 8 bits against the colour range, so the 3-D view can draw
the mosaic as a volume (:func:`volume_message`); ``keep_volume`` turns that off.

``radarx`` is imported lazily: without it every entry point raises :class:`MosaicError` with a
``pip install radarx`` hint.
"""

from __future__ import annotations

import contextlib
import io
import logging
import math
import os
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from engine import hdf5_guard, valuemask, varmap

logger = logging.getLogger(__name__)

PRODUCTS = ("cappi", "composite", "echo_top")
# Web-Mercator sphere radius (EPSG:3857).
MERC_R = 6378137.0
MAX_MERC_LAT = 85.0
# Refuse grids that would take minutes / gigabytes; the message tells the user what to change.
MAX_GRID_CELLS = 9_000_000
MAX_IMAGE_PIXELS = 12_000_000
# Working-set budget (bytes) for one band of the 3-D gridding (fields, ranges, coverage, merge).
BAND_BUDGET_BYTES = 450 * 1024 * 1024
# Memory the mosaic cache may hold (float grids only; the PNG is small).
CACHE_MAX_BYTES = 600 * 1024 * 1024
CACHE_MAX_ITEMS = 4
# Largest merged 3-D field kept for the 3-D view (voxels = nz * ny * nx; 1 byte each once
# quantised, 2 while the bands are being collected).
VOLUME_MAX_VOXELS = 64_000_000
EARTH_RADIUS = 6371000.0
K_E = 4.0 / 3.0

_DEFAULT_UNITS = {
    "DBZH": "dBZ",
    "ZDR": "dB",
    "PHIDP": "deg",
    "KDP": "deg/km",
    "RHOHV": "1",
    "VRADH": "m/s",
    "WRADH": "m/s",
}
# cmweather colour maps (colorcet/matplotlib names as the fallback).
_CMAPS = {
    "DBZH": "NWSRef",
    "VRADH": "NWSVel",
    "WRADH": "NWS_SPW",
    "RHOHV": "NWS_CC",
    "ZDR": "RefDiff",
    "KDP": "Carbone17",
    "PHIDP": "Wild25",
}
_FALLBACK_CMAP = "HomeyerRainbow"
_ECHO_TOP_CMAP = "HomeyerRainbow"
# Radar "no echo" in some readers is a finite sentinel (NEXRAD: -33 dBZ) rather than NaN.
ProgressFn = Callable[[int, str], None]


class MosaicError(Exception):
    """A user-facing mosaic problem (bad request, missing radarx, no data...)."""


# ---------------------------------------------------------------------------
# Request parameters
# ---------------------------------------------------------------------------


@dataclass
class GridSpec:
    resolution_m: float = 1000.0
    extent: Any = "auto"  # "auto" or {west, south, east, north}
    z_max_m: float = 15000.0
    z_step_m: float = 500.0
    max_range_km: float = 230.0

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> GridSpec:
        d = dict(d or {})
        spec = cls()
        for key in ("resolution_m", "z_max_m", "z_step_m", "max_range_km"):
            if d.get(key) is not None:
                setattr(spec, key, _number(d[key], f"grid.{key}"))
        if d.get("extent") is not None:
            spec.extent = d["extent"]
        if not 250.0 <= spec.resolution_m <= 5000.0:
            raise MosaicError("grid.resolution_m must be between 250 and 5000 metres")
        if spec.z_step_m < 50.0 or spec.z_step_m > 5000.0:
            raise MosaicError("grid.z_step_m must be between 50 and 5000 metres")
        if spec.z_max_m < spec.z_step_m or spec.z_max_m > 30000.0:
            raise MosaicError("grid.z_max_m must be between z_step_m and 30000 metres")
        if not 10.0 <= spec.max_range_km <= 600.0:
            raise MosaicError("grid.max_range_km must be between 10 and 600 km")
        return spec


@dataclass
class MergeSpec:
    enabled: bool = True
    range_scale_km: float = 50.0
    time_scale_s: float = 300.0
    beamwidth_deg: float = 1.0
    calibrate: bool = False
    reference_index: int = 0
    advect: bool = False
    motion: tuple[float, float] | None = None  # storm motion (u east, v north) in m/s
    average: str | None = None  # "linear" | "dbz"; None = linear for reflectivity, else dbz

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> MergeSpec:
        d = dict(d or {})
        spec = cls()
        if d.get("enabled") is not None:
            spec.enabled = bool(d["enabled"])
        for key in ("range_scale_km", "time_scale_s", "beamwidth_deg"):
            if d.get(key) is not None:
                setattr(spec, key, _number(d[key], f"merge.{key}"))
        spec.calibrate = bool(d.get("calibrate", False))
        spec.advect = bool(d.get("advect", False))
        if d.get("reference_index") is not None:
            spec.reference_index = int(_number(d["reference_index"], "merge.reference_index"))
        motion = d.get("motion")
        if motion is not None:
            try:
                if isinstance(motion, dict):
                    pair = (float(motion["u"]), float(motion["v"]))
                else:
                    pair = (float(motion[0]), float(motion[1]))
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise MosaicError("merge.motion must be [u, v] or {u, v} in m/s") from exc
            if not all(math.isfinite(c) for c in pair):
                raise MosaicError("merge.motion must be finite")
            spec.motion = pair
        average = d.get("average")
        if average is not None:
            if average not in ("linear", "dbz"):
                raise MosaicError("merge.average must be 'linear' or 'dbz'")
            spec.average = average
        if spec.range_scale_km < 0 or spec.time_scale_s < 0:
            raise MosaicError("merge scales must not be negative")
        if not 0.05 <= spec.beamwidth_deg <= 10.0:
            raise MosaicError("merge.beamwidth_deg must be between 0.05 and 10 degrees")
        return spec


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise MosaicError(f"{label} must be a number")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise MosaicError(f"{label} must be a number") from exc
    if not math.isfinite(out):
        raise MosaicError(f"{label} must be finite")
    return out


# ---------------------------------------------------------------------------
# Result records and the cache
# ---------------------------------------------------------------------------


@dataclass
class RadarStat:
    file_id: str | None
    name: str
    latitude: float
    longitude: float
    altitude_m: float
    volume_time: str | None
    coverage_fraction: float = 0.0
    share: float = 0.0
    bias_db: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "name": self.name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "altitude_m": self.altitude_m,
            "volume_time": self.volume_time,
            "coverage_fraction": self.coverage_fraction,
            "share": self.share,
            "bias_db": self.bias_db,
        }


@dataclass
class MosaicRecord:
    """Everything kept about a finished mosaic (float grids, geometry, rendering info)."""

    mosaic_id: str
    product: str
    variable: str
    units: str
    height_m: float | None
    threshold_dbz: float | None
    resolution_m: float
    # merged product on the Web-Mercator-uniform image grid, rows north -> south
    image: np.ndarray
    bbox: tuple[float, float, float, float]  # west, south, east, north (degrees)
    # AEQD working grid (kept for per-radar probing)
    origin: tuple[float, float]  # lat, lon
    x0: float
    y0: float
    cell_m: float
    radar_values: np.ndarray  # (radar, ny, nx) float32, product per radar
    level_m: np.ndarray  # (ny, nx) float32, height ASL where the merged product is taken
    radars: list[RadarStat]
    overlap_fraction: float
    vmin: float
    vmax: float
    cmap: str
    legend: list[list[Any]]
    png: bytes
    stats: dict[str, float | None]
    analysis_time: str | None
    merge: dict[str, Any]
    grid_info: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    z_ref_m: float = 0.0
    k_e: float = K_E
    created: float = field(default_factory=time.time)
    # Merged 3-D field on the AEQD working grid for the 3-D view: uint8 (nz, ny, nx), x fastest,
    # y south -> north, z upwards. code 0 = no echo / not measured, 1..255 map linearly onto
    # [volume_range[0], volume_range[1]] (the same packing the polar 3-D view uses).
    volume: np.ndarray | None = None
    volume_z_m: np.ndarray | None = None  # level heights (m ASL)
    volume_range: tuple[float, float] | None = None
    volume_note: str | None = None  # why there is no volume (when volume is None)
    # value mask the product was built with (None = none) and its wording ("hide below 0 dBZ")
    value_mask: dict[str, Any] | None = None
    value_mask_text: str | None = None
    # The merged product on the AEQD working grid (ny, nx; y south -> north; NaN = no radar
    # measured an echo) and which of its cells lie inside some radar's coverage: a nowcast needs
    # to tell a dry cell from one that was not observed (engine/nowcast).
    merged_aeqd: np.ndarray | None = None
    covered_aeqd: np.ndarray | None = None

    @property
    def nbytes(self) -> int:
        vol = 0 if self.volume is None else int(self.volume.nbytes)
        extra = sum(int(a.nbytes) for a in (self.merged_aeqd, self.covered_aeqd) if a is not None)
        return (
            int(self.image.nbytes + self.radar_values.nbytes + self.level_m.nbytes)
            + vol
            + extra
            + len(self.png)
        )

    @property
    def corners(self) -> list[list[float]]:
        w, s, e, n = self.bbox
        return [[w, n], [e, n], [e, s], [w, s]]

    def header(self) -> dict[str, Any]:
        """JSON-able description (without ``type``/``request_id``/frame counts)."""
        return {
            "mosaic_id": self.mosaic_id,
            "product": self.product,
            "variable": self.variable,
            "units": self.units,
            "height_m": self.height_m,
            "threshold_dbz": self.threshold_dbz,
            "grid": self.grid_info,
            "corners": self.corners,
            "vmin": self.vmin,
            "vmax": self.vmax,
            "cmap": self.cmap,
            "legend": self.legend,
            "stats": self.stats,
            "radars": [r.as_dict() for r in self.radars],
            "overlap_fraction": self.overlap_fraction,
            "analysis_time": self.analysis_time,
            "merge": self.merge,
            "warnings": self.warnings,
            "has_volume": self.volume is not None,
            "volume_shape": list(self.volume.shape) if self.volume is not None else None,
            "volume_note": self.volume_note,
            "value_mask": self.value_mask,
            "value_mask_text": self.value_mask_text,
        }


class MosaicCache:
    """LRU of :class:`MosaicRecord` bounded by item count and bytes (thread-safe)."""

    def __init__(self, max_items: int = CACHE_MAX_ITEMS, max_bytes: int = CACHE_MAX_BYTES):
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, MosaicRecord] = OrderedDict()
        self._lock = threading.RLock()

    def put(self, record: MosaicRecord) -> None:
        with self._lock:
            self._items[record.mosaic_id] = record
            self._items.move_to_end(record.mosaic_id)
            while len(self._items) > 1 and (
                len(self._items) > self.max_items
                or sum(r.nbytes for r in self._items.values()) > self.max_bytes
            ):
                self._items.popitem(last=False)

    def get(self, mosaic_id: str) -> MosaicRecord:
        with self._lock:
            record = self._items.get(mosaic_id)
            if record is None:
                raise MosaicError(
                    f"Unknown or expired mosaic '{mosaic_id}' (only the last "
                    f"{self.max_items} mosaics are kept); create it again"
                )
            self._items.move_to_end(mosaic_id)
            return record

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def discard(self, mosaic_id: str) -> None:
        """Forget one record (no error when it is already gone)."""
        with self._lock:
            self._items.pop(mosaic_id, None)

    def __len__(self) -> int:
        return len(self._items)


MOSAICS = MosaicCache()


# ---------------------------------------------------------------------------
# radarx access
# ---------------------------------------------------------------------------


def _radarx():
    """``(radarx.grid, radarx version)``; raises a friendly error when radarx is missing."""
    try:
        import radarx
        import radarx.grid as rgrid
    except ImportError as exc:
        raise MosaicError(
            "Multi-radar mosaics need the Python package radarx: pip install radarx"
        ) from exc
    if not hasattr(rgrid, "grid_radars"):
        raise MosaicError(
            "The installed radarx has no radarx.grid.grid_radars; upgrade it: pip install -U radarx"
        )
    return rgrid, str(getattr(radarx, "__version__", "?"))


def radarx_available() -> bool:
    try:
        _radarx()
    except MosaicError:
        return False
    return True


# ---------------------------------------------------------------------------
# Volume preparation
# ---------------------------------------------------------------------------


@dataclass
class _Volume:
    file_id: str | None
    tree: xr.DataTree
    name: str
    latitude: float
    longitude: float
    altitude_m: float
    time: np.datetime64
    max_range_m: float
    units: str


def _canonical(variable: str) -> str:
    role = varmap.role_for_name(variable)
    return role.legacy if role is not None else variable


def _find_name(ds: xr.Dataset, variable: str) -> str | None:
    """Name of ``variable`` (or the variable playing its role) in a sweep dataset."""
    match = varmap.resolve(ds, variable)
    return match.variable if match is not None else None


def _site(tree: xr.DataTree, ds0: xr.Dataset) -> tuple[float, float, float]:
    root = tree.root.to_dataset()
    out: dict[str, float] = {}
    for key in ("latitude", "longitude", "altitude"):
        for src in (ds0, root):
            if key in src.variables:
                with contextlib.suppress(TypeError, ValueError):
                    val = float(np.asarray(src[key].values).ravel()[0])
                    if math.isfinite(val):
                        out[key] = val
                        break
    if "latitude" not in out or "longitude" not in out:
        raise MosaicError("a volume has no radar site latitude/longitude")
    return out["latitude"], out["longitude"], out.get("altitude", 0.0)


def _volume_time(sweeps: Sequence[xr.Dataset]) -> np.datetime64:
    """Mean ray time of the volume (``NaT`` if the sweeps carry no time)."""
    stamps = []
    for ds in sweeps:
        if "time" in ds.variables:
            t = np.ravel(ds["time"].values).astype("datetime64[ns]")
            t = t[~np.isnat(t)]
            if t.size:
                stamps.append(t)
    if not stamps:
        return np.datetime64("NaT", "ns")
    t = np.concatenate(stamps)
    t0 = t.min()
    return t0 + (t - t0).astype(np.int64).mean().astype("timedelta64[ns]")


def _iso(t: np.datetime64 | None) -> str | None:
    if t is None or np.isnat(t):
        return None
    return str(np.datetime_as_string(t.astype("datetime64[s]"), unit="s")) + "Z"


def _prepare_volume(
    file_id: str | None,
    reader: Any,
    variable: str,
    index: int,
    qc_rhohv: float | None,
) -> _Volume:
    """In-memory radarx-ready tree of one open volume with ``variable`` under its own name."""
    from engine.reader import mask_below_threshold, sorted_sweep_names

    tree = getattr(reader, "datatree", None)
    if tree is None:
        raise MosaicError("a selected file is not open any more")
    names = sorted_sweep_names(tree)
    if not names:
        raise MosaicError("a selected file has no sweeps")
    keep_aux = {"RHOHV"} if qc_rhohv else set()
    nodes: dict[str, xr.Dataset] = {}
    max_range = 0.0
    for i, _ in enumerate(names):
        ds = reader.get_sweep_dataset(i, mask=False)  # mask just the moments used (below)
        if "azimuth" not in ds.coords or "range" not in ds.coords:
            continue
        mode = str(np.asarray(ds["sweep_mode"].values).ravel()[0]) if "sweep_mode" in ds else ""
        if "rhi" in mode.lower():
            continue
        actual = _find_name(ds, variable)
        if actual is None:
            continue
        wanted = [actual]
        for aux in keep_aux:
            found = _find_name(ds, aux)
            if found and found not in wanted:
                wanted.append(found)
        sub = mask_below_threshold(ds[wanted])
        for extra in ("sweep_fixed_angle",):
            if extra in ds.variables:
                sub = sub.assign({extra: ds[extra]}) if extra not in sub else sub
        sub = sub.load()
        rename = {}
        if actual != variable:
            rename[actual] = variable
        for aux in keep_aux:
            found = _find_name(sub, aux)
            if found and found != aux:
                rename[found] = aux
        if rename:
            sub = sub.rename(rename)
        max_range = max(max_range, float(np.nanmax(np.asarray(ds["range"].values))))
        nodes[f"/sweep_{len(nodes)}"] = sub
    if not nodes:
        raise MosaicError(f"'{variable}' is not in any sweep of file {index + 1}")
    first = next(iter(nodes.values()))
    lat, lon, alt = _site(tree, reader.get_sweep_dataset(0, mask=False))
    nodes["/"] = xr.Dataset(
        coords={
            "latitude": np.float64(lat),
            "longitude": np.float64(lon),
            "altitude": np.float64(alt),
        }
    )
    prepared = xr.DataTree.from_dict(nodes)
    name = _instrument_name(tree, reader, index)
    units = str(
        first[variable].attrs.get("units", "") or _DEFAULT_UNITS.get(_canonical(variable), "")
    )
    return _Volume(
        file_id=file_id,
        tree=prepared,
        name=name,
        latitude=lat,
        longitude=lon,
        altitude_m=alt,
        time=_volume_time([n for k, n in nodes.items() if k != "/"]),
        max_range_m=max_range,
        units=units,
    )


def _instrument_name(tree: xr.DataTree, reader: Any, index: int) -> str:
    for src in (getattr(tree, "attrs", {}), getattr(tree.root.to_dataset(), "attrs", {})):
        val = src.get("instrument_name")
        if val and str(val) != "None":
            return str(val)
    path = getattr(reader, "_path", None)
    if path:
        return Path(str(path)).name.split(".")[0][:12] or f"radar_{index}"
    return f"radar_{index}"


def _unique(names: Sequence[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in names:
        seen[n] = seen.get(n, 0) + 1
        out.append(n if seen[n] == 1 else f"{n}_{seen[n]}")
    return out


# ---------------------------------------------------------------------------
# Geometry: extent, grids, Web Mercator
# ---------------------------------------------------------------------------


def _merc_y(lat_deg: np.ndarray | float) -> np.ndarray | float:
    return MERC_R * np.log(np.tan(np.pi / 4.0 + np.radians(lat_deg) / 2.0))


def _merc_lat(y: np.ndarray) -> np.ndarray:
    return np.degrees(2.0 * np.arctan(np.exp(np.asarray(y) / MERC_R)) - np.pi / 2.0)


def _aeqd(lat0: float, lon0: float):
    import pyproj

    return pyproj.Proj(proj="aeqd", lat_0=lat0, lon_0=lon0, datum="WGS84")


def _auto_bbox(volumes: Sequence[_Volume], max_range_km: float) -> tuple[float, ...]:
    """Lon/lat box around the union of the radars' coverage discs."""
    import pyproj

    geod = pyproj.Geod(ellps="WGS84")
    az = np.linspace(0.0, 360.0, 73)[:-1]
    lons, lats = [], []
    for v in volumes:
        reach = min(max_range_km * 1000.0, v.max_range_m) if v.max_range_m > 0 else 0.0
        if reach <= 0:
            continue
        lo, la, _ = geod.fwd(
            np.full(az.shape, v.longitude),
            np.full(az.shape, v.latitude),
            az,
            np.full(az.shape, reach),
        )
        lons.append(lo)
        lats.append(la)
    if not lons:
        raise MosaicError("the selected radars have no range gates")
    lon = np.concatenate(lons)
    lat = np.concatenate(lats)
    if lon.max() - lon.min() > 180.0:
        raise MosaicError("the radar network crosses the antimeridian; pick an explicit extent")
    return (
        float(lon.min()),
        float(max(lat.min(), -MAX_MERC_LAT)),
        float(lon.max()),
        float(min(lat.max(), MAX_MERC_LAT)),
    )


def _parse_bbox(extent: Any) -> tuple[float, float, float, float]:
    try:
        west, south, east, north = (float(extent[k]) for k in ("west", "south", "east", "north"))
    except (KeyError, TypeError, ValueError) as exc:
        raise MosaicError("grid.extent must be 'auto' or {west, south, east, north}") from exc
    if not all(math.isfinite(c) for c in (west, south, east, north)):
        raise MosaicError("grid.extent must be finite")
    if not (-180 <= west < east <= 180):
        raise MosaicError("grid.extent: need -180 <= west < east <= 180")
    if not (-MAX_MERC_LAT <= south < north <= MAX_MERC_LAT):
        raise MosaicError(
            f"grid.extent: need -{MAX_MERC_LAT:g} <= south < north <= {MAX_MERC_LAT:g}"
        )
    return west, south, east, north


@dataclass
class _Layout:
    bbox: tuple[float, float, float, float]
    origin: tuple[float, float]  # lat, lon
    x: np.ndarray
    y: np.ndarray
    cell_m: float
    nx_img: int
    ny_img: int


def _layout(bbox: tuple[float, float, float, float], cell_m: float) -> _Layout:
    west, south, east, north = bbox
    lat_c, lon_c = 0.5 * (south + north), 0.5 * (west + east)
    proj = _aeqd(lat_c, lon_c)
    t = np.linspace(0.0, 1.0, 201)
    edge_lon = np.concatenate(
        [west + t * (east - west)] * 2 + [np.full_like(t, west)] + [np.full_like(t, east)]
    )
    edge_lat = np.concatenate(
        [np.full_like(t, south), np.full_like(t, north)] + [south + t * (north - south)] * 2
    )
    ex, ey = proj(edge_lon, edge_lat)
    pad = 2.0 * cell_m
    xs = np.arange(
        math.floor((ex.min() - pad) / cell_m) * cell_m,
        math.ceil((ex.max() + pad) / cell_m) * cell_m + 0.5 * cell_m,
        cell_m,
    )
    ys = np.arange(
        math.floor((ey.min() - pad) / cell_m) * cell_m,
        math.ceil((ey.max() + pad) / cell_m) * cell_m + 0.5 * cell_m,
        cell_m,
    )
    if xs.size * ys.size > MAX_GRID_CELLS:
        raise MosaicError(
            f"The mosaic grid would have {xs.size * ys.size / 1e6:.1f} million cells "
            f"({xs.size} x {ys.size}); increase the resolution or shrink the extent"
        )
    # Image pixels: ~cell_m of ground distance at the box's mid latitude.
    px = cell_m / math.cos(math.radians(lat_c))
    wm = MERC_R * math.radians(east - west)
    hm = float(_merc_y(north) - _merc_y(south))
    nx_img = max(2, math.ceil(wm / px))
    ny_img = max(2, math.ceil(hm / px))
    if nx_img * ny_img > MAX_IMAGE_PIXELS:
        raise MosaicError(
            f"The mosaic image would be {nx_img} x {ny_img} pixels; increase the resolution "
            "or shrink the extent"
        )
    return _Layout(bbox, (lat_c, lon_c), xs, ys, cell_m, nx_img, ny_img)


def _image_axes(
    bbox: tuple[float, float, float, float], nx: int, ny: int
) -> tuple[np.ndarray, np.ndarray]:
    """Pixel-centre longitude (nx) and latitude (ny, north first) of the Web-Mercator image."""
    west, south, east, north = bbox
    xm = np.radians(west) * MERC_R + (np.arange(nx) + 0.5) * (
        MERC_R * math.radians(east - west) / nx
    )
    y_top, y_bot = float(_merc_y(north)), float(_merc_y(south))
    ym = y_top - (np.arange(ny) + 0.5) * ((y_top - y_bot) / ny)
    return np.degrees(xm / MERC_R), _merc_lat(ym)


def _bilinear_nan(field2d: np.ndarray, fx: np.ndarray, fy: np.ndarray) -> np.ndarray:
    """Bilinear sample of ``field2d`` at fractional (col ``fx``, row ``fy``) indices.

    NaN cells do not contaminate their neighbours: the value is the weight-normalised mean of
    the valid corners and is kept only where at least half of the interpolation weight is valid
    (a crisp edge at the data boundary). Fully vectorised, no per-pixel Python.
    """
    ny, nx = field2d.shape
    ok_pix = (fx >= -0.5) & (fx <= nx - 0.5) & (fy >= -0.5) & (fy <= ny - 0.5)
    x0 = np.floor(fx).astype(np.int64)
    y0 = np.floor(fy).astype(np.int64)
    tx = (fx - x0).astype(np.float32)
    ty = (fy - y0).astype(np.float32)
    num = np.zeros(fx.shape, dtype=np.float32)
    den = np.zeros(fx.shape, dtype=np.float32)
    for dy, wy in ((0, 1.0 - ty), (1, ty)):
        yy = y0 + dy
        row_ok = (yy >= 0) & (yy < ny)
        yc = np.clip(yy, 0, ny - 1)
        for dx, wx in ((0, 1.0 - tx), (1, tx)):
            xx = x0 + dx
            ok = row_ok & (xx >= 0) & (xx < nx)
            vals = field2d[yc, np.clip(xx, 0, nx - 1)]
            good = ok & np.isfinite(vals)
            w = np.where(good, wy * wx, 0.0).astype(np.float32)
            num += w * np.where(good, vals, 0.0)
            den += w
    with np.errstate(invalid="ignore", divide="ignore"):
        out = num / den
    out[~(ok_pix & (den >= 0.5))] = np.nan
    return out


def _resample_to_image(field2d: np.ndarray, layout: _Layout) -> np.ndarray:
    """Resample an AEQD ``(ny, nx)`` field to the Web-Mercator-uniform image grid."""
    lon, lat = _image_axes(layout.bbox, layout.nx_img, layout.ny_img)
    proj = _aeqd(*layout.origin)
    # x, y vary smoothly over the image: evaluate PROJ on a coarse lattice and interpolate.
    step = 4
    ci = np.unique(np.r_[np.arange(0, lon.size, step), lon.size - 1])
    cj = np.unique(np.r_[np.arange(0, lat.size, step), lat.size - 1])
    glon, glat = np.meshgrid(lon[ci], lat[cj])
    cx, cy = proj(glon, glat)
    from scipy import ndimage

    ii = np.interp(np.arange(lon.size), ci, np.arange(ci.size))
    jj = np.interp(np.arange(lat.size), cj, np.arange(cj.size))
    coords = np.stack(np.meshgrid(ii, jj))[::-1]  # (row, col) order for map_coordinates
    xs = ndimage.map_coordinates(cx, coords, order=1, mode="nearest")
    ys = ndimage.map_coordinates(cy, coords, order=1, mode="nearest")
    fx = (xs - layout.x[0]) / layout.cell_m
    fy = (ys - layout.y[0]) / layout.cell_m
    return _bilinear_nan(np.asarray(field2d, dtype=np.float32), fx, fy)


# ---------------------------------------------------------------------------
# Reductions (z axis is -3)
# ---------------------------------------------------------------------------


def _composite(vals: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Column maximum and the index of the level it occurs at."""
    comp = np.fmax.reduce(vals, axis=-3)
    idx = np.argmax(np.where(np.isnan(vals), -np.inf, vals), axis=-3)
    return comp, idx


def _echo_top(
    vals: np.ndarray, z: np.ndarray, threshold: float, no_echo: float = -14.0
) -> tuple[np.ndarray, np.ndarray]:
    """Echo-top height (m ASL) of every column, after Lakshmanan et al. (2013).

    The highest level with ``value >= threshold`` is found; if a higher level exists the top is
    interpolated linearly in dBZ between the two (a missing or weaker level above counts as
    ``no_echo``); otherwise it is half a level spacing above the highest level. Returns
    ``(top, index of the highest level at/above the threshold)``; NaN/0 where there is none.
    """
    nz = vals.shape[-3]
    hit = vals >= threshold  # NaN compares False
    anyhit = hit.any(axis=-3)
    kb = nz - 1 - np.argmax(hit[..., ::-1, :, :], axis=-3)
    kb = np.where(anyhit, kb, 0)
    zb = z[kb]
    vb = np.take_along_axis(vals, kb[..., None, :, :], axis=-3)[..., 0, :, :]
    ka = np.minimum(kb + 1, nz - 1)
    va = np.take_along_axis(vals, ka[..., None, :, :], axis=-3)[..., 0, :, :]
    va = np.where(np.isfinite(va), va, no_echo)
    za = z[ka]
    dz = float(np.median(np.diff(z))) if nz > 1 else 0.0
    with np.errstate(invalid="ignore", divide="ignore"):
        frac = (vb - threshold) / (vb - va)
    interp = zb + np.clip(frac, 0.0, 1.0) * (za - zb)
    top = np.where(kb < nz - 1, interp, zb + 0.5 * dz)
    top = np.where(anyhit, top, np.nan).astype(np.float32)
    return top, kb


# ---------------------------------------------------------------------------
# The mosaic
# ---------------------------------------------------------------------------


def _beam_weight_fn():
    """radarx's elevation weight (private helper); ``None`` if this radarx has changed."""
    try:
        from radarx.grid import multi

        return multi._beam_weight_numpy
    except (ImportError, AttributeError):  # pragma: no cover - depends on the radarx version
        return None


def _antenna_elevation(rng: np.ndarray, z: np.ndarray, site_alt: float, k_e: float) -> np.ndarray:
    """Antenna elevation (deg) of cells at slant range ``rng`` and height ``z`` (4/3 Earth)."""
    big_r = EARTH_RADIUS * k_e
    a = big_r + site_alt
    b = big_r + z
    with np.errstate(invalid="ignore", divide="ignore"):
        cos_t = np.clip((a * a + b * b - rng * rng) / (2.0 * a * b), -1.0, 1.0)
    sin_t = np.sqrt(1.0 - cos_t * cos_t)
    return np.degrees(np.arctan2(b * cos_t - a, b * sin_t))


@dataclass
class _Bands:
    """Accumulators filled band by band."""

    ny: int
    nx: int
    nr: int
    merged: np.ndarray = field(init=False)
    radars: np.ndarray = field(init=False)
    level: np.ndarray = field(init=False)
    inside: int = 0
    covered_any: int = 0
    overlap: int = 0
    valid: int = 0
    cover_count: np.ndarray = field(init=False)
    share_count: np.ndarray = field(init=False)
    covered: np.ndarray = field(init=False)  # (ny, nx) bool: some radar covers the cell

    def __post_init__(self) -> None:
        self.merged = np.full((self.ny, self.nx), np.nan, dtype=np.float32)
        self.radars = np.full((self.nr, self.ny, self.nx), np.nan, dtype=np.float32)
        self.level = np.full((self.ny, self.nx), np.nan, dtype=np.float32)
        self.cover_count = np.zeros(self.nr, dtype=np.int64)
        self.share_count = np.zeros(self.nr, dtype=np.int64)
        self.covered = np.zeros((self.ny, self.nx), dtype=bool)


@dataclass
class _VolumeAcc:
    """Collects the merged 3-D field band by band (float16), quantised once the range is known."""

    keep: np.ndarray  # indices (into the gridding z axis) of the levels that are kept
    buf: np.ndarray  # (len(keep), ny, nx) float16, NaN = no echo

    @classmethod
    def create(cls, keep: np.ndarray, ny: int, nx: int) -> _VolumeAcc:
        return cls(keep, np.full((keep.size, ny, nx), np.nan, dtype=np.float16))


def _quantise_volume(buf: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """float (nz, ny, nx) -> uint8: 0 = no data (NaN or below ``lo``), 1..255 over ``[lo, hi]``."""
    out = np.zeros(buf.shape, dtype=np.uint8)
    scale = 254.0 / (hi - lo)
    for k in range(buf.shape[0]):  # one level at a time keeps the float32 temporary small
        v = buf[k].astype(np.float32)
        ok = np.isfinite(v) & (v >= lo)
        code = np.clip(np.rint((np.where(ok, v, lo) - lo) * scale) + 1.0, 1.0, 255.0)
        out[k] = np.where(ok, code, 0.0).astype(np.uint8)
    return out


def _volume_range(
    variable: str, product: str, buf: np.ndarray, vmin: float | None, vmax: float | None
) -> tuple[float, float]:
    """Value range the 8-bit codes of the 3-D field span."""
    if product != "echo_top" and vmin is not None and vmax is not None:
        return float(vmin), float(vmax)
    known = _known_range(variable)
    if known is not None:
        return known
    finite = buf[np.isfinite(buf)][:: max(1, buf.size // 2_000_000)].astype(np.float32)
    if finite.size == 0:
        return 0.0, 1.0
    lo, hi = np.percentile(finite, [2.0, 98.0])
    return (float(lo), float(hi if hi > lo else lo + 1.0))


def build_mosaic(
    readers: Sequence[tuple[str | None, Any]],
    variable: str = "DBZH",
    product: str = "cappi",
    grid: GridSpec | dict[str, Any] | None = None,
    merge: MergeSpec | dict[str, Any] | None = None,
    *,
    height_m: float = 3000.0,
    threshold_dbz: float = 18.0,
    vmin: float | None = None,
    vmax: float | None = None,
    min_rhohv: float | None = None,
    progress: ProgressFn | None = None,
    n_threads: int | None = None,
    engine: str = "auto",
    cache: MosaicCache | None = MOSAICS,
    keep_volume: bool | None = None,
    value_mask: dict[str, Any] | None = None,
) -> MosaicRecord:
    """Build a mosaic of the volumes behind ``readers`` (``[(file_id, RadarReader), ...]``).

    ``grid`` / ``merge`` are :class:`GridSpec` / :class:`MergeSpec` or the corresponding dicts of
    the ``create_mosaic`` message. ``height_m`` (metres above sea level) is used by ``cappi``,
    ``threshold_dbz`` by ``echo_top``. The result (also stored in ``cache``) carries the PNG, the
    corner coordinates and the statistics; see :meth:`MosaicRecord.header`.

    ``keep_volume`` keeps the merged 3-D field (all levels) for the 3-D view: ``None`` (default)
    keeps it when it fits :data:`VOLUME_MAX_VOXELS`, ``False`` never, ``True`` whenever it fits.
    A ``cappi`` normally grids a single level; keeping the volume grids every level as well.

    ``value_mask`` hides values of the finished product (and each radar's own value) the way the
    viewer's value mask does (:mod:`engine.valuemask`): hidden cells become NaN in the image, the
    statistics, probes and exports. The 3-D volume is not changed: the 3-D view masks it live.
    """
    t_start = time.perf_counter()
    report = progress or (lambda _p, _m: None)
    if not readers:
        raise MosaicError("select at least one radar file")
    if product not in PRODUCTS:
        raise MosaicError(f"product must be one of {', '.join(PRODUCTS)}")
    if not isinstance(variable, str) or not variable:
        raise MosaicError("variable must be a non-empty string")
    gspec = grid if isinstance(grid, GridSpec) else GridSpec.from_dict(grid)
    mspec = merge if isinstance(merge, MergeSpec) else MergeSpec.from_dict(merge)
    height_m = _number(height_m, "height_m")
    threshold_dbz = _number(threshold_dbz, "threshold_dbz")
    if product == "cappi" and not -500.0 <= height_m <= 30000.0:
        raise MosaicError("height_m must be between -500 and 30000 metres")
    canon = _canonical(variable)
    if product == "echo_top" and canon != "DBZH":
        raise MosaicError("echo_top needs a reflectivity variable (e.g. DBZH)")
    rgrid, rx_version = _radarx()
    if not 0 <= mspec.reference_index < len(readers):
        raise MosaicError("merge.reference_index is out of range")
    warnings_out: list[str] = []
    # Resource governor: changes the grid only when the estimate exceeds the machine budget;
    # every change is stated (warnings, grid_info, NetCDF attributes). Refuses if nothing fits.
    from engine import governor

    reduced = governor.govern_grid("mosaic", readers, gspec)
    warnings_out += [r.sentence() for r in reduced]

    # 1. volumes ---------------------------------------------------------------------------
    report(8, "Preparing radar volumes")
    t_prep0 = time.perf_counter()
    volumes = [
        _prepare_volume(fid, rd, variable, i, min_rhohv) for i, (fid, rd) in enumerate(readers)
    ]
    t_prepare = time.perf_counter() - t_prep0
    names = _unique([v.name for v in volumes])
    nr = len(volumes)
    units = next((v.units for v in volumes if v.units), _DEFAULT_UNITS.get(canon, ""))
    is_refl = canon == "DBZH" or units.lower().startswith("dbz")
    if canon in ("VRADH",) and nr > 1:
        warnings_out.append(
            "Radial velocities of different radars look along different beams; merging them "
            "is not physically meaningful."
        )
    times = [v.time for v in volumes if not np.isnat(v.time)]
    analysis_time = max(times) if times else None

    # 2. extent and grids --------------------------------------------------------------------
    bbox = (
        _auto_bbox(volumes, gspec.max_range_km)
        if gspec.extent in (None, "auto")
        else _parse_bbox(gspec.extent)
    )
    layout = _layout(bbox, gspec.resolution_m)
    x, y = layout.x, layout.y
    levels = np.arange(gspec.z_step_m, gspec.z_max_m + 0.5 * gspec.z_step_m, gspec.z_step_m)
    voxels = int(levels.size) * int(y.size) * int(x.size)
    keep_vol = keep_volume is not False and voxels <= VOLUME_MAX_VOXELS
    volume_note: str | None = None
    if keep_volume is False:
        volume_note = "The 3-D volume was not kept (keep_volume is off)."
    elif not keep_vol:
        volume_note = (
            f"The 3-D volume was not kept: {voxels / 1e6:.0f} million voxels is over the "
            f"{VOLUME_MAX_VOXELS / 1e6:.0f} million limit; use a coarser resolution, a smaller "
            "extent or a lower top."
        )
        warnings_out.append(volume_note)
    cappi_idx = 0
    if product == "cappi" and keep_vol:
        # grid the cappi height together with the volume levels (one radarx call per band)
        z = np.unique(np.concatenate([levels, [height_m]]))
        cappi_idx = int(np.argmin(np.abs(z - height_m)))
        keep_idx = np.flatnonzero(np.isclose(z[:, None], levels[None, :]).any(axis=1))
    elif product == "cappi":
        z = np.array([height_m], dtype=np.float64)
        keep_idx = np.zeros(0, dtype=np.int64)
    else:
        z = levels
        keep_idx = np.arange(levels.size) if keep_vol else np.zeros(0, dtype=np.int64)
    nz = z.size
    average = mspec.average or ("linear" if is_refl else "dbz")
    do_merge = nr > 1 and mspec.enabled
    advect_pair = None
    if mspec.advect:
        if mspec.motion is None:
            warnings_out.append(
                "Advection needs a storm motion (merge.motion = [u, v] in m/s); it was skipped."
            )
        else:
            advect_pair = mspec.motion
    common: dict[str, Any] = {
        "origin": layout.origin,
        "names": names,
        "beamwidth": mspec.beamwidth_deg,
        "range_scale": mspec.range_scale_km * 1000.0,
        "time_scale": mspec.time_scale_s,
        "max_range": gspec.max_range_km * 1000.0,
        "rhohv": "RHOHV",
        "min_rhohv": min_rhohv,
        "n_threads": n_threads,
        "engine": engine,
    }
    if analysis_time is not None:
        common["time"] = analysis_time
    if advect_pair is not None:
        common["motion"] = advect_pair
        common["advect_sweeps"] = True

    # 3. calibration (coarse pre-pass, bias applied to the polar data) ------------------------
    biases: list[float | None] = [None] * nr
    if mspec.calibrate and nr > 1:
        report(12, "Estimating radar biases")
        biases = _estimate_biases(rgrid, volumes, variable, layout, gspec, mspec, common)
        for i, b in enumerate(biases):
            if b is not None and abs(b) > 0:
                volumes[i].tree = _apply_bias(volumes[i].tree, variable, b)
            elif b is None:
                warnings_out.append(
                    f"No bias could be estimated for {names[i]} (too little overlapping echo "
                    "between 15 and 50 dBZ); it was left uncorrected."
                )
    elif mspec.calibrate:
        warnings_out.append("Calibration needs at least two radars; skipped.")

    # 4. banded gridding and reduction ---------------------------------------------------------
    ny, nx = y.size, x.size
    acc = _Bands(ny, nx, nr)
    vol_acc = _VolumeAcc.create(keep_idx, ny, nx) if keep_idx.size else None
    per_row = nz * (nr * 36 + 28) * nx
    rows = int(max(16, min(ny, BAND_BUDGET_BYTES // max(per_row, 1))))
    n_bands = -(-ny // rows)
    beam_fn = _beam_weight_fn() if do_merge else None
    # Cells below the colour range are "no echo" (some readers store a -33 dBZ sentinel instead of
    # NaN): they are not drawn and do not count as valid / in the merge share.
    cut = -np.inf
    if product != "echo_top":
        if vmin is not None:
            cut = _number(vmin, "vmin")
        else:
            known = _known_range(variable)
            cut = known[0] if known else -np.inf
    t_grid = 0.0
    t_reduce = 0.0
    inside_fn = _InsideBox(layout.bbox)
    for band, r0 in enumerate(range(0, ny, rows)):
        r1 = min(ny, r0 + rows)
        report(
            15 + int(65 * band / n_bands),
            f"Gridding radars onto {nx} x {ny} cells (band {band + 1}/{n_bands})",
        )
        t0 = time.perf_counter()
        out = rgrid.grid_radars(
            [v.tree for v in volumes],
            x,
            y[r0:r1],
            z,
            data_vars=[variable],
            merge=[variable] if do_merge else None,
            **common,
        )
        t1 = time.perf_counter()
        _reduce_band(
            out,
            acc,
            r0,
            r1,
            variable,
            product,
            z,
            threshold_dbz,
            do_merge,
            average,
            mspec,
            volumes,
            analysis_time,
            advect_pair is not None,
            beam_fn,
            inside_fn,
            cut,
            cappi_idx,
            vol_acc,
        )
        t_grid += t1 - t0
        t_reduce += time.perf_counter() - t1
        del out

    # 5. resample + render ---------------------------------------------------------------------
    report(84, "Resampling to the map grid")
    t0 = time.perf_counter()
    image = _resample_to_image(acc.merged, layout)
    t_resample = time.perf_counter() - t0

    if product == "echo_top":
        image = (image / 1000.0).astype(np.float32)
        acc.radars = (acc.radars / 1000.0).astype(np.float32)
        units_out = "km"
        lo_default, hi_default = 0.0, float(math.ceil(gspec.z_max_m / 1000.0))
    else:
        units_out = units
        lo_default, hi_default = _default_range(variable, image)
    vmin_f = float(lo_default if vmin is None else _number(vmin, "vmin"))
    vmax_f = float(hi_default if vmax is None else _number(vmax, "vmax"))
    if vmax_f <= vmin_f:
        raise MosaicError("vmax must be greater than vmin")
    vmask = valuemask.normalize(value_mask)
    vmask_text = valuemask.describe(vmask, units_out) if vmask else None
    if vmask is not None:
        image, _ = valuemask.apply(image, vmask)
        acc.radars, _ = valuemask.apply(acc.radars, vmask)
    cmap_name = _ECHO_TOP_CMAP if product == "echo_top" else _CMAPS.get(canon, _FALLBACK_CMAP)
    report(92, "Rendering PNG")
    t0 = time.perf_counter()
    lut, cmap_used = _build_lut(cmap_name)
    png = render_png(image, lut, vmin_f, vmax_f)
    t_png = time.perf_counter() - t0
    legend = _legend(lut, vmin_f, vmax_f)

    # 5b. the merged 3-D field, quantised against the colour range -------------------------------
    volume = None
    volume_z = None
    volume_rng = None
    if vol_acc is not None:
        report(95, "Packing the 3-D volume")
        volume_rng = _volume_range(variable, product, vol_acc.buf, vmin, vmax)
        volume = _quantise_volume(vol_acc.buf, *volume_rng)
        volume_z = z[vol_acc.keep].astype(np.float64)
        if not volume.any():
            volume_note = "The 3-D volume is empty: no radar has an echo above the colour range."
        vol_acc = None  # drop the float16 collector

    # 6. statistics ----------------------------------------------------------------------------
    finite = image[np.isfinite(image) & (image >= vmin_f)]
    stats = {
        "valid_fraction": float(finite.size / image.size) if image.size else 0.0,
        "min": float(finite.min()) if finite.size else None,
        "max": float(finite.max()) if finite.size else None,
        "mean": float(finite.mean()) if finite.size else None,
    }
    inside = max(acc.inside, 1)
    shares = acc.share_count / max(acc.valid, 1)
    radar_stats = []
    for i, v in enumerate(volumes):
        radar_stats.append(
            RadarStat(
                file_id=v.file_id,
                name=names[i],
                latitude=v.latitude,
                longitude=v.longitude,
                altitude_m=v.altitude_m,
                volume_time=_iso(v.time),
                coverage_fraction=float(acc.cover_count[i] / inside),
                share=float(shares[i]),
                bias_db=biases[i] if (mspec.calibrate and nr > 1) else None,
            )
        )
    west, south, east, north = layout.bbox
    elapsed_ms = (time.perf_counter() - t_start) * 1000.0
    timing = {
        "prepare_ms": round(t_prepare * 1000.0, 1),
        "radarx_ms": round(t_grid * 1000.0, 1),
        "reduce_ms": round(t_reduce * 1000.0, 1),
        "resample_ms": round(t_resample * 1000.0, 1),
        "png_ms": round(t_png * 1000.0, 1),
        "total_ms": round(elapsed_ms, 1),
    }
    if acc.merged.size and not np.isfinite(acc.merged).any():
        warnings_out.append("The mosaic is empty: no radar has data at this product level.")
    record = MosaicRecord(
        mosaic_id=uuid.uuid4().hex[:12],
        product=product,
        variable=variable,
        units=units_out,
        height_m=height_m if product == "cappi" else None,
        threshold_dbz=threshold_dbz if product == "echo_top" else None,
        resolution_m=gspec.resolution_m,
        image=image,
        bbox=layout.bbox,
        origin=layout.origin,
        x0=float(x[0]),
        y0=float(y[0]),
        cell_m=layout.cell_m,
        radar_values=acc.radars,
        level_m=acc.level,
        radars=radar_stats,
        overlap_fraction=float(acc.overlap / max(acc.covered_any, 1)),
        vmin=vmin_f,
        vmax=vmax_f,
        cmap=cmap_used,
        legend=legend,
        png=png,
        stats=stats,
        analysis_time=_iso(analysis_time) if analysis_time is not None else None,
        merge={
            "enabled": bool(mspec.enabled),
            "applied": bool(do_merge),
            "average": average,
            "calibrate": bool(mspec.calibrate and nr > 1),
            "advect": advect_pair is not None,
            "range_scale_km": mspec.range_scale_km,
            "time_scale_s": mspec.time_scale_s,
            "beamwidth_deg": mspec.beamwidth_deg,
            "radarx": rx_version,
            "timing": timing,
        },
        grid_info={
            "nx": layout.nx_img,
            "ny": layout.ny_img,
            "resolution_m": gspec.resolution_m,
            "west": west,
            "south": south,
            "east": east,
            "north": north,
            "cells_aeqd": [nx, ny],
            "levels": int(levels.size if volume is not None else nz),
            "reductions": [r.to_dict() for r in reduced],
        },
        warnings=warnings_out,
        z_ref_m=float(height_m if product == "cappi" else z[0]),
        volume=volume,
        volume_z_m=volume_z,
        volume_range=volume_rng,
        volume_note=volume_note,
        value_mask=vmask,
        value_mask_text=vmask_text,
        merged_aeqd=acc.merged,
        covered_aeqd=acc.covered,
    )
    if cache is not None:
        cache.put(record)
    logger.info(
        "Mosaic %s %s %s: %d radars, %dx%d cells, %s",
        record.mosaic_id,
        product,
        variable,
        nr,
        nx,
        ny,
        timing,
    )
    report(98, "Mosaic ready")
    return record


class _InsideBox:
    """Which AEQD cells fall inside the lon/lat box of the image."""

    def __init__(self, bbox: tuple[float, float, float, float]):
        self.bbox = bbox

    def __call__(self, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
        w, s, e, n = self.bbox
        return (lon >= w) & (lon <= e) & (lat >= s) & (lat <= n)


def _reduce_band(
    out: xr.Dataset,
    acc: _Bands,
    r0: int,
    r1: int,
    variable: str,
    product: str,
    z: np.ndarray,
    threshold: float,
    do_merge: bool,
    average: str,
    mspec: MergeSpec,
    volumes: Sequence[_Volume],
    analysis_time: np.datetime64 | None,
    advected: bool,
    beam_fn: Callable | None,
    inside_fn: _InsideBox,
    cut: float,
    cappi_idx: int = 0,
    vol_acc: _VolumeAcc | None = None,
) -> None:
    """Reduce one gridded band to 2-D and accumulate statistics into ``acc``.

    With ``vol_acc`` the merged 3-D field of the band is also stored (see :class:`_VolumeAcc`).
    """
    nr = len(volumes)
    fld = out[variable].transpose("radar", "z", "y", "x").values  # (nr, nz, cy, nx) float32
    cov = out["coverage"].transpose("radar", "z", "y", "x").values > 0
    rng = out["range"].transpose("radar", "z", "y", "x").values
    inside = inside_fn(out["lon"].values, out["lat"].values)
    if nr == 1:
        merged = fld[0]
    elif do_merge:
        merged = out[f"{variable}_merged"].transpose("z", "y", "x").values
    else:
        merged = np.fmax.reduce(fld, axis=0)

    # product reduction --------------------------------------------------------------------
    if product == "cappi":
        m2, kidx = merged[cappi_idx], np.full(merged.shape[-2:], cappi_idx, dtype=np.int64)
        per_radar = fld[:, cappi_idx]
        cov2 = cov[:, cappi_idx]
        level = np.full(m2.shape, float(z[cappi_idx]), dtype=np.float32)
    elif product == "composite":
        m2, kidx = _composite(merged)
        per_radar = np.fmax.reduce(fld, axis=-3)
        cov2 = cov.any(axis=1)
        level = z[kidx].astype(np.float32)
    else:
        m2, kidx = _echo_top(merged, z, threshold)
        per_radar, _ = _echo_top(fld, z, threshold)
        cov2 = cov.any(axis=1)
        level = np.where(np.isfinite(m2), m2, z[kidx]).astype(np.float32)
    if nr > 1 and do_merge:
        # radarx's merge gives a covered cell without an echo a low "no echo" value (each radar's
        # lowest reading) so that it takes part in the blend. That is right for the average at the
        # edge of a storm, but a cell where NO covering radar measured anything is not data: drawn,
        # it shows up as a flat plate over each radar's whole coverage disc. Leave it empty.
        m2 = np.where(np.isfinite(per_radar).any(axis=0), m2, np.nan)
    m2 = np.where(inside, m2, np.nan).astype(np.float32)
    if vol_acc is not None:
        # the same "no radar measured anything" rule as above, applied to every level
        vm = merged[vol_acc.keep]
        if nr > 1 and do_merge:
            vm = np.where(np.isfinite(fld[:, vol_acc.keep]).any(axis=0), vm, np.nan)
        vm = np.where(inside[None], vm, np.nan)
        with np.errstate(over="ignore", invalid="ignore"):
            vol_acc.buf[:, r0:r1] = np.clip(vm, -60000.0, 60000.0).astype(np.float16)
    acc.merged[r0:r1] = m2
    acc.radars[:, r0:r1] = per_radar
    acc.level[r0:r1] = level

    # statistics -----------------------------------------------------------------------------
    acc.inside += int(inside.sum())
    cov_in = cov2 & inside[None]
    acc.cover_count += cov_in.sum(axis=(1, 2))
    n_cov = cov_in.sum(axis=0)
    acc.covered[r0:r1] = n_cov >= 1
    acc.covered_any += int((n_cov >= 1).sum())
    acc.overlap += int((n_cov >= 2).sum())
    valid = np.isfinite(m2) & (m2 >= cut)
    acc.valid += int(valid.sum())
    if nr == 1:
        acc.share_count[0] += int(valid.sum())
        return
    # Which radar dominates each valid cell: the largest merge weight (range, beam, time
    # factors of radarx.grid.merge_radars) among the radars that cover the cell, or the
    # largest value when the radars are not merged.
    k = kidx[None, None]
    if do_merge:
        r_at = np.take_along_axis(rng, k, axis=1)[:, 0]
        score = np.zeros(r_at.shape, dtype=np.float64)
        elevations = out["sweep_elevation"].values
        for i in range(nr):
            w = np.ones(r_at.shape[1:], dtype=np.float64)
            if mspec.range_scale_km > 0:
                w *= np.exp(-((r_at[i] / (mspec.range_scale_km * 1000.0)) ** 2))
            if beam_fn is not None:
                el = _antenna_elevation(
                    r_at[i].astype(np.float64),
                    z[kidx],
                    volumes[i].altitude_m,
                    out.attrs.get("k_e", K_E),
                )
                sweep_el = [float(e) for e in elevations[i] if np.isfinite(e)]
                w *= beam_fn(el, sweep_el, mspec.beamwidth_deg)
            if (
                mspec.time_scale_s > 0
                and not advected
                and analysis_time is not None
                and not np.isnat(volumes[i].time)
            ):
                dt = float((volumes[i].time - analysis_time) / np.timedelta64(1, "s"))
                w *= math.exp(-((dt / mspec.time_scale_s) ** 2))
            covered_here = np.take_along_axis(cov, k, axis=1)[i, 0]
            score[i] = np.where(covered_here, w, -1.0)
    else:
        score = np.stack(
            [
                np.where(np.isnan(per), -np.inf, per)
                for per in np.take_along_axis(fld, k, axis=1)[:, 0]
            ]
        )
    winner = score.argmax(axis=0)
    for i in range(nr):
        acc.share_count[i] += int(((winner == i) & valid).sum())


def _known_range(variable: str) -> tuple[float, float] | None:
    """The renderer's colour range for ``variable`` (or its canonical name), if it has one."""
    from engine.renderer import _resolve_range

    lo, hi = _resolve_range(variable)
    if lo is None or hi is None:
        lo, hi = _resolve_range(_canonical(variable))
    if lo is not None and hi is not None:
        return float(lo), float(hi)
    return None


def _default_range(variable: str, image: np.ndarray) -> tuple[float, float]:
    known = _known_range(variable)
    if known is not None:
        return known
    finite = image[np.isfinite(image)]
    if finite.size == 0:
        return 0.0, 1.0
    lo, hi = np.percentile(finite, [2.0, 98.0])
    if hi <= lo:
        hi = lo + 1.0
    return float(lo), float(hi)


def _estimate_biases(
    rgrid: Any,
    volumes: Sequence[_Volume],
    variable: str,
    layout: _Layout,
    gspec: GridSpec,
    mspec: MergeSpec,
    common: dict[str, Any],
) -> list[float | None]:
    """Relative radar biases from ``network_bias`` on a coarse copy of the grid."""
    cell = max(layout.cell_m, 3000.0)
    xs = layout.x[:: max(1, round(cell / layout.cell_m))]
    ys = layout.y[:: max(1, round(cell / layout.cell_m))]
    zs = np.arange(1000.0, min(gspec.z_max_m, 8000.0) + 1.0, 1000.0)
    try:
        out = rgrid.grid_radars(
            [v.tree for v in volumes],
            xs,
            ys,
            zs,
            data_vars=[variable],
            calibrate=[variable],
            reference=mspec.reference_index,
            merge=None,
            **common,
        )
    except (ValueError, KeyError) as exc:
        logger.warning("Bias estimation failed: %s", exc)
        return [None] * len(volumes)
    bias = out.get(f"{variable}_bias")
    if bias is None:
        return [None] * len(volumes)
    vals = np.asarray(bias.values, dtype=float)
    return [float(b) if np.isfinite(b) else None for b in vals]


def _apply_bias(tree: xr.DataTree, variable: str, bias: float) -> xr.DataTree:
    """Copy of the prepared tree with ``variable`` corrected by ``- bias`` in every sweep."""
    nodes: dict[str, xr.Dataset] = {"/": tree.root.to_dataset()}
    for name, node in tree.children.items():
        ds = node.to_dataset()
        if variable in ds:
            attrs = dict(ds[variable].attrs)
            ds = ds.assign({variable: ds[variable] - bias})
            ds[variable].attrs = attrs
        nodes[f"/{name}"] = ds
    return xr.DataTree.from_dict(nodes)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _build_lut(name: str) -> tuple[np.ndarray, str]:
    """256 x 4 uint8 RGBA look-up table of a cmweather / matplotlib colour map."""
    import matplotlib

    with contextlib.suppress(ImportError):
        import cmweather  # noqa: F401 - registers the cmweather colour maps

    cmap = None
    for candidate in (name, _FALLBACK_CMAP, "viridis"):
        try:
            cmap = matplotlib.colormaps[candidate]
            name = candidate
            break
        except KeyError:
            continue
    assert cmap is not None
    lut = (cmap(np.linspace(0.0, 1.0, 256)) * 255.0 + 0.5).astype(np.uint8)
    lut[:, 3] = 255
    return lut, name


def render_png(image: np.ndarray, lut: np.ndarray, vmin: float, vmax: float) -> bytes:
    """RGBA PNG of ``image``; NaN and values below ``vmin`` are transparent (LUT, no loops)."""
    from PIL import Image

    scale = 255.0 / (vmax - vmin)
    with np.errstate(invalid="ignore"):
        idx = np.clip((image - vmin) * scale + 0.5, 0.0, 255.0)
        hide = ~(image >= vmin)  # NaN and below-range
    rgba = lut[np.nan_to_num(idx, nan=0.0).astype(np.uint8)]
    rgba[hide, 3] = 0
    buf = io.BytesIO()
    Image.fromarray(rgba).save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def _legend(lut: np.ndarray, vmin: float, vmax: float, n: int = 16) -> list[list[Any]]:
    out = []
    for i in range(n):
        frac = i / (n - 1)
        rgb = lut[round(frac * 255)][:3]
        out.append(
            [vmin + frac * (vmax - vmin), "#{:02x}{:02x}{:02x}".format(*(int(c) for c in rgb))]
        )
    return out


# ---------------------------------------------------------------------------
# 3-D volume (for the 3-D view)
# ---------------------------------------------------------------------------


def volume_message(
    mosaic_id: str, cache: MosaicCache = MOSAICS
) -> tuple[dict[str, Any], np.ndarray]:
    """``(header, uint8 array (nz, ny, nx))`` of the merged 3-D field of a cached mosaic.

    The grid is the AEQD working grid: cell ``(k, j, i)`` is centred ``x0 + i * cell_m`` metres
    east and ``y0 + j * cell_m`` metres north of ``origin`` (lat, lon) at ``z[k]`` metres ASL.
    Codes: 0 = no echo / not measured, 1..255 -> ``vmin + (code - 1) * step``.
    """
    rec = cache.get(mosaic_id)
    if rec.volume is None or rec.volume_z_m is None or rec.volume_range is None:
        raise MosaicError(
            rec.volume_note or "This mosaic has no 3-D volume; create it again to get one"
        )
    nz, ny, nx = rec.volume.shape
    lo, hi = rec.volume_range
    proj = _aeqd(*rec.origin)
    sites = []
    for r in rec.radars:
        x, y = proj(r.longitude, r.latitude)
        sites.append(
            {
                "name": r.name,
                "file_id": r.file_id,
                "latitude": r.latitude,
                "longitude": r.longitude,
                "altitude_m": r.altitude_m,
                "x_m": float(x),
                "y_m": float(y),
            }
        )
    west, south, east, north = rec.bbox
    canon = _canonical(rec.variable)
    header = {
        "mosaic_id": rec.mosaic_id,
        "variable": rec.variable,
        "units": rec.units if rec.product != "echo_top" else _DEFAULT_UNITS.get(canon, ""),
        "product": rec.product,
        "grid": {
            "x0": rec.x0,
            "y0": rec.y0,
            "cell_m": rec.cell_m,
            "nx": nx,
            "ny": ny,
            "nz": nz,
            "z": [float(v) for v in rec.volume_z_m],
            "origin_lat": rec.origin[0],
            "origin_lon": rec.origin[1],
            "west": west,
            "south": south,
            "east": east,
            "north": north,
        },
        "vmin": lo,
        "vmax": hi,
        "dtype": "uint8",
        "scale": {"offset": lo, "step": (hi - lo) / 254.0, "nodata": 0},
        "cmap": _CMAPS.get(canon, _FALLBACK_CMAP),
        "radars": sites,
        "analysis_time": rec.analysis_time,
    }
    return header, rec.volume


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


def probe(mosaic_id: str, lon: float, lat: float, cache: MosaicCache = MOSAICS) -> dict[str, Any]:
    """Value of the merged product at ``lon, lat`` plus every radar's own value and geometry."""
    import pyproj

    rec = cache.get(mosaic_id)
    lon = _number(lon, "lon")
    lat = _number(lat, "lat")
    west, south, east, north = rec.bbox
    ny_i, nx_i = rec.image.shape
    value = None
    if west <= lon <= east and south <= lat <= north and abs(lat) < MAX_MERC_LAT + 0.5:
        yt, yb = float(_merc_y(north)), float(_merc_y(south))
        col = int((math.radians(lon - west)) / math.radians(east - west) * nx_i)
        row = int((yt - float(_merc_y(lat))) / (yt - yb) * ny_i)
        col, row = min(max(col, 0), nx_i - 1), min(max(row, 0), ny_i - 1)
        v = float(rec.image[row, col])
        value = v if math.isfinite(v) and v >= rec.vmin else None
    proj = _aeqd(*rec.origin)
    px, py = proj(lon, lat)
    ci = round((px - rec.x0) / rec.cell_m)
    ri = round((py - rec.y0) / rec.cell_m)
    ny, nx = rec.level_m.shape
    in_grid = 0 <= ci < nx and 0 <= ri < ny
    z_cell = float(rec.level_m[ri, ci]) if in_grid else float("nan")
    if not math.isfinite(z_cell):
        z_cell = rec.z_ref_m
    geod = pyproj.Geod(ellps="WGS84")
    radars = []
    for i, r in enumerate(rec.radars):
        _, _, ground = geod.inv(r.longitude, r.latitude, lon, lat)
        big_r = EARTH_RADIUS * rec.k_e
        a, b = big_r + r.altitude_m, big_r + z_cell
        th = ground / big_r
        slant = math.sqrt(max(0.0, a * a + b * b - 2.0 * a * b * math.cos(th)))
        elev = math.degrees(math.atan2(b * math.cos(th) - a, b * math.sin(th)))
        rv = float(rec.radar_values[i, ri, ci]) if in_grid else float("nan")
        radars.append(
            {
                "name": r.name,
                "value": rv if math.isfinite(rv) and rv >= rec.vmin else None,
                "range_km": slant / 1000.0,
                "elevation_deg": elev,
            }
        )
    return {
        "mosaic_id": mosaic_id,
        "lon": lon,
        "lat": lat,
        "value": value,
        "units": rec.units,
        "radars": radars,
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _mosaic_axes(rec: MosaicRecord) -> tuple[np.ndarray, np.ndarray]:
    """Pixel-centre Web-Mercator ``x`` (west -> east) and ``y`` (south -> north), in metres."""
    west, south, east, north = rec.bbox
    ny, nx = rec.image.shape
    xw, xe = MERC_R * math.radians(west), MERC_R * math.radians(east)
    yt, yb = float(_merc_y(north)), float(_merc_y(south))
    dx, dy = (xe - xw) / nx, (yt - yb) / ny
    return xw + (np.arange(nx) + 0.5) * dx, yb + (np.arange(ny) + 0.5) * dy


def _iso_to_dt64(text: str | None) -> np.datetime64:
    if not text:
        return np.datetime64("NaT", "ns")
    try:
        return np.datetime64(str(text).rstrip("Z"), "ns")
    except ValueError:
        return np.datetime64("NaT", "ns")


def _mosaic_attrs(rec: MosaicRecord) -> dict[str, Any]:
    """Global attributes: CF, ACDD and a full description of how the mosaic was made."""
    from engine import layer_provenance, provenance

    west, south, east, north = rec.bbox
    names = [r.name for r in rec.radars]
    m = rec.merge or {}
    params = {
        "variable": rec.variable,
        "product": rec.product,
        "height_m": rec.height_m,
        "threshold_dbz": rec.threshold_dbz,
        "grid_resolution_m": rec.resolution_m,
        "merge": bool(m.get("applied")),
        "average": m.get("average"),
        "range_scale_km": m.get("range_scale_km"),
        "time_scale_s": m.get("time_scale_s"),
        "beamwidth_deg": m.get("beamwidth_deg"),
        "calibrate": m.get("calibrate"),
        "advect": m.get("advect"),
        "radars": names,
        "value_mask": rec.value_mask_text,
    }
    attrs = provenance.standard_global_attrs(
        title=f"Multi-radar mosaic: {rec.product} of {rec.variable}",
        action="create mosaic",
        params=params,
        source="radarx.grid.grid_radars cone-grid + multi-radar merge of " + ", ".join(names),
        references=(
            "CF Conventions 1.10 https://cfconventions.org/; "
            "radarx https://pypi.org/project/radarx/"
        ),
        comment=(
            "Product grid in Web-Mercator (EPSG:3857) metres, cells uniform in x and y; 2-D "
            "latitude/longitude give each cell centre. Cells where no radar measured an echo are "
            "_FillValue (NaN). Heights are above mean sea level."
        ),
        conventions="CF-1.10",
        layer=layer_provenance.for_mosaic_header(rec.header()),
    )
    attrs.update(
        product=rec.product,
        variable=rec.variable,
        grid_resolution_m=float(rec.resolution_m),
        merge_applied="true" if m.get("applied") else "false",
        merge_average=str(m.get("average", "")),
        merge_range_scale_km=float(m.get("range_scale_km", 0.0) or 0.0),
        merge_time_scale_s=float(m.get("time_scale_s", 0.0) or 0.0),
        merge_beamwidth_deg=float(m.get("beamwidth_deg", 0.0) or 0.0),
        merge_calibrated="true" if m.get("calibrate") else "false",
        merge_advected="true" if m.get("advect") else "false",
        radars=", ".join(names),
        geospatial_lat_min=float(south),
        geospatial_lat_max=float(north),
        geospatial_lon_min=float(west),
        geospatial_lon_max=float(east),
    )
    if rec.value_mask_text:
        attrs["value_mask"] = rec.value_mask_text
    if rec.height_m is not None:
        attrs["height_m"] = float(rec.height_m)
    if rec.threshold_dbz is not None:
        attrs["threshold_dbz"] = float(rec.threshold_dbz)
    if m.get("radarx"):
        attrs["radarx_version"] = str(m["radarx"])
    if rec.analysis_time:
        attrs["analysis_time"] = rec.analysis_time
        attrs["time_coverage_start"] = min(
            [r.volume_time for r in rec.radars if r.volume_time] or [rec.analysis_time]
        )
        attrs["time_coverage_end"] = max(
            [r.volume_time for r in rec.radars if r.volume_time] or [rec.analysis_time]
        )
    return attrs


def to_dataset(rec: MosaicRecord) -> xr.Dataset:
    """CF-1.10 grid dataset of a cached mosaic.

    * ``y``/``x``: Web-Mercator metres (``projection_y/x_coordinate``), south -> north, with 2-D
      auxiliary ``lat``/``lon`` and a ``crs`` grid-mapping variable (``mercator``, EPSG:3857 as
      ``crs_wkt`` too); ``crs_working_grid`` documents the azimuthal-equidistant grid the radars
      were merged on;
    * scalar ``time`` (analysis time) with ``time_bounds`` = first/last radar volume, scalar ``z``
      (CAPPI height, ``positive = "up"``);
    * ``radar`` dimension: site position, volume time (``seconds since``), coverage, merge share,
      calibration bias.
    """
    import pyproj

    from engine import cfwrite, provenance

    ny, nx = rec.image.shape
    x, y = _mosaic_axes(rec)
    lon = np.degrees(x / MERC_R)
    lat = _merc_lat(y)
    data = rec.image[::-1].astype(np.float32)  # south -> north
    name = f"{rec.variable}_{rec.product}"
    units = cfwrite.normalize_units(rec.units) or "1"

    long_names = {
        "cappi": f"{rec.variable} constant-altitude PPI"
        + (f" at {rec.height_m:g} m above mean sea level" if rec.height_m is not None else ""),
        "composite": f"{rec.variable} column maximum",
        "echo_top": "echo-top height"
        + (f" of the {rec.threshold_dbz:g} dBZ contour" if rec.threshold_dbz is not None else ""),
    }
    var_attrs: dict[str, Any] = {
        "long_name": long_names.get(rec.product, name),
        "units": units,
        "grid_mapping": "crs",
        "product": rec.product,
        "comment": (
            "Heights are above mean sea level. "
            + (
                "Maximum over all grid heights."
                if rec.product == "composite"
                else "Lowest height of the threshold crossing, from the merged volume."
                if rec.product == "echo_top"
                else ""
            )
        ).strip(),
    }
    role = varmap.role_for_name(rec.variable)
    if (
        role is not None
        and role.key == "reflectivity"
        and units == "dBZ"
        and rec.product != "echo_top"
    ):
        var_attrs["standard_name"] = "equivalent_reflectivity_factor"
    if rec.height_m is not None:
        var_attrs["height_m"] = float(rec.height_m)
    if rec.threshold_dbz is not None:
        var_attrs["threshold_dbz"] = float(rec.threshold_dbz)

    wkt = pyproj.CRS.from_epsg(3857).to_wkt()
    crs_attrs = {
        "grid_mapping_name": "mercator",
        "longitude_of_projection_origin": 0.0,
        "standard_parallel": 0.0,
        "false_easting": 0.0,
        "false_northing": 0.0,
        "semi_major_axis": 6378137.0,
        "semi_minor_axis": 6378137.0,
        "long_name": "WGS 84 / Pseudo-Mercator (Web Mercator, EPSG:3857)",
        "epsg_code": "EPSG:3857",
        "crs_wkt": wkt,
        "spatial_ref": wkt,
    }
    working_attrs = {
        "grid_mapping_name": "azimuthal_equidistant",
        "latitude_of_projection_origin": float(rec.origin[0]),
        "longitude_of_projection_origin": float(rec.origin[1]),
        "false_easting": 0.0,
        "false_northing": 0.0,
        "semi_major_axis": 6378137.0,
        "inverse_flattening": 298.257223563,
        "long_name": "azimuthal-equidistant grid the radars were merged on (not used by the data)",
        "cell_size_m": float(rec.cell_m),
        "x_first_cell_m": float(rec.x0),
        "y_first_cell_m": float(rec.y0),
        "comment": (
            "The radars were gridded and merged on this grid, then resampled to the "
            "Web-Mercator grid of the data variables."
        ),
    }
    ds = xr.Dataset(
        {
            name: (("y", "x"), data, var_attrs),
            "crs": ((), 0, crs_attrs),
            "crs_working_grid": ((), 0, working_attrs),
            "lat": (
                ("y", "x"),
                np.broadcast_to(lat[:, None], (ny, nx)).astype(np.float64),
                {"standard_name": "latitude", "long_name": "latitude of cell centre"},
            ),
            "lon": (
                ("y", "x"),
                np.broadcast_to(lon[None, :], (ny, nx)).astype(np.float64),
                {"standard_name": "longitude", "long_name": "longitude of cell centre"},
            ),
        },
        coords={
            "y": (
                "y",
                y,
                {
                    "standard_name": "projection_y_coordinate",
                    "long_name": "y coordinate of projection (Web Mercator)",
                    "units": "m",
                    "axis": "Y",
                },
            ),
            "x": (
                "x",
                x,
                {
                    "standard_name": "projection_x_coordinate",
                    "long_name": "x coordinate of projection (Web Mercator)",
                    "units": "m",
                    "axis": "X",
                },
            ),
        },
    )
    ds["lat"].attrs["units"] = "degrees_north"
    ds["lon"].attrs["units"] = "degrees_east"
    if rec.product == "cappi" and rec.height_m is not None:
        ds = ds.assign_coords(
            z=(
                (),
                float(rec.height_m),
                {
                    "standard_name": "altitude",
                    "long_name": "height above mean sea level",
                    "units": "m",
                    "positive": "up",
                    "axis": "Z",
                },
            )
        )

    # time: analysis time, bounds = first/last radar volume --------------------------------
    stamps = [_iso_to_dt64(r.volume_time) for r in rec.radars]
    valid = [t for t in stamps if not np.isnat(t)]
    analysis = _iso_to_dt64(rec.analysis_time)
    if np.isnat(analysis) and valid:
        analysis = min(valid)
    if not np.isnat(analysis):
        lo = min(valid) if valid else analysis
        hi = max(valid) if valid else analysis
        ds = ds.assign_coords(
            time=(
                (),
                analysis,
                {
                    "standard_name": "time",
                    "long_name": "analysis time (UTC)",
                    "bounds": "time_bounds",
                },
            )
        )
        ds["time_bounds"] = (
            ("bnds",),
            np.array([lo, hi], dtype="datetime64[ns]"),
            {"long_name": "first and last radar volume time (UTC)"},
        )

    # radar sources --------------------------------------------------------------------------
    ref = min(valid) if valid else None
    ds = cfwrite.set_char_coord(
        ds, "radar_name", "radar", [r.name for r in rec.radars], "radar (site) name"
    )
    ds["radar_latitude"] = (
        ("radar",),
        np.array([r.latitude for r in rec.radars], dtype=np.float64),
        {"standard_name": "latitude", "long_name": "radar latitude", "units": "degrees_north"},
    )
    ds["radar_longitude"] = (
        ("radar",),
        np.array([r.longitude for r in rec.radars], dtype=np.float64),
        {"standard_name": "longitude", "long_name": "radar longitude", "units": "degrees_east"},
    )
    ds["radar_altitude"] = (
        ("radar",),
        np.array([r.altitude_m for r in rec.radars], dtype=np.float64),
        {
            "standard_name": "altitude",
            "long_name": "radar antenna altitude above mean sea level",
            "units": "m",
            "positive": "up",
        },
    )
    ds["radar_volume_time"] = (
        ("radar",),
        np.array(stamps, dtype="datetime64[ns]"),
        {"long_name": "start time of the radar volume used (UTC)"},
    )
    ds["radar_coverage_fraction"] = (
        ("radar",),
        np.array([r.coverage_fraction for r in rec.radars], dtype=np.float32),
        {"long_name": "fraction of the mosaic area covered by the radar", "units": "1"},
    )
    ds["radar_share"] = (
        ("radar",),
        np.array([r.share for r in rec.radars], dtype=np.float32),
        {
            "long_name": "fraction of valid cells where the radar has the top merge weight",
            "units": "1",
        },
    )
    ds["radar_bias"] = (
        ("radar",),
        np.array(
            [np.nan if r.bias_db is None else r.bias_db for r in rec.radars], dtype=np.float32
        ),
        {
            "long_name": "reflectivity bias removed from the radar before merging (NaN: none)",
            "units": "dB",
        },
    )
    ds.attrs = _mosaic_attrs(rec)
    from engine import governor

    governor.stamp(ds.attrs, rec.grid_info.get("reductions") or [])
    # --- encodings
    ds[name].encoding = cfwrite.field_encoding((ny, nx))
    for v in ("lat", "lon"):
        ds[v].encoding = {**cfwrite.compression_encoding((ny, nx)), "_FillValue": None}
    for v in ("x", "y"):
        ds[v].encoding = {"_FillValue": None}
    if "z" in ds.coords:
        ds["z"].encoding = {"_FillValue": None}
    if "time" in ds.coords:
        ds["time"].encoding = {
            "units": "seconds since 1970-01-01T00:00:00Z",
            "calendar": "standard",
            "dtype": "float64",
            "_FillValue": None,
        }
        ds["time_bounds"].encoding = {
            "units": "seconds since 1970-01-01T00:00:00Z",
            "calendar": "standard",
            "dtype": "float64",
            "_FillValue": None,
        }
    epoch = provenance.iso_utc(ref) if ref is not None else "1970-01-01T00:00:00Z"
    ds["radar_volume_time"].encoding = {
        "units": f"seconds since {epoch}",
        "calendar": "standard",
        "dtype": "float64",
        "_FillValue": np.float64(np.nan),
    }
    return ds


@hdf5_guard.guarded
def write_netcdf(rec: MosaicRecord, path: str | os.PathLike[str]) -> None:
    """Write the mosaic as a CF-1.10 NetCDF4 file (see :func:`to_dataset`)."""
    ds = to_dataset(rec)
    with hdf5_guard.new_file(path):
        ds.to_netcdf(path, engine="netcdf4", format="NETCDF4")


def world_file(rec: MosaicRecord) -> str:
    """ESRI world file text of the PNG (EPSG:3857 metres, pixel-centre convention)."""
    west, south, east, north = rec.bbox
    ny, nx = rec.image.shape
    xw, xe = MERC_R * math.radians(west), MERC_R * math.radians(east)
    yt, yb = float(_merc_y(north)), float(_merc_y(south))
    dx, dy = (xe - xw) / nx, (yt - yb) / ny
    return "\n".join(f"{v:.9f}" for v in (dx, 0.0, 0.0, -dy, xw + 0.5 * dx, yt - 0.5 * dy)) + "\n"


def projection_wkt() -> str:
    """ESRI ``.prj`` text (WKT1) of EPSG:3857; ``pyproj.CRS.from_wkt`` reads it back exactly."""
    import pyproj

    return pyproj.CRS.from_epsg(3857).to_wkt(version="WKT1_ESRI")


def png_bytes(rec: MosaicRecord) -> bytes:
    """The mosaic PNG with descriptive ``tEXt`` chunks (title, software, radars, CRS, bounds).

    The pixels are those of ``rec.png``; the file is accompanied by ``.pgw`` / ``.prj`` sidecars
    (:func:`world_file`, :func:`projection_wkt`) for GIS use. Only radar names are recorded.
    """
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    from engine import provenance

    west, south, east, north = rec.bbox
    meta = provenance.figure_metadata(
        title=f"Radar mosaic: {rec.variable} {rec.product}",
        description=(
            f"{rec.variable} {rec.product}"
            + (f" at {rec.height_m:g} m" if rec.height_m is not None else "")
            + f" ({rec.units}); colour range {rec.vmin:g}..{rec.vmax:g}; "
            f"analysis time {rec.analysis_time or 'unknown'}"
        ),
        params={
            "radars": [r.name for r in rec.radars],
            "crs": "EPSG:3857 (see .prj/.pgw)",
            "bbox_wsen_deg": [west, south, east, north],
            "resolution_m": rec.resolution_m,
            "colormap": rec.cmap,
        },
    )
    info = PngInfo()
    for key, value in meta.items():
        info.add_text(key, value)
    with Image.open(io.BytesIO(rec.png)) as im:
        im.load()
        buf = io.BytesIO()
        im.save(buf, format="PNG", pnginfo=info, compress_level=6)
    return buf.getvalue()

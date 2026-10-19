"""Multi-Doppler (dual / multi-radar) wind retrieval built on ``radarx.retrieve.multidoppler``.

``run_multidoppler`` turns two or more opened radar volumes that carry a (dealiased) radial
velocity into a gridded 3-D wind analysis:

1. every volume is prepared (variable aliases, in-memory sweeps, site, volume time);
2. a common Cartesian grid is chosen (azimuthal-equidistant, heights above sea level). With the
   default ``extent="auto"`` it spans the region seen by two radars at a beam-crossing angle of
   at least ``min_crossing_angle``, and the lowest level is the first one every column of that
   region has velocity data at;
3. ``radarx.retrieve.multi_doppler_input`` cone-grids all radars onto it;
4. the wind is retrieved, either with the classical analytic solution
   (``dual_doppler_analytic``, any number >= 2 of radars as a least-squares problem, default) or
   the variational analysis (``multi_doppler``, which also uses a background wind);
5. cells without two well-crossing beams are masked and the derived fields (speed, horizontal
   divergence, vertical vorticity, uncertainty) are computed.

Results are float32 ``(z, y, x)`` fields kept in a small LRU (:data:`WIND_RESULTS`) so a result
can be exported without recomputing it. ``u`` and ``v`` are along the grid axes (east / north to
well within a degree over the analysis domain); ``w`` is the vertical air velocity (the fall
speed is removed when ``fall_speed_correction`` is on).

``radarx`` is imported lazily: without it every entry point raises :class:`MultiDopplerError`
with a ``pip install radarx`` hint.
"""

from __future__ import annotations

import contextlib
import logging
import math
import threading
import time
import uuid
import warnings
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from engine.plugins import VAR_ALIASES

logger = logging.getLogger(__name__)

ProgressFn = Callable[[int, str], None]

METHODS = ("analytic", "variational")
BOUNDARIES = ("auto", "upward", "downward", "obrien")
DEALIAS_MODES = ("auto", "always", "never")
BACKGROUND_MODES = ("none", "vvp", "uniform", "manual")
# Fields delivered to the client, in wire order (each a float32 (nz, ny, nx) block).
FIELD_ORDER = (
    "u",
    "v",
    "w",
    "speed",
    "divergence",
    "vorticity",
    "uncertainty",
    "crossing_angle",
    "n_radars",
    "dbz",
)
FIELD_INFO: dict[str, tuple[str, str]] = {
    "u": ("m/s", "Eastward wind (grid x)"),
    "v": ("m/s", "Northward wind (grid y)"),
    "w": ("m/s", "Vertical air velocity (up positive)"),
    "speed": ("m/s", "Horizontal wind speed"),
    "divergence": ("1e-3 1/s", "Horizontal divergence du/dx + dv/dy"),
    "vorticity": ("1e-3 1/s", "Vertical vorticity dv/dx - du/dy"),
    "uncertainty": ("m/s", "Wind error sqrt(u_err^2 + v_err^2)"),
    "crossing_angle": ("deg", "Largest horizontal beam-crossing angle"),
    "n_radars": ("1", "Radars observing the cell"),
    "dbz": ("dBZ", "Mean reflectivity of the radars"),
}
MIN_CELLS_PER_AXIS = 3
# Refuse grids that would take minutes / hundreds of megabytes on the wire.
MAX_GRID_CELLS = 2_500_000
MAX_RADARS = 8
# Warn when the volumes were taken further apart than this (the storm moves between scans).
TIME_WARN_S = 300.0
# Radars closer than this (ground distance, metres) cannot cross their beams.
MIN_BASELINE_M = 5000.0
# Dealiasing: only Doppler sweeps up to this elevation are unfolded (higher ones are either not
# aliased or too short-ranged to matter); aliased sweeps above it are left out of the analysis.
DEALIAS_MAX_ELEVATION_DEG = 12.0
# A sweep "looks aliased" when more than this share of its valid gate-to-gate velocity steps
# exceeds 1.2 x the Nyquist velocity (a fold line is a step of ~2 x Nyquist; real shear and noise
# hardly ever make one).
ALIAS_JUMP_FRACTION = 0.003
ALIAS_MIN_GATES = 500
NEXRAD_VELOCITY_FLAG = 63.0  # |v| at or above this is a Level II range-fold / no-data code
# The automatic lowest level is the first one at which this share of the analysis columns has
# two radars with a usable beam crossing.
Z_MIN_COLUMN_FRACTION = 0.05
# Automatic boundary: integrate w upward only if this share of the columns with data at any
# height already has two radars in the lowest two levels.
AUTO_UPWARD_MIN_LOW_COVERAGE = 0.6
# radarx warns when w = 0 is imposed at a level this far above the highest radar (metres).
W_BOTTOM_WARN_HEIGHT_M = 200.0
CACHE_MAX_ITEMS = 3
CACHE_MAX_BYTES = 500 * 1024 * 1024


class MultiDopplerError(Exception):
    """A user-facing multi-Doppler problem (bad request, missing radarx, no overlap...)."""


class MultiDopplerCancelledError(Exception):
    """The run was cancelled by the user (raised between the stages of a run)."""


# ---------------------------------------------------------------------------
# Variable names (thin helper: one place to swap for engine/varmap later)
# ---------------------------------------------------------------------------


def _var_names(canonical: str) -> list[str]:
    """File-level names a canonical variable (``VRADH``, ``DBZH``) may go by."""
    return list(VAR_ALIASES.get(canonical, [canonical]))


def _find_var(ds: xr.Dataset, canonical: str) -> str | None:
    """Name of ``canonical`` (or one of its aliases) in a sweep dataset, else ``None``."""
    for name in _var_names(canonical):
        if name in ds.data_vars:
            return name
    return None


# ---------------------------------------------------------------------------
# Request parameters
# ---------------------------------------------------------------------------


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise MultiDopplerError(f"{label} must be a number")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise MultiDopplerError(f"{label} must be a number") from exc
    if not math.isfinite(out):
        raise MultiDopplerError(f"{label} must be finite")
    return out


@dataclass
class GridSpec:
    """Analysis grid. Horizontal coordinates are metres east / north of ``origin``."""

    origin: Any = "auto"  # "auto" (centroid of the radars) or {lat, lon}
    extent: Any = "auto"  # "auto" or {x_min_km, x_max_km, y_min_km, y_max_km}
    dx_m: float = 1000.0  # horizontal spacing (x and y)
    dz_m: float = 500.0  # vertical spacing
    z_min_m: float | None = None  # lowest level ASL; None = first level every column is seen at
    z_max_m: float = 10000.0  # highest level ASL
    max_range_km: float = 100.0  # ground range from a radar beyond which its data is not used

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> GridSpec:
        d = dict(d or {})
        spec = cls()
        for key in ("dx_m", "dz_m", "z_max_m", "max_range_km"):
            if d.get(key) is not None:
                setattr(spec, key, _number(d[key], f"grid.{key}"))
        if d.get("z_min_m") is not None:
            spec.z_min_m = _number(d["z_min_m"], "grid.z_min_m")
        origin = d.get("origin")
        if origin not in (None, "auto"):
            try:
                lat, lon = float(origin["lat"]), float(origin["lon"])
            except (KeyError, TypeError, ValueError) as exc:
                raise MultiDopplerError("grid.origin must be 'auto' or {lat, lon}") from exc
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise MultiDopplerError("grid.origin: latitude/longitude out of range")
            spec.origin = {"lat": lat, "lon": lon}
        extent = d.get("extent")
        if extent not in (None, "auto"):
            try:
                box = {
                    k: float(extent[k]) for k in ("x_min_km", "x_max_km", "y_min_km", "y_max_km")
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise MultiDopplerError(
                    "grid.extent must be 'auto' or {x_min_km, x_max_km, y_min_km, y_max_km}"
                ) from exc
            if not all(math.isfinite(v) for v in box.values()):
                raise MultiDopplerError("grid.extent must be finite")
            if box["x_max_km"] <= box["x_min_km"] or box["y_max_km"] <= box["y_min_km"]:
                raise MultiDopplerError("grid.extent: each maximum must exceed its minimum")
            spec.extent = box
        if not 250.0 <= spec.dx_m <= 10000.0:
            raise MultiDopplerError("grid.dx_m must be between 250 and 10000 metres")
        if not 100.0 <= spec.dz_m <= 2000.0:
            raise MultiDopplerError("grid.dz_m must be between 100 and 2000 metres")
        if not 1000.0 <= spec.z_max_m <= 20000.0:
            raise MultiDopplerError("grid.z_max_m must be between 1000 and 20000 metres")
        if spec.z_min_m is not None and not -500.0 <= spec.z_min_m < spec.z_max_m:
            raise MultiDopplerError("grid.z_min_m must be at least -500 and below z_max_m")
        if not 10.0 <= spec.max_range_km <= 400.0:
            raise MultiDopplerError("grid.max_range_km must be between 10 and 400 km")
        return spec


@dataclass
class Options:
    method: str = "analytic"
    min_crossing_angle: float = 30.0
    fall_speed_correction: bool = True
    freezing_level: Any = "auto"  # "auto" / "rain" (rain law at all heights) or metres ASL
    melting_depth_m: float = 500.0
    boundary: str = "auto"  # auto | upward | downward | obrien
    dealias: str = "auto"  # auto | always | never (unfold aliased velocities before gridding)
    dealias_max_elevation: float = DEALIAS_MAX_ELEVATION_DEG
    max_speed: float | None = 100.0  # output QC: cells with a faster wind (m/s) become NaN
    max_abs_w: float | None = 40.0  # output QC: |w| above this (m/s) becomes NaN
    max_uncertainty: float | None = None  # output QC: u/v/w NaN where the error is larger (m/s)
    keep_raw: bool = False  # also keep the unmasked u, v, w (u_raw, v_raw, w_raw)
    w_bottom: float = 0.0
    obs_error: float = 1.0  # assumed radial-velocity error (m/s) of the uncertainty
    background: dict[str, Any] = field(default_factory=lambda: {"mode": "none"})
    storm_motion: tuple[float, float] | None = None  # advect the volumes to a common time
    uncertainty: bool = False  # variational only: posterior error (slower)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Options:
        d = dict(d or {})
        o = cls()
        if d.get("method") is not None:
            if d["method"] not in METHODS:
                raise MultiDopplerError(f"options.method must be one of {', '.join(METHODS)}")
            o.method = d["method"]
        if d.get("boundary") is not None:
            if d["boundary"] not in BOUNDARIES:
                raise MultiDopplerError(f"options.boundary must be one of {', '.join(BOUNDARIES)}")
            o.boundary = d["boundary"]
        if d.get("dealias") is not None:
            if d["dealias"] is True:
                d["dealias"] = "always"
            elif d["dealias"] is False:
                d["dealias"] = "never"
            if d["dealias"] not in DEALIAS_MODES:
                raise MultiDopplerError(
                    f"options.dealias must be one of {', '.join(DEALIAS_MODES)}"
                )
            o.dealias = d["dealias"]
        for key, label in (
            ("dealias_max_elevation", "dealias_max_elevation"),
            ("min_crossing_angle", "min_crossing_angle"),
            ("melting_depth_m", "melting_depth_m"),
            ("w_bottom", "w_bottom"),
            ("obs_error", "obs_error"),
        ):
            if d.get(key) is not None:
                setattr(o, key, _number(d[key], f"options.{label}"))
        if not 0.0 <= o.min_crossing_angle <= 90.0:
            raise MultiDopplerError("options.min_crossing_angle must be between 0 and 90 degrees")
        if not 0.0 < o.dealias_max_elevation <= 90.0:
            raise MultiDopplerError(
                "options.dealias_max_elevation must be between 0 and 90 degrees"
            )
        for key in ("max_speed", "max_abs_w", "max_uncertainty"):
            if key in d:  # null switches that check off
                if d[key] is None:
                    setattr(o, key, None)
                else:
                    val = _number(d[key], f"options.{key}")
                    if val <= 0:
                        raise MultiDopplerError(f"options.{key} must be positive")
                    setattr(o, key, val)
        if o.obs_error <= 0:
            raise MultiDopplerError("options.obs_error must be positive")
        if o.melting_depth_m < 0:
            raise MultiDopplerError("options.melting_depth_m must not be negative")
        for key in ("fall_speed_correction", "uncertainty", "keep_raw"):
            if d.get(key) is not None:
                setattr(o, key, bool(d[key]))
        fl = d.get("freezing_level")
        if fl is not None:
            if fl in ("auto", "rain"):
                o.freezing_level = fl
            else:
                lvl = _number(fl, "options.freezing_level")
                if not 0.0 <= lvl <= 8000.0:
                    raise MultiDopplerError(
                        "options.freezing_level must be 'auto', 'rain' or a height of 0-8000 m"
                    )
                o.freezing_level = lvl
        o.background = _parse_background(d.get("background"))
        motion = d.get("storm_motion")
        if motion is not None:
            try:
                if isinstance(motion, dict):
                    pair = (float(motion["u"]), float(motion["v"]))
                else:
                    pair = (float(motion[0]), float(motion[1]))
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise MultiDopplerError(
                    "options.storm_motion must be [u, v] or {u, v} in m/s"
                ) from exc
            if not all(math.isfinite(c) for c in pair):
                raise MultiDopplerError("options.storm_motion must be finite")
            o.storm_motion = pair
        return o


def _parse_background(bg: Any) -> dict[str, Any]:
    if bg is None:
        return {"mode": "none"}
    if not isinstance(bg, dict):
        raise MultiDopplerError("options.background must be an object")
    mode = bg.get("mode", "none")
    if mode not in BACKGROUND_MODES:
        raise MultiDopplerError(
            f"options.background.mode must be one of {', '.join(BACKGROUND_MODES)}"
        )
    if mode == "uniform":
        return {
            "mode": mode,
            "u": _number(bg.get("u"), "options.background.u"),
            "v": _number(bg.get("v"), "options.background.v"),
        }
    if mode == "manual":
        rows = bg.get("profile")
        if not isinstance(rows, list) or not rows:
            raise MultiDopplerError(
                "options.background.profile must be a non-empty list of {height_m, u, v}"
            )
        prof = []
        for r in rows:
            if not isinstance(r, dict):
                raise MultiDopplerError("options.background.profile rows must be objects")
            prof.append(
                (
                    _number(r.get("height_m"), "background height_m"),
                    _number(r.get("u"), "background u"),
                    _number(r.get("v"), "background v"),
                )
            )
        prof.sort()
        heights = [p[0] for p in prof]
        if len(set(heights)) != len(heights):
            raise MultiDopplerError("options.background.profile has duplicate heights")
        return {"mode": mode, "profile": [{"height_m": h, "u": u, "v": v} for h, u, v in prof]}
    return {"mode": mode}


# ---------------------------------------------------------------------------
# Results and the cache
# ---------------------------------------------------------------------------


@dataclass
class WindRecord:
    """A finished analysis: float32 fields plus everything needed to describe / export it."""

    wind_id: str
    fields: np.ndarray  # (len(FIELD_ORDER), nz, ny, nx) float32
    x: np.ndarray  # metres east of the origin
    y: np.ndarray
    z: np.ndarray  # metres above sea level
    origin: tuple[float, float]  # lat, lon
    radars: list[dict[str, Any]]
    summary: dict[str, Any]
    warnings: list[str]
    created: float = field(default_factory=time.time)
    raw: dict[str, np.ndarray] | None = None  # unmasked u, v, w ``(z, y, x)`` (``keep_raw`` only)

    @property
    def nbytes(self) -> int:
        return int(self.fields.nbytes) + sum(int(a.nbytes) for a in (self.raw or {}).values())

    def header(self) -> dict[str, Any]:
        """JSON-able description (without ``type`` / ``request_id`` / frame counts)."""
        nz, ny, nx = self.fields.shape[1:]
        lon_w, lat_s, lon_e, lat_n = self.lonlat_bbox()
        return {
            "wind_id": self.wind_id,
            "fields": [
                {"name": n, "units": FIELD_INFO[n][0], "long_name": FIELD_INFO[n][1]}
                for n in FIELD_ORDER
            ],
            "shape": [len(FIELD_ORDER), nz, ny, nx],
            "grid": {
                "nx": nx,
                "ny": ny,
                "nz": nz,
                "x": [float(self.x[0]), float(self.x[-1])],
                "y": [float(self.y[0]), float(self.y[-1])],
                "dx": float(self.x[1] - self.x[0]),
                "dy": float(self.y[1] - self.y[0]),
                "z": [float(v) for v in self.z],
                "origin_lat": self.origin[0],
                "origin_lon": self.origin[1],
                "west": lon_w,
                "south": lat_s,
                "east": lon_e,
                "north": lat_n,
            },
            "radars": self.radars,
            "warnings": self.warnings,
            **self.summary,
        }

    def lonlat_bbox(self) -> tuple[float, float, float, float]:
        proj = _aeqd(*self.origin)
        xs = np.array([self.x[0], self.x[-1], self.x[0], self.x[-1]])
        ys = np.array([self.y[0], self.y[0], self.y[-1], self.y[-1]])
        lon, lat = proj(xs, ys, inverse=True)
        return float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())


class WindCache:
    """LRU of :class:`WindRecord` bounded by item count and bytes (thread-safe)."""

    def __init__(self, max_items: int = CACHE_MAX_ITEMS, max_bytes: int = CACHE_MAX_BYTES):
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, WindRecord] = OrderedDict()
        self._lock = threading.RLock()

    def put(self, record: WindRecord) -> None:
        with self._lock:
            self._items[record.wind_id] = record
            self._items.move_to_end(record.wind_id)
            while len(self._items) > 1 and (
                len(self._items) > self.max_items
                or sum(r.nbytes for r in self._items.values()) > self.max_bytes
            ):
                self._items.popitem(last=False)

    def get(self, wind_id: str) -> WindRecord:
        with self._lock:
            record = self._items.get(wind_id)
            if record is None:
                raise MultiDopplerError(
                    f"Unknown or expired wind analysis '{wind_id}' (only the last "
                    f"{self.max_items} are kept); run it again"
                )
            self._items.move_to_end(wind_id)
            return record

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def discard(self, wind_id: str) -> None:
        """Forget one record (no error when it is already gone)."""
        with self._lock:
            self._items.pop(wind_id, None)

    def __len__(self) -> int:
        return len(self._items)


WIND_RESULTS = WindCache()


# ---------------------------------------------------------------------------
# radarx access
# ---------------------------------------------------------------------------


def _radarx_mod():
    """``(radarx.retrieve.multidoppler, radarx version)``; friendly error when radarx is missing."""
    try:
        import radarx
        from radarx.retrieve import multidoppler as md
    except ImportError as exc:
        raise MultiDopplerError(
            "Multi-Doppler wind retrieval needs the Python package radarx: pip install radarx"
        ) from exc
    for name in ("multi_doppler_input", "dual_doppler_analytic", "multi_doppler"):
        if not hasattr(md, name):
            raise MultiDopplerError(
                f"The installed radarx has no radarx.retrieve.multidoppler.{name}; "
                "upgrade it: pip install -U radarx"
            )
    return md, str(getattr(radarx, "__version__", "?"))


def radarx_available() -> bool:
    try:
        _radarx_mod()
    except MultiDopplerError:
        return False
    return True


def _aeqd(lat0: float, lon0: float):
    import pyproj

    return pyproj.Proj(proj="aeqd", lat_0=lat0, lon_0=lon0, datum="WGS84")


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
    has_dbz: bool
    n_vel_sweeps: int
    min_elevation_deg: float = 0.5
    nyquist_ms: float | None = None
    dealias: dict[str, Any] = field(default_factory=dict)  # what was done about aliasing


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
        raise MultiDopplerError("a volume has no radar site latitude/longitude")
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


def _prepare_volume(
    file_id: str | None,
    reader: Any,
    index: int,
    *,
    dealias: str = "never",
    dealias_max_elevation: float = DEALIAS_MAX_ELEVATION_DEG,
) -> _Volume:
    """In-memory radarx-ready tree of one open volume (``VRADH`` and, if present, ``DBZH``).

    With ``dealias`` "auto" the Doppler sweeps up to ``dealias_max_elevation`` that look aliased
    are unfolded (a copy: the opened file is not touched); "always" unfolds all of them.
    """
    from engine.reader import sorted_sweep_names

    tree = getattr(reader, "datatree", None)
    if tree is None:
        raise MultiDopplerError("a selected file is not open any more")
    names = sorted_sweep_names(tree)
    if not names:
        raise MultiDopplerError("a selected file has no sweeps")
    label = _instrument_name(tree, reader, index)
    nodes: dict[str, xr.Dataset] = {}
    max_range = 0.0
    n_dbz = 0
    elevations: list[float] = []
    info: dict[str, Any] = {
        "mode": dealias,
        "sweeps": 0,
        "looks_aliased": 0,
        "dealiased": 0,
        "left_aliased": 0,
        "dropped": 0,
        "failed": 0,
        "nyquist_ms": None,
        "nyquist_source": None,
        "jump_before_pct": None,
        "jump_after_pct": None,
        "_nyq": [],
        "_src": set(),
        "_before": [],
        "_after": [],
    }
    for i, _ in enumerate(names):
        ds = reader.get_sweep_dataset(i)
        if "azimuth" not in ds.coords or "range" not in ds.coords:
            continue
        mode = str(np.asarray(ds["sweep_mode"].values).ravel()[0]) if "sweep_mode" in ds else ""
        if "rhi" in mode.lower():
            continue
        vel = _find_var(ds, "VRADH")
        dbz = _find_var(ds, "DBZH")
        if vel is None:
            continue  # e.g. the reflectivity-only surveillance cut of a split cut
        wanted = [n for n in (vel, dbz) if n is not None]
        sub = ds[wanted]
        if "sweep_fixed_angle" in ds.variables:
            sub = sub.assign({"sweep_fixed_angle": ds["sweep_fixed_angle"]})
        sub = sub.load()
        rename = {}
        if vel != "VRADH":
            rename[vel] = "VRADH"
        if dbz is not None and dbz != "DBZH":
            rename[dbz] = "DBZH"
        if rename:
            sub = sub.rename(rename)
        elev = _sweep_elevation(ds)
        sub, keep = _treat_aliasing(sub, ds, elev, dealias, dealias_max_elevation, info)
        if not keep:
            continue
        info["sweeps"] += 1
        n_dbz += dbz is not None
        elevations.append(elev)
        max_range = max(max_range, float(np.nanmax(np.asarray(ds["range"].values))))
        nodes[f"/sweep_{len(nodes)}"] = sub
    nyqs: list[float] = info.pop("_nyq")
    sources: set[str] = info.pop("_src")
    before: list[float] = info.pop("_before")
    after: list[float] = info.pop("_after")
    n_vel_sweeps = len(nodes)
    if n_vel_sweeps == 0:
        if info["dropped"]:
            raise MultiDopplerError(
                f"{label}: every Doppler sweep looks aliased and could not be used "
                f"({info['dropped']} above {dealias_max_elevation:g} degrees were left out); "
                "check the velocities or the Nyquist velocity"
            )
        raise MultiDopplerError(
            f"{label} has no radial velocity (looked for {', '.join(_var_names('VRADH'))}); "
            "multi-Doppler needs dealiased radial velocity from every radar"
        )
    if nyqs:
        info["nyquist_ms"] = [min(nyqs), max(nyqs)]
        info["nyquist_source"] = "file" if sources == {"file"} else "estimated from max |V|"
    if before:
        info["jump_before_pct"] = 100.0 * float(np.mean(before))
    if after:
        info["jump_after_pct"] = 100.0 * float(np.mean(after))
    has_dbz = n_dbz == n_vel_sweeps
    if not has_dbz and n_dbz:  # reflectivity on only some velocity sweeps: use none of it
        nodes = {k: (v.drop_vars("DBZH") if "DBZH" in v else v) for k, v in nodes.items()}
    lat, lon, alt = _site(tree, reader.get_sweep_dataset(0))
    nodes["/"] = xr.Dataset(
        coords={
            "latitude": np.float64(lat),
            "longitude": np.float64(lon),
            "altitude": np.float64(alt),
        }
    )
    return _Volume(
        file_id=file_id,
        tree=xr.DataTree.from_dict(nodes),
        name=label,
        latitude=lat,
        longitude=lon,
        altitude_m=alt,
        time=_volume_time([n for k, n in nodes.items() if k != "/"]),
        max_range_m=max_range,
        has_dbz=has_dbz,
        n_vel_sweeps=n_vel_sweeps,
        min_elevation_deg=min((e for e in elevations if math.isfinite(e)), default=0.5),
        nyquist_ms=min(nyqs) if nyqs else None,
        dealias=info,
    )


def _jump_fraction(vals: np.ndarray, nyquist: float) -> float:
    """Share of valid gate-to-gate velocity steps (along azimuth and range) above 1.2 Nyquist."""
    num = den = 0
    with np.errstate(invalid="ignore"):
        for axis in (0, 1):
            d = np.abs(np.diff(vals, axis=axis))
            ok = np.isfinite(d)
            den += int(ok.sum())
            num += int((d[ok] > 1.2 * nyquist).sum())
    return num / den if den else 0.0


def _sweep_nyquist_ms(ds: xr.Dataset, vals: np.ndarray) -> tuple[float | None, str]:
    """Nyquist velocity of a sweep and its source ("file" or "estimated")."""
    ny = _nyquist(ds)
    if ny is not None:
        return ny, "file"
    v = np.abs(vals[np.isfinite(vals)])
    v = v[v < NEXRAD_VELOCITY_FLAG]
    if v.size < ALIAS_MIN_GATES or float(v.max()) < 5.0:
        return None, "unknown"
    # aliased velocities pile up at the Nyquist velocity: the largest value is a good estimate
    return float(math.ceil(float(v.max()) * 2.0) / 2.0), "estimated"


def _unfold(sub: xr.Dataset, nyquist: float) -> np.ndarray:
    """Dealiased ``VRADH`` of a sweep (radarx region-based unfolding), as float32."""
    from radarx.retrieve import dealias_velocity

    with warnings.catch_warnings():
        # radarx warns when the absolute fold is only assumed (partial azimuth coverage, no
        # reference); that is checked afterwards against the other radars (_check_absolute_folds)
        warnings.simplefilter("ignore", UserWarning)
        out = dealias_velocity(sub, "VRADH", float(nyquist), method="region", threshold=0.3)
    return np.asarray(out.transpose(*sub["VRADH"].dims).values, dtype=np.float32)


def _treat_aliasing(
    sub: xr.Dataset,
    ds: xr.Dataset,
    elev: float,
    mode: str,
    max_elevation: float,
    info: dict[str, Any],
) -> tuple[xr.Dataset, bool]:
    """Unfold one velocity sweep if needed; returns ``(sweep, keep)`` and fills ``info``.

    The private ``_nyq`` / ``_src`` / ``_before`` / ``_after`` entries of ``info`` collect the
    Nyquist velocity, its source and the share of gate-to-gate jumps before / after for the
    sweeps that were examined. A sweep above ``max_elevation`` that looks aliased is dropped
    (``keep`` False) unless ``mode`` is "never".
    """
    vals = np.asarray(sub["VRADH"].values, dtype=np.float32)
    if vals.size == 0:
        return sub, True
    nyq, source = _sweep_nyquist_ms(ds, vals)
    if nyq is None or int(np.isfinite(vals).sum()) < ALIAS_MIN_GATES:
        return sub, True
    jumps = _jump_fraction(vals, nyq)
    info["_nyq"].append(nyq)
    info["_src"].add(source)
    info["_before"].append(jumps)
    info["_after"].append(jumps)  # replaced below when the sweep is unfolded
    looks_aliased = jumps > ALIAS_JUMP_FRACTION
    info["looks_aliased"] += int(looks_aliased)
    if mode == "never":
        return sub, True
    low = not math.isfinite(elev) or elev <= max_elevation
    if not low:
        if looks_aliased:
            info["dropped"] += 1
            for key in ("_nyq", "_before", "_after"):
                info[key].pop()
            return sub, False
        return sub, True
    if mode == "auto" and not looks_aliased:
        return sub, True
    try:
        unfolded = _unfold(sub, nyq)
    except Exception:  # radarx refused this sweep (all-NaN, odd geometry...)
        logger.exception("dealiasing of a velocity sweep failed")
        info["failed"] += 1
        info["left_aliased"] += int(looks_aliased)
        return sub, True
    jumps_after = _jump_fraction(unfolded, nyq)
    if jumps_after > jumps:  # no better than the measured field: keep the measurement
        info["left_aliased"] += int(looks_aliased)
        return sub, True
    attrs = {**sub["VRADH"].attrs, "dealias_nyquist": float(nyq)}
    sub = sub.assign({"VRADH": (sub["VRADH"].dims, unfolded, attrs)})
    info["dealiased"] += 1
    info["left_aliased"] += int(jumps_after > ALIAS_JUMP_FRACTION)
    info["_after"][-1] = jumps_after
    return sub, True


def _sweep_elevation(ds: xr.Dataset) -> float:
    """Fixed elevation angle (degrees) of a sweep, NaN when unknown."""
    for src in (ds.get("sweep_fixed_angle"), ds.coords.get("elevation")):
        if src is not None:
            with contextlib.suppress(TypeError, ValueError):
                val = float(np.nanmean(np.asarray(src.values, dtype=float)))
                if math.isfinite(val):
                    return val
    return float("nan")


def _nyquist(ds: xr.Dataset) -> float | None:
    """Nyquist velocity stored in the file (variable, coordinate or attribute), else ``None``."""
    for key in ("nyquist_velocity", "nyquist_vel"):
        if key in ds.variables:
            with contextlib.suppress(TypeError, ValueError):
                val = float(np.nanmean(np.asarray(ds[key].values, dtype=float)))
                if math.isfinite(val) and val > 0:
                    return val
    vel = _find_var(ds, "VRADH")
    for attrs in ((ds[vel].attrs if vel else {}), ds.attrs):
        for key in ("nyquist_velocity", "nyquist_vel"):
            with contextlib.suppress(TypeError, ValueError):
                val = float(attrs.get(key))
                if math.isfinite(val) and val > 0:
                    return val
    return None


# ---------------------------------------------------------------------------
# Geometry: origin, extent, grid
# ---------------------------------------------------------------------------


def _crossing_deg(b1: np.ndarray, b2: np.ndarray) -> np.ndarray:
    """Horizontal angle (0-90 deg) between two beam bearings given in radians."""
    return np.degrees(np.arcsin(np.abs(np.sin(b1 - b2))))


def _choose_origin(volumes: Sequence[_Volume], spec: GridSpec) -> tuple[float, float]:
    if isinstance(spec.origin, dict):
        return float(spec.origin["lat"]), float(spec.origin["lon"])
    proj0 = _aeqd(volumes[0].latitude, volumes[0].longitude)
    pts = np.array([proj0(v.longitude, v.latitude) for v in volumes])
    cx, cy = pts.mean(axis=0)
    lon, lat = proj0(cx, cy, inverse=True)
    return float(lat), float(lon)


def _radar_xy(volumes: Sequence[_Volume], origin: tuple[float, float]) -> np.ndarray:
    proj = _aeqd(*origin)
    return np.array([proj(v.longitude, v.latitude) for v in volumes], dtype=float)


def _dual_mask(
    xy: np.ndarray,
    reach: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    min_crossing: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Boolean ``(ny, nx)`` masks: seen by >= 2 radars; and with a crossing >= ``min_crossing``."""
    xx, yy = np.meshgrid(x, y)
    nr = xy.shape[0]
    inside = []
    bearing = []
    for i in range(nr):
        dx, dy = xx - xy[i, 0], yy - xy[i, 1]
        d = np.hypot(dx, dy)
        inside.append((d <= reach[i]) & (d > 1000.0))
        bearing.append(np.arctan2(dx, dy))
    seen = sum(m.astype(int) for m in inside) >= 2
    best = np.zeros(xx.shape)
    for i in range(nr):
        for j in range(i + 1, nr):
            both = inside[i] & inside[j]
            best = np.maximum(best, np.where(both, _crossing_deg(bearing[i], bearing[j]), 0.0))
    return seen, best >= min_crossing


def _plan_axes(
    volumes: Sequence[_Volume],
    spec: GridSpec,
    origin: tuple[float, float],
    min_crossing: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Horizontal grid axes (metres from ``origin``)."""
    xy = _radar_xy(volumes, origin)
    reach = np.array(
        [
            min(spec.max_range_km * 1000.0, v.max_range_m or spec.max_range_km * 1000.0)
            for v in volumes
        ]
    )
    # baselines: two radars at (nearly) the same place cannot cross their beams
    for i in range(len(volumes)):
        for j in range(i + 1, len(volumes)):
            if math.hypot(*(xy[i] - xy[j])) < MIN_BASELINE_M:
                raise MultiDopplerError(
                    f"{volumes[i].name} and {volumes[j].name} are at the same location "
                    f"(< {MIN_BASELINE_M / 1000:.0f} km apart): their beams never cross, so no "
                    "horizontal wind can be retrieved. Select radars at different sites."
                )
    lo = (xy - reach[:, None]).min(axis=0)
    hi = (xy + reach[:, None]).max(axis=0)
    # coarse lattice for the overlap search (finer than the typical 1 km grid is not needed)
    step = max(1000.0, spec.dx_m)
    gx = np.arange(lo[0], hi[0] + step, step)
    gy = np.arange(lo[1], hi[1] + step, step)
    seen, good = _dual_mask(xy, reach, gx, gy, min_crossing)
    if not seen.any():
        raise MultiDopplerError(
            "The selected radars have no overlapping coverage within "
            f"{spec.max_range_km:g} km of each radar, so no wind can be retrieved. "
            "Increase the maximum range or select radars closer together."
        )
    if not good.any():
        raise MultiDopplerError(
            "The radars overlap but their beams cross at less than "
            f"{min_crossing:g} degrees everywhere in the overlap (the radars are almost in line "
            "with the storm). Lower the minimum crossing angle or pick a different radar pair."
        )
    dx = spec.dx_m
    if spec.extent == "auto":
        xs, ys = gx[good.any(axis=0)], gy[good.any(axis=1)]
        x0, x1 = xs.min() - step, xs.max() + step
        y0, y1 = ys.min() - step, ys.max() + step
    else:
        e = spec.extent
        x0, x1, y0, y1 = (
            e["x_min_km"] * 1000.0,
            e["x_max_km"] * 1000.0,
            e["y_min_km"] * 1000.0,
            e["y_max_km"] * 1000.0,
        )
    x = np.arange(math.ceil(x0 / dx) * dx, x1 + 0.5 * dx, dx)
    y = np.arange(math.ceil(y0 / dx) * dx, y1 + 0.5 * dx, dx)
    if x.size < MIN_CELLS_PER_AXIS or y.size < MIN_CELLS_PER_AXIS:
        raise MultiDopplerError(
            f"The analysis area is only {x.size} x {y.size} cells; decrease the grid spacing "
            "or enlarge the extent"
        )
    if spec.extent != "auto" and not _dual_mask(xy, reach, x, y, min_crossing)[1].any():
        raise MultiDopplerError(
            "The chosen extent contains no area observed by two radars at a crossing angle "
            f"of at least {min_crossing:g} degrees. Use the automatic extent or move it."
        )
    return x, y


def _lowest_beam_height(vol: _Volume, ground_range_m: np.ndarray) -> np.ndarray:
    """Height ASL of the lowest sweep's beam centre over ground ranges (4/3 Earth)."""
    el = math.radians(vol.min_elevation_deg)
    # slant range for a ground distance s on the 4/3 Earth: r ~ s / cos(el) (adequate < 150 km)
    r = ground_range_m / max(math.cos(el), 0.5)
    return vol.altitude_m + r * math.sin(el) + r**2 / (2 * 4.0 / 3.0 * 6371e3)


def _plan_levels(
    spec: GridSpec,
    volumes: Sequence[_Volume],
    xy: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    min_crossing: float,
) -> tuple[np.ndarray, str | None]:
    """Height levels ASL and, for the automatic start, a sentence saying which one and why.

    ``z_min_m=None`` starts at the lowest multiple of ``dz`` at which at least
    :data:`Z_MIN_COLUMN_FRACTION` of the analysis columns have two radars whose lowest-tilt beams
    reach that height and cross at the minimum angle. The beam geometry decides (not the echo,
    which varies from storm to storm), and a level that few columns can see is not demanded to
    be seen everywhere: far radars cannot see the low levels over a large box, and requiring
    that left no usable levels at all.
    """
    z0 = spec.z_min_m
    note: str | None = None
    if z0 is None:
        z0, note = _auto_z_min(spec, volumes, xy, x, y, min_crossing)
    z = np.arange(z0, spec.z_max_m + 0.5 * spec.dz_m, spec.dz_m)
    if z.size < MIN_CELLS_PER_AXIS:
        raise MultiDopplerError(
            f"Only {z.size} height levels between {z0:g} and {spec.z_max_m:g} m; "
            "decrease the vertical spacing or raise the maximum height"
        )
    return z, note


def _auto_z_min(
    spec: GridSpec,
    volumes: Sequence[_Volume],
    xy: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    min_crossing: float,
) -> tuple[float, str]:
    reach = spec.max_range_km * 1000.0
    xx, yy = np.meshgrid(x, y)
    dist = [np.hypot(xx - rx, yy - ry) for rx, ry in xy]
    bearing = [np.arctan2(xx - rx, yy - ry) for rx, ry in xy]
    need = [_lowest_beam_height(v, d) for v, d in zip(volumes, dist, strict=True)]
    inside = [(d <= reach) & (d > 1000.0) for d in dist]
    pairs = []
    for i in range(len(volumes)):
        for j in range(i + 1, len(volumes)):
            ok = inside[i] & inside[j] & (_crossing_deg(bearing[i], bearing[j]) >= min_crossing)
            pairs.append((i, j, ok))
    floor = max(math.ceil(max(v.altitude_m for v in volumes) / spec.dz_m) * spec.dz_m, -500.0)
    candidates = np.arange(floor, spec.z_max_m + 0.5 * spec.dz_m, spec.dz_m)
    fractions = []
    for zc in candidates:
        seen = np.zeros(xx.shape, dtype=bool)
        for i, j, ok in pairs:
            seen |= ok & (need[i] <= zc) & (need[j] <= zc)
        frac = float(seen.mean())
        fractions.append(frac)
        if frac >= Z_MIN_COLUMN_FRACTION:
            break
    want = 100.0 * Z_MIN_COLUMN_FRACTION
    if not fractions or fractions[-1] < Z_MIN_COLUMN_FRACTION:
        best = max(fractions, default=0.0)
        raise MultiDopplerError(
            f"At no height up to {spec.z_max_m:g} m do {want:g} % of the analysis columns have two "
            f"radars with beams crossing at {min_crossing:g} degrees or more (best "
            f"{100 * best:.1f} %). Use a smaller extent, a larger maximum range, a lower minimum "
            "crossing angle, or radars closer together."
        )
    z0 = float(candidates[len(fractions) - 1])
    prev = (
        f"{100 * fractions[-2]:.1f} % at {candidates[len(fractions) - 2]:g} m"
        if len(fractions) > 1
        else "none lower"
    )
    note = (
        f"Lowest analysis level {z0:g} m, chosen automatically: the first height where at least "
        f"{want:g} % of the analysis columns ({100 * fractions[-1]:.1f} % there; {prev}) have "
        f"two radars with beams crossing at {min_crossing:g} degrees or more. Below it the "
        "radars cannot see enough of the area; give a lowest level to override."
    )
    return z0, note


# ---------------------------------------------------------------------------
# Background wind
# ---------------------------------------------------------------------------


def _layer_gates(
    tree: xr.DataTree, altitude_m: float, reach_m: float, stride: int = 2
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """``(vr, a, b, height)`` of the velocity gates of a volume.

    ``a = sin(az) cos(el)`` and ``b = cos(az) cos(el)`` are the direction cosines of the beam
    towards east and north; ``height`` is above sea level (4/3 Earth). Every ``stride``-th gate
    in range and azimuth is kept to bound the memory.
    """
    vrs, aa, bb, hh = [], [], [], []
    for key in sorted(tree.children):
        ds = tree[key].to_dataset()
        if "VRADH" not in ds.data_vars:
            continue
        if "sweep_fixed_angle" in ds.variables:
            el = float(np.asarray(ds["sweep_fixed_angle"].values).ravel()[0])
        elif "elevation" in ds.coords:
            el = float(np.nanmean(ds["elevation"].values))
        else:
            continue
        vr = np.asarray(ds["VRADH"].transpose("azimuth", "range").values, dtype=np.float32)
        az = np.radians(np.asarray(ds["azimuth"].values, dtype=float))
        rng = np.asarray(ds["range"].values, dtype=float)
        sel = (rng <= reach_m) & (rng > 0)
        vr = vr[::stride][:, sel][:, ::stride]
        az = az[::stride]
        rng = rng[sel][::stride]
        h = altitude_m + rng * math.sin(math.radians(el)) + rng**2 / (2 * 4 / 3 * 6371e3)
        cos_el = math.cos(math.radians(el))
        a = np.broadcast_to(np.sin(az)[:, None] * cos_el, vr.shape)
        b = np.broadcast_to(np.cos(az)[:, None] * cos_el, vr.shape)
        height = np.broadcast_to(h[None, :], vr.shape)
        ok = np.isfinite(vr)
        vrs.append(vr[ok])
        aa.append(a[ok])
        bb.append(b[ok])
        hh.append(height[ok])
    if not vrs:
        empty = np.empty(0, dtype=np.float64)
        return empty, empty, empty, empty
    cat = [np.concatenate(p).astype(np.float64) for p in (vrs, aa, bb, hh)]
    return cat[0], cat[1], cat[2], cat[3]


def _vvp_profile(
    volumes: Sequence[_Volume], z: np.ndarray, spec: GridSpec
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Horizontally uniform wind profile ``(u, v)`` at heights ``z`` from the radars' own data.

    A volume-velocity-processing style fit: in a layer around each height every velocity gate of
    a radar satisfies ``vr = u a + v b`` (the vertical motion and the fall speed are neglected
    at the low elevations that dominate the layer); ``(u, v)`` is the least-squares solution
    after rejecting 3 sigma outliers. Layers with too few gates, or whose gates do not cover the
    azimuths well (ill-conditioned fit), are skipped; the radars' profiles are averaged and
    gaps are filled by interpolation. Used only as the background of the variational method.
    """
    notes: list[str] = []
    us, vs = [], []
    reach = spec.max_range_km * 1000.0
    half = 0.5 * max(1000.0, spec.dz_m)
    for vol in volumes:
        vr, a, b, h = _layer_gates(vol.tree, vol.altitude_m, reach)
        u = np.full(z.shape, np.nan)
        v = np.full(z.shape, np.nan)
        for k, zk in enumerate(z):
            sel = np.abs(h - zk) <= half
            if sel.sum() < 200:
                continue
            ak, bk, yk = a[sel], b[sel], vr[sel]
            keep = np.ones(yk.shape, dtype=bool)
            sol = None
            for _ in range(3):
                mat = np.stack([ak[keep], bk[keep]], axis=1)
                if mat.shape[0] < 100 or np.linalg.cond(mat) > 20.0:
                    sol = None
                    break
                sol = np.linalg.lstsq(mat, yk[keep], rcond=None)[0]
                resid = yk - (sol[0] * ak + sol[1] * bk)
                sigma = float(np.std(resid[keep]))
                keep = np.abs(resid) <= max(3.0 * sigma, 1.0)
            if sol is not None:
                u[k], v[k] = sol
        if np.isfinite(u).any():
            us.append(u)
            vs.append(v)
        else:
            notes.append(f"No wind profile could be fitted for {vol.name}")
    if not us:
        raise MultiDopplerError(
            "Could not derive a background wind from the radars' own velocities (too few "
            "gates or poor azimuth coverage at the analysis heights). Use a uniform or manual "
            "background instead."
        )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        u = np.nanmean(np.stack(us), axis=0)
        v = np.nanmean(np.stack(vs), axis=0)
    ok = np.isfinite(u) & np.isfinite(v)
    return np.interp(z, z[ok], u[ok]), np.interp(z, z[ok], v[ok]), notes


def _background(
    opts: Options,
    volumes: Sequence[_Volume],
    grid_like: xr.DataArray,
    spec: GridSpec,
) -> tuple[xr.Dataset | None, dict[str, Any]]:
    """Background wind Dataset (u, v on z, y, x) and a JSON description of what was used."""
    bg = opts.background
    mode = bg["mode"]
    z = grid_like["z"].values.astype(float)
    if mode == "none":
        return None, {"mode": "none"}
    if mode == "uniform":
        u = np.full(z.shape, bg["u"])
        v = np.full(z.shape, bg["v"])
        info: dict[str, Any] = {"mode": mode, "u": bg["u"], "v": bg["v"]}
    elif mode == "manual":
        h = np.array([p["height_m"] for p in bg["profile"]])
        u = np.interp(z, h, [p["u"] for p in bg["profile"]])
        v = np.interp(z, h, [p["v"] for p in bg["profile"]])
        info = {"mode": mode, "profile": bg["profile"]}
    else:  # vvp
        u, v, _ = _vvp_profile(volumes, z, spec)
        pick = slice(None, None, max(1, z.size // 12))
        info = {
            "mode": "vvp",
            "profile": [
                {"height_m": float(h), "u": float(a), "v": float(b)}
                for h, a, b in zip(z[pick], u[pick], v[pick], strict=True)
            ],
        }
    shape = (z.size, grid_like.sizes["y"], grid_like.sizes["x"])
    dims = ("z", "y", "x")
    coords = {"z": grid_like["z"], "y": grid_like["y"], "x": grid_like["x"]}
    ds = xr.Dataset(
        {
            "u": (dims, np.broadcast_to(u[:, None, None], shape).astype(np.float32).copy()),
            "v": (dims, np.broadcast_to(v[:, None, None], shape).astype(np.float32).copy()),
        },
        coords=coords,
    )
    return ds, info


# ---------------------------------------------------------------------------
# The analysis
# ---------------------------------------------------------------------------


def _check(cancel: Callable[[], bool] | None) -> None:
    if cancel is not None and cancel():
        raise MultiDopplerCancelledError("Wind retrieval cancelled")


def _stats(a: np.ndarray) -> dict[str, float | None]:
    f = a[np.isfinite(a)]
    if not f.size:
        return {"min": None, "max": None, "mean": None, "p2": None, "p98": None}
    p2, p98 = np.percentile(f, [2, 98])
    return {
        "min": float(f.min()),
        "max": float(f.max()),
        "mean": float(f.mean()),
        "p2": float(p2),
        "p98": float(p98),
    }


def run_multidoppler(
    readers: Sequence[tuple[str | None, Any]],
    grid: GridSpec | dict[str, Any] | None = None,
    options: Options | dict[str, Any] | None = None,
    *,
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
    n_threads: int | None = None,
    cache: WindCache | None = WIND_RESULTS,
) -> WindRecord:
    """Retrieve the wind from the volumes behind ``readers`` (``[(file_id, RadarReader), ...]``).

    ``grid`` / ``options`` are :class:`GridSpec` / :class:`Options` or the dicts of the
    ``run_multidoppler`` message. ``cancel`` is polled between the stages; ``progress`` gets
    ``(percent, message)``. The record is also stored in ``cache``.
    """
    t_start = time.perf_counter()
    report = progress or (lambda _p, _m: None)
    if len(readers) < 2:
        raise MultiDopplerError(
            "Multi-Doppler needs at least two radars: open the files of two (or more) radars "
            "that scan the same weather and select them"
        )
    if len(readers) > MAX_RADARS:
        raise MultiDopplerError(f"at most {MAX_RADARS} radars per wind analysis")
    spec = grid if isinstance(grid, GridSpec) else GridSpec.from_dict(grid)
    opts = options if isinstance(options, Options) else Options.from_dict(options)
    md, rx_version = _radarx_mod()
    notes: list[str] = []
    timing: dict[str, float] = {}

    # 1. volumes --------------------------------------------------------------------------
    report(5, "Preparing radar volumes")
    t0 = time.perf_counter()
    volumes = [
        _prepare_volume(
            fid,
            rd,
            i,
            dealias=opts.dealias,
            dealias_max_elevation=opts.dealias_max_elevation,
        )
        for i, (fid, rd) in enumerate(readers)
    ]
    names = _unique([v.name for v in volumes])
    for v, n in zip(volumes, names, strict=True):
        v.name = n
    timing["prepare_ms"] = (time.perf_counter() - t0) * 1000.0
    _check(cancel)
    infos, caveats = _dealias_notes(volumes, opts)
    notes += caveats
    if opts.dealias != "never" and len(volumes) >= 2:
        z_prof = np.arange(500.0, spec.z_max_m + 1.0, max(spec.dz_m, 500.0))
        infos += _check_absolute_folds(volumes, z_prof, spec)
    times = [v.time for v in volumes if not np.isnat(v.time)]
    analysis_time = None
    if len(times) >= 2:
        spread = float((max(times) - min(times)) / np.timedelta64(1, "s"))
        if spread > TIME_WARN_S:
            notes.append(
                f"The volumes were observed {spread / 60:.0f} min apart; the storm moves "
                "between scans. Give a storm motion to advect them to a common time."
            )
    if times:
        analysis_time = max(times)

    # 2. grid -----------------------------------------------------------------------------
    report(12, "Planning the analysis grid")
    origin = _choose_origin(volumes, spec)
    x, y = _plan_axes(volumes, spec, origin, opts.min_crossing_angle)
    xy = _radar_xy(volumes, origin)
    z, z_note = _plan_levels(spec, volumes, xy, x, y, opts.min_crossing_angle)
    if z_note:
        infos.append(z_note)
    if x.size * y.size * z.size > MAX_GRID_CELLS:
        raise MultiDopplerError(
            f"The analysis grid would have {x.size * y.size * z.size / 1e6:.1f} million cells "
            f"({x.size} x {y.size} x {z.size}); increase the spacing or shrink the extent"
        )
    use_dbz = all(v.has_dbz for v in volumes)
    fall_speed = opts.fall_speed_correction
    if fall_speed and not use_dbz:
        fall_speed = False
        notes.append(
            "Not every radar has reflectivity, so the fall-speed correction was skipped "
            "(the vertical velocity then includes the hydrometeor fall speed)."
        )
    if not use_dbz:
        for v in volumes:
            if v.has_dbz:
                v.tree = _drop_variable(v.tree, "DBZH")
    _check(cancel)

    # 3. gridding -------------------------------------------------------------------------
    report(20, f"Gridding {len(volumes)} radars onto {x.size} x {y.size} x {z.size} cells")
    t0 = time.perf_counter()
    kwargs: dict[str, Any] = {}
    if opts.storm_motion is not None and analysis_time is not None:
        kwargs = {"time": analysis_time, "motion": opts.storm_motion}
    elif opts.storm_motion is not None:
        notes.append("The volumes carry no ray times, so the storm motion could not be applied.")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        g = md.multi_doppler_input(
            [v.tree for v in volumes],
            x,
            y,
            z,
            origin=origin,
            field="VRADH",
            dbz="DBZH",
            n_threads=n_threads,
            **kwargs,
        )
    notes += _radarx_warnings(caught)
    g = _limit_range(g, spec.max_range_km * 1000.0)
    timing["grid_ms"] = (time.perf_counter() - t0) * 1000.0
    _check(cancel)

    if g.sizes["z"] < MIN_CELLS_PER_AXIS:
        raise MultiDopplerError(
            "Too few height levels have velocity data from two radars "
            f"(lowest level with data: {float(g['z'].values[0]):.0f} m). Raise the maximum "
            "height, lower the vertical spacing or move the grid closer to the radars."
        )
    if not (np.isfinite(g["VRADH"].values).sum(axis=0) >= 2).any():
        raise MultiDopplerError(
            "No grid cell is observed by two radars: the volumes have no velocity data in the "
            "common area. Check that both radars scan the same weather."
        )
    z = g["z"].values.astype(float)

    # 4. background and retrieval ----------------------------------------------------------
    bg_ds, bg_info = _background(opts, volumes, g["VRADH"], spec)
    if bg_ds is not None and opts.method == "analytic":
        notes.append(
            "The background wind is only used by the variational method; the analytic "
            "solution ignores it."
        )
    freezing: Any = opts.freezing_level
    if freezing == "auto":
        freezing = "rain"
        if fall_speed:
            notes.append(
                "No freezing level is known, so the rain fall-speed relation is used at all "
                "heights; this overestimates the fall speed in ice and biases w. Give a "
                "freezing level to correct it."
            )
    boundary, boundary_note = _resolve_boundary(opts, g, volumes)
    infos.append(boundary_note)
    report(45, "Retrieving the wind (" + opts.method + ")")
    t0 = time.perf_counter()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        if opts.method == "analytic":
            res = md.dual_doppler_analytic(
                g,
                field="VRADH",
                dbz="DBZH",
                obs_error=opts.obs_error,
                min_crossing_angle=opts.min_crossing_angle,
                fall_speed_correction=fall_speed,
                freezing_level=freezing,
                melting_depth=opts.melting_depth_m,
                boundary=boundary,
                w_bottom=opts.w_bottom,
                n_threads=n_threads,
            )
        else:
            w_boundary = {
                "upward": "bottom",
                # an automatic downward choice also frees w only below each column's echo top
                "downward": ("top", "echo_top") if opts.boundary == "auto" else "top",
                "obrien": ("bottom", "obrien"),
            }[boundary]
            res = md.multi_doppler(
                g,
                bg_ds,
                field="VRADH",
                dbz="DBZH",
                fall_speed_correction=fall_speed,
                freezing_level=freezing,
                melting_depth=opts.melting_depth_m,
                w_boundary=w_boundary,
                w_bottom=opts.w_bottom,
                obs_error=opts.obs_error,
                error=opts.uncertainty,
                error_samples=8,
                n_threads=n_threads,
            )
    notes += _tag_boundary_warnings(_radarx_warnings(caught))
    timing["retrieve_ms"] = (time.perf_counter() - t0) * 1000.0
    _check(cancel)

    # 5. masks, derived fields -------------------------------------------------------------
    report(85, "Computing derived fields")
    fields, has_unc, qc, raw = _assemble_fields(res, g, opts, x, y)
    valid = np.isfinite(fields["u"]) & np.isfinite(fields["v"])
    if not valid.any():
        if qc["masked_cells"]:
            raise MultiDopplerError(
                f"Quality control removed every retrieved wind ({qc['masked_cells']} cells "
                f"faster than {opts.max_speed} m/s or too uncertain): the velocities are probably "
                "aliased. Turn the dealiasing on or check the radars' velocity data."
            )
        raise MultiDopplerError(
            "The retrieval produced no valid wind: no cell is observed by two radars at a "
            f"crossing angle of at least {opts.min_crossing_angle:g} degrees. Lower the minimum "
            "crossing angle, check the radars' velocity data or enlarge the grid."
        )
    notes += qc.pop("warnings")
    stack = np.stack([fields[n] for n in FIELD_ORDER]).astype(np.float32)

    # 6. summary ---------------------------------------------------------------------------
    n_seen = int((fields["n_radars"] >= 2).sum())
    crossing = fields["crossing_angle"][valid]
    crossing = crossing[np.isfinite(crossing)]
    w_ok = np.isfinite(fields["w"])
    summary = {
        "method": opts.method,
        "boundary": boundary,
        "boundary_requested": opts.boundary,
        "coverage_pct": 100.0 * float(valid.sum()) / valid.size,
        "dual_coverage_pct": 100.0 * n_seen / valid.size,
        "w_valid_pct": 100.0 * float(w_ok.sum()) / max(int(valid.sum()), 1),
        "valid_cells": int(valid.sum()),
        "total_cells": int(valid.size),
        "crossing_angle": {
            "min_required": opts.min_crossing_angle,
            "min": float(crossing.min()) if crossing.size else None,
            "mean": float(crossing.mean()) if crossing.size else None,
            "median": float(np.median(crossing)) if crossing.size else None,
            "max": float(crossing.max()) if crossing.size else None,
        },
        "field_stats": {n: _stats(fields[n]) for n in FIELD_ORDER},
        "level_coverage": _level_coverage(fields, z),
        "qc": qc,
        "dealias": {
            "mode": opts.dealias,
            "max_elevation_deg": opts.dealias_max_elevation,
            "radars": [{"name": v.name, **v.dealias} for v in volumes],
        },
        "notes": [n for n in dict.fromkeys(infos) if n],
        "has_uncertainty": has_unc,
        "background": bg_info,
        "fall_speed_correction": bool(fall_speed),
        "freezing_level": freezing,
        "min_crossing_angle": opts.min_crossing_angle,
        "z_min_auto": spec.z_min_m is None,
        "analysis_time": _iso(analysis_time) if analysis_time is not None else None,
        "radarx": rx_version,
        "timing": {k: round(v, 1) for k, v in timing.items()},
    }
    summary["timing"]["total_ms"] = round((time.perf_counter() - t_start) * 1000.0, 1)
    radars = [
        {
            "file_id": v.file_id,
            "name": v.name,
            "latitude": v.latitude,
            "longitude": v.longitude,
            "altitude_m": v.altitude_m,
            "volume_time": _iso(v.time),
            "dealias": v.dealias,
        }
        for v in volumes
    ]
    record = WindRecord(
        wind_id=uuid.uuid4().hex[:12],
        fields=stack,
        x=x,
        y=y,
        z=z,
        origin=origin,
        radars=radars,
        summary=summary,
        warnings=list(dict.fromkeys(notes)),
        raw=raw,
    )
    if cache is not None:
        cache.put(record)
    report(95, "Done")
    return record


def _check_absolute_folds(volumes: Sequence[_Volume], z: np.ndarray, spec: GridSpec) -> list[str]:
    """Fix dealiased sweeps whose whole velocity field is off by a multiple of 2 x Nyquist.

    Region-based unfolding is exact up to one constant per sweep (it assumes a zero mean radial
    velocity when the azimuth coverage is partial). Every dealiased sweep is compared with the
    wind profile the radars agree on (median of their layer fits); a sweep whose median offset is
    clearly one or more Nyquist intervals (>= 75 % of 2 x Nyquist, which real wind differences do
    not reach) and that fits better after the shift is shifted. Returns sentences for the notes.
    """
    todo = [v for v in volumes if v.dealias.get("dealiased")]
    if not todo:
        return []
    try:
        profiles = [_vvp_profile([v], z, spec)[:2] for v in volumes]
    except MultiDopplerError:
        return []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        u_ref = np.nanmedian(np.stack([p[0] for p in profiles]), axis=0)
        v_ref = np.nanmedian(np.stack([p[1] for p in profiles]), axis=0)
    out: list[str] = []
    for vol in todo:
        shifted = 0
        for key in sorted(vol.tree.children):
            ds = vol.tree[key].to_dataset()
            nyq = ds["VRADH"].attrs.get("dealias_nyquist")
            if not nyq or "sweep_fixed_angle" not in ds:
                continue
            el = math.radians(float(ds["sweep_fixed_angle"]))
            az = np.radians(ds["azimuth"].values.astype(float))
            rng = ds["range"].values.astype(float)
            h = vol.altitude_m + rng * math.sin(el) + rng**2 / (2 * 4 / 3 * 6371e3)
            ref = (
                np.interp(h, z, u_ref)[None, :] * np.sin(az)[:, None]
                + np.interp(h, z, v_ref)[None, :] * np.cos(az)[:, None]
            ) * math.cos(el)
            vr = ds["VRADH"].transpose("azimuth", "range").values.astype(np.float32)
            ok = np.isfinite(vr) & (rng[None, :] <= spec.max_range_km * 1000.0)
            if ok.sum() < ALIAS_MIN_GATES:
                continue
            diff = (vr - ref)[ok]
            med = float(np.median(diff))
            k = round(med / (2 * nyq))
            if k == 0 or abs(med) < 0.75 * 2 * nyq:
                continue
            fixed = diff - k * 2 * nyq
            if np.mean(np.abs(fixed)) >= 0.7 * np.mean(np.abs(diff)):
                continue
            ds = ds.assign(
                VRADH=(
                    ds["VRADH"].dims,
                    (ds["VRADH"].values - k * 2 * nyq).astype(np.float32),
                    ds["VRADH"].attrs,
                )
            )
            vol.tree[key].dataset = ds
            shifted += 1
        vol.dealias["refolded"] = shifted
        if shifted:
            out.append(
                f"{vol.name}: {shifted} dealiased sweeps were off by whole Nyquist intervals "
                "compared with the other radars and were shifted to agree with them."
            )
    return out


def _dealias_notes(volumes: Sequence[_Volume], opts: Options) -> tuple[list[str], list[str]]:
    """``(info sentences, warnings)`` about what was done (or not) about aliased velocities."""
    infos: list[str] = []
    warn: list[str] = []
    for v in volumes:
        d = v.dealias
        if not d or (not d.get("sweeps") and not d.get("dropped")):
            continue
        if opts.dealias == "never":
            if d.get("looks_aliased"):
                warn.append(
                    f"{v.name}: {d['looks_aliased']} of {d['sweeps']} Doppler sweeps look aliased "
                    "(velocity folds) and dealiasing is off, so the winds will be wrong where "
                    "the velocity folds. Set dealias to auto."
                )
            continue
        if d.get("dealiased"):
            lo, hi = d["nyquist_ms"] or (None, None)
            ny = f"{lo:g}" if lo == hi else f"{lo:g}-{hi:g}"
            jump = ""
            if d.get("jump_before_pct") is not None and d.get("jump_after_pct") is not None:
                jump = (
                    f"; gate-to-gate jumps above 1.2 x Nyquist {d['jump_before_pct']:.2f} % "
                    f"-> {d['jump_after_pct']:.2f} %"
                )
            infos.append(
                f"{v.name}: dealiased {d['dealiased']} of {d['sweeps']} Doppler sweeps "
                f"(up to {opts.dealias_max_elevation:g} deg; Nyquist {ny} m/s, "
                f"{d['nyquist_source']}){jump}."
            )
        elif d.get("nyquist_ms"):
            infos.append(f"{v.name}: velocities do not look aliased, no dealiasing needed.")
        if d.get("dropped"):
            infos.append(
                f"{v.name}: {d['dropped']} aliased sweeps above {opts.dealias_max_elevation:g} "
                "deg were left out of the analysis."
            )
        if d.get("failed") or d.get("left_aliased"):
            warn.append(
                f"{v.name}: dealiasing could not unfold every aliased sweep "
                f"({d.get('failed', 0)} failed, {d.get('left_aliased', 0)} still show folds); "
                "winds there may be wrong."
            )
        if d.get("sweeps") and d.get("nyquist_ms") is None:
            warn.append(
                f"{v.name}: the Nyquist velocity is not in the file and could not be estimated, "
                "so the velocities were used as stored."
            )
    return infos, warn


def _resolve_boundary(
    opts: Options, g: xr.Dataset, volumes: Sequence[_Volume]
) -> tuple[str, str | None]:
    """The vertical boundary to use and a sentence about it (``auto`` picks from the coverage).

    Integrating ``w`` upward from ``w = 0`` at the lowest level only works where the columns are
    covered by two radars from the bottom up. Otherwise (far radars cannot see the low levels)
    ``w`` is integrated downward from the top, where ``w = 0`` at the echo top. The variational
    ``w = 0`` at the bottom also needs that level to be at the radars' height.
    """
    if opts.boundary != "auto":
        return opts.boundary, None
    count = np.isfinite(g["VRADH"].transpose("radar", "z", "y", "x").values).sum(axis=0)
    dual = count >= 2
    columns = dual.any(axis=0)
    z0 = float(g["z"].values[0])
    above_radar = z0 - max(v.altitude_m for v in volumes)
    if not columns.any():
        return "upward", None
    low = float(np.mean([dual[k][columns].mean() for k in range(min(2, dual.shape[0]))]))
    too_high = opts.method == "variational" and above_radar > W_BOTTOM_WARN_HEIGHT_M
    if low >= AUTO_UPWARD_MIN_LOW_COVERAGE and not too_high:
        return "upward", (
            f"Vertical boundary: upward (w = 0 at the lowest level, {z0:g} m); "
            f"{100 * low:.0f} % of the columns with data are covered by two radars at the "
            "lowest levels."
        )
    why = (
        f"only {100 * low:.0f} % of the columns with data are covered by two radars at the "
        "lowest levels"
        if low < AUTO_UPWARD_MIN_LOW_COVERAGE
        else f"the lowest level ({z0:g} m) is {above_radar:.0f} m above the highest radar"
    )
    top = "the echo top" if opts.method == "variational" else "the top of the grid"
    return "downward", (
        f"Vertical boundary: downward (w = 0 at {top}); {why}, so w = 0 at the bottom "
        "would leave most columns without a vertical velocity."
    )


def _tag_boundary_warnings(texts: list[str]) -> list[str]:
    """Put radarx's lower-boundary warning first-class: say plainly what it means."""
    out = []
    for text in texts:
        if "w_boundary='bottom'" in text:
            text = "Vertical boundary warning: " + text
        out.append(text)
    return out


def _drop_variable(tree: xr.DataTree, name: str) -> xr.DataTree:
    nodes: dict[str, xr.Dataset] = {"/": tree.root.to_dataset()}
    for key, child in tree.children.items():
        ds = child.to_dataset()
        nodes[f"/{key}"] = ds.drop_vars(name) if name in ds.data_vars else ds
    return xr.DataTree.from_dict(nodes)


def _radarx_warnings(caught: Sequence[warnings.WarningMessage]) -> list[str]:
    out = []
    for w in caught:
        if issubclass(w.category, (RuntimeWarning, UserWarning)):
            text = str(w.message).strip()
            if text and "All-NaN" not in text and "Mean of empty" not in text:
                out.append(text)
    return out


def _limit_range(g: xr.Dataset, max_range_m: float) -> xr.Dataset:
    """Drop velocity / reflectivity of a radar beyond ``max_range_m`` ground range from it."""
    if not {"radar_x", "radar_y"} <= set(g.variables):
        return g
    xx = g["x"].values[None, None, None, :] - g["radar_x"].values[:, None, None, None]
    yy = g["y"].values[None, None, :, None] - g["radar_y"].values[:, None, None, None]
    far = xr.DataArray(
        np.broadcast_to(np.hypot(xx, yy) > max_range_m, g["VRADH"].shape),
        dims=("radar", "z", "y", "x"),
        coords=g["VRADH"].coords,
    )
    out = g.copy()
    for name in ("VRADH", "DBZH"):
        if name in out:
            out[name] = out[name].where(~far)
    return out


def _assemble_fields(
    res: xr.Dataset, g: xr.Dataset, opts: Options, x: np.ndarray, y: np.ndarray
) -> tuple[dict[str, np.ndarray], bool, dict[str, Any], dict[str, np.ndarray] | None]:
    """Mask and quality-control the retrieval and add the derived fields.

    Returns ``({name: (z,y,x) f32}, has_uncertainty, qc, raw)``. ``qc`` counts the cells the
    output checks (:attr:`Options.max_speed`, ``max_abs_w``, ``max_uncertainty``) set to NaN and
    carries the warning text; ``raw`` holds the unchecked ``u, v, w`` when ``keep_raw`` is set.
    """

    def arr(name: str) -> np.ndarray | None:
        return res[name].transpose("z", "y", "x").values.astype(np.float32) if name in res else None

    u, v, w = arr("u"), arr("v"), arr("w")
    assert u is not None and v is not None and w is not None
    n_rad = arr("n_radars")
    cross = arr("beam_crossing_angle")
    if n_rad is None:
        n_rad = (
            (np.isfinite(g["VRADH"].transpose("radar", "z", "y", "x").values))
            .sum(0)
            .astype(np.float32)
        )
    if cross is None:
        cross = np.full(u.shape, np.nan, dtype=np.float32)
    good = np.isfinite(u) & np.isfinite(v) & (n_rad >= 2)
    with np.errstate(invalid="ignore"):
        good &= np.isfinite(cross) & (cross >= opts.min_crossing_angle - 1e-6)
    nan = np.float32(np.nan)
    u = np.where(good, u, nan)
    v = np.where(good, v, nan)
    w = np.where(good, w, nan)
    ue, ve = arr("u_error"), arr("v_error")
    has_unc = ue is not None and ve is not None
    unc = (
        np.where(good, np.hypot(ue, ve), nan).astype(np.float32)
        if has_unc
        else np.full(u.shape, nan, dtype=np.float32)
    )
    raw = {"u": u.copy(), "v": v.copy(), "w": w.copy()} if opts.keep_raw else None

    # output quality control: impossible values (folded velocities, edge artefacts) -> NaN
    n_wind = int(good.sum())
    with np.errstate(invalid="ignore"):
        speed0 = np.hypot(u, v)
        bad_speed = good & (speed0 > opts.max_speed) if opts.max_speed else np.zeros_like(good)
        bad_unc = (
            good & has_unc & (unc > opts.max_uncertainty)
            if opts.max_uncertainty
            else np.zeros_like(good)
        )
        kill = bad_speed | bad_unc
        u = np.where(kill, nan, u)
        v = np.where(kill, nan, v)
        w = np.where(kill, nan, w)
        unc = np.where(kill, nan, unc)
        good = good & ~kill
        n_w = int(np.isfinite(w).sum())
        bad_w = (
            np.isfinite(w) & (np.abs(w) > opts.max_abs_w) if opts.max_abs_w else np.zeros_like(good)
        )
        w = np.where(bad_w, nan, w)
    qc = {
        "max_speed": opts.max_speed,
        "max_abs_w": opts.max_abs_w,
        "max_uncertainty": opts.max_uncertainty,
        "wind_cells": n_wind,
        "speed_masked": int(bad_speed.sum()),
        "uncertainty_masked": int((bad_unc & ~bad_speed).sum()),
        "masked_cells": int(kill.sum()),
        "masked_pct": 100.0 * int(kill.sum()) / max(n_wind, 1),
        "w_cells": n_w,
        "w_masked": int(bad_w.sum()),
        "w_masked_pct": 100.0 * int(bad_w.sum()) / max(n_w, 1),
        "raw_kept": opts.keep_raw,
        "warnings": _qc_warnings(opts, n_wind, bad_speed, bad_unc, kill, n_w, bad_w),
    }

    dx = float(x[1] - x[0])
    dy = float(y[1] - y[0])
    dudx = np.gradient(u, dx, axis=2)
    dudy = np.gradient(u, dy, axis=1)
    dvdx = np.gradient(v, dx, axis=2)
    dvdy = np.gradient(v, dy, axis=1)
    if "DBZH" in g:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            dbz = np.nanmean(g["DBZH"].transpose("radar", "z", "y", "x").values, axis=0)
    else:
        dbz = np.full(u.shape, nan)
    fields = {
        "u": u,
        "v": v,
        "w": w,
        "speed": np.hypot(u, v),
        "divergence": (dudx + dvdy) * 1e3,
        "vorticity": (dvdx - dudy) * 1e3,
        "uncertainty": unc,
        "crossing_angle": np.where(good, cross, nan),
        "n_radars": np.where(np.isfinite(n_rad), n_rad, nan),
        "dbz": dbz,
    }
    out = {k: np.asarray(v_, dtype=np.float32) for k, v_ in fields.items()}
    return out, has_unc, qc, raw


def _qc_warnings(
    opts: Options,
    n_wind: int,
    bad_speed: np.ndarray,
    bad_unc: np.ndarray,
    kill: np.ndarray,
    n_w: int,
    bad_w: np.ndarray,
) -> list[str]:
    out = []
    if int(kill.sum()):
        parts = []
        if int(bad_speed.sum()):
            parts.append(f"{int(bad_speed.sum())} faster than {opts.max_speed:g} m/s")
        if int((bad_unc & ~bad_speed).sum()):
            parts.append(
                f"{int((bad_unc & ~bad_speed).sum())} with an error above "
                f"{opts.max_uncertainty:g} m/s"
            )
        out.append(
            f"Quality control set {int(kill.sum())} wind cells "
            f"({100 * int(kill.sum()) / max(n_wind, 1):.1f} % of the retrieved winds) to missing: "
            + ", ".join(parts)
            + ". Such values usually mean folded velocities or edge artefacts."
        )
    if int(bad_w.sum()):
        out.append(
            f"Quality control set {int(bad_w.sum())} vertical velocities "
            f"({100 * int(bad_w.sum()) / max(n_w, 1):.1f} % of those retrieved) with "
            f"|w| above {opts.max_abs_w:g} m/s to missing."
        )
    return out


def _level_coverage(fields: dict[str, np.ndarray], z: np.ndarray) -> list[dict[str, float]]:
    """Per height: share (%) of the cells with two or more radars, a wind, and a ``w``."""
    cells = fields["u"][0].size
    n_rad = fields["n_radars"]
    wind = np.isfinite(fields["u"]) & np.isfinite(fields["v"])
    w_ok = np.isfinite(fields["w"])
    out = []
    for k, zk in enumerate(z):
        out.append(
            {
                "z_m": float(zk),
                "dual_pct": 100.0 * float((n_rad[k] >= 2).sum()) / cells,
                "wind_pct": 100.0 * float(wind[k].sum()) / cells,
                "w_pct": 100.0 * float(w_ok[k].sum()) / cells,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def to_dataset(rec: WindRecord) -> xr.Dataset:
    """CF-flavoured Dataset of a result: ``(z, y, x)`` fields with 2-D ``lon`` / ``lat``."""
    proj = _aeqd(*rec.origin)
    xx, yy = np.meshgrid(rec.x, rec.y)
    lon, lat = proj(xx, yy, inverse=True)
    data = {
        name: (
            ("z", "y", "x"),
            rec.fields[i],
            {"units": FIELD_INFO[name][0], "long_name": FIELD_INFO[name][1]},
        )
        for i, name in enumerate(FIELD_ORDER)
    }
    for name, vals in (rec.raw or {}).items():
        data[f"{name}_raw"] = (
            ("z", "y", "x"),
            vals,
            {"units": "m/s", "long_name": f"{FIELD_INFO[name][1]} before quality control"},
        )
    return xr.Dataset(
        data,
        coords={
            "x": ("x", rec.x, {"units": "m", "long_name": "distance east of the grid origin"}),
            "y": ("y", rec.y, {"units": "m", "long_name": "distance north of the grid origin"}),
            "z": ("z", rec.z, {"units": "m", "long_name": "height above sea level"}),
            "lon": (("y", "x"), lon, {"units": "degrees_east"}),
            "lat": (("y", "x"), lat, {"units": "degrees_north"}),
        },
        attrs={
            "title": "Multi-Doppler wind analysis (xradar-desktop, radarx)",
            "method": rec.summary.get("method", ""),
            "origin_latitude": rec.origin[0],
            "origin_longitude": rec.origin[1],
            "radars": ", ".join(r["name"] for r in rec.radars),
            "radar_names": [str(r["name"]) for r in rec.radars],
            "radar_latitude": [float(r["latitude"]) for r in rec.radars],
            "radar_longitude": [float(r["longitude"]) for r in rec.radars],
            "analysis_time": str(rec.summary.get("analysis_time") or ""),
            "Conventions": "CF-1.8",
        },
    )


def write_netcdf(rec: WindRecord, path: str | Path) -> None:
    ds = to_dataset(rec)
    enc = {
        n: {"zlib": True, "complevel": 1, "_FillValue": np.float32(np.nan)} for n in ds.data_vars
    }
    ds.to_netcdf(path, engine="netcdf4", format="NETCDF4", encoding=enc)

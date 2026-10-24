"""The observed lightning layer of the viewer: readers, flash clustering, GLM parallax and flash-rate series.

Provenance class OBSERVED (``docs/SCIENCE.md``): positions, times and strengths are what the instrument
reported. Two kinds of local files are read, **from disk only** (nothing is downloaded here):

* **GLM**, the Geostationary Lightning Mapper of GOES-16 to GOES-19: Level 2 LCFA netCDF files
  (``OR_GLM-L2-LCFA_G16_s...nc``). The file holds events, groups and flashes already grouped by the
  producer (their thresholds are in the file, see ``group_time_threshold`` / ``flash_time_threshold``).
  The layer uses the **groups** as the measured points (centroid position, time, radiant energy) and the
  **flashes** as their parents; the groups of one flash joined in time order are the flash "channel".
  A GLM pixel is about 8 km at nadir, so a channel is a cluster of pixels, not a lightning path. GLM has
  **no height**.
* **LMA**, a ground Lightning Mapping Array: ASCII ``LYLOUT_*.dat`` source files, read by
  ``radarx.io.read_lma`` (optional library). Every VHF **source** has a time, latitude, longitude,
  altitude (m above mean sea level), power (dBW), reduced chi-square and the number of stations that
  located it. The sources are grouped into flashes here by :func:`link_points`.

Flash clustering (LMA)
----------------------
The algorithm is ``radarx.retrieve.cluster_flashes`` (the optional radarx library, which also reads the
files): two sources are linked when their straight-line (Earth-centred, WGS84) separation ``dx`` and time
difference ``dt`` satisfy ``(dx / D)**2 + (dt / T)**2 <= 1`` and a flash is a connected group of linked
sources (single linkage), the space-time metric of Fuchs et al. (2016), who ran DBSCAN (Ester et al.
1996) on the same normalised coordinates. The defaults ``D = 3 km``, ``T = 0.15 s`` are the values they used
for the sensitive Colorado network (6 km for the less sensitive Alabama and D.C. networks);
``min_points`` is the ``min_samples`` of DBSCAN; ``max_duration`` cuts a longer flash in time order
(radarx's own rule, the buffers of the paper are not reproduced). This module only chooses the sources
(quality filter, box, window) and passes the thresholds, which the panel shows next to the result.

    Fuchs, B. R., E. C. Bruning, S. A. Rutledge, L. D. Carey, P. R. Krehbiel, W. Rison, 2016:
    Climatological analyses of LMA data with an open-source lightning flash-clustering algorithm.
    J. Geophys. Res. Atmos., 121, 8625-8648, https://doi.org/10.1002/2015JD024663
    Ester, M., H.-P. Kriegel, J. Sander, X. Xu, 1996: A density-based algorithm for discovering clusters
    in large spatial databases with noise. KDD-96 Proceedings, 226-231.

GLM parallax
------------
GLM looks at a cloud from 35 786 km away, so a flash high in the cloud is seen displaced away from
its true ground position by about ``dh * tan(zeta)`` (``zeta``: zenith angle of the satellite at the
flash; about 40 to 50 degrees over the central United States, so about 1 km per km of height). The product
navigates events to a "lightning ellipsoid" above the Earth's surface; **the file does not state the
height of that surface**, so the height the product refers to (``GLM_PRODUCT_HEIGHT_KM``) is an ASSUMPTION
of this program (12 km, within the 6 to 16 km range that NOAA describes for the lightning ellipsoid; check
it against the GOES-R product guide before relying on it). :func:`glm_parallax_shift` moves a position along
the line of sight to the satellite from one assumed height to another. The geometry is ``radarx.retrieve.glm_parallax`` (radarx 0.5.4 and later; a local fallback is used with older
versions). The layer either leaves the positions
as the product gives them and shows the parallax as an uncertainty (the shift for 5 km of cloud-top height
error at the centre of the data), or, when the user asks, moves them to a chosen cloud-top height.
Earth model: WGS84; the satellite at the nominal subpoint of the file at ``nominal_satellite_height``.
"""

# ruff: noqa: E501
from __future__ import annotations

import base64
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from engine.lightning_obs import glm_product_time, offsets_to_seconds

# ── Limits and defaults ─────────────────────────────────────────────────────

MAX_POINTS = 200_000  # sources / groups returned to the viewer; more are refused by truncation, said in the answer
MAX_FILES = 400
#: Fuchs et al. (2016) values for the Colorado LMA
DEFAULT_LINK = {
    "distance_m": 3000.0,
    "time_s": 0.15,
    "min_points": 1,
    "max_duration_s": 3.0,
}
#: LMA quality filter of Fuchs et al. (2016), p. 8628: chi-square at most 1, at least six stations
DEFAULT_LMA_QC = {"max_chi2": 1.0, "min_stations": 6}
#: ASSUMPTION: height (km above the WGS84 ellipsoid) the GLM product positions refer to. Not in the file.
GLM_PRODUCT_HEIGHT_KM = 12.0
#: Cloud-top height error (km) used to show the parallax as an uncertainty.
PARALLAX_UNCERTAINTY_KM = 5.0

# WGS84
_A = 6378137.0
_F = 1 / 298.257223563
_B = _A * (1 - _F)
_E2 = _F * (2 - _F)

SPEED_OF_SOUND_MS = 343.0

_GLM_NAME = re.compile(
    r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})\d_e(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})\d_"
)


# ── Geodesy ─────────────────────────────────────────────────────────────────


def geodetic_to_ecef(lat_deg, lon_deg, h_m) -> np.ndarray:
    """ECEF (x, y, z) in metres of geodetic positions (WGS84), shape (..., 3)."""
    lat = np.radians(np.asarray(lat_deg, dtype=float))
    lon = np.radians(np.asarray(lon_deg, dtype=float))
    h = np.asarray(h_m, dtype=float)
    n = _A / np.sqrt(1 - _E2 * np.sin(lat) ** 2)
    x = (n + h) * np.cos(lat) * np.cos(lon)
    y = (n + h) * np.cos(lat) * np.sin(lon)
    z = (n * (1 - _E2) + h) * np.sin(lat)
    return np.stack(np.broadcast_arrays(x, y, z), axis=-1)


def ecef_to_geodetic(xyz) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Geodetic latitude, longitude (degrees) and height (m) of ECEF positions (iterative, WGS84)."""
    p = np.asarray(xyz, dtype=float)
    x, y, z = p[..., 0], p[..., 1], p[..., 2]
    lon = np.arctan2(y, x)
    rho = np.hypot(x, y)
    lat = np.arctan2(z, rho * (1 - _E2))
    h = np.zeros_like(lat)
    for _ in range(6):
        n = _A / np.sqrt(1 - _E2 * np.sin(lat) ** 2)
        h = rho / np.cos(lat) - n
        lat = np.arctan2(z, rho * (1 - _E2 * n / (n + h)))
    return np.degrees(lat), np.degrees(lon), h


def glm_parallax_shift(
    lat,
    lon,
    sat_lon_deg: float,
    sat_height_km: float,
    h_from_km: float,
    h_to_km: float,
    sat_lat_deg: float = 0.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Where the ground point under a flash is when the flash is at ``h_to_km`` instead of ``h_from_km``.

    The flash is seen by the satellite along one line of sight. The product placed it where that line
    crosses the surface ``h_from_km`` above the WGS84 ellipsoid; if the flash really was at ``h_to_km``
    (the line crosses that surface higher up and nearer the satellite), its position vertically below is
    the returned latitude and longitude (degrees). A higher assumed height moves it towards the
    sub-satellite point; a lower one moves it away. ``h_to_km == h_from_km`` returns the input, and a
    point under the satellite does not move. The crossing is found by bisection on the geodetic height
    along the ray (to a millimetre).

    The same maths is ``radarx.retrieve.glm_parallax`` (radarx 0.5.4 and later); when the installed radarx
    has it, that function does the work and this one only converts kilometres to metres. The code below
    is the fallback for older radarx versions (tested against the same known answers).
    """
    try:
        from radarx.retrieve import glm_parallax as _glm_parallax
    except ImportError:
        _glm_parallax = None
    if _glm_parallax is not None:
        return _glm_parallax(
            lat,
            lon,
            satellite_longitude=sat_lon_deg,
            satellite_latitude=sat_lat_deg,
            satellite_height=sat_height_km * 1000.0,
            height_from=h_from_km * 1000.0,
            height_to=h_to_km * 1000.0,
        )
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    shape = np.broadcast(lat, lon).shape
    lat_b, lon_b = np.broadcast_arrays(lat, lon)
    if h_to_km == h_from_km:
        return lat_b.copy(), lon_b.copy()
    sat = geodetic_to_ecef(sat_lat_deg, sat_lon_deg, sat_height_km * 1000.0)
    p0 = geodetic_to_ecef(lat_b, lon_b, h_from_km * 1000.0)
    d = p0 - sat
    length = np.linalg.norm(d, axis=-1, keepdims=True)
    u = d / length
    s0 = length[..., 0]
    target = h_to_km * 1000.0
    # bracket: a height change of dh moves the point by dh / cos(zeta) along the ray; 400 km is far more than needed
    lo = s0 - 400_000.0
    hi = s0 + 400_000.0
    for _ in range(46):
        mid = 0.5 * (lo + hi)
        pm = sat + u * mid[..., None]
        hm = ecef_to_geodetic(pm)[2]
        # height falls as s grows (the ray goes towards the ground): too high -> go further along the ray
        too_high = hm > target
        lo = np.where(too_high, mid, lo)
        hi = np.where(too_high, hi, mid)
    s = 0.5 * (lo + hi)
    out_lat, out_lon, _ = ecef_to_geodetic(sat + u * s[..., None])
    return out_lat.reshape(shape), out_lon.reshape(shape)


def distance_km(lat1, lon1, lat2, lon2) -> np.ndarray:
    """Great-circle distance (km) on a sphere of 6371 km (for the size of a parallax shift, not for geodesy)."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dl = np.radians(np.asarray(lon2, dtype=float) - np.asarray(lon1, dtype=float))
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * 6371.0 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


# ── Data model ──────────────────────────────────────────────────────────────


@dataclass
class LightningSet:
    """Observed lightning inside a time window and box: points (sources / groups) and their flashes."""

    kind: str  # "glm" | "lma"
    instrument: str
    t_ref: datetime  # all times in the arrays are seconds after this (UTC)
    # points (LMA sources, GLM groups)
    p_t: np.ndarray
    p_lat: np.ndarray
    p_lon: np.ndarray
    p_alt_km: np.ndarray  # NaN for GLM (no height)
    p_strength: np.ndarray  # LMA: power dBW; GLM: group radiant energy J
    p_stations: np.ndarray  # LMA: stations that located the source; GLM: -1
    p_flash: np.ndarray  # index into the flash arrays
    # flashes
    f_t0: np.ndarray
    f_t1: np.ndarray
    f_lat: np.ndarray  # LMA: initiation point (first source); GLM: product centroid
    f_lon: np.ndarray
    f_alt_km: np.ndarray  # LMA initiation height (km ASL); NaN for GLM
    f_n_points: np.ndarray
    f_strength: np.ndarray  # GLM: flash radiant energy J; LMA: NaN
    f_area_km2: np.ndarray  # GLM: product flash area; LMA: convex-hull area is not computed -> NaN
    coverage: list[tuple[float, float]]  # seconds after t_ref where the instrument data exist
    files: list[str]
    meta: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: LMA only: the radarx dataset of the kept sources with their flashes (not sent to the viewer; the statistics use it)
    source_ds: Any = None

    @property
    def n_points(self) -> int:
        return int(self.p_t.size)

    @property
    def n_flashes(self) -> int:
        return int(self.f_t0.size)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_when(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), UTC)
    text = str(value).replace("Z", "+00:00")
    d = datetime.fromisoformat(text)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _glm_granule_span(path: Path) -> tuple[datetime, datetime] | None:
    """Start and end of a GLM granule from its file name (None when the name does not follow the pattern)."""
    m = _GLM_NAME.search(path.name)
    if not m:
        return None
    g = [int(v) for v in m.groups()]

    def when(y, doy, hh, mm, ss):
        return datetime(y, 1, 1, tzinfo=UTC) + timedelta(
            days=doy - 1, hours=hh, minutes=mm, seconds=ss
        )

    return when(*g[0:5]), when(*g[5:10])


def _in_box(lat, lon, box) -> np.ndarray:
    if box is None:
        return np.ones(np.shape(lat), dtype=bool)
    south, west, north, east = box
    lon = np.asarray(lon, dtype=float)
    inside_lon = (lon >= west) & (lon <= east) if west <= east else (lon >= west) | (lon <= east)
    return (np.asarray(lat) >= south) & (np.asarray(lat) <= north) & inside_lon


def _check_files(paths: Sequence[str | Path]) -> list[Path]:
    files = [Path(p).expanduser() for p in paths]
    if not files:
        raise ValueError("no lightning files given")
    if len(files) > MAX_FILES:
        raise ValueError(f"{len(files)} files given; at most {MAX_FILES} at a time")
    missing = [f.name for f in files if not f.is_file()]
    if missing:
        raise FileNotFoundError(
            "lightning file(s) not found: "
            + ", ".join(missing)
            + " (nothing is downloaded by this tool)"
        )
    return files


def _percentile_scale(values: np.ndarray, log: bool) -> tuple[np.ndarray, float, float]:
    """Brightness 0..1 from the 5th to the 95th percentile of the values (log10 first when ``log``)."""
    v = np.asarray(values, dtype=float)
    out = np.full(v.shape, 0.6)
    ok = np.isfinite(v) & ((v > 0) if log else True)
    if not ok.any():
        return out, float("nan"), float("nan")
    w = np.log10(v[ok]) if log else v[ok]
    lo, hi = np.percentile(w, 5), np.percentile(w, 95)
    if hi <= lo:
        out[ok] = 0.6
    else:
        out[ok] = np.clip((w - lo) / (hi - lo), 0, 1)
    out[~ok] = 0.0
    return out, float(lo), float(hi)


# ── GLM ─────────────────────────────────────────────────────────────────────


def read_glm(
    paths: Sequence[str | Path],
    *,
    box: tuple[float, float, float, float] | None = None,
    t0: datetime | str | None = None,
    t1: datetime | str | None = None,
    parallax_to_km: float | None = None,
    good_only: bool = True,
) -> LightningSet:
    """GLM Level 2 LCFA files (local) as a :class:`LightningSet` of groups and flashes.

    ``box`` = (south, west, north, east) degrees keeps the flashes whose product centroid is inside;
    ``t0`` / ``t1`` keep the flashes that start inside the window (granules whose file name puts them
    outside are not even opened). ``parallax_to_km``: move every position to that cloud-top height
    (see the module text; None leaves the product positions). ``good_only`` drops flashes whose
    ``flash_quality_flag`` is not 0.
    """
    import xarray as xr

    files = _check_files(paths)
    w0, w1 = _parse_when(t0), _parse_when(t1)
    keep: list[tuple[Path, tuple[datetime, datetime] | None]] = []
    for f in files:
        span = _glm_granule_span(f)
        if span and w0 and span[1] < w0 - timedelta(seconds=10):
            continue
        if span and w1 and span[0] > w1 + timedelta(seconds=10):
            continue
        keep.append((f, span))
    if not keep:
        raise ValueError("none of the GLM files overlaps the requested time window")

    rows_f: list[dict[str, np.ndarray]] = []
    rows_g: list[dict[str, np.ndarray]] = []
    spans: list[tuple[datetime, datetime]] = []
    names: list[str] = []
    sat = {"lon": None, "height": None, "lat": 0.0}
    meta: dict[str, Any] = {}
    dropped_quality = 0
    n_flash_total = 0
    base_all: datetime | None = None
    for fi, (f, span) in enumerate(keep):
        with xr.open_dataset(f) as ds:
            if "flash_lat" not in ds or "group_lat" not in ds:
                raise ValueError(
                    f"{f.name}: not a GLM Level 2 LCFA file (no flash_lat / group_lat)"
                )
            base = glm_product_time(ds)
            base_all = base_all or base
            end = _parse_when(ds.attrs.get("time_coverage_end")) or (
                span[1] if span else base + timedelta(seconds=20)
            )
            spans.append((_parse_when(ds.attrs.get("time_coverage_start")) or base, end))
            names.append(f.name)
            if sat["lon"] is None and "nominal_satellite_subpoint_lon" in ds:
                sat["lon"] = float(ds["nominal_satellite_subpoint_lon"].values)
                sat["lat"] = (
                    float(ds["nominal_satellite_subpoint_lat"].values)
                    if "nominal_satellite_subpoint_lat" in ds
                    else 0.0
                )
                sat["height"] = float(ds["nominal_satellite_height"].values)
                for key in (
                    "platform_ID",
                    "instrument_ID",
                    "orbital_slot",
                    "spatial_resolution",
                    "processing_level",
                ):
                    if key in ds.attrs:
                        meta[key] = str(ds.attrs[key])
                for key in ("group_time_threshold", "flash_time_threshold"):
                    if key in ds:
                        meta[key] = float(ds[key].values)
                if "lat_field_of_view_bounds" in ds:
                    lat_b = [float(v) for v in ds["lat_field_of_view_bounds"].values]
                    lon_b = [float(v) for v in ds["lon_field_of_view_bounds"].values]
                    meta["field_of_view"] = {
                        "lat": [min(lat_b), max(lat_b)],
                        "lon": [min(lon_b), max(lon_b)],
                    }
            fl_lat = np.asarray(ds["flash_lat"].values, dtype=float)
            fl_lon = np.asarray(ds["flash_lon"].values, dtype=float)
            fl_id = np.asarray(ds["flash_id"].values).astype(np.int64)
            fl_t0 = offsets_to_seconds(
                np.asarray(ds["flash_time_offset_of_first_event"].values), base
            )
            fl_t1 = offsets_to_seconds(
                np.asarray(ds["flash_time_offset_of_last_event"].values), base
            )
            fl_e = np.asarray(ds["flash_energy"].values, dtype=float)
            fl_a = (
                np.asarray(ds["flash_area"].values, dtype=float) / 1e6
                if "flash_area" in ds
                else np.full_like(fl_lat, np.nan)
            )
            fl_q = (
                np.asarray(ds["flash_quality_flag"].values, dtype=float)
                if "flash_quality_flag" in ds
                else np.zeros_like(fl_lat)
            )
            n_flash_total += fl_lat.size
            t_abs0 = np.asarray(
                [
                    (base + timedelta(seconds=float(s))).timestamp() if np.isfinite(s) else np.nan
                    for s in fl_t0
                ]
            )
            ok = (
                np.isfinite(fl_lat)
                & np.isfinite(fl_lon)
                & np.isfinite(fl_t0)
                & _in_box(fl_lat, fl_lon, box)
            )
            if w0:
                ok &= t_abs0 >= w0.timestamp()
            if w1:
                ok &= t_abs0 <= w1.timestamp()
            if good_only:
                bad = ok & (fl_q != 0)
                dropped_quality += int(bad.sum())
                ok &= fl_q == 0
            if not ok.any():
                continue
            sel = np.nonzero(ok)[0]
            rows_f.append(
                {
                    "t0": t_abs0[sel],
                    "t1": np.asarray(
                        [
                            (base + timedelta(seconds=float(s))).timestamp()
                            if np.isfinite(s)
                            else np.nan
                            for s in fl_t1[sel]
                        ]
                    ),
                    "lat": fl_lat[sel],
                    "lon": fl_lon[sel],
                    "energy": fl_e[sel],
                    "area": fl_a[sel],
                    "file": np.full(sel.size, fi),
                    "id": fl_id[sel],
                }
            )
            g_par = np.asarray(ds["group_parent_flash_id"].values).astype(np.int64)
            in_sel = np.isin(g_par, fl_id[sel])
            gi = np.nonzero(in_sel)[0]
            g_t = offsets_to_seconds(np.asarray(ds["group_time_offset"].values), base)[gi]
            rows_g.append(
                {
                    "t": np.asarray(
                        [
                            (base + timedelta(seconds=float(s))).timestamp()
                            if np.isfinite(s)
                            else np.nan
                            for s in g_t
                        ]
                    ),
                    "lat": np.asarray(ds["group_lat"].values, dtype=float)[gi],
                    "lon": np.asarray(ds["group_lon"].values, dtype=float)[gi],
                    "energy": np.asarray(ds["group_energy"].values, dtype=float)[gi],
                    "parent": g_par[gi],
                    "file": np.full(gi.size, fi),
                }
            )
    if base_all is None:
        raise ValueError("no GLM file could be read")

    notes: list[str] = []
    if dropped_quality:
        notes.append(
            f"{dropped_quality} flashes with a non-zero quality flag were left out (good flashes only)."
        )
    if not rows_f:
        return _empty_set(
            "glm",
            _glm_instrument(meta),
            base_all,
            spans,
            names,
            meta,
            [*notes, "No GLM flashes in this window and box."],
        )

    f_t0 = np.concatenate([r["t0"] for r in rows_f])
    order = np.argsort(f_t0, kind="stable")
    f_files = np.concatenate([r["file"] for r in rows_f])[order]
    f_ids = np.concatenate([r["id"] for r in rows_f])[order]
    f_arr = {
        k: np.concatenate([r[k] for r in rows_f])[order]
        for k in ("t0", "t1", "lat", "lon", "energy", "area")
    }
    key_to_index = {(int(f_files[i]), int(f_ids[i])): i for i in range(f_ids.size)}
    g_cat = {
        k: np.concatenate([r[k] for r in rows_g])
        for k in ("t", "lat", "lon", "energy", "parent", "file")
    }
    g_flash = np.array(
        [
            key_to_index.get((int(a), int(b)), -1)
            for a, b in zip(g_cat["file"], g_cat["parent"], strict=True)
        ],
        dtype=np.int64,
    )
    good = (
        (g_flash >= 0)
        & np.isfinite(g_cat["t"])
        & np.isfinite(g_cat["lat"])
        & np.isfinite(g_cat["lon"])
    )
    gt = np.nonzero(good)[0]
    gt = gt[np.argsort(g_cat["t"][gt], kind="stable")]
    if gt.size > MAX_POINTS:
        # keep the earliest flashes whose groups fit under the cap, whole (never half a flash)
        cumulative = np.cumsum(np.bincount(g_flash[gt], minlength=f_arr["t0"].size))
        keep_n = int(np.searchsorted(cumulative, MAX_POINTS, side="right"))
        gt = gt[g_flash[gt] < keep_n]
        f_arr = {k: v[:keep_n] for k, v in f_arr.items()}
        meta["truncated"] = True
        notes.append(
            f"Only the first {keep_n} flashes in time are kept ({MAX_POINTS} groups at most). "
            "Narrow the box or give fewer files."
        )
    p_lat, p_lon = g_cat["lat"][gt], g_cat["lon"][gt]
    f_lat, f_lon = f_arr["lat"].copy(), f_arr["lon"].copy()

    # parallax: the shift for a cloud-top height error, always; the correction only when asked
    if sat["lon"] is not None and sat["height"] is not None:
        ctr_lat = float(np.mean(f_lat))
        ctr_lon = float(np.mean(f_lon))
        dl, dn = glm_parallax_shift(
            ctr_lat,
            ctr_lon,
            sat["lon"],
            sat["height"],
            GLM_PRODUCT_HEIGHT_KM,
            GLM_PRODUCT_HEIGHT_KM + PARALLAX_UNCERTAINTY_KM,
            sat["lat"],
        )
        meta["parallax"] = {
            "assumed_product_height_km": GLM_PRODUCT_HEIGHT_KM,
            "uncertainty_cloud_top_error_km": PARALLAX_UNCERTAINTY_KM,
            "uncertainty_shift_km": float(distance_km(ctr_lat, ctr_lon, dl, dn)),
            "applied_to_km": None,
            "assumption": "The file does not state the height of the surface the product positions refer to; "
            f"{GLM_PRODUCT_HEIGHT_KM:g} km is an assumption of this program.",
        }
        if parallax_to_km is not None and parallax_to_km != GLM_PRODUCT_HEIGHT_KM:
            p_lat, p_lon = glm_parallax_shift(
                p_lat,
                p_lon,
                sat["lon"],
                sat["height"],
                GLM_PRODUCT_HEIGHT_KM,
                parallax_to_km,
                sat["lat"],
            )
            f_lat, f_lon = glm_parallax_shift(
                f_lat,
                f_lon,
                sat["lon"],
                sat["height"],
                GLM_PRODUCT_HEIGHT_KM,
                parallax_to_km,
                sat["lat"],
            )
            meta["parallax"]["applied_to_km"] = float(parallax_to_km)
            meta["parallax"]["applied_shift_km_mean"] = float(
                np.mean(distance_km(g_cat["lat"][gt], g_cat["lon"][gt], p_lat, p_lon))
            )

    t_ref = datetime.fromtimestamp(float(np.floor(f_arr["t0"][0])), UTC)
    tr = t_ref.timestamp()
    # re-index the groups by the sorted flash order
    nf = f_arr["t0"].size
    p_strength = g_cat["energy"][gt]
    out = LightningSet(
        kind="glm",
        instrument=_glm_instrument(meta),
        t_ref=t_ref,
        p_t=g_cat["t"][gt] - tr,
        p_lat=p_lat,
        p_lon=p_lon,
        p_alt_km=np.full(gt.size, np.nan),
        p_strength=p_strength,
        p_stations=np.full(gt.size, -1, dtype=np.int64),
        p_flash=g_flash[gt],
        f_t0=f_arr["t0"] - tr,
        f_t1=f_arr["t1"] - tr,
        f_lat=f_lat,
        f_lon=f_lon,
        f_alt_km=np.full(nf, np.nan),
        f_n_points=np.bincount(g_flash[gt], minlength=nf),
        f_strength=f_arr["energy"],
        f_area_km2=f_arr["area"],
        coverage=_merge_spans([(a.timestamp() - tr, b.timestamp() - tr) for a, b in spans]),
        files=names,
        meta=meta,
        notes=notes,
    )
    out.meta.update(n_flashes_in_files=int(n_flash_total))
    return out


def _glm_instrument(meta: dict[str, Any]) -> str:
    plat = meta.get("platform_ID", "")
    num = plat[1:] if plat.startswith("G") else plat
    slot = meta.get("orbital_slot", "")
    return f"GOES-{num} GLM" + (f" ({slot})" if slot else "") if num else "GOES GLM"


def _merge_spans(spans: list[tuple[float, float]], gap: float = 0.5) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1] + gap:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _empty_set(kind, instrument, base, spans, names, meta, notes) -> LightningSet:
    z = np.zeros(0)
    zi = np.zeros(0, dtype=np.int64)
    t_ref = base.replace(microsecond=0)
    tr = t_ref.timestamp()
    return LightningSet(
        kind, instrument, t_ref, z, z, z, z, z, zi, zi, z, z, z, z, z, zi, z, z,
        _merge_spans([(a.timestamp() - tr, b.timestamp() - tr) for a, b in spans]), names, meta, notes,
    )  # fmt: skip


# ── LMA ─────────────────────────────────────────────────────────────────────


def read_lma(
    paths: Sequence[str | Path],
    *,
    box: tuple[float, float, float, float] | None = None,
    t0: datetime | str | None = None,
    t1: datetime | str | None = None,
    max_chi2: float | None = DEFAULT_LMA_QC["max_chi2"],
    min_stations: int | None = DEFAULT_LMA_QC["min_stations"],
    link: dict[str, Any] | None = None,
) -> LightningSet:
    """LMA ``LYLOUT_*.dat`` source files (local) as a :class:`LightningSet` of sources and clustered flashes.

    Needs the optional radarx library for the file reader. Sources are kept when their reduced
    chi-square is at most ``max_chi2`` and at least ``min_stations`` stations located them (the quality
    filter of Fuchs et al. 2016; None keeps all), and when they are inside the box and the time window.
    The flash clustering uses ``link`` (see :func:`link_points`, defaults ``DEFAULT_LINK``).
    """
    files = _check_files(paths)
    try:
        from radarx.io import read_lma as _read_lma
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise ImportError(
            "Reading LMA files needs the radarx library (pip install radarx)."
        ) from exc
    ds = _read_lma(files, max_chi2=max_chi2, min_stations=min_stations)
    return lma_set_from_dataset(
        ds,
        [f.name for f in files],
        box=box,
        t0=t0,
        t1=t1,
        link=link,
        qc={"max_chi2": max_chi2, "min_stations": min_stations},
    )


def lma_set_from_dataset(
    ds, names: list[str], *, box=None, t0=None, t1=None, link=None, qc=None
) -> LightningSet:
    """The :class:`LightningSet` of an LMA source dataset (``radarx.io.read_lma`` layout)."""
    prm = {**DEFAULT_LINK, **(link or {})}
    et = np.asarray(ds["event_time"].values).astype("datetime64[ns]")
    sec = et.astype("int64") / 1e9
    lat = np.asarray(ds["event_latitude"].values, dtype=float)
    lon = np.asarray(ds["event_longitude"].values, dtype=float)
    alt = (
        np.asarray(ds["event_altitude"].values, dtype=float)
        if "event_altitude" in ds
        else np.full(lat.size, np.nan)
    )
    pwr = (
        np.asarray(ds["event_power"].values, dtype=float)
        if "event_power" in ds
        else np.full(lat.size, np.nan)
    )
    nst = (
        np.asarray(ds["event_stations"].values).astype(np.int64)
        if "event_stations" in ds
        else np.full(lat.size, -1, dtype=np.int64)
    )
    w0, w1 = _parse_when(t0), _parse_when(t1)
    n_file = lat.size
    ok = np.isfinite(lat) & np.isfinite(lon) & np.isfinite(sec) & _in_box(lat, lon, box)
    if w0:
        ok &= sec >= w0.timestamp()
    if w1:
        ok &= sec <= w1.timestamp()
    idx = np.nonzero(ok)[0]
    idx = idx[np.argsort(sec[idx], kind="stable")]
    notes: list[str] = []
    meta: dict[str, Any] = {"link": prm, "qc": qc or {}, "n_sources_in_files": int(n_file)}
    if "station_latitude" in ds:
        stations = []
        codes = (
            np.asarray(ds["station_code"].values).astype(str)
            if "station_code" in ds
            else np.arange(ds["station_latitude"].size).astype(str)
        )
        names_s = (
            np.asarray(ds["station_name"].values).astype(str) if "station_name" in ds else codes
        )
        for k in range(ds["station_latitude"].size):
            stations.append(
                {
                    "code": str(codes[k]),
                    "name": str(names_s[k]),
                    "lat": float(ds["station_latitude"].values[k]),
                    "lon": float(ds["station_longitude"].values[k]),
                }
            )
        meta["stations"] = stations
    if "network_center_latitude" in ds:
        meta["network_center"] = {
            "lat": float(ds["network_center_latitude"].values),
            "lon": float(ds["network_center_longitude"].values),
        }
    coverage_all = (float(np.nanmin(sec)), float(np.nanmax(sec))) if n_file else None
    if idx.size == 0:
        base = datetime.fromtimestamp(coverage_all[0], UTC) if coverage_all else datetime.now(UTC)
        spans = (
            [
                (
                    datetime.fromtimestamp(coverage_all[0], UTC),
                    datetime.fromtimestamp(coverage_all[1], UTC),
                )
            ]
            if coverage_all
            else []
        )
        return _empty_set(
            "lma",
            "LMA (Lightning Mapping Array)",
            base,
            spans,
            names,
            meta,
            ["No LMA sources in this window and box."],
        )
    if idx.size > MAX_POINTS:
        notes.append(
            f"{idx.size} sources are in the window; only the first {MAX_POINTS} in time are kept. Narrow the time window."
        )
        meta["truncated"] = True
        idx = idx[:MAX_POINTS]
    t_abs = sec[idx]
    t_ref = datetime.fromtimestamp(float(np.floor(t_abs[0])), UTC)
    tr = t_ref.timestamp()
    try:
        from radarx.retrieve import cluster_flashes
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise ImportError("Clustering LMA sources into flashes needs the radarx library.") from exc
    # the flash algorithm lives in radarx (Fuchs et al. 2016); the sources are already in time order
    clustered = cluster_flashes(
        ds.isel(number_of_events=idx),
        distance=float(prm["distance_m"]),
        time=float(prm["time_s"]),
        min_points=int(prm["min_points"]),
        max_duration=float(prm["max_duration_s"]) if prm.get("max_duration_s") else None,
        noise="flash",
    )
    flash = np.asarray(clustered["event_parent_flash_id"].values).astype(np.int64)
    nf = int(flash.max()) + 1 if flash.size else 0
    first = np.full(nf, -1, dtype=np.int64)
    last = np.full(nf, -1, dtype=np.int64)
    for i in range(flash.size):  # sources are in time order
        f = flash[i]
        if first[f] < 0:
            first[f] = i
        last[f] = i
    alt_km = alt[idx] / 1000.0
    cov = (coverage_all[0] - tr, coverage_all[1] - tr) if coverage_all else (0.0, 0.0)
    return LightningSet(
        kind="lma",
        instrument="LMA (Lightning Mapping Array)",
        t_ref=t_ref,
        p_t=t_abs - tr,
        p_lat=lat[idx],
        p_lon=lon[idx],
        p_alt_km=alt_km,
        p_strength=pwr[idx],
        p_stations=nst[idx],
        p_flash=flash,
        f_t0=(t_abs - tr)[first],
        f_t1=(t_abs - tr)[last],
        f_lat=lat[idx][first],
        f_lon=lon[idx][first],
        f_alt_km=alt_km[first],
        f_n_points=np.bincount(flash, minlength=nf),
        f_strength=np.full(nf, np.nan),
        f_area_km2=np.full(nf, np.nan),
        coverage=[cov],
        files=names,
        meta=meta,
        notes=notes,
        source_ds=clustered,
    )


def read_lightning(paths: Sequence[str | Path], **kw) -> LightningSet:
    """Read local lightning files of one kind: GLM (``.nc``) or LMA (``.dat``, ``.dat.gz``)."""
    files = _check_files(paths)
    nc = [f for f in files if f.suffix.lower() in (".nc", ".nc4")]
    if nc and len(nc) != len(files):
        raise ValueError("give either GLM netCDF files or LMA .dat files, not both")
    if nc:
        keys = ("box", "t0", "t1", "parallax_to_km", "good_only")
        return read_glm(files, **{k: v for k, v in kw.items() if k in keys})
    keys = ("box", "t0", "t1", "max_chi2", "min_stations", "link")
    return read_lma(files, **{k: v for k, v in kw.items() if k in keys})


# ── Series ──────────────────────────────────────────────────────────────────


def rate_series(
    t_s: Sequence[float] | np.ndarray,
    start_s: float,
    end_s: float,
    bin_s: float = 60.0,
    coverage: Sequence[tuple[float, float]] | None = None,
) -> dict[str, np.ndarray]:
    """Counts per time bin and the rate per minute; bins the data do not cover are NaN (a gap, not zero).

    Bins start at ``start_s``; the last bin may be shorter than ``bin_s`` (its rate uses its true length).
    A bin counts as covered when the instrument data (``coverage`` intervals) cover all of it; with
    ``coverage=None`` every bin is covered.
    """
    if not end_s > start_s or bin_s <= 0:
        raise ValueError("need end > start and a positive bin length")
    n = math.ceil((end_s - start_s) / bin_s - 1e-9)
    edges = start_s + bin_s * np.arange(n + 1)
    edges[-1] = min(edges[-1], end_s)
    t = np.asarray(t_s, dtype=float)
    counts = np.histogram(t[(t >= start_s) & (t <= end_s)], bins=edges)[0].astype(float)
    width = np.diff(edges)
    rate = counts / (width / 60.0)
    if coverage is not None:
        for k in range(n):
            lo, hi = edges[k], edges[k + 1]
            if not any(c0 <= lo + 1e-9 and c1 >= hi - 1e-9 for c0, c1 in coverage):
                counts[k] = np.nan
                rate[k] = np.nan
    return {"edges": edges, "count": counts, "rate_per_min": rate}


def thunder_delay_s(distance_km_: float, compress: float = 1.0) -> float:
    """Light-to-sound delay of a flash ``distance_km_`` away at 343 m/s, divided by ``compress`` (1 = real time)."""
    return distance_km_ * 1000.0 / SPEED_OF_SOUND_MS / max(compress, 1e-9)


# ── Wire form ───────────────────────────────────────────────────────────────


def _b64(a: np.ndarray, dtype: str = "<f4") -> str:
    return base64.b64encode(np.ascontiguousarray(a, dtype=dtype).tobytes()).decode("ascii")


def to_wire(ls: LightningSet) -> dict[str, Any]:
    """The answer of ``lightning_open``: columns as base64 little-endian float32 / int32 (times in seconds after ``t_ref``)."""
    bright, lo, hi = _percentile_scale(ls.p_strength, log=ls.kind == "glm")
    return {
        "kind": ls.kind,
        "instrument": ls.instrument,
        "t_ref": _iso(ls.t_ref),
        "files": ls.files,
        "points": {
            "n": ls.n_points,
            "t": _b64(ls.p_t),
            "lat": _b64(ls.p_lat),
            "lon": _b64(ls.p_lon),
            "alt_km": _b64(ls.p_alt_km),
            "strength": _b64(ls.p_strength),
            "brightness": _b64(bright),
            "stations": _b64(ls.p_stations, "<i4"),
            "flash": _b64(ls.p_flash, "<i4"),
        },
        "flashes": {
            "n": ls.n_flashes,
            "t0": _b64(ls.f_t0),
            "t1": _b64(ls.f_t1),
            "lat": _b64(ls.f_lat),
            "lon": _b64(ls.f_lon),
            "alt_km": _b64(ls.f_alt_km),
            "n_points": _b64(ls.f_n_points, "<i4"),
            "strength": _b64(ls.f_strength),
            "area_km2": _b64(ls.f_area_km2),
        },
        "coverage": [[float(a), float(b)] for a, b in ls.coverage],
        "strength": {
            "unit": "dBW" if ls.kind == "lma" else "J",
            "what": "power of the VHF source"
            if ls.kind == "lma"
            else "radiant energy of the GLM group",
            "brightness_scale": ("linear in dBW" if ls.kind == "lma" else "log10 of the energy")
            + f", 5th to 95th percentile of the loaded points ({lo:.3g} to {hi:.3g})",
        },
        "meta": ls.meta,
        "notes": ls.notes,
        "provenance": provenance_of(ls),
    }


def provenance_of(ls: LightningSet) -> dict[str, Any]:
    """The layer provenance record (class OBSERVED) the Layers panel shows."""
    t_first = ls.t_ref + timedelta(seconds=float(ls.coverage[0][0])) if ls.coverage else ls.t_ref
    glm = ls.kind == "glm"
    return {
        "kind": "observed",
        "source": f"{ls.instrument}: {', '.join(ls.files[:3])}"
        + (f" and {len(ls.files) - 3} more" if len(ls.files) > 3 else ""),
        "title": "Lightning flashes (GLM)" if glm else "Lightning sources and flashes (LMA)",
        "valid_time": _iso(t_first),
        "method": (
            "Flashes and groups as grouped by the GLM Level 2 product; no height; the position is that of the product "
            "unless a parallax correction to an assumed cloud-top height is switched on."
            if glm
            else "Sources as located by the array; flashes by space-time linking (Fuchs et al. 2016) with the thresholds shown."
        ),
        "uncertainty": (
            "Detection efficiency is not given in the file and falls toward the edge of the field of view; 8 km pixels at nadir; "
            "parallax of the order of the cloud-top height error times tan(zenith angle)."
            if glm
            else "Detection efficiency and location error depend on range from the network and on the number of stations; the reduced chi-square and station filters are shown."
        ),
    }

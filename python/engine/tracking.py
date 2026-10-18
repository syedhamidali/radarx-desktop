"""Storm cell identification and tracking for radar data (numpy/scipy implementation).

Connected-component cell detection on polar reflectivity sweeps and simple
nearest-neighbour tracking between consecutive scans.  This is the dependency-free
fallback of :mod:`engine.cells`, which prefers ``radarx`` when it is installed and
adds the unified API (multi-scan tracking, statistics, overlays).  The dataclasses
defined here are shared by both backends.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import xarray as xr
from scipy import ndimage

logger = logging.getLogger(__name__)


@dataclass
class CellInfo:
    """Properties of a single identified storm cell."""

    id: str
    centroid_az: float  # degrees
    centroid_range: float  # metres
    area_km2: float
    max_dbz: float
    mean_dbz: float
    boundary_points: list[list[float]]  # [[az_deg, range_m], ...]
    # Optional additions (None / empty until the producer knows them)
    lat: float | None = None  # centroid latitude, needs the radar position
    lon: float | None = None
    echo_top_proxy_km: float | None = None  # highest sampled beam height in the cell (km)
    speed_mps: float | None = None  # motion, set once the cell is part of a track
    direction_deg: float | None = None  # meteorological "from" direction
    track_id: str | None = None
    lifetime: int | None = None  # scans the cell's track has been seen in
    extra: dict[str, float] = field(default_factory=dict)  # e.g. vil_max, vil_mean

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dictionary (optional fields only when known)."""
        out: dict[str, Any] = {
            "id": self.id,
            "centroid_az": round(self.centroid_az, 2),
            "centroid_range": round(self.centroid_range, 2),
            "area_km2": round(self.area_km2, 2),
            "max_dbz": round(self.max_dbz, 1),
            "mean_dbz": round(self.mean_dbz, 1),
            "boundary_points": [[round(az, 2), round(rng, 2)] for az, rng in self.boundary_points],
        }
        for key, val, nd in (
            ("lat", self.lat, 4),
            ("lon", self.lon, 4),
            ("echo_top_proxy_km", self.echo_top_proxy_km, 2),
            ("speed_mps", self.speed_mps, 2),
            ("direction_deg", self.direction_deg, 1),
        ):
            if val is not None and math.isfinite(val):
                out[key] = round(float(val), nd)
        if self.track_id is not None:
            out["track_id"] = self.track_id
        if self.lifetime is not None:
            out["lifetime"] = int(self.lifetime)
        if self.extra:
            out["extra"] = {
                k: round(float(v), 3) for k, v in self.extra.items() if math.isfinite(v)
            }
        return out


@dataclass
class CellTrack:
    """A tracked storm cell across consecutive scans."""

    track_id: str
    cell_history: list[CellInfo] = field(default_factory=list)
    speed_mps: float = 0.0  # metres per second
    direction_deg: float = 0.0  # meteorological convention (from-direction)
    # Optional additions (``cells.track`` fills them; the legacy pair tracker leaves defaults)
    u_mps: float = 0.0  # eastward velocity
    v_mps: float = 0.0  # northward velocity
    lifetime: int = 0  # scans the track spans (0 = unknown)
    split_from: str | None = None  # id of the track this one split off from
    merged_into: str | None = None  # id of the track this one merged into when it ended
    status: str = ""  # "new" | "continued" | "ended" ("" = unknown)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dictionary."""
        out: dict[str, Any] = {
            "track_id": self.track_id,
            "cell_history": [c.to_dict() for c in self.cell_history],
            "speed_mps": round(self.speed_mps, 2),
            "direction_deg": round(self.direction_deg, 1),
            "u_mps": round(self.u_mps, 2),
            "v_mps": round(self.v_mps, 2),
            "lifetime": int(self.lifetime or len(self.cell_history)),
            "split": self.split_from is not None,
            "merge": self.merged_into is not None,
        }
        if self.split_from is not None:
            out["split_from"] = self.split_from
        if self.merged_into is not None:
            out["merged_into"] = self.merged_into
        if self.status:
            out["status"] = self.status
        return out


# ---------------------------------------------------------------------------
# Cell identification
# ---------------------------------------------------------------------------


def _circ_diff_deg(a: np.ndarray | float, b: np.ndarray | float) -> np.ndarray | float:
    """Signed circular difference ``b - a`` in degrees, in [-180, 180)."""
    return (np.asarray(b) - np.asarray(a) + 180.0) % 360.0 - 180.0


def _covers_full_circle(azimuth: np.ndarray) -> bool:
    """True when the last ray is circularly adjacent to the first (full 360 sweep)."""
    n = azimuth.size
    if n < 3:
        return False
    steps = np.abs(_circ_diff_deg(azimuth[:-1], azimuth[1:]))
    med = float(np.median(steps))
    if med <= 0.0:
        return False
    seam = abs(float(_circ_diff_deg(azimuth[-1], azimuth[0])))
    # the rays must also span (almost) all of the circle, not a sector that
    # happens to have its ends close together
    total = float(np.sum(steps)) + seam
    return seam <= 2.0 * med and total <= 360.0 * 1.01 + 2.0 * med


def _merge_seam_labels(labels: np.ndarray, n_features: int) -> np.ndarray:
    """Merge components that touch across the first/last-azimuth seam (8-connectivity)."""
    first, last = labels[0], labels[-1]
    pairs: set[tuple[int, int]] = set()
    for shift in (-1, 0, 1):
        if shift < 0:
            f, l_ = first[:shift], last[-shift:]
        elif shift > 0:
            f, l_ = first[shift:], last[:-shift]
        else:
            f, l_ = first, last
        sel = (f > 0) & (l_ > 0) & (f != l_)
        pairs.update(zip(f[sel].tolist(), l_[sel].tolist(), strict=True))
    if not pairs:
        return labels

    parent = list(range(n_features + 1))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for u, v in pairs:
        ru, rv = find(u), find(v)
        if ru != rv:
            parent[max(ru, rv)] = min(ru, rv)
    mapping = np.array([find(i) for i in range(n_features + 1)], dtype=labels.dtype)
    return mapping[labels]


def _polar_to_xy(az_deg: np.ndarray, rng_m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    az = np.radians(az_deg)
    return rng_m * np.sin(az), rng_m * np.cos(az)


def beam_height_km(range_m: np.ndarray | float, elevation_deg: float) -> np.ndarray | float:
    """Beam-centre height (km above the radar) with the 4/3 effective Earth radius model."""
    r = np.asarray(range_m, dtype=np.float64)
    ke_r = 4.0 / 3.0 * 6371000.0
    el = math.radians(elevation_deg)
    h = np.sqrt(r**2 + ke_r**2 + 2.0 * r * ke_r * math.sin(el)) - ke_r
    return h / 1000.0


def polar_to_latlon(lat0: float, lon0: float, az_deg: float, range_m: float) -> tuple[float, float]:
    """Spherical destination point ``range_m`` from (lat0, lon0) along bearing ``az_deg``."""
    d = range_m / 6371000.0
    br = math.radians(az_deg)
    p1 = math.radians(lat0)
    l1 = math.radians(lon0)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(br))
    l2 = l1 + math.atan2(
        math.sin(br) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2)
    )
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def polar_geometry(
    data: xr.DataArray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(values, azimuth_deg, range_m)`` of a 2-D ``(azimuth, range)`` reflectivity array."""
    if "azimuth" in data.dims and "range" in data.dims and data.ndim == 2:
        data = data.transpose("azimuth", "range")
    values = np.asarray(data.values)
    if not np.issubdtype(values.dtype, np.floating):
        values = values.astype(np.float32)
    azimuth = np.asarray(data.coords["azimuth"].values, dtype=np.float64)
    range_m = np.asarray(data.coords["range"].values, dtype=np.float64)
    if values.shape != (azimuth.size, range_m.size):
        raise ValueError(
            f"data shape {values.shape} does not match azimuth/range "
            f"({azimuth.size}, {range_m.size})"
        )
    return values, azimuth, range_m


def cells_from_labels(
    values: np.ndarray,
    labels: np.ndarray,
    azimuth: np.ndarray,
    range_m: np.ndarray,
    min_size_km2: float = 10.0,
    *,
    elevation_deg: float | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> tuple[list[CellInfo], np.ndarray]:
    """Per-cell statistics of a polar label image (0 = no cell), shared by every backend.

    *labels* has the shape of *values* (``azimuth`` x ``range``); regions that touch across
    the 0/360 seam of a full-circle sweep must already carry one label.  Cells smaller than
    *min_size_km2* are dropped, the rest are ranked by descending peak reflectivity (ties:
    larger first) and numbered ``C001``, ``C002``...

    Returns the cells and a relabelled int32 image in which value ``k`` is ``cells[k - 1]``.
    """
    n_az = len(azimuth)
    labels = np.asarray(labels)
    n_features = int(labels.max()) if labels.size else 0
    ranked = np.zeros(labels.shape, dtype=np.int32)
    if n_features == 0:
        return [], ranked

    if len(range_m) >= 2:
        gate_spacing_m = float(np.median(np.abs(np.diff(range_m))))
    else:
        gate_spacing_m = float(range_m[0]) if len(range_m) == 1 else 250.0
    if n_az >= 2:
        az_spacing_deg = float(np.median(np.abs(_circ_diff_deg(azimuth[:-1], azimuth[1:]))))
    else:
        az_spacing_deg = 1.0
    az_spacing_rad = math.radians(az_spacing_deg)
    wrap = _covers_full_circle(azimuth)

    # ---- vectorised per-label statistics (single pass over the masked gates) ----
    az_idx, rng_idx = np.nonzero(labels)
    lab = labels[az_idx, rng_idx].astype(np.intp)
    gate_area = np.abs(range_m[rng_idx]) * az_spacing_rad * gate_spacing_m  # m^2
    vals = values[az_idx, rng_idx].astype(np.float64)
    minlen = n_features + 1

    count = np.bincount(lab, minlength=minlen)
    area_m2 = np.bincount(lab, weights=gate_area, minlength=minlen)
    val_sum = np.bincount(lab, weights=vals, minlength=minlen)

    # Weights for centroids: area, falling back to unit weights if a cell has zero area.
    w = np.where(area_m2[lab] > 0.0, gate_area, 1.0)
    wsum = np.bincount(lab, weights=w, minlength=minlen)
    rad = np.radians(azimuth[az_idx])
    sin_sum = np.bincount(lab, weights=w * np.sin(rad), minlength=minlen)
    cos_sum = np.bincount(lab, weights=w * np.cos(rad), minlength=minlen)
    rng_sum = np.bincount(lab, weights=w * range_m[rng_idx], minlength=minlen)

    present = np.nonzero(count)[0]
    keep = present[area_m2[present] / 1e6 >= min_size_km2]
    if keep.size == 0:
        return [], ranked

    # Bounding boxes of the (possibly seam-merged) components
    objects = ndimage.find_objects(labels.astype(np.int32))

    entries: list[tuple[CellInfo, int]] = []
    for label_id in keep.tolist():
        sl = objects[label_id - 1]
        if sl is None:
            continue
        component_mask = labels[sl] == label_id
        centroid_az = math.degrees(math.atan2(sin_sum[label_id], cos_sum[label_id])) % 360.0
        centroid_range = float(rng_sum[label_id] / wsum[label_id])
        boundary_points = _extract_boundary(
            component_mask,
            azimuth[sl[0]],
            range_m[sl[1]],
            max_points=120,
            wrap_az=wrap and sl[0].start == 0 and sl[0].stop == n_az,
        )
        cell = CellInfo(
            id=f"C{label_id:03d}",
            centroid_az=centroid_az,
            centroid_range=centroid_range,
            area_km2=float(area_m2[label_id]) / 1e6,
            max_dbz=float(np.nanmax(values[sl][component_mask])),
            mean_dbz=float(val_sum[label_id] / count[label_id]),
            boundary_points=boundary_points,
        )
        if lat is not None and lon is not None:
            cell.lat, cell.lon = polar_to_latlon(lat, lon, centroid_az, centroid_range)
        if elevation_deg is not None:
            cell.echo_top_proxy_km = float(
                beam_height_km(range_m[sl[1]][np.nonzero(component_mask)[1]].max(), elevation_deg)
            )
        entries.append((cell, label_id))

    # Sort by descending max dBZ (ties: larger cell first), then re-label by rank
    entries.sort(key=lambda e: (-e[0].max_dbz, -e[0].area_km2))
    lut = np.zeros(minlen, dtype=np.int32)
    cells: list[CellInfo] = []
    for i, (cell, label_id) in enumerate(entries):
        cell.id = f"C{i + 1:03d}"
        lut[label_id] = i + 1
        cells.append(cell)
    return cells, lut[labels]


def identify_cells_labelled(
    data: xr.DataArray,
    threshold_dbz: float = 35.0,
    min_size_km2: float = 10.0,
    *,
    elevation_deg: float | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> tuple[list[CellInfo], np.ndarray]:
    """Like :func:`identify_cells` but also returns the ranked polar label image."""
    values, azimuth, range_m = polar_geometry(data)
    if values.size == 0:
        return [], np.zeros(values.shape, dtype=np.int32)

    # Binary mask: above threshold (NaN compares False)
    with np.errstate(invalid="ignore"):
        mask = values >= threshold_dbz

    # Connected-component labelling (8-connectivity)
    structure = ndimage.generate_binary_structure(2, 2)
    labels, n_features = ndimage.label(mask, structure=structure)

    if n_features == 0:
        logger.info("No cells found above %.1f dBZ", threshold_dbz)
        return [], np.zeros(values.shape, dtype=np.int32)

    if _covers_full_circle(azimuth):
        labels = _merge_seam_labels(labels, n_features)

    cells, ranked = cells_from_labels(
        values,
        labels,
        azimuth,
        range_m,
        min_size_km2,
        elevation_deg=elevation_deg,
        lat=lat,
        lon=lon,
    )
    logger.info(
        "Identified %d cells above %.1f dBZ (min area %.1f km2)",
        len(cells),
        threshold_dbz,
        min_size_km2,
    )
    return cells, ranked


def identify_cells(
    data: xr.DataArray,
    threshold_dbz: float = 35.0,
    min_size_km2: float = 10.0,
) -> list[CellInfo]:
    """Identify storm cells in a reflectivity sweep using connected-component labelling.

    Parameters
    ----------
    data : xr.DataArray
        2-D reflectivity array with coordinates ``azimuth`` (degrees) and
        ``range`` (metres).  NaN values are treated as below threshold.
    threshold_dbz : float
        Minimum reflectivity value (dBZ) to include a gate in a cell.
    min_size_km2 : float
        Cells with area below this value are discarded.

    Returns
    -------
    list[CellInfo]
        Identified cells sorted by descending ``max_dbz``.  When the sweep
        covers a full circle, cells touching the 0/360 seam are merged into
        one cell and their azimuth centroid is a circular mean.
    """
    return identify_cells_labelled(data, threshold_dbz, min_size_km2)[0]


def _extract_boundary(
    mask: np.ndarray,
    azimuth: np.ndarray,
    range_m: np.ndarray,
    max_points: int = 120,
    wrap_az: bool = False,
) -> list[list[float]]:
    """Extract boundary pixels from a binary mask and return as [az, range] pairs.

    Uses morphological erosion to find edge pixels, then orders them by angle
    around the centroid (in Cartesian space, so the order is meaningful and
    seam-safe) and sub-samples to at most *max_points* evenly spaced points.

    *mask* is indexed ``[azimuth, range]``; *azimuth*/*range_m* are the matching
    coordinate slices.  With *wrap_az* the first and last rows are neighbours
    (full-circle sweep) so the seam itself is not reported as boundary.
    """
    if wrap_az:
        padded = np.pad(mask, ((1, 1), (0, 0)), mode="wrap")
        eroded = ndimage.binary_erosion(padded)[1:-1]
    else:
        eroded = ndimage.binary_erosion(mask)
    edge = mask & ~eroded

    az_idx, rng_idx = np.nonzero(edge)
    if len(az_idx) == 0:
        # Fallback: component is only one pixel thick
        az_idx, rng_idx = np.nonzero(mask)

    az_vals = np.mod(azimuth[az_idx], 360.0)
    rng_vals = range_m[rng_idx]
    x, y = _polar_to_xy(az_vals, rng_vals)
    angles = np.arctan2(y - y.mean(), x - x.mean())
    order = np.argsort(angles, kind="stable")
    if len(order) > max_points:
        order = order[np.linspace(0, len(order) - 1, max_points).astype(np.intp)]
    return np.column_stack([az_vals[order], rng_vals[order]]).tolist()


# ---------------------------------------------------------------------------
# Cell tracking (nearest-neighbour)
# ---------------------------------------------------------------------------


def _cell_distance_km(a: CellInfo, b: CellInfo) -> float:
    """Approximate distance in km between two cell centroids.

    Uses polar coordinates: each cell has (azimuth_deg, range_m).
    Distance computed via the law of cosines on the slant-range plane.
    """
    r1 = a.centroid_range
    r2 = b.centroid_range
    daz = math.radians(b.centroid_az - a.centroid_az)
    dist_m = math.sqrt(max(r1**2 + r2**2 - 2 * r1 * r2 * math.cos(daz), 0.0))
    return dist_m / 1000.0


def match_nearest(
    cells_t0: list[CellInfo],
    cells_t1: list[CellInfo],
    max_distance_km: float = 30.0,
) -> dict[int, int]:
    """Global greedy nearest-neighbour assignment ``index in t0 -> index in t1``.

    Candidate pairs are taken in order of increasing distance, so a cell is never stolen
    from its true nearest neighbour just because it appears earlier in the (max-dBZ
    ordered) list.
    """
    candidates: list[tuple[float, int, int]] = []
    for i, c0 in enumerate(cells_t0):
        for j, c1 in enumerate(cells_t1):
            dist = _cell_distance_km(c0, c1)
            if dist < max_distance_km:
                candidates.append((dist, i, j))
    candidates.sort()
    match: dict[int, int] = {}
    used_t1: set[int] = set()
    for _dist, i, j in candidates:
        if i in match or j in used_t1:
            continue
        match[i] = j
        used_t1.add(j)
    return match


def track_cells(
    cells_t0: list[CellInfo],
    cells_t1: list[CellInfo],
    max_distance_km: float = 30.0,
    dt_seconds: float = 300.0,
) -> list[CellTrack]:
    """Match cells between two consecutive scans using nearest-neighbour assignment.

    Parameters
    ----------
    cells_t0 : list[CellInfo]
        Cells from the earlier scan.
    cells_t1 : list[CellInfo]
        Cells from the later scan.
    max_distance_km : float
        Maximum allowed distance for a match.
    dt_seconds : float
        Time elapsed between the two scans (used for speed calculation).

    Returns
    -------
    list[CellTrack]
        One track per matched pair, plus new tracks for unmatched t1 cells.
    """
    tracks: list[CellTrack] = []
    match = match_nearest(cells_t0, cells_t1, max_distance_km)
    used_t1: set[int] = set(match.values())

    for i, c0 in enumerate(cells_t0):
        best_idx = match.get(i)
        if best_idx is not None:
            c1 = cells_t1[best_idx]
            speed_mps, direction_deg = _compute_motion(c0, c1, dt_seconds)
            tracks.append(
                CellTrack(
                    track_id=f"T{c0.id[1:]}",
                    cell_history=[c0, c1],
                    speed_mps=speed_mps,
                    direction_deg=direction_deg,
                )
            )
        else:
            # Cell from t0 with no match — ended
            tracks.append(
                CellTrack(
                    track_id=f"T{c0.id[1:]}",
                    cell_history=[c0],
                )
            )

    # Unmatched t1 cells — new cells
    for j, c1 in enumerate(cells_t1):
        if j not in used_t1:
            tracks.append(
                CellTrack(
                    track_id=f"TN{j + 1:02d}",
                    cell_history=[c1],
                )
            )

    logger.info(
        "Tracked %d cells (%d matched, %d new)",
        len(tracks),
        len(used_t1),
        len(cells_t1) - len(used_t1),
    )
    return tracks


def _compute_motion(
    c0: CellInfo,
    c1: CellInfo,
    dt_seconds: float,
) -> tuple[float, float]:
    """Compute motion vector (speed in m/s, direction in degrees).

    Direction follows meteorological convention: the direction *from which*
    the cell is moving.
    """
    if not dt_seconds > 0:  # also catches NaN
        return 0.0, 0.0

    # Convert polar to approximate Cartesian (metres)
    az0 = math.radians(c0.centroid_az)
    az1 = math.radians(c1.centroid_az)
    x0 = c0.centroid_range * math.sin(az0)
    y0 = c0.centroid_range * math.cos(az0)
    x1 = c1.centroid_range * math.sin(az1)
    y1 = c1.centroid_range * math.cos(az1)

    dx = x1 - x0
    dy = y1 - y0
    dist_m = math.sqrt(dx**2 + dy**2)
    speed_mps = dist_m / dt_seconds

    # Direction: where the cell is moving *to* (heading), then convert
    # to meteorological "from" direction.
    heading_deg = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    from_deg = (heading_deg + 180) % 360
    if dist_m == 0.0:
        return 0.0, 0.0  # stationary: direction undefined

    return speed_mps, from_deg


def extrapolate_cell(
    cell: CellInfo,
    speed_mps: float,
    direction_deg: float,
    lead_time_seconds: float,
) -> tuple[float, float]:
    """Extrapolate a cell's centroid position forward in time.

    Parameters
    ----------
    cell : CellInfo
        Current cell position.
    speed_mps : float
        Motion speed in m/s.
    direction_deg : float
        Meteorological "from" direction in degrees.
    lead_time_seconds : float
        Forecast lead time in seconds.

    Returns
    -------
    tuple[float, float]
        Predicted (azimuth_deg, range_m).
    """
    # "From" direction → heading (to-direction)
    heading_rad = math.radians((direction_deg + 180) % 360)
    dist_m = speed_mps * lead_time_seconds

    # Current position in Cartesian
    az_rad = math.radians(cell.centroid_az)
    x = cell.centroid_range * math.sin(az_rad) + dist_m * math.sin(heading_rad)
    y = cell.centroid_range * math.cos(az_rad) + dist_m * math.cos(heading_rad)

    # Back to polar
    pred_range = math.sqrt(x**2 + y**2)
    pred_az = (math.degrees(math.atan2(x, y)) + 360) % 360

    return pred_az, pred_range

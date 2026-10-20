"""Storm finding and tracking on gridded radar volumes (shared by ``rxd animate`` and tools/storm_clip).

Pure NumPy/SciPy functions on the merged 3-D mosaic (uint8 codes, 0 = no echo): the convective
cells of a volume, the storm followed from step to step, the echo-free eye of a tropical cyclone,
the storm motion and the box around a track. No file reading, no browser.
"""

# ruff: noqa: E501

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# ---------------------------------------------------------------------------
# Storm finding and tracking
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    x_km: float  # reflectivity-weighted centre, km east of the grid origin
    y_km: float
    max_dbz: float
    area_km2: float
    top_km: float  # highest level (km ASL) with at least ``top_dbz`` in the cell
    score: float


def decode(volume: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """uint8 codes (0 = no echo) -> dBZ (NaN = no echo)."""
    out = lo + (volume.astype(np.float32) - 1.0) * (hi - lo) / 254.0
    out[volume == 0] = np.nan
    return out


def find_cells(
    volume: np.ndarray,
    value_range: tuple[float, float],
    z_km: np.ndarray,
    x0_m: float,
    y0_m: float,
    cell_m: float,
    threshold_dbz: float = 45.0,
    top_dbz: float = 18.0,
    min_area_km2: float = 20.0,
) -> list[Cell]:
    """Convective cells of a 3-D volume: connected areas of column-maximum reflectivity >= threshold.

    ``volume`` is the mosaic's uint8 ``(nz, ny, nx)`` field. The score favours strong, tall and large
    cells: ``max_dbz + 2 * top_km + 4 * log10(area_km2)``.
    """
    from scipy import ndimage

    lo, hi = value_range
    dbz = decode(volume, lo, hi)
    with np.errstate(all="ignore"):
        comp = np.nanmax(np.where(np.isfinite(dbz), dbz, -np.inf), axis=0)
    mask = comp >= threshold_dbz
    labels, n = ndimage.label(mask, structure=np.ones((3, 3), dtype=bool))
    cells: list[Cell] = []
    px_km2 = (cell_m / 1000.0) ** 2
    for i in range(1, n + 1):
        sel = labels == i
        area = float(sel.sum()) * px_km2
        if area < min_area_km2:
            continue
        w = np.clip(comp[sel] - threshold_dbz + 1.0, 1.0, None)
        jj, ii = np.nonzero(sel)
        x = x0_m + float((ii * w).sum() / w.sum()) * cell_m
        y = y0_m + float((jj * w).sum() / w.sum()) * cell_m
        col = np.nanmax(
            np.where(sel[None, :, :], np.where(np.isfinite(dbz), dbz, -np.inf), -np.inf),
            axis=(1, 2),
        )
        levels = np.nonzero(col >= top_dbz)[0]
        top = float(z_km[levels.max()]) if levels.size else 0.0
        mx = float(comp[sel].max())
        cells.append(
            Cell(x / 1000.0, y / 1000.0, mx, area, top, mx + 2.0 * top + 4.0 * math.log10(area))
        )
    return sorted(cells, key=lambda c: c.score, reverse=True)


def track_storm(
    cells_per_step: list[list[Cell]],
    times_s: list[float],
    max_jump_km: float = 35.0,
    n_seeds: int = 8,
) -> list[Cell]:
    """The best cell of the first step followed through the later steps (nearest centre, with the
    motion between the last two steps extrapolated). Returns one cell per step; if no cell can be
    followed for the whole clip, the track that goes furthest (then the best total score) is padded
    with the last cell moved along its motion.
    """
    if not cells_per_step or not cells_per_step[0]:
        raise ValueError("no convective cell found in the first time step")
    best: list[Cell] = []
    best_key: tuple[int, float] = (-1, -1.0)
    for seed in cells_per_step[0][:n_seeds]:
        track = [seed]
        vel = (0.0, 0.0)  # km per second
        for k in range(1, len(cells_per_step)):
            dt = times_s[k] - times_s[k - 1]
            px = track[-1].x_km + vel[0] * dt
            py = track[-1].y_km + vel[1] * dt
            cand = [
                (
                    math.hypot(c.x_km - px, c.y_km - py)
                    + 0.05 * abs(c.area_km2 - track[-1].area_km2) ** 0.5,
                    c,
                )
                for c in cells_per_step[k]
            ]
            cand = [(d, c) for d, c in cand if math.hypot(c.x_km - px, c.y_km - py) <= max_jump_km]
            if not cand:
                break
            nxt = min(cand, key=lambda t: t[0])[1]
            vel = ((nxt.x_km - track[-1].x_km) / dt, (nxt.y_km - track[-1].y_km) / dt)
            track.append(nxt)
        key = (len(track), sum(c.score for c in track))
        if key > best_key:
            best, best_key = track, key
    while len(best) < len(cells_per_step):  # lost the storm: carry it along its last motion
        k = len(best)
        dt = times_s[k] - times_s[k - 1]
        if len(best) >= 2:
            vx = (best[-1].x_km - best[-2].x_km) / (times_s[k - 1] - times_s[k - 2])
            vy = (best[-1].y_km - best[-2].y_km) / (times_s[k - 1] - times_s[k - 2])
        else:
            vx = vy = 0.0
        last = best[-1]
        best.append(
            Cell(
                last.x_km + vx * dt,
                last.y_km + vy * dt,
                last.max_dbz,
                last.area_km2,
                last.top_km,
                last.score,
            )
        )
    return best


def linear_track(track: list[Cell], times_s: list[float]) -> list[Cell]:
    """The track replaced by its least-squares straight line in time (a steady motion).

    For a storm that moves steadily and whose "cell" jumps about (a tropical cyclone whose eyewall
    arcs are found and lost, or radars whose volumes repeat between steps) the fitted line is the
    better motion; for a storm that accelerates or turns, keep the cell track (the default).
    """
    if len(track) < 2:
        return track
    t = np.asarray(times_s, dtype=float)
    fx = np.polyfit(t, [c.x_km for c in track], 1)
    fy = np.polyfit(t, [c.y_km for c in track], 1)
    return [
        Cell(
            float(np.polyval(fx, ti)),
            float(np.polyval(fy, ti)),
            c.max_dbz,
            c.area_km2,
            c.top_km,
            c.score,
        )
        for c, ti in zip(track, t, strict=True)
    ]


def column_max_dbz(volume: np.ndarray, value_range: tuple[float, float]) -> np.ndarray:
    """Column-maximum reflectivity (dBZ, NaN where no echo) of the mosaic's uint8 volume."""
    lo, hi = value_range
    dbz = decode(volume, lo, hi)
    with np.errstate(all="ignore"):
        comp = np.nanmax(np.where(np.isfinite(dbz), dbz, -np.inf), axis=0)
    return np.where(np.isfinite(comp), comp, np.nan)


@dataclass
class Eye:
    x_km: float
    y_km: float
    radius_km: float  # radius of the echo-free hole, from the data (not a wind-based radius)


def find_eye(
    comp_dbz: np.ndarray,
    x0_m: float,
    y0_m: float,
    cell_m: float,
    around_km: tuple[float, float],
    search_km: float = 80.0,
    echo_dbz: float = 20.0,
    min_radius_km: float = 4.0,
) -> Eye | None:
    """The eye of a tropical cyclone as the centre of the largest echo-free hole enclosed by echo.

    The hole is where the column-maximum reflectivity is below ``echo_dbz``; its centre maximises the
    distance to the nearest echo. Only holes within ``search_km`` of ``around_km`` that echo
    surrounds on most sides (at least 60% of the ring between 1.2 and 2.2 radii holds echo) count,
    so open sea beside the storm is not an eye. The radius is that distance (a measure of the hole
    in the radar echo, not of the wind maximum). None if there is no such hole.
    """
    from scipy import ndimage

    echo = np.nan_to_num(comp_dbz, nan=-99.0) >= echo_dbz
    if not echo.any():
        return None
    dist_km = ndimage.distance_transform_edt(~echo) * (cell_m / 1000.0)
    ny, nx = comp_dbz.shape
    jj, ii = np.mgrid[0:ny, 0:nx]
    xs = (x0_m + ii * cell_m) / 1000.0
    ys = (y0_m + jj * cell_m) / 1000.0
    near = np.hypot(xs - around_km[0], ys - around_km[1]) <= search_km
    cand = np.where(near & ~echo, dist_km, 0.0)
    for _ in range(60):  # the biggest hole first; reject open water and try the next best
        k = int(np.argmax(cand))
        r = float(cand.flat[k])
        if r < min_radius_km:
            return None
        cj, ci = divmod(k, nx)
        ring = (np.hypot(xs - xs[cj, ci], ys - ys[cj, ci]) >= 1.2 * r) & (
            np.hypot(xs - xs[cj, ci], ys - ys[cj, ci]) <= 2.2 * r
        )
        if ring.any() and float(echo[ring].mean()) >= 0.6:
            # sub-cell centre: the mean position of the points almost as far from the echo as the best
            near_c = np.hypot(xs - xs[cj, ci], ys - ys[cj, ci]) <= r
            core = (dist_km >= 0.9 * r) & ~echo & near_c
            cx = float(xs[core].mean()) if core.any() else float(xs[cj, ci])
            cy = float(ys[core].mean()) if core.any() else float(ys[cj, ci])
            return Eye(cx, cy, r)
        cand[np.hypot(xs - xs[cj, ci], ys - ys[cj, ci]) <= r] = 0.0
    return None


def storm_motion(
    track_xy_km: list[tuple[float, float]], times_s: list[float]
) -> tuple[float, float]:
    """Mean storm motion (u east, v north) in m/s from the first to the last position."""
    dt = times_s[-1] - times_s[0]
    if dt <= 0:
        return 0.0, 0.0
    return (
        (track_xy_km[-1][0] - track_xy_km[0][0]) * 1000.0 / dt,
        (track_xy_km[-1][1] - track_xy_km[0][1]) * 1000.0 / dt,
    )


def box_around(
    points_km: list[tuple[float, float]], half_km: float
) -> tuple[float, float, float, float]:
    """(xmin, ymin, xmax, ymax) km: the points' bounding box grown by ``half_km`` on every side."""
    xs = [p[0] for p in points_km]
    ys = [p[1] for p in points_km]
    return min(xs) - half_km, min(ys) - half_km, max(xs) + half_km, max(ys) + half_km

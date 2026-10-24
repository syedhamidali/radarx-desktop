"""Rotation tracks: follow rotation over a sequence of volumes and keep the swath of the maximum.

The science of one volume is radarx's: dealiased velocity (``radarx.retrieve.dealias_velocity``),
the local linear least-squares azimuthal shear (``radarx.retrieve.llsd``; Smith and Elmore 2004,
Miller et al. 2013) and compact shear maxima (``radarx.retrieve.rotation_couplets``). This module
holds the part radarx does not have, which is application glue over a file sequence:

* :func:`accumulate_swath`: the running maximum of the azimuthal shear of many volumes on one
  radar-centred grid (the "rotation track" swath, the idea of the NSSL MRMS rotation tracks, Smith
  et al. 2016, BAMS 97, 1617-1630, here from one radar and one tilt);
* :func:`link_tracks`: a plain greedy nearest-neighbour link of the couplets of successive
  volumes into tracks, with a speed limit.

Everything is class RETRIEVED (docs/SCIENCE.md). None of it is a tornado warning, a tornado
detection or a damage estimate. Only numpy is used.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import pairwise

import numpy as np

__all__ = ["Couplet", "Track", "accumulate_swath", "link_tracks", "swath_grid"]


@dataclass
class Couplet:
    """A rotation couplet of one volume (a row of ``radarx.retrieve.rotation_couplets``)."""

    x_m: float  # east of the radar, ground projection of the shear peak
    y_m: float  # north of the radar
    azimuth_deg: float
    range_m: float
    shear: float  # 1/s, peak azimuthal shear (positive = cyclonic)
    delta_v: float  # m/s
    area_km2: float

    @property
    def vrot(self) -> float:
        return 0.5 * self.delta_v


@dataclass
class Track:
    """One couplet followed through a sequence of volumes."""

    times_s: list[float]  # seconds since the epoch, one per observation
    couplets: list[Couplet]
    gaps_s: list[float] = field(default_factory=list)  # time between consecutive observations

    @property
    def peak(self) -> Couplet:
        return max(self.couplets, key=lambda c: abs(c.shear))

    def peak_time_s(self) -> float:
        return self.times_s[self.couplets.index(self.peak)]

    def length_km(self) -> float:
        pts = self.couplets
        return sum(math.hypot(b.x_m - a.x_m, b.y_m - a.y_m) for a, b in pairwise(pts)) / 1000.0

    def motion(self) -> tuple[float | None, float | None]:
        """Mean speed (m/s) and the direction (degrees) the couplet moves toward, or Nones."""
        if len(self.couplets) < 2 or self.times_s[-1] <= self.times_s[0]:
            return None, None
        dx = self.couplets[-1].x_m - self.couplets[0].x_m
        dy = self.couplets[-1].y_m - self.couplets[0].y_m
        dt = self.times_s[-1] - self.times_s[0]
        return math.hypot(dx, dy) / dt, (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def link_tracks(
    frames: list[tuple[float, list[Couplet]]], max_speed_ms: float = 40.0, min_obs: int = 2
) -> list[Track]:
    """Follow couplets from volume to volume by nearest neighbour.

    ``frames`` are ``(time in seconds, couplets)``, in any order. A couplet continues a track when
    it lies within ``max_speed_ms`` times the time step of the track's last position; the nearest
    pair is linked first and a couplet joins at most one track. This is a plain greedy link, not
    the cell-tracking engine (that follows reflectivity cells by area overlap and a motion field,
    which a small, short-lived vortex does not satisfy). Tracks with fewer than ``min_obs``
    observations are dropped. A track that is not continued is closed; a new one starts from a
    couplet that joins nothing.
    """
    live: list[Track] = []
    for t, objs in sorted(frames, key=lambda f: f[0]):
        pairs = []
        for ti, tr in enumerate(live):
            dt = t - tr.times_s[-1]
            if dt <= 0:
                continue
            for oi, ob in enumerate(objs):
                d = math.hypot(ob.x_m - tr.couplets[-1].x_m, ob.y_m - tr.couplets[-1].y_m)
                if d <= max_speed_ms * dt:
                    pairs.append((d, ti, oi))
        used_t: set[int] = set()
        used_o: set[int] = set()
        for _, ti, oi in sorted(pairs):
            if ti in used_t or oi in used_o:
                continue
            used_t.add(ti)
            used_o.add(oi)
            tr = live[ti]
            tr.gaps_s.append(t - tr.times_s[-1])
            tr.times_s.append(t)
            tr.couplets.append(objs[oi])
        for oi, ob in enumerate(objs):
            if oi not in used_o:
                live.append(Track([t], [ob]))
    tracks = [tr for tr in live if len(tr.couplets) >= min_obs]
    tracks.sort(key=lambda tr: -abs(tr.peak.shear))
    return tracks


def swath_grid(max_range_m: float, spacing_m: float = 500.0) -> tuple[np.ndarray, np.ndarray]:
    """Radar-centred axes (east, north), metres, of the grid that holds the swath."""
    n = math.ceil(max_range_m / spacing_m)
    ax = (np.arange(-n, n + 1) * spacing_m).astype(np.float64)
    return ax, ax.copy()


def accumulate_swath(
    swath: np.ndarray,
    shear: np.ndarray,
    azimuth: np.ndarray,
    range_m: np.ndarray,
    elevation_deg: float,
    axes: tuple[np.ndarray, np.ndarray],
    sign: str = "cyclonic",
) -> int:
    """Fold one volume's azimuthal shear into the running maximum ``swath`` (in place).

    ``swath`` (north, east) starts as NaN ("never seen"). Each grid cell takes the shear of the
    nearest ray and gate (the cell must lie within the swept area), so the swath keeps the
    resolution of the radar, not that of the grid. ``sign`` keeps positive shear (``cyclonic``),
    negative shear as its absolute value (``anticyclonic``) or the larger of the two (``both``).
    Returns the number of grid cells that were updated.
    """
    if sign not in ("cyclonic", "anticyclonic", "both"):
        raise ValueError(f"sign must be cyclonic, anticyclonic or both, got {sign!r}")
    x_ax, y_ax = axes
    xx, yy = np.meshgrid(x_ax, y_ax)
    rng = np.asarray(range_m, dtype=np.float64)
    az_in = np.mod(np.asarray(azimuth, dtype=np.float64), 360.0)
    order = np.argsort(az_in, kind="stable")
    az = az_in[order]
    s = np.asarray(shear, dtype=np.float64)[order]
    slant = np.hypot(xx, yy) / math.cos(math.radians(elevation_deg))
    ang = np.mod(np.degrees(np.arctan2(xx, yy)), 360.0)
    idx = np.searchsorted(az, ang)
    lo = np.clip(idx - 1, 0, az.size - 1)
    hi = np.clip(idx, 0, az.size - 1)
    d_lo = np.abs((ang - az[lo] + 180.0) % 360.0 - 180.0)
    d_hi = np.abs((ang - az[hi] + 180.0) % 360.0 - 180.0)
    ray = np.where(d_lo <= d_hi, lo, hi)
    step = float(np.median(np.diff(rng)))
    gate = np.clip(np.rint((slant - rng[0]) / step).astype(np.int64), 0, rng.size - 1)
    inside = (slant >= rng[0]) & (slant <= rng[-1])
    val = s[ray, gate]
    if sign == "anticyclonic":
        val = -val
    elif sign == "both":
        val = np.abs(val)
    val = np.where(inside, val, np.nan)
    with np.errstate(invalid="ignore"):
        take = np.isfinite(val) & (~np.isfinite(swath) | (val > swath))
    swath[take] = val[take]
    return int(take.sum())

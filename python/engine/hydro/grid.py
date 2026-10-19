"""Polar sweeps on a common map grid, and the compositing of several radars.

The grid is the azimuthal-equidistant grid of :mod:`engine.mosaic` (``_layout``): square cells of
``cell_m`` metres, ``x`` east and ``y`` north of the grid origin. Each cell takes the *mean* of all
gates of the sweep that fall inside it (a summed-area table over the polar array makes this O(1)
per cell), not a nearest-gate sample, so a 1 km cell of a 250 m radar is a true areal average and
the accumulation over a catchment does not depend on how the gates happen to fall.

A cell is served by up to ``n_sweeps`` of the lowest beams of each radar. The *best beam* is the
one with the highest quality index (low and unblocked), the hybrid-scan idea of the operational
networks (Fulton et al. 1998; Zhang et al. 2011): the lowest beam wins unless it is blocked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from engine.hydro import qpe as hqpe

#: cells whose window holds less than this fraction of gates with an estimate are left empty
MIN_VALID_FRACTION = 0.5


# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------


@dataclass
class QpeGrid:
    """Common analysis grid (azimuthal equidistant, metres) with lon/lat of every cell centre."""

    bbox: tuple[float, float, float, float]  # west, south, east, north (degrees)
    origin: tuple[float, float]  # lat, lon of the AEQD origin
    x: np.ndarray  # (nx,) cell-centre x, m
    y: np.ndarray  # (ny,) cell-centre y, m (south -> north)
    cell_m: float
    layout: Any = field(default=None, repr=False)  # engine.mosaic._Layout (Web-Mercator image)
    _lonlat: tuple[np.ndarray, np.ndarray] | None = field(default=None, repr=False)

    @property
    def shape(self) -> tuple[int, int]:
        return (self.y.size, self.x.size)

    @property
    def crs_wkt(self) -> str:
        return self.proj4()

    def proj4(self) -> str:
        return (
            f"+proj=aeqd +lat_0={self.origin[0]:.8f} +lon_0={self.origin[1]:.8f} "
            "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
        )

    def lonlat(self) -> tuple[np.ndarray, np.ndarray]:
        """2-D longitude and latitude (degrees) of the cell centres."""
        if self._lonlat is None:
            import pyproj

            tr = pyproj.Transformer.from_crs(self.proj4(), "EPSG:4326", always_xy=True)
            xx, yy = np.meshgrid(self.x, self.y)
            lon, lat = tr.transform(xx, yy)
            self._lonlat = (lon.astype(np.float64), lat.astype(np.float64))
        return self._lonlat

    def cell_area_m2(self) -> float:
        """True area of one cell. AEQD is not equal-area; see :func:`areal_scale`."""
        return self.cell_m * self.cell_m

    def areal_scale(self) -> np.ndarray:
        """Area distortion factor of the grid (AEQD: ``theta / sin(theta)`` tangentially).

        True cell area = ``cell_m**2 / areal_scale``. The factor is 1 on the axes through the
        origin and ~1.0006 at 150 km, 1.0014 at 230 km: negligible for a regional grid but applied
        so that areas are exact to the first order.
        """
        xx, yy = np.meshgrid(self.x, self.y)
        rho = np.hypot(xx, yy)
        th = np.clip(rho / hqpe.EARTH_RADIUS_M, 1e-12, math.pi - 1e-6)
        return (th / np.sin(th)).astype(np.float64)

    def cell_areas_km2(self) -> np.ndarray:
        return (self.cell_m**2 / self.areal_scale()) / 1e6


def make_grid(bbox: tuple[float, float, float, float], resolution_m: float = 1000.0) -> QpeGrid:
    """The mosaic engine's grid for ``bbox`` (west, south, east, north in degrees)."""
    from engine import mosaic

    lay = mosaic._layout(tuple(bbox), float(resolution_m))
    return QpeGrid(
        bbox=lay.bbox,
        origin=lay.origin,
        x=lay.x,
        y=lay.y,
        cell_m=float(resolution_m),
        layout=lay,
    )


def bbox_for_radars(
    sites: list[dict[str, float]], max_range_km: float
) -> tuple[float, float, float, float]:
    """A lon/lat box around the coverage discs of the radars (``mosaic._auto_bbox``)."""
    from types import SimpleNamespace

    from engine import mosaic

    vols = [
        SimpleNamespace(
            latitude=s["latitude"], longitude=s["longitude"], max_range_m=max_range_km * 1000.0
        )
        for s in sites
    ]
    return mosaic._auto_bbox(vols, max_range_km)  # type: ignore[arg-type]


@dataclass
class RadarGeometry:
    """Azimuth and ground distance from one radar to every cell of a grid."""

    latitude: float
    longitude: float
    altitude_m: float
    az_deg: np.ndarray  # (ny, nx) float32, 0..360
    s_m: np.ndarray  # (ny, nx) float32 ground distance

    @classmethod
    def build(
        cls, grid: QpeGrid, latitude: float, longitude: float, altitude_m: float
    ) -> RadarGeometry:
        import pyproj

        tr = pyproj.Transformer.from_crs(
            grid.proj4(),
            f"+proj=aeqd +lat_0={latitude:.8f} +lon_0={longitude:.8f} +datum=WGS84 +units=m",
            always_xy=True,
        )
        xx, yy = np.meshgrid(grid.x, grid.y)
        xr_, yr_ = tr.transform(xx, yy)
        az = np.degrees(np.arctan2(xr_, yr_)) % 360.0
        s = np.hypot(xr_, yr_)
        return cls(latitude, longitude, altitude_m, az.astype(np.float32), s.astype(np.float32))


# ---------------------------------------------------------------------------
# Sampling one sweep onto the grid
# ---------------------------------------------------------------------------


@dataclass
class SweepSample:
    """A sweep averaged onto the grid cells (NaN / False where the sweep does not serve a cell)."""

    rate: np.ndarray  # (ny, nx) float32 mm/h
    sigma: np.ndarray  # rate-weighted fractional sigma
    quality: np.ndarray
    height_agl_m: np.ndarray
    blockage: np.ndarray | None
    valid: np.ndarray  # bool: the cell has an estimate from this sweep
    covered: np.ndarray  # bool: the cell lies in the sweep's range coverage
    frac_ml: np.ndarray
    frac_hail: np.ndarray
    frac_atten: np.ndarray
    frac_block: np.ndarray


def _window_indices(
    sq: hqpe.SweepQpe, geom: RadarGeometry, cell_m: float, max_range_m: float
) -> dict[str, np.ndarray]:
    s = geom.s_m.ravel().astype(np.float64)
    az = geom.az_deg.ravel().astype(np.float64)
    sg = sq.ground_range_m
    dr = float(np.median(np.diff(sg))) if sg.size > 1 else cell_m
    half = max(cell_m / 2.0, dr / 2.0)
    inside = (s >= sg[0] - dr / 2.0) & (s <= min(max_range_m, sg[-1] + dr / 2.0))
    idx = np.flatnonzero(inside)
    s_i = s[idx]
    az_i = az[idx]
    j0 = np.searchsorted(sg, s_i - half, side="left")
    j1 = np.searchsorted(sg, s_i + half, side="right")
    j1 = np.maximum(j1, j0 + 1)
    j1 = np.minimum(j1, sg.size)
    j0 = np.minimum(j0, j1 - 1)
    naz = sq.azimuth.size
    daz = float(np.median(np.diff(np.concatenate([sq.azimuth, [sq.azimuth[0] + 360.0]]))))
    dmax = np.degrees(np.arctan2(half * math.sqrt(2.0) / 1.0, np.maximum(s_i, half)))
    dmax = np.minimum(dmax, 179.0)
    dmax = np.maximum(dmax, daz / 2.0 + 1e-6)
    ext = np.concatenate([sq.azimuth - 360.0, sq.azimuth, sq.azimuth + 360.0])
    i0 = np.searchsorted(ext, az_i - dmax, side="left")
    i1 = np.searchsorted(ext, az_i + dmax, side="right")
    i1 = np.minimum(np.maximum(i1, i0 + 1), i0 + naz)
    return {"idx": idx, "i0": i0, "i1": i1, "j0": j0, "j1": j1, "naz": np.array([naz])}


def _sat(channel: np.ndarray) -> np.ndarray:
    """Summed-area table with a zero first row and column: shape ``(naz + 1, nr + 1)``."""
    out = np.zeros((channel.shape[0] + 1, channel.shape[1] + 1), dtype=np.float64)
    np.cumsum(np.cumsum(channel, axis=0, dtype=np.float64), axis=1, out=out[1:, 1:])
    return out


def _window_sum(sat: np.ndarray, naz: int, i0, i1, j0, j1) -> np.ndarray:
    """Sum over rows ``[i0, i1)`` (indices into the 3x-wrapped azimuth axis) and columns."""
    total = sat[naz]  # (nr+1,) cumulative over all rays

    def row(e: np.ndarray, j: np.ndarray) -> np.ndarray:
        return (e // naz) * total[j] + sat[e % naz, j]

    return row(i1, j1) - row(i0, j1) - row(i1, j0) + row(i0, j0)


def sample_sweep(
    sq: hqpe.SweepQpe,
    geom: RadarGeometry,
    shape: tuple[int, int],
    cell_m: float,
    max_range_m: float = 230_000.0,
    min_valid: float = MIN_VALID_FRACTION,
) -> SweepSample:
    """Average the sweep's gates over every grid cell (see the module docstring)."""
    win = _window_indices(sq, geom, cell_m, max_range_m)
    idx = win["idx"]
    naz = int(win["naz"][0])
    n_total = ((win["i1"] - win["i0"]) * (win["j1"] - win["j0"])).astype(np.float64)

    rate = sq.rate.astype(np.float64)
    valid_g = np.isfinite(rate)
    rate0 = np.where(valid_g, rate, 0.0)
    sig0 = np.where(valid_g & np.isfinite(sq.sigma), sq.sigma, 0.0) * rate0
    has_dem = sq.blockage is not None
    chans: dict[str, np.ndarray] = {
        "valid": valid_g.astype(np.float64),
        "rate": rate0,
        "sig": sig0,
        "ml": ((sq.flags & hqpe.FLAG_ML) > 0).astype(np.float64),
        "hail": ((sq.flags & hqpe.FLAG_HAIL) > 0).astype(np.float64),
        "att": ((sq.flags & hqpe.FLAG_ATTEN) > 0).astype(np.float64),
    }
    if has_dem:  # quality, height and blockage vary with azimuth only through the terrain
        chans["q"] = np.where(valid_g, sq.quality, 0.0)
        chans["h"] = np.where(valid_g, sq.beam_height_agl_m, 0.0)
        chans["cbb"] = np.where(valid_g, sq.blockage, 0.0)
        chans["blk"] = ((sq.flags & hqpe.FLAG_BLOCK) > 0).astype(np.float64)
    sums: dict[str, np.ndarray] = {}
    for name, ch in chans.items():
        sums[name] = _window_sum(_sat(ch), naz, win["i0"], win["i1"], win["j0"], win["j1"])

    def full(vals: np.ndarray, fill: float = np.nan, dtype=np.float32) -> np.ndarray:
        out = np.full(shape[0] * shape[1], fill, dtype=dtype)
        out[idx] = vals
        return out.reshape(shape)

    n_valid = sums["valid"]
    ok = n_valid >= np.maximum(min_valid * n_total, 1.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean_rate = np.where(ok, sums["rate"] / np.maximum(n_valid, 1.0), np.nan)
        sigma = np.where(
            ok & (sums["rate"] > 0), sums["sig"] / np.maximum(sums["rate"], 1e-12), np.nan
        )
        if has_dem:
            qual = np.where(ok, sums["q"] / np.maximum(n_valid, 1.0), np.nan)
            hag = np.where(ok, sums["h"] / np.maximum(n_valid, 1.0), np.nan)
            cbb = np.where(ok, sums["cbb"] / np.maximum(n_valid, 1.0), np.nan)
        else:  # without terrain these depend on range only: read them off the beam profile
            s_cell = geom.s_m.ravel()[idx].astype(np.float64)
            qual = np.where(ok, np.interp(s_cell, sq.ground_range_m, sq.quality[0]), np.nan)
            hag = np.where(
                ok, np.interp(s_cell, sq.ground_range_m, sq.beam_height_agl_m[0]), np.nan
            )
            cbb = None
        keys = ("ml", "hail", "att", "blk") if has_dem else ("ml", "hail", "att")
        fr = {k: sums[k] / np.maximum(n_total, 1.0) for k in keys}
        fr.setdefault("blk", np.zeros_like(n_total))
    covered = full(np.ones(idx.size, dtype=bool), False, dtype=bool)
    return SweepSample(
        rate=full(mean_rate),
        sigma=full(sigma),
        quality=full(qual),
        height_agl_m=full(hag),
        blockage=full(cbb) if cbb is not None else None,
        valid=full(ok, False, dtype=bool),
        covered=covered,
        frac_ml=full(fr["ml"], 0.0),
        frac_hail=full(fr["hail"], 0.0),
        frac_atten=full(fr["att"], 0.0),
        frac_block=full(fr["blk"], 0.0),
    )


# ---------------------------------------------------------------------------
# One radar, one volume time, on the grid
# ---------------------------------------------------------------------------

#: bits of ``RadarField.flags`` (uint8, per cell): a flag is set when >= 25 % of the gates of the
#: serving beam in the cell carry it
CELL_FLAG_FRACTION = 0.25


@dataclass
class RadarField:
    """The best-beam rain-rate estimate of one radar volume on the common grid."""

    name: str
    latitude: float
    longitude: float
    altitude_m: float
    time: np.datetime64
    rate: np.ndarray  # (ny, nx) float32 mm/h, NaN = no estimate / not covered
    sigma: np.ndarray
    quality: np.ndarray
    height_agl_m: np.ndarray
    range_km: np.ndarray
    blockage: np.ndarray | None
    flags: np.ndarray  # uint8, qpe.FLAG_* bits
    beam: np.ndarray  # int8: index of the serving sweep (-1 none)
    sweeps: list[dict[str, Any]]
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def covered(self) -> np.ndarray:
        return np.isfinite(self.rate)


def merge_sweeps(
    name: str,
    site: dict[str, float],
    time: np.datetime64,
    sweeps: list[hqpe.SweepQpe],
    geom: RadarGeometry,
    shape: tuple[int, int],
    cell_m: float,
    max_range_m: float,
    block_max: float = 0.5,
) -> RadarField:
    """Best-beam combination of the (lowest) sweeps of one radar volume."""
    rate = np.full(shape, np.nan, dtype=np.float32)
    sigma = np.full(shape, np.nan, dtype=np.float32)
    qual = np.full(shape, -1.0, dtype=np.float32)
    hag = np.full(shape, np.nan, dtype=np.float32)
    blk = None
    flags = np.zeros(shape, dtype=np.uint8)
    beam = np.full(shape, -1, dtype=np.int8)
    info = []
    for k, sq in enumerate(sweeps):
        sm = sample_sweep(sq, geom, shape, cell_m, max_range_m)
        eligible = sm.valid.copy()
        if sm.blockage is not None:
            eligible &= np.nan_to_num(sm.blockage, nan=0.0) < block_max
        better = eligible & (np.nan_to_num(sm.quality, nan=-1.0) > qual)
        rate = np.where(better, sm.rate, rate)
        sigma = np.where(better, sm.sigma, sigma)
        qual = np.where(better, sm.quality, qual)
        hag = np.where(better, sm.height_agl_m, hag)
        beam = np.where(better, np.int8(k), beam)
        if sm.blockage is not None:
            if blk is None:
                blk = np.full(shape, np.nan, dtype=np.float32)
            blk = np.where(better, sm.blockage, blk)
        f = (
            (sm.frac_ml >= CELL_FLAG_FRACTION).astype(np.uint8) * hqpe.FLAG_ML
            | (sm.frac_hail >= CELL_FLAG_FRACTION).astype(np.uint8) * hqpe.FLAG_HAIL
            | (sm.frac_atten >= CELL_FLAG_FRACTION).astype(np.uint8) * hqpe.FLAG_ATTEN
            | (sm.frac_block >= CELL_FLAG_FRACTION).astype(np.uint8) * hqpe.FLAG_BLOCK
        )
        flags = np.where(better, f, flags).astype(np.uint8)
        info.append(
            {
                "elevation_deg": round(float(sq.elevation_deg), 3),
                "method": sq.meta.get("method"),
                "cells": int(better.sum()),
            }
        )
    qual = np.where(beam >= 0, qual, np.nan).astype(np.float32)
    rng = np.where(beam >= 0, geom.s_m / 1000.0, np.nan).astype(np.float32)
    return RadarField(
        name=name,
        latitude=site["latitude"],
        longitude=site["longitude"],
        altitude_m=site.get("altitude", 0.0),
        time=time,
        rate=rate,
        sigma=sigma,
        quality=qual,
        height_agl_m=hag,
        range_km=rng,
        blockage=blk,
        flags=flags,
        beam=beam,
        sweeps=info,
    )


# ---------------------------------------------------------------------------
# Compositing several radars
# ---------------------------------------------------------------------------


@dataclass
class Composite:
    rate: np.ndarray
    sigma: np.ndarray
    quality: np.ndarray
    height_agl_m: np.ndarray
    range_km: np.ndarray
    flags: np.ndarray
    n_radars: np.ndarray  # uint8: radars with an estimate in the cell
    source: np.ndarray  # int8: index of the radar with the highest quality (-1 none)
    blockage: np.ndarray | None = None


def composite(
    fields: list[RadarField],
    rates: list[np.ndarray] | None = None,
    mode: str = "weighted",
    power: float = 2.0,
) -> Composite:
    """Combine radars on the common grid.

    ``mode="best"``: each cell takes the radar with the highest quality index (best beam).
    ``mode="weighted"``: quality-weighted mean ``R = sum(w R) / sum(w)`` with ``w = Q^power``
    (default 2); the weights favour the radar with the lower, unblocked beam while the others still
    reduce the noise. The composite 1-sigma is the weighted mean of the sigmas (errors of the
    radars are *not* assumed independent, so it is not reduced by averaging).

    ``rates`` replaces each field's rate (the time-interpolated rates of an accumulation).
    """
    if mode not in ("best", "weighted"):
        raise hqpe.QpeError("composite mode must be 'best' or 'weighted'")
    shape = fields[0].rate.shape
    rts = rates if rates is not None else [f.rate for f in fields]
    stack_r = np.stack([np.asarray(r, dtype=np.float32) for r in rts])
    stack_q = np.stack([f.quality for f in fields])
    stack_s = np.stack([f.sigma for f in fields])
    fin = np.isfinite(stack_r) & np.isfinite(stack_q)
    qz = np.where(fin, np.maximum(stack_q, 1e-3), 0.0)
    n = fin.sum(axis=0).astype(np.uint8)
    any_ = n > 0
    best = np.where(any_, np.argmax(qz, axis=0), -1).astype(np.int8)

    def pick(arrs: list[np.ndarray | None], fill=np.nan) -> np.ndarray:
        st = np.stack([a if a is not None else np.full(shape, np.nan, np.float32) for a in arrs])
        return np.take_along_axis(st, np.maximum(best, 0)[None], axis=0)[0]

    if mode == "best":
        rate = np.where(any_, np.take_along_axis(stack_r, np.maximum(best, 0)[None], 0)[0], np.nan)
        sigma = np.where(any_, np.take_along_axis(stack_s, np.maximum(best, 0)[None], 0)[0], np.nan)
    else:
        w = qz**power
        wsum = w.sum(axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            rate = np.where(any_, np.nansum(np.where(fin, stack_r, 0.0) * w, axis=0) / wsum, np.nan)
            sig_w = np.where(fin & np.isfinite(stack_s), stack_s, 0.0) * w
            wsig = np.where(fin & np.isfinite(stack_s), w, 0.0).sum(axis=0)
            sigma = np.where(wsig > 0, sig_w.sum(axis=0) / np.maximum(wsig, 1e-12), np.nan)
    qual = np.where(any_, qz.max(axis=0), np.nan)
    flags = np.where(any_, pick([f.flags.astype(np.float32) for f in fields], 0.0), 0).astype(
        np.uint8
    )
    blk = None
    if any(f.blockage is not None for f in fields):
        blk = pick([f.blockage for f in fields]).astype(np.float32)
    return Composite(
        rate=rate.astype(np.float32),
        sigma=sigma.astype(np.float32),
        quality=qual.astype(np.float32),
        height_agl_m=np.where(any_, pick([f.height_agl_m for f in fields]), np.nan).astype(
            np.float32
        ),
        range_km=np.where(any_, pick([f.range_km for f in fields]), np.nan).astype(np.float32),
        flags=flags,
        n_radars=n,
        source=best,
        blockage=blk,
    )

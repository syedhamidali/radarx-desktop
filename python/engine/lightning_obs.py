"""Observed lightning for storm movies: readers of local lightning files (the engine's copy; the offline
clip tool in ``tools/storm_clip`` uses it too), the flash table of ``rxd animate --flashes`` and the thinning
of GLM flashes to the strongest one per time bin near the tracked storm.

Two kinds of files are understood, both read from DISK ONLY (nothing is downloaded here: the GLM
files must be obtained by the user, for example from NOAA's public GOES bucket, and given to
``clipdata.py --lightning`` or ``rxd animate --flashes-from-glm``):

* GLM, the Geostationary Lightning Mapper on GOES-16/17/18/19: Level 2 LCFA netCDF files
  (``OR_GLM-L2-LCFA_G16_s<time>...nc``). Flash position and time come from ``flash_lat``,
  ``flash_lon`` and ``flash_time_offset_of_first_event`` (seconds from the product time of the
  file, in the ``product_time`` variable or the ``time_coverage_start`` attribute); the radiant
  energy ``flash_energy`` scales the relative strength.
* LMA, a ground Lightning Mapping Array: ASCII ``LYLOUT_*.dat`` source files, read by
  ``radarx.io.read_lma`` and grouped into flashes by ``radarx.retrieve.cluster_flashes``. Flash start
  time and initiation point (latitude, longitude, altitude) are used; the number of sources scales
  the relative strength.

Only the time and place of a flash are data here; the clip draws a synthetic branching channel at
that place (stated on screen). A flash outside the clip's time window or box is dropped.
"""

# ruff: noqa: E501
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

UTC = UTC


@dataclass
class ObservedFlash:
    time: datetime  # UTC
    lat: float
    lon: float
    alt_km: float | None  # initiation height ASL if the instrument gives it (LMA), else None
    energy: float  # relative strength 0..1 (within the file set)
    energy_raw: float | None = (
        None  # the instrument's flash energy (GLM, J), for choosing the strongest
    )
    quality: int | None = None  # GLM flash_quality_flag (0 = good)
    kind: str | None = None  # "CG" / "IC" when a table gives it


def _to_utc(value: Any) -> datetime:
    """A numpy datetime64, ISO string or datetime as an aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, np.datetime64):
        return datetime.fromtimestamp(
            float(value.astype("datetime64[ns]").astype("int64")) / 1e9, UTC
        )
    text = str(value).replace("Z", "+00:00")
    d = datetime.fromisoformat(text)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _relative(values: np.ndarray) -> np.ndarray:
    """Values scaled linearly to 0.2..1 over their range (a strength for the clip, not a physical unit)."""
    v = np.asarray(values, dtype=float)
    if v.size == 0:
        return v
    lo, hi = float(np.nanmin(v)), float(np.nanmax(v))
    if not math.isfinite(lo) or hi <= lo:
        return np.full_like(v, 0.6)
    return 0.2 + 0.8 * (v - lo) / (hi - lo)


def flashes_from_glm_dataset(ds) -> list[ObservedFlash]:
    """Flashes of one opened GLM L2 LCFA dataset (an ``xarray.Dataset``)."""
    lat = np.asarray(ds["flash_lat"].values, dtype=float)
    lon = np.asarray(ds["flash_lon"].values, dtype=float)
    off = np.asarray(ds["flash_time_offset_of_first_event"].values)
    if "product_time" in ds:
        base = _to_utc(np.asarray(ds["product_time"].values).ravel()[0])
    elif "time_coverage_start" in ds.attrs:
        base = _to_utc(ds.attrs["time_coverage_start"])
    else:
        raise ValueError("GLM file without product_time / time_coverage_start")
    if np.issubdtype(off.dtype, np.datetime64):
        # xarray decodes "seconds since <time>" offsets to absolute times: back to seconds from the product time
        base64 = np.datetime64(base.astimezone(UTC).replace(tzinfo=None), "ns")
        off_s = (off.astype("datetime64[ns]") - base64) / np.timedelta64(1, "s")
    elif np.issubdtype(off.dtype, np.timedelta64):
        off_s = off.astype("timedelta64[ns]").astype("int64") / 1e9
    else:
        off_s = off.astype(float)
    energy = (
        np.asarray(ds["flash_energy"].values, dtype=float)
        if "flash_energy" in ds
        else np.ones_like(lat)
    )
    rel = _relative(energy)
    qual = (
        np.asarray(ds["flash_quality_flag"].values, dtype=float)
        if "flash_quality_flag" in ds
        else np.zeros_like(lat)
    )
    ok = np.isfinite(lat) & np.isfinite(lon) & np.isfinite(off_s)
    return [
        ObservedFlash(
            base + timedelta(seconds=float(off_s[i])),
            float(lat[i]),
            float(lon[i]),
            None,
            float(rel[i]),
            energy_raw=float(energy[i]) if np.isfinite(energy[i]) else None,
            quality=int(qual[i]) if np.isfinite(qual[i]) else None,
        )
        for i in np.nonzero(ok)[0]
    ]


def flashes_from_lma_dataset(ds) -> list[ObservedFlash]:
    """Flashes of an LMA dataset that ``radarx.retrieve.cluster_flashes`` has grouped (the flash table)."""
    t = ds["flash_time_start"].values
    lat = np.asarray(ds["flash_init_latitude"].values, dtype=float)
    lon = np.asarray(ds["flash_init_longitude"].values, dtype=float)
    alt = np.asarray(ds["flash_init_altitude"].values, dtype=float)  # metres ASL
    cnt = (
        np.asarray(ds["flash_event_count"].values, dtype=float)
        if "flash_event_count" in ds
        else np.ones_like(lat)
    )
    rel = _relative(np.log1p(cnt))
    ok = np.isfinite(lat) & np.isfinite(lon)
    return [
        ObservedFlash(
            _to_utc(t[i]),
            float(lat[i]),
            float(lon[i]),
            float(alt[i]) / 1000.0 if np.isfinite(alt[i]) else None,
            float(rel[i]),
        )
        for i in np.nonzero(ok)[0]
    ]


def read_flashes(paths: list[str | Path]) -> tuple[str, list[ObservedFlash]]:
    """Read lightning files (all of one kind) from disk: ('glm' | 'lma', flashes sorted by time)."""
    files = [Path(p).expanduser() for p in paths]
    if not files:
        raise ValueError("no lightning files given")
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        raise FileNotFoundError(
            "lightning file(s) not found: "
            + ", ".join(missing)
            + " (nothing is downloaded by this tool)"
        )
    nc = [f for f in files if f.suffix.lower() in (".nc", ".nc4")]
    if nc and len(nc) != len(files):
        raise ValueError("give either GLM netCDF files or LMA .dat files, not both")
    out: list[ObservedFlash] = []
    if nc:
        import xarray as xr

        for f in nc:
            with xr.open_dataset(f) as ds:
                out.extend(flashes_from_glm_dataset(ds))
        kind = "glm"
    else:
        from radarx.io import read_lma
        from radarx.retrieve import cluster_flashes

        ds = read_lma(files, max_chi2=1.0, min_stations=6)
        out = flashes_from_lma_dataset(cluster_flashes(ds))
        kind = "lma"
    out.sort(key=lambda f: f.time)
    return kind, out


def to_bundle(
    flashes: list[ObservedFlash],
    t0: datetime,
    t_end: datetime,
    to_xy_km,
    box_km: tuple[float, float, float, float] | None = None,
) -> list[dict[str, float]]:
    """Flashes inside [t0, t_end] (and the box) as bundle records: t_s since ``t0``, x / y km, z km, energy.

    ``to_xy_km(lon, lat)`` converts to the bundle frame (east / north km of its origin).
    """
    out = []
    for f in flashes:
        if not (t0 <= f.time <= t_end):
            continue
        x, y = to_xy_km(f.lon, f.lat)
        if box_km and not (box_km[0] <= x <= box_km[2] and box_km[1] <= y <= box_km[3]):
            continue
        rec = {
            "t_s": (f.time - t0).total_seconds(),
            "x": float(x),
            "y": float(y),
            "energy": float(f.energy),
        }
        if f.alt_km is not None:
            rec["z"] = float(f.alt_km)
        out.append(rec)
    return out

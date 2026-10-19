"""Lightning (OBSERVED, NALMA + NSSL deployable LMA) as flashes: ``lma/flashes.json``.

Sources are read from the analysed ``LYLOUT_*.dat.gz`` files (10 min each) with ``radarx.io.read_lma``,
quality-selected, restricted to the bundle domain and the window, grouped into flashes with
``radarx.retrieve.cluster_flashes`` (the single-linkage algorithm of Fuchs et al. 2016, J. Geophys. Res.,
121, 8625-8648, as implemented in radarx; the Alabama-network values of that paper for the distance
scale are used) and written as JSON (SPEC v1). The sources keep the instrument's own numbers: time,
latitude, longitude, altitude (m ASL), power (dBW).

Honest limits: LMA detects VHF radiation of breakdown (channels, mostly in-cloud), not the ground strike
point, and the detection efficiency falls off with range from the stations (best inside the network);
this file is the union of the 14 NALMA stations in north Alabama and the NSSL deployable stations
(8 sites) in Mississippi (the header of each file lists them). The flash grouping is a clustering with
parameters, not a measurement: the parameters are in the JSON.
"""

from __future__ import annotations

import gzip
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

UTC = timezone.utc

DEFAULT_PARAMS = {
    "max_chi2": 1.0,  # Fuchs et al. 2016 discard chi2 > 1
    "min_stations": 6,  # Fuchs et al. 2016 discard sources with fewer than 6 stations
    "altitude_m": (0.0, 20000.0),
    "cluster_distance_m": 6000.0,  # the 6 km Fuchs et al. use for the Alabama and D.C. networks
    "cluster_time_s": 0.15,
    "cluster_min_points": 2,
    "cluster_max_duration_s": 3.0,
}


def file_start(path: str | Path) -> datetime:
    m = re.search(r"LYLOUT_(\d{6})_(\d{6})_(\d+)", Path(path).name)
    d = datetime.strptime(m.group(1) + m.group(2), "%y%m%d%H%M%S").replace(tzinfo=UTC)
    return d


def files_for_window(folder: str | Path, start: datetime, end: datetime) -> list[Path]:
    """LYLOUT files (10 min, ``*_0600.dat.gz``) that overlap [start, end]."""
    out = []
    for f in sorted(Path(folder).glob("LYLOUT_*_0600.dat.gz")):
        t0 = file_start(f)
        if t0 <= end and t0 + timedelta(seconds=600) >= start:
            out.append(f)
    return out


def header_info(path: str | Path) -> dict[str, Any]:
    """Network facts from the file header: stations, active stations, analysis program."""
    info: dict[str, Any] = {"stations": []}
    with gzip.open(path, "rt", errors="replace") as fh:
        for line in fh:
            if line.startswith("*** data ***"):
                break
            if line.startswith("Number of stations:"):
                info["n_stations"] = int(line.split(":")[1])
            elif line.startswith("Number of active stations:"):
                info["n_active"] = int(line.split(":")[1])
            elif line.startswith("Active stations:"):
                info["active"] = line.split(":")[1].split()
            elif line.startswith("Analysis program version"):
                info["analysis_version"] = line.split(":")[1].strip()
            elif line.startswith("Location:"):
                info["location"] = line.split(":")[1].strip()
            elif line.startswith("Sta_info:"):
                p = line.split()
                info["stations"].append(
                    {
                        "id": p[1],
                        "name": p[2],
                        "lat": float(p[3]),
                        "lon": float(p[4]),
                        "alt_m": float(p[5]),
                    }
                )
    return info


def read_sources(files: list[str | Path], params: dict[str, Any] | None = None):
    """The quality-selected sources of ``files`` as an xarray Dataset (radarx ``read_lma``)."""
    import radarx.io as rio

    p = {**DEFAULT_PARAMS, **(params or {})}
    return rio.read_lma(
        [str(f) for f in files],
        max_chi2=p["max_chi2"],
        min_stations=p["min_stations"],
        altitude=tuple(p["altitude_m"]),
    )


def cluster(ds, params: dict[str, Any] | None = None):
    import radarx.retrieve as rxr

    p = {**DEFAULT_PARAMS, **(params or {})}
    return rxr.cluster_flashes(
        ds,
        distance=p["cluster_distance_m"],
        time=p["cluster_time_s"],
        min_points=p["cluster_min_points"],
        max_duration=p["cluster_max_duration_s"],
        noise="drop",
    )


def _t_seconds(times) -> np.ndarray:
    return np.asarray(times).astype("datetime64[ns]").astype("int64") / 1e9


def to_flashes_json(
    ds,
    window_start: datetime,
    window_end: datetime,
    box: tuple[float, float, float, float] | None,
    params: dict[str, Any] | None = None,
    station_count: dict[str, Any] | None = None,
    min_flash_sources: int = 2,
) -> dict[str, Any]:
    """The SPEC v1 flashes dictionary from a clustered Dataset (``cluster``) inside the window and box.

    ``box`` = (south, west, north, east) in degrees. Sources outside the window are dropped; a flash is
    kept with the sources inside the window (so ``t_start_s`` may be negative only if the flash began before
    the window and is clipped to its first source inside).
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    t = _t_seconds(ds["event_time"].values)
    lat = np.asarray(ds["event_latitude"].values, float)
    lon = np.asarray(ds["event_longitude"].values, float)
    alt = (
        np.asarray(ds["event_altitude"].values, float)
        if "event_altitude" in ds
        else np.asarray(ds["event_height"].values, float)
    )
    pw = (
        np.asarray(ds["event_power"].values, float)
        if "event_power" in ds
        else np.full(t.shape, np.nan)
    )
    fid = np.asarray(ds["event_parent_flash_id"].values)
    st = window_start.timestamp()
    en = window_end.timestamp()
    sel = (t >= st) & (t <= en) & (fid >= 0)
    if box is not None:
        s, w, n, e = box
        sel &= (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e)
    idx = np.nonzero(sel)[0]
    order = idx[np.argsort(t[idx], kind="stable")]
    by_flash: dict[int, list[int]] = {}
    for i in order:
        by_flash.setdefault(int(fid[i]), []).append(int(i))
    flashes = []
    for _k, (f, ids) in enumerate(sorted(by_flash.items(), key=lambda kv: t[kv[1][0]])):
        if len(ids) < min_flash_sources:
            continue
        a = np.array(ids)
        flashes.append(
            {
                "id": int(f),
                "t_start_s": round(float(t[a[0]] - st), 3),
                "t_end_s": round(float(t[a[-1]] - st), 3),
                "centroid": [
                    round(float(lat[a].mean()), 4),
                    round(float(lon[a].mean()), 4),
                    round(float(alt[a].mean()), 0),
                ],
                "n_sources": int(a.size),
                "sources": [
                    [
                        round(float(t[i] - st), 3),
                        round(float(lat[i]), 4),
                        round(float(lon[i]), 4),
                        round(float(alt[i]), 0),
                        round(float(pw[i]), 1),
                    ]
                    for i in a
                ],
            }
        )
    n_src = int(sum(f["n_sources"] for f in flashes))
    return {
        "t0": window_start.isoformat().replace("+00:00", "Z"),
        "n_sources": n_src,
        "flashes": flashes,
        "params": {k: (list(v) if isinstance(v, tuple) else v) for k, v in p.items()},
        "station_count": station_count or {},
        "n_flashes": len(flashes),
        "min_flash_sources": min_flash_sources,
        "class": "observed",
        "instrument": "Lightning Mapping Array (NALMA + NSSL deployable LMA), VHF sources",
        "clustering": "radarx.retrieve.cluster_flashes (single linkage, Fuchs et al. 2016)",
    }


def per_minute_counts(ds, window_start: datetime, window_end: datetime) -> list[int]:
    t = _t_seconds(ds["event_time"].values)
    st = window_start.timestamp()
    n = math.ceil((window_end - window_start).total_seconds() / 60.0)
    h, _ = np.histogram(t - st, bins=np.arange(0, 60 * n + 1, 60))
    return [int(x) for x in h]

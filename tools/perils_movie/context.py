"""Context layers: MRMS (reflectivity, azimuthal shear) per step, an objective surface analysis from the stations,
and the environment time series at the storm from the radiosondes.

* **MRMS**: the files in ``GRID/MRMS`` are GRIB2. No GRIB reader is installed in the Python environment (no
  cfgrib / pygrib / eccodes), so the external command line program ``wgrib2`` is used if it is on the PATH
  (it was found at /opt/local/bin/wgrib2); without it the MRMS layers are skipped and said so.
  ``MergedBaseReflectivityQC`` (500 m above mean sea level) and ``MergedAzShear_0-2kmAGL`` (the MRMS rotation
  track product, s-1 as written) are cropped to the bundle domain and stored on their own lat / lon grid
  (0.01 degree).  No MESH file is in the folder.
* **Surface analysis** (class: analysis): a single-pass Barnes objective analysis of the station observations
  at the step time (mean of the observations within +-30 s of the step), Gaussian weights with
  ``sigma`` = 15 km (e-folding radius 2 sigma), only where at least ``min_stations`` stations are within 30 km. It is
  an interpolation of observations, labelled as such; it does not invent structure between stations but it smooths it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

UTC = timezone.utc


def wgrib2_path() -> str | None:
    return shutil.which("wgrib2") or (
        "/opt/local/bin/wgrib2" if Path("/opt/local/bin/wgrib2").exists() else None
    )


def mrms_files(folder: str | Path, glob: str) -> list[tuple[datetime, Path]]:
    out = []
    for f in sorted(Path(folder).glob(glob)):
        if f.name.endswith(".idx"):
            continue
        m = re.search(r"(\d{8})-(\d{6})\.grib2$", f.name)
        if m:
            out.append(
                (datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=UTC), f)
            )
    return out


def nearest(
    files: list[tuple[datetime, Path]], t: datetime, tol_s: float
) -> tuple[datetime, Path] | None:
    if not files:
        return None
    best = min(files, key=lambda ft: abs((ft[0] - t).total_seconds()))
    return best if abs((best[0] - t).total_seconds()) <= tol_s else None


def read_mrms_crop(
    grib: str | Path, lon_range: tuple[float, float], lat_range: tuple[float, float], tmp: Path
) -> dict[str, np.ndarray]:
    """Crop a MRMS GRIB2 file to the box with wgrib2 and return lat, lon and the field (NaN where undefined).

    ``lon_range`` in degrees east (negative west); the file uses 0..360.
    """
    exe = wgrib2_path()
    if exe is None:
        raise RuntimeError("wgrib2 is not installed: MRMS (GRIB2) cannot be read")
    import netCDF4

    tmp.mkdir(parents=True, exist_ok=True)
    small = tmp / "crop.grb"
    nc = tmp / "crop.nc"
    for p in (small, nc):
        p.unlink(missing_ok=True)
    lo0, lo1 = (lon_range[0] % 360.0, lon_range[1] % 360.0)
    subprocess.run(
        [
            exe,
            str(grib),
            "-small_grib",
            f"{lo0}:{lo1}",
            f"{lat_range[0]}:{lat_range[1]}",
            str(small),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run([exe, str(small), "-netcdf", str(nc)], check=True, capture_output=True)
    with netCDF4.Dataset(nc) as ds:
        names = [v for v in ds.variables if ds[v].ndim == 2]
        field = np.ma.filled(np.ma.asarray(ds[names[0]][:]).astype(np.float32), np.nan)
        lat = np.asarray(ds["latitude"][:], float)
        lon = np.asarray(ds["longitude"][:], float)
    lon = np.where(lon > 180, lon - 360.0, lon)
    field = np.where(field <= -90.0, np.nan, field)
    return {"lat": lat.astype(np.float32), "lon": lon.astype(np.float32), "field": field}


def barnes(
    x_km: np.ndarray,
    y_km: np.ndarray,
    values: np.ndarray,
    gx_km: np.ndarray,
    gy_km: np.ndarray,
    sigma_km: float = 15.0,
    min_stations: int = 2,
    radius_km: float = 30.0,
) -> np.ndarray:
    """Single-pass Barnes analysis of scattered ``values`` to the grid (``gx_km`` (nx), ``gy_km`` (ny))."""
    ok = np.isfinite(values) & np.isfinite(x_km) & np.isfinite(y_km)
    x, y, v = x_km[ok], y_km[ok], values[ok]
    out = np.full((gy_km.size, gx_km.size), np.nan, np.float32)
    if v.size < min_stations:
        return out
    GX, GY = np.meshgrid(gx_km, gy_km)
    num = np.zeros(GX.shape)
    den = np.zeros(GX.shape)
    cnt = np.zeros(GX.shape, int)
    for xi, yi, vi in zip(x, y, v, strict=True):
        d2 = (GX - xi) ** 2 + (GY - yi) ** 2
        w = np.exp(-d2 / (2.0 * sigma_km**2))
        num += w * vi
        den += w
        cnt += d2 <= radius_km**2
    with np.errstate(invalid="ignore", divide="ignore"):
        res = np.where((cnt >= min_stations) & (den > 1e-6), num / den, np.nan)
    return res.astype(np.float32)


def station_snapshot(
    stations: list[dict[str, Any]], t_s: float, half_s: float = 30.0
) -> dict[str, list[tuple[float, float, float]]]:
    """Mean of each station's variables within +-half_s of ``t_s``: {var: [(lat, lon, value), ...]}."""
    out: dict[str, list[tuple[float, float, float]]] = {}
    for st in stations:
        if st.get("lat") is None or st.get("moving"):
            continue
        s = st["series"]
        t = np.array(s["t_s"], float)
        m = np.abs(t - t_s) <= half_s
        if not m.any():
            continue
        for k, vals in s.items():
            if k == "t_s":
                continue
            a = np.array([np.nan if v is None else v for v in vals], float)[m]
            if np.isfinite(a).any():
                out.setdefault(k, []).append((st["lat"], st["lon"], float(np.nanmean(a))))
    return out


def read_tobac_track(
    path: str | Path, start: datetime, end: datetime, margin_s: float = 1200.0
) -> dict[str, Any] | None:
    """Rows of the tobac feature tracks (``Track.nc``) near the window.

    The file (made by the data owner from KGWX 500 m grids on this same grid, threshold 15 dBZ) holds the tracked
    cell centroids ``projection_x/y_coordinate`` (m east / north of the grid origin), ``areas`` (grid cells; at 500 m
    one cell is 0.25 km2: an assumption, the file does not state the unit) and the maximum reflectivity per
    time. Features without a track (``cell`` = -1) are dropped. OBSERVED radar echo, tracked by an algorithm.
    """
    import netCDF4

    with netCDF4.Dataset(path) as ds:
        units = ds["time"].units
        base = datetime.strptime(units.split("since")[1].strip()[:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=UTC
        )
        t = base.timestamp() + np.asarray(ds["time"][:], float)
        cell = np.asarray(ds["cell"][:], int)
        x = np.asarray(ds["projection_x_coordinate"][:], float) / 1000.0
        y = np.asarray(ds["projection_y_coordinate"][:], float) / 1000.0
        area = np.asarray(ds["areas"][:], float)
        zmax = np.asarray(ds["max_reflectivity"][:], float)
    sel = (cell >= 0) & (t >= start.timestamp() - margin_s) & (t <= end.timestamp() + margin_s)
    if not sel.any():
        return None
    rows = [
        {
            "t_s": round(float(t[i] - start.timestamp()), 1),
            "cell": int(cell[i]),
            "x_km": round(float(x[i]), 3),
            "y_km": round(float(y[i]), 3),
            "area_cells": float(area[i]),
            "area_km2_if_0p25_per_cell": float(area[i]) * 0.25,
            "max_dbz": round(float(zmax[i]), 1),
        }
        for i in np.nonzero(sel)[0]
    ]
    out: dict[str, Any] = {
        "class": "observed (radar echo tracked by tobac)",
        "source": "tobac Track.nc made by the data owner from KGWX gridded reflectivity (threshold 15 dBZ)",
        "frame": "x_km east, y_km north of the grid origin",
        "rows": rows,
    }
    if len(rows) >= 2:
        a, b = rows[0], rows[-1]
        dt = b["t_s"] - a["t_s"]
        if dt > 0 and a["cell"] == b["cell"]:
            u = (b["x_km"] - a["x_km"]) * 1000.0 / dt
            v = (b["y_km"] - a["y_km"]) * 1000.0 / dt
            out["centroid_motion"] = {
                "u_ms": round(u, 2),
                "v_ms": round(v, 2),
                "speed_ms": round(float(np.hypot(u, v)), 2),
                "toward_deg": round(float((np.degrees(np.arctan2(u, v)) + 360.0) % 360.0), 1),
                "from_t_s": a["t_s"],
                "to_t_s": b["t_s"],
            }
    return out


def wind_components(speed: float, direction_deg: float) -> tuple[float, float]:
    r = np.radians(direction_deg)
    return -speed * np.sin(r), -speed * np.cos(r)

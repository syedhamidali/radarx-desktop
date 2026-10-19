"""Reading the PERiLS Py-ART grids (one radar, one volume, on the common 500 m grid) with minimal QC.

The grids in ``MULTIDOPPLER/GRID/<radar>/*.nc`` are all on the same Cartesian grid (azimuthal
equidistant projection on a sphere of radius 6370997 m, as Py-ART writes it): x, y from -90 km to
+90 km in 500 m steps, z from 0 to 10 km in 250 m steps above the grid origin altitude. Nothing is
ever written next to the source files.

QC is deliberately small and reversible (``QCConfig``; ``QCConfig.off()`` switches it all off, and every
rejected voxel is counted per criterion and radar, see ``load_grid`` and ``docs/PERILS_BUNDLE.md``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

UTC = timezone.utc
R_SPHERE = 6370997.0  # m, Py-ART's "pyart_aeqd"

# fields read from the grids (name in the file) -> name used here
FIELDS = ("REF", "VEL", "ZDR", "RHO", "KDP", "ROI", "NCP", "DBZC", "CREF")

#: nominal band, type and a descriptive name of each radar of the PERiLS collection
RADAR_INFO: dict[str, dict[str, str]] = {
    "KGWX": {"band": "S", "type": "WSR-88D (fixed)", "name": "KGWX Columbus AFB, MS"},
    "KBMX": {"band": "S", "type": "WSR-88D (fixed)", "name": "KBMX Birmingham, AL"},
    "COW1": {
        "band": "C",
        "type": "mobile C-band (CSWR COW)",
        "name": "COW1 (Center for Severe Weather Research)",
    },
    "SMARTR2": {"band": "C", "type": "mobile C-band (SMART-R 2)", "name": "SMART-R 2 (OU/NSSL)"},
    "DOW7": {
        "band": "X",
        "type": "mobile X-band (CSWR DOW)",
        "name": "DOW7 (Center for Severe Weather Research)",
    },
    "DOW8": {
        "band": "X",
        "type": "mobile X-band (CSWR DOW)",
        "name": "DOW8 (Center for Severe Weather Research)",
    },
    "NOXP": {
        "band": "X",
        "type": "mobile X-band (NSSL NOXP)",
        "name": "NOXP (NSSL X-band polarimetric)",
    },
    "UAH": {
        "band": "X",
        "type": "mobile X-band (UAH MAX)",
        "name": "UAH MAX (Mobile Alabama X-band)",
    },
}
#: weight of each band in the reflectivity blend (X band is attenuated in heavy rain and is not
#: corrected here, so it is down-weighted; a choice, not a measurement; see docs/PERILS_BUNDLE.md)
BAND_WEIGHT = {"S": 1.0, "C": 1.0, "X": 0.5}

_TIME_NAME = [
    re.compile(
        r"(?P<d>\d{8})_(?P<t>\d{6})"
    ),  # KGWX20220331_011153_V06, cfrad.20220331_011013.013_to_...
    re.compile(r"(?P<d>\d{8})(?P<t>\d{6})\.nc$"),  # RAW_NA_000_100_20220331011003.nc
]


@dataclass
class QCConfig:
    """Minimal QC of one radar grid. Every threshold can be switched off with ``None``."""

    roi_max_m: float | None = (
        3500.0  # radius of influence above which a voxel is too poorly sampled
    )
    rho_min: float | None = 0.6  # correlation coefficient below which reflectivity is noise ...
    rho_protect_dbz: float = (
        35.0  # ... unless reflectivity is at least this (debris / hail keep their echo)
    )
    ncp_min: float | None = 0.1  # normalised coherent power (UAH only)
    min_dbz: float | None = 0.0  # merged reflectivity below this is "no echo"
    vel_abs_max: float = 100.0  # m/s, physically impossible above
    ref_range: tuple[float, float] = (-30.0, 80.0)
    zdr_range: tuple[float, float] = (-5.0, 8.0)
    kdp_range: tuple[float, float] = (-3.0, 30.0)  # deg/km

    @classmethod
    def off(cls) -> QCConfig:
        """No QC beyond removing impossible values (fill values and garbage), recorded as such."""
        return cls(roi_max_m=None, rho_min=None, ncp_min=None, min_dbz=None)

    def as_dict(self) -> dict[str, Any]:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items()}


Z_STEP_M = 250.0


def shift_to_asl(a: np.ndarray, origin_alt_m: float, z_step_m: float = Z_STEP_M) -> np.ndarray:
    """Resample ``a`` (nz, ...) whose heights are above ``origin_alt_m`` onto heights above sea level.

    Py-ART grids put z = 0 at the altitude of the grid origin, which in these files is the altitude
    of each radar's own site (78 m for COW1, 231 m for KBMX, 330 m for NOXP, ...). The same height index
    is therefore a different height above sea level for every radar. The bundle grid is in metres ASL
    (0, 250, ... 10000): ``out(z_asl) = a(z_asl - origin_alt)``, linear in z. A voxel is kept if the
    nearer of the two levels around it has data (value = weighted mean of the levels that have);
    below the radar's own level and above the grid top it is NaN.
    """
    nz = a.shape[0]
    z_asl = np.arange(nz) * z_step_m
    zf = (z_asl - origin_alt_m) / z_step_m  # fractional index in the source grid
    k0 = np.floor(zf).astype(int)
    f = zf - k0
    out = np.full(a.shape, np.nan, a.dtype)
    for k in range(nz):
        i0, i1, w1 = k0[k], k0[k] + 1, f[k]
        if i1 < 0 or i0 > nz - 1:
            continue
        a0 = a[i0] if 0 <= i0 < nz else None
        a1 = a[i1] if 0 <= i1 < nz and w1 > 1e-9 else None
        if a0 is None and a1 is None:
            continue
        if a1 is None:
            out[k] = a0
            continue
        if a0 is None:
            if w1 >= 0.5:
                out[k] = a1
            continue
        w0 = 1.0 - w1
        f0, f1 = np.isfinite(a0), np.isfinite(a1)
        num = np.where(f0, w0 * np.nan_to_num(a0), 0.0) + np.where(f1, w1 * np.nan_to_num(a1), 0.0)
        den = np.where(f0, w0, 0.0) + np.where(f1, w1, 0.0)
        near_ok = f0 if w0 >= 0.5 else f1
        with np.errstate(invalid="ignore", divide="ignore"):
            out[k] = np.where(near_ok & (den > 0), num / den, np.nan)
    return out


@dataclass
class RadarGrid:
    name: str
    path: Path
    t_start: datetime
    t_end: datetime
    lat: float
    lon: float
    alt_m: float
    fields: dict[str, np.ndarray] = field(
        default_factory=dict
    )  # name -> (nz, ny, nx) float32, NaN = none
    qc: dict[str, int] = field(default_factory=dict)  # criterion -> voxels removed
    notes: list[str] = field(default_factory=list)
    origin_alt_m: float = (
        0.0  # altitude of the file's z = 0 (the radar site); fields are already on the ASL grid
    )

    @property
    def t_mid(self) -> datetime:
        return self.t_start + (self.t_end - self.t_start) / 2


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


def projection(lat0: float, lon0: float):
    """The grid's azimuthal equidistant projection (m east, m north of the origin)."""
    import pyproj

    return pyproj.Proj(proj="aeqd", lat_0=lat0, lon_0=lon0, R=R_SPHERE)


def xy_km(proj, lat, lon):
    x, y = proj(lon, lat)
    return np.asarray(x) / 1000.0, np.asarray(y) / 1000.0


def latlon(proj, x_km, y_km):
    lon, lat = proj(np.asarray(x_km) * 1000.0, np.asarray(y_km) * 1000.0, inverse=True)
    return np.asarray(lat), np.asarray(lon)


# ---------------------------------------------------------------------------
# file names and times
# ---------------------------------------------------------------------------


def parse_time(path: str | Path) -> datetime | None:
    """UTC start time from a grid file name (cfrad, NEXRAD, NOXP and UAH naming), else None."""
    name = Path(path).name
    for rx in _TIME_NAME:
        m = rx.search(name)
        if m:
            return datetime.strptime(m["d"] + m["t"], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    m = re.search(r"NOX(\d{6})(\d{6})", name)  # NOX220331011202
    if m:
        return datetime.strptime("20" + m[1] + m[2], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    return None


def list_grids(root: str | Path, radar: str) -> list[tuple[datetime, Path]]:
    """``[(start time, path)]`` of ``root/<radar>/*.nc`` sorted by time (names only, nothing opened)."""
    out = []
    for f in sorted((Path(root) / radar).glob("*.nc")):
        t = parse_time(f)
        if t is not None:
            out.append((t, f))
    return sorted(out)


def _attr_time(ds, key: str) -> datetime | None:
    v = getattr(ds, key, None)
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def grid_times(path: str | Path) -> tuple[datetime, datetime]:
    """(start, end) UTC of a grid from its attributes (volume start/end), else from ``time`` + 0 s."""
    import netCDF4

    with netCDF4.Dataset(path) as ds:
        t0 = _attr_time(ds, "start_datetime") or _attr_time(ds, "time_coverage_start")
        t1 = _attr_time(ds, "end_datetime") or _attr_time(ds, "time_coverage_end")
        if t0 is None:
            u = ds["time"].units
            base = datetime.fromisoformat(u.split("since")[1].strip().replace("Z", "+00:00"))
            base = base if base.tzinfo else base.replace(tzinfo=UTC)
            t0 = base + timedelta(seconds=float(np.ravel(ds["time"][:])[0]))
        if t1 is None or t1 < t0:
            t1 = t0
    return t0, t1


def volume_times(root: str | Path, radar: str) -> list[tuple[datetime, datetime, Path]]:
    """(start, end, path) per grid file, the times read from the files (cheap: attributes only)."""
    out = []
    for t_name, f in list_grids(root, radar):
        try:
            t0, t1 = grid_times(f)
        except Exception:  # unreadable file: keep the name time
            t0, t1 = t_name, t_name
        out.append((t0, t1, f))
    return out


# ---------------------------------------------------------------------------
# loading with QC
# ---------------------------------------------------------------------------


def _read(ds, name: str) -> np.ndarray | None:
    if name not in ds.variables:
        return None
    v = ds[name]
    a = v[0] if v.ndim == 4 else v[:]
    return np.ma.filled(np.ma.asarray(a).astype(np.float32), np.nan)


def load_grid(
    path: str | Path, radar: str, qc: QCConfig | None = None, shift_asl: bool = True
) -> RadarGrid:
    """One radar's grid with QC. Fields: ``dbz, vel, zdr, rho, kdp`` and ``roi`` (m) where present.

    * reflectivity is ``DBZC`` for SMART-R2 (its ``REF`` holds unmasked fill values), else ``REF``;
    * ``KDP`` of SMART-R2 holds scaled garbage (about -327): dropped, recorded in ``notes``;
    * every field is limited to a physical range; ROI, RHO and NCP gates are applied as in ``QCConfig``;
    * the heights are moved from above the radar site to above sea level (``shift_to_asl``).
    """
    import warnings

    import netCDF4

    qc = qc or QCConfig()
    path = Path(path)
    t0, t1 = grid_times(path)
    counts: dict[str, int] = {}
    notes: list[str] = []
    with netCDF4.Dataset(path) as ds, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        origin_alt = float(np.ravel(ds["origin_altitude"][:])[0])
        lat = float(np.ravel(ds["radar_latitude"][:])[0])
        lon = float(np.ravel(ds["radar_longitude"][:])[0])
        alt = float(np.ravel(ds["radar_altitude"][:])[0])
        raw = {n: _read(ds, n) for n in FIELDS}
    ref_name = "REF"
    if radar == "SMARTR2" and raw.get("DBZC") is not None:
        ref_name = "DBZC"
        notes.append("reflectivity from DBZC (its REF holds unmasked fill values)")
    dbz = raw[ref_name]
    out: dict[str, np.ndarray] = {}
    if dbz is None:
        raise ValueError(f"{path.name}: no reflectivity")

    def limit(a: np.ndarray, lo: float, hi: float, key: str) -> np.ndarray:
        bad = np.isfinite(a) & ((a < lo) | (a > hi))
        counts[key] = counts.get(key, 0) + int(bad.sum())
        a = a.copy()
        a[bad] = np.nan
        return a

    dbz = limit(dbz, *qc.ref_range, "ref_out_of_range")
    vel = raw["VEL"]
    if vel is not None:
        vel = limit(vel, -qc.vel_abs_max, qc.vel_abs_max, "vel_out_of_range")
    zdr = raw["ZDR"]
    if zdr is not None:
        zdr = limit(zdr, *qc.zdr_range, "zdr_out_of_range")
    kdp = raw["KDP"]
    if kdp is not None:
        n_all = int(np.isfinite(kdp).sum())
        n_bad = int((np.isfinite(kdp) & ((kdp < qc.kdp_range[0]) | (kdp > qc.kdp_range[1]))).sum())
        if n_all and n_bad > 0.5 * n_all:
            notes.append(
                "KDP dropped (more than half of the values are outside the physical range)"
            )
            counts["kdp_dropped"] = n_all
            kdp = None
        else:
            kdp = limit(kdp, *qc.kdp_range, "kdp_out_of_range")
    rho = raw["RHO"]
    if rho is not None:
        rho = limit(rho, 0.0, 1.05, "rho_out_of_range")
        rho = np.minimum(rho, 1.0)
    roi = raw["ROI"]

    # -- reflectivity gates (reversible: QCConfig.off()) ----------------------------------------
    if qc.roi_max_m is not None and roi is not None:
        bad = np.isfinite(roi) & (roi > qc.roi_max_m)
        for n_, a in (("dbz", dbz), ("vel", vel), ("zdr", zdr), ("rho", rho), ("kdp", kdp)):
            if a is not None:
                hit = bad & np.isfinite(a)
                counts[f"roi_{n_}"] = int(hit.sum())
                a[hit] = np.nan
    if qc.rho_min is not None and rho is not None:
        bad = np.isfinite(dbz) & np.isfinite(rho) & (rho < qc.rho_min) & (dbz < qc.rho_protect_dbz)
        counts["rho_dbz"] = int(bad.sum())
        dbz[bad] = np.nan
    ncp = raw["NCP"]
    if qc.ncp_min is not None and ncp is not None:
        bad = np.isfinite(ncp) & (ncp < qc.ncp_min)
        for n_, a in (("dbz", dbz), ("vel", vel)):
            if a is not None:
                hit = bad & np.isfinite(a)
                counts[f"ncp_{n_}"] = int(hit.sum())
                a[hit] = np.nan
    # a velocity or dual-pol value without a reflectivity gate is dropped (same gate, same radar)
    nodata = ~np.isfinite(dbz)
    for n_, a in (("vel", vel), ("zdr", zdr), ("rho", rho), ("kdp", kdp)):
        if a is not None:
            hit = nodata & np.isfinite(a)
            counts[f"nodbz_{n_}"] = int(hit.sum())
            a[hit] = np.nan
    out["dbz"] = dbz
    for n_, a in (("vel", vel), ("zdr", zdr), ("rho", rho), ("kdp", kdp)):
        if a is not None:
            out[n_] = a
    if roi is not None:
        out["roi"] = roi
    if shift_asl:
        for k in list(out):
            out[k] = shift_to_asl(out[k], origin_alt)
        notes.append(
            f"heights shifted from above {origin_alt:g} m (the radar site) to above sea level"
        )
    counts["valid_dbz"] = int(np.isfinite(out["dbz"]).sum())
    return RadarGrid(radar, path, t0, t1, lat, lon, alt, out, counts, notes, origin_alt)


# ---------------------------------------------------------------------------
# ZDR calibration check
# ---------------------------------------------------------------------------

ZDR_RAIN_EXPECTED_DB = (
    0.55  # typical ZDR of rain at 25 to 40 dBZ (about 0.4 to 0.8 dB): a reference, not a truth
)
ZDR_MIN_SAMPLES = 3000


def estimate_zdr_offset(
    f: dict[str, np.ndarray], z_asl_m: np.ndarray, min_samples: int = ZDR_MIN_SAMPLES
) -> tuple[float | None, int]:
    """Bias of a radar's ZDR from rain: median ZDR of voxels with 25 to 40 dBZ, RHO >= 0.97, ROI < 2.5 km,
    0.3 to 2.5 km above sea level, minus the typical rain value ``ZDR_RAIN_EXPECTED_DB``.

    Returns ``(offset_db, n_samples)``; the offset is None when there are fewer than ``min_samples``.
    It is an ESTIMATE (the drop size distribution of the day is not the typical one): +-0.3 dB at best.
    Several of the mobile radars of the collection have ZDR biases of 3 to 4 dB (NOXP), so ZDR from
    different radars cannot be mixed without a correction.
    """
    if not all(k in f for k in ("zdr", "rho", "dbz")):
        return None, 0
    z = z_asl_m[:, None, None]
    sel = (
        np.isfinite(f["zdr"])
        & np.isfinite(f["rho"])
        & (f["rho"] >= 0.97)
        & (f["dbz"] >= 25.0)
        & (f["dbz"] <= 40.0)
        & (z >= 300.0)
        & (z <= 2500.0)
    )
    roi = f.get("roi")
    if roi is not None:
        sel &= roi < 2500.0
    n = int(sel.sum())
    if n < min_samples:
        return None, n
    return float(np.median(f["zdr"][sel])) - ZDR_RAIN_EXPECTED_DB, n

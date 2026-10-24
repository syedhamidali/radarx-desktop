"""Observed soundings: read radiosonde files, compute and *label* the thermodynamic parameters.

Reading (no network): University of Wyoming text list (``TEXT:LIST``) and CSV, SPC/SHARPpy/NSHARP
text (``%RAW%``), IEM RAOB JSON, IGRA2 station files (through radarx), and any CSV whose header
names the columns (units in the header are honoured: ``pressure(mb)``, ``temp (deg C)``,
``wind speed (kts)``, ``height (m AGL)`` ...).

Everything is converted to SI in a radarx-style profile (``xarray.Dataset`` on a ``height``
dimension, geometric metres above sea level, pressure in Pa, temperature in K) so the parcel
thermodynamics of :mod:`radarx.io.parcel` (Bolton 1980) apply directly.

Diagnostics (:func:`diagnose`) always say **which parcel** (surface-based, mixed-layer 100 hPa,
most-unstable 300 hPa), which **height reference** (AGL = above the launch site) and carry a
plain-language definition with units and reference for every number. A missing quantity is
``None``, never a made-up value; the assumptions that were needed are listed in ``notes``.
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from engine import layer_provenance, provenance

G0 = 9.80665  # m s-2, conventional standard gravity (CGPM 1901)
RE = 6371008.8  # m, mean Earth radius (as radarx.io.sounding)
T0 = 273.15
KNOT = 0.514444  # m s-1 per knot
MISSING = -9998.0  # values at or below this are "missing" in the text formats
MAX_FILE_BYTES = 64 * 1024 * 1024
UI_MAX_LEVELS = 900


class SoundingError(ValueError):
    """The file is not a usable sounding (the message says what is missing)."""


class RadarxMissing(ImportError):  # noqa: N818
    """The parcel thermodynamics need radarx (``pip install radarx``)."""


# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------


@dataclass
class Sounding:
    """One profile and where it came from. ``profile`` is a radarx-style Dataset (SI units)."""

    profile: Any  # xarray.Dataset
    station: str = ""
    station_name: str = ""
    latitude: float | None = None
    longitude: float | None = None
    launch_time: str = ""  # ISO-8601 UTC or ""
    source: str = ""
    fmt: str = ""
    file: str = ""  # file name (never a folder)
    notes: list[str] = field(default_factory=list)
    drift: dict[str, Any] | None = None
    #: dew point assumed (dry) above the last reported humidity; K array same length as profile
    dewpoint_filled: Any = None
    #: "observed" (a radiosonde) or "model" (a model/reanalysis column such as ERA5 or HRRR)
    data_class: str = "observed"
    #: a ready-made provenance (a model column from the environment providers has run time,
    #: forecast hour and grid spacing to show); ``None`` = the generic one below
    prov: Any = None

    @property
    def surface_height_m(self) -> float:
        return float(self.profile["height"].values[0])

    def drift_note(self) -> str:
        if self.data_class == "model":
            return "A model or reanalysis column at one grid point and one time (no balloon drift)."
        if self.drift:
            d = self.drift
            return (
                f"The balloon drifted {d['max_km']:.0f} km from the launch site "
                f"(towards {d['towards_deg']:.0f} deg) over {d['duration_min']:.0f} min; "
                "the profile is not vertical above the station."
            )
        return (
            "This file has no per-level positions or times: the profile is treated as a vertical "
            "line over the launch site, although a real balloon drifts with the wind."
        )

    def layer_provenance(self) -> layer_provenance.LayerProvenance:
        if self.prov is not None:
            return self.prov
        model = self.data_class == "model"
        return layer_provenance.sounding(
            station=self.station or self.station_name,
            launch_time=self.launch_time,
            source=self.source or ("model profile file" if model else "radiosonde file"),
            method=("Model or reanalysis profile" if model else "Radiosonde profile")
            + f" read from {self.file or 'a file'}"
            + (f" ({self.fmt})" if self.fmt else "")
            + "; geopotential heights converted to geometric metres.",
            kind="model" if model else "observed",
        )

    def levels(self, max_levels: int | None = UI_MAX_LEVELS) -> dict[str, Any]:
        """The profile as plain lists (hPa, m ASL, degC, m s-1) for the Skew-T; thinned if long."""
        ds = self.profile
        n = ds.sizes["height"]
        idx = np.arange(n)
        thinned = False
        if max_levels and n > max_levels:
            idx = np.unique(np.linspace(0, n - 1, max_levels).round().astype(int))
            thinned = True

        def col(name: str, scale: float = 1.0, offset: float = 0.0) -> list[float | None]:
            if name not in ds:
                return [None] * len(idx)
            v = np.asarray(ds[name].values, dtype=float)[idx] * scale + offset
            return [None if not math.isfinite(x) else round(float(x), 3) for x in v]

        return {
            "pressure_hpa": col("pressure", 0.01),
            "height_msl_m": col("height"),
            "temperature_c": col("temperature", 1.0, -T0),
            "dewpoint_c": col("dewpoint_reported", 1.0, -T0),
            "u_ms": col("u"),
            "v_ms": col("v"),
            "n_levels": int(n),
            "thinned": thinned,
        }

    def to_dict(self, *, max_levels: int | None = UI_MAX_LEVELS) -> dict[str, Any]:
        lv = self.levels(max_levels)
        return {
            "station": self.station,
            "station_name": self.station_name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "launch_time": self.launch_time,
            "source": self.source,
            "format": self.fmt,
            "data_class": self.data_class,
            "file": self.file,
            "surface_height_m": round(self.surface_height_m, 1),
            "notes": list(self.notes),
            "drift_note": self.drift_note(),
            "drift": self.drift,
            "levels": lv,
            "provenance": self.layer_provenance().to_dict(),
        }


# ---------------------------------------------------------------------------
# Building a profile
# ---------------------------------------------------------------------------


def geometric_height(geopotential_m: Any) -> Any:
    """Geometric height (m) of a geopotential height (m): ``z = Re H / (Re - H)``.

    Standard spherical-Earth conversion, the one radarx uses (16 m at 10 km, 63 m at 20 km).
    """
    h = np.asarray(geopotential_m, dtype=float)
    return RE * h / (RE - h)


def _wind_components(direction_deg: Any, speed_ms: Any) -> tuple[Any, Any]:
    """Meteorological convention: *direction* is where the wind blows **from**."""
    rad = np.radians(np.asarray(direction_deg, dtype=float))
    spd = np.asarray(speed_ms, dtype=float)
    return -spd * np.sin(rad), -spd * np.cos(rad)


def build_profile(
    *,
    pressure_pa: Any,
    temperature_k: Any,
    dewpoint_k: Any,
    height_m: Any | None = None,
    geopotential_m: Any | None = None,
    u: Any | None = None,
    v: Any | None = None,
    wind_dir_deg: Any | None = None,
    wind_speed_ms: Any | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    time: str = "",
) -> tuple[Any, list[str], Any]:
    """Assemble and clean a profile. Returns ``(dataset, notes, dewpoint_filled_mask)``.

    Cleaning: levels without temperature are dropped (and without pressure *and* height);
    levels are sorted by height; duplicate heights are merged; heights missing between valid ones
    are interpolated in log-pressure. Nothing is smoothed.
    """
    import xarray as xr

    notes: list[str] = []
    p = np.asarray(pressure_pa, dtype=float)
    t = np.asarray(temperature_k, dtype=float)
    td = np.asarray(dewpoint_k, dtype=float)
    n = p.size
    if height_m is not None:
        z = np.asarray(height_m, dtype=float)
    elif geopotential_m is not None:
        z = geometric_height(geopotential_m)
    else:
        z = np.full(n, np.nan)
    if u is None and wind_dir_deg is not None and wind_speed_ms is not None:
        uu, vv = _wind_components(wind_dir_deg, wind_speed_ms)
    elif u is not None and v is not None:
        uu, vv = np.asarray(u, dtype=float), np.asarray(v, dtype=float)
    else:
        uu = vv = np.full(n, np.nan)
    for arr in (p, t, td, z, uu, vv):
        if arr.shape != (n,):
            raise SoundingError("the columns of the sounding have different lengths")
    for arr in (p, t, td, z, uu, vv):
        arr[~np.isfinite(arr)] = np.nan
    p[p <= 0] = np.nan

    # heights missing between known ones: interpolate in log(p)
    known = np.isfinite(z) & np.isfinite(p)
    miss = ~np.isfinite(z) & np.isfinite(p)
    if miss.any() and known.sum() >= 2:
        lp = -np.log(p[known])
        order = np.argsort(lp)
        lpm = -np.log(p[miss])
        inside = (lpm >= lp[order][0]) & (lpm <= lp[order][-1])
        fill = np.interp(lpm, lp[order], z[known][order])
        z[np.nonzero(miss)[0][inside]] = fill[inside]
        if inside.any():
            notes.append(f"{int(inside.sum())} level(s) without height: interpolated in log(p).")
    if not np.isfinite(z).any():
        raise SoundingError("the sounding has neither heights nor pressures to derive them")
    if not np.isfinite(p).any():
        raise SoundingError("the sounding has no pressure column")

    keep = np.isfinite(z) & np.isfinite(p) & np.isfinite(t)
    dropped = int(n - keep.sum())
    if dropped:
        notes.append(f"{dropped} level(s) without temperature, pressure or height were skipped.")
    if keep.sum() < 3:
        raise SoundingError("fewer than 3 usable levels (need temperature, pressure and height)")
    p, t, td, z, uu, vv = (a[keep] for a in (p, t, td, z, uu, vv))
    order = np.argsort(z, kind="stable")
    p, t, td, z, uu, vv = (a[order] for a in (p, t, td, z, uu, vv))

    # merge duplicate heights (mean of the valid values)
    uniq, inv = np.unique(z, return_inverse=True)
    if uniq.size != z.size:

        def merge(a: Any) -> Any:
            ok = np.isfinite(a)
            tot = np.bincount(inv, np.where(ok, a, 0.0), uniq.size)
            cnt = np.bincount(inv, ok.astype(float), uniq.size)
            with np.errstate(invalid="ignore", divide="ignore"):
                return np.where(cnt > 0, tot / cnt, np.nan)

        p, t, td, uu, vv = (merge(a) for a in (p, t, td, uu, vv))
        z = uniq
        notes.append("Levels with the same height were merged (mean).")
    # pressure must fall with height; drop levels that break that (sensor glitches)
    bad = np.zeros(z.size, dtype=bool)
    last = np.inf
    for i in range(z.size):
        if p[i] >= last:
            bad[i] = True
        else:
            last = p[i]
    if bad.any():
        notes.append(
            f"{int(bad.sum())} level(s) where pressure did not fall with height were skipped."
        )
        p, t, td, z, uu, vv = (a[~bad] for a in (p, t, td, z, uu, vv))

    # dew point gaps: interior gaps are interpolated in log(p); trailing gaps are *assumed dry*
    filled = np.zeros(z.size, dtype=bool)
    valid_td = np.isfinite(td)
    if valid_td.sum() >= 2:
        lastvalid = int(np.nonzero(valid_td)[0][-1])
        firstvalid = int(np.nonzero(valid_td)[0][0])
        interior = ~valid_td & (np.arange(z.size) > firstvalid) & (np.arange(z.size) < lastvalid)
        if interior.any():
            td[interior] = np.interp(-np.log(p[interior]), -np.log(p[valid_td]), td[valid_td])
            filled |= interior
            notes.append(
                f"Dew point missing at {int(interior.sum())} level(s) between reported ones: "
                "interpolated in log(p) for the parcel calculation (not drawn)."
            )
        trailing = ~np.isfinite(td) & (np.arange(z.size) > lastvalid)
        if trailing.any():
            td[trailing] = t[trailing] - 60.0
            filled |= trailing
            notes.append(
                f"No dew point above {p[lastvalid] / 100:.0f} hPa: the air there is treated as "
                "very dry (dew point 60 K below the temperature) for the parcel calculation; the "
                "effect on CAPE is negligible at those temperatures. Not drawn."
            )
    elif valid_td.sum() < 2:
        raise SoundingError("the sounding has no dew point (or humidity) column")
    td = np.minimum(td, t)  # a dew point above the temperature is a reporting artefact

    ds = xr.Dataset(
        {
            "pressure": ("height", p, {"units": "Pa", "standard_name": "air_pressure"}),
            "temperature": ("height", t, {"units": "K", "standard_name": "air_temperature"}),
            "dewpoint": ("height", td, {"units": "K", "standard_name": "dew_point_temperature"}),
            "u": ("height", uu, {"units": "m s-1", "standard_name": "eastward_wind"}),
            "v": ("height", vv, {"units": "m s-1", "standard_name": "northward_wind"}),
        },
        coords={
            "height": ("height", z, {"units": "m", "long_name": "geometric height above sea level"})
        },
    )
    ds["wind_speed"] = np.hypot(ds["u"], ds["v"])
    ds["wind_speed"].attrs["units"] = "m s-1"
    if latitude is not None:
        ds = ds.assign_coords(latitude=float(latitude))
    if longitude is not None:
        ds = ds.assign_coords(longitude=float(longitude))
    if time:
        ds = ds.assign_coords(time=np.datetime64(time.rstrip("Z")))
    # the Td we draw is the reported one (NaN where it was filled)
    ds["dewpoint_reported"] = ("height", np.where(filled, np.nan, td))
    return ds, notes, filled


def _col(values: list[str], scale: float = 1.0) -> np.ndarray:
    out = np.full(len(values), np.nan)
    for i, v in enumerate(values):
        s = v.strip()
        if not s:
            continue
        try:
            x = float(s)
        except ValueError:
            continue
        if x <= MISSING:
            continue
        out[i] = x * scale
    return out


#: Names that mark a profile as model or reanalysis output rather than a radiosonde (kept narrow:
#: a radiosonde file that mentions e.g. its sonde "model" must stay observed).
_MODEL_WORDS = re.compile(
    r"\b(era5|era-5|era-interim|merra-?2|reanalysis|hrrr|gfs|ecmwf|"
    r"model (?:profile|column|output|analysis|sounding))\b",
    re.IGNORECASE,
)


def classify_source(*texts: Any) -> str:
    """``"model"`` when a source text names a model or reanalysis (ERA5, HRRR, ...), else
    ``"observed"``."""
    for t in texts:
        if t and _MODEL_WORDS.search(str(t)):
            return "model"
    return "observed"


def _finish(built: tuple[Any, list[str], Any], **meta: Any) -> Sounding:
    ds, notes, filled = built
    s = Sounding(profile=ds, notes=notes, dewpoint_filled=filled, **meta)
    if s.launch_time:
        s.launch_time = provenance.iso_utc(s.launch_time) or s.launch_time
    return s


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

_WYO_FIELDS = {
    "PRES": "pressure",
    "HGHT": "height",
    "TEMP": "temperature",
    "DWPT": "dewpoint",
    "DRCT": "dir",
    "SKNT": "speed_kt",
}


def _launch_from_stamp(stamp: str) -> str:
    """``yymmdd/hhmm`` (Wyoming, SHARPpy) to ISO."""
    m = re.match(r"(\d{2})(\d{2})(\d{2})/(\d{2})(\d{2})", stamp.strip())
    if not m:
        return ""
    yy, mo, dd, hh, mi = (int(g) for g in m.groups())
    year = 2000 + yy if yy < 70 else 1900 + yy
    try:
        return datetime(year, mo, dd, hh, mi, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return ""


def parse_wyoming_text(text: str, file: str = "") -> Sounding:
    """University of Wyoming ``TEXT:LIST``: fixed 7-character columns ``PRES HGHT TEMP DWPT ...``.

    Heights are geopotential metres, temperatures degC, wind from-direction in degrees and speed in
    knots. Blank fields are missing. The station block below the table supplies the station,
    launch time, latitude/longitude and elevation.
    """
    lines = text.replace("\r", "").split("\n")
    head = next((i for i, ln in enumerate(lines) if "PRES" in ln and "HGHT" in ln), None)
    if head is None:
        raise SoundingError("not a University of Wyoming text list (no PRES/HGHT header)")
    names = lines[head].split()
    width = 7
    cols: dict[str, int] = {}
    for k, name in enumerate(names):
        cols[name] = k
    rows: list[list[str]] = []
    for ln in lines[head + 3 :]:
        if not ln.strip() or ln.lstrip().startswith(("<", "-")):
            if rows and ln.lstrip().startswith("<"):
                break
            continue
        if not re.match(r"^\s*\d", ln):
            break
        rows.append([ln[i * width : (i + 1) * width] for i in range(len(names))])
    if len(rows) < 3:
        raise SoundingError("the Wyoming text list has no data rows")

    def get(name: str, scale: float = 1.0) -> np.ndarray:
        if name not in cols:
            return np.full(len(rows), np.nan)
        return _col([r[cols[name]] for r in rows], scale)

    info: dict[str, str] = {}
    for ln in lines[head:]:
        m = re.match(r"^\s*([A-Za-z ]+?):\s*(\S.*?)\s*$", ln)
        if m:
            info[m.group(1).strip().lower()] = m.group(2)
    title = next((re.sub("<[^>]+>", "", ln).strip() for ln in lines if "Observations at" in ln), "")
    launch = _launch_from_stamp(info.get("observation time", ""))
    if not launch and title:
        m = re.search(r"at (\d{2})Z (\d{1,2}) (\w{3}) (\d{4})", title)
        if m:
            try:
                launch = (
                    datetime.strptime(
                        f"{m.group(4)} {m.group(3)} {m.group(2)} {m.group(1)}", "%Y %b %d %H"
                    )
                    .replace(tzinfo=UTC)
                    .strftime("%Y-%m-%dT%H:%M:%SZ")
                )
            except ValueError:
                launch = ""
    lat = _float(info.get("station latitude"))
    lon = _float(info.get("station longitude"))
    station = info.get("station identifier", "") or (
        title.split()[1] if len(title.split()) > 1 else ""
    )
    built = build_profile(
        pressure_pa=get("PRES", 100.0),
        geopotential_m=get("HGHT"),
        temperature_k=get("TEMP") + T0,
        dewpoint_k=get("DWPT") + T0,
        wind_dir_deg=get("DRCT"),
        wind_speed_ms=get("SKNT", KNOT),
        latitude=lat,
        longitude=lon,
        time=launch,
    )
    return _finish(
        built,
        station=station,
        station_name=re.sub(r"^\d+\s+\S+\s+|\s+Observations at.*$", "", title).strip(),
        latitude=lat,
        longitude=lon,
        launch_time=launch,
        source="University of Wyoming upper-air archive (text list)",
        fmt="wyoming_text",
        file=file,
    )


def _float(text: Any) -> float | None:
    try:
        v = float(str(text).strip())
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def parse_sharppy(text: str, file: str = "") -> Sounding:
    """SPC / SHARPpy / NSHARP text: ``%TITLE%`` ... ``%RAW%`` rows ``p(hPa), z(m), T, Td (degC),
    wind direction (deg), speed (knots)`` ... ``%END%``. Missing values are -9999."""
    lines = text.replace("\r", "").split("\n")
    stripped = [ln.strip() for ln in lines]
    if "%RAW%" not in stripped:
        raise SoundingError("not a SHARPpy/SPC sounding (no %RAW% section)")
    station, launch = "", ""
    if "%TITLE%" in stripped:
        title = lines[stripped.index("%TITLE%") + 1].split()
        if title:
            station = title[0]
        if len(title) > 1:
            launch = _launch_from_stamp(title[1])
    rows: list[list[str]] = []
    for ln in lines[stripped.index("%RAW%") + 1 :]:
        if ln.strip() == "%END%":
            break
        if ln.strip():
            rows.append(ln.split(","))
    if len(rows) < 3 or any(len(r) < 6 for r in rows):
        raise SoundingError("the %RAW% section needs 6 columns: p, z, T, Td, direction, speed")
    c = [_col([r[i] for r in rows]) for i in range(6)]
    built = build_profile(
        pressure_pa=c[0] * 100.0,
        geopotential_m=c[1],
        temperature_k=c[2] + T0,
        dewpoint_k=c[3] + T0,
        wind_dir_deg=c[4],
        wind_speed_ms=c[5] * KNOT,
        time=launch,
    )
    return _finish(
        built,
        station=station,
        launch_time=launch,
        source="SHARPpy / SPC sounding file",
        fmt="sharppy",
        file=file,
    )


def parse_iem_json(text: str, file: str = "") -> Sounding:
    """IEM RAOB JSON: ``profiles[].profile[]`` rows with ``pres hght tmpc dwpc sknt drct``."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SoundingError(f"not valid JSON: {exc}") from exc
    profs = data.get("profiles") if isinstance(data, dict) else None
    if not profs or not profs[0].get("profile"):
        raise SoundingError("no sounding in the IEM response (wrong station or time?)")
    prof = profs[0]
    rows = prof["profile"]

    def col(key: str, scale: float = 1.0) -> np.ndarray:
        return _col([("" if r.get(key) is None else str(r.get(key))) for r in rows], scale)

    launch = provenance.iso_utc(str(prof.get("valid", "")).replace("Z", "")) or ""
    built = build_profile(
        pressure_pa=col("pres", 100.0),
        geopotential_m=col("hght"),
        temperature_k=col("tmpc") + T0,
        dewpoint_k=col("dwpc") + T0,
        wind_dir_deg=col("drct"),
        wind_speed_ms=col("sknt", KNOT),
        time=launch,
    )
    return _finish(
        built,
        station=str(prof.get("station", "")),
        launch_time=launch,
        source="Iowa Environmental Mesonet RAOB archive",
        fmt="iem_json",
        file=file,
    )


# column name (units stripped) -> canonical
_HEADER_KEYS = {
    "pressure": ("pressure", "pres", "p", "level", "pressure_hpa", "press"),
    "height": ("height", "hght", "z", "height_m", "geopotential_height", "alt", "altitude"),
    "temperature": ("temperature", "temp", "tmpc", "t", "temperature_c", "tdry", "air_temperature"),
    "dewpoint": ("dewpoint", "dewpt", "dwpt", "dwpc", "td", "dewpoint_c", "dew_point", "dwpoint"),
    "rh": ("rh", "relh", "relative_humidity", "humidity"),
    "wdir": ("wind_direction", "drct", "wdir", "direction", "dir", "winddir"),
    "wspd": ("wind_speed", "wspd", "speed", "sknt", "windspeed", "wspeed"),
    "u": ("u", "u_wind", "uwind"),
    "v": ("v", "v_wind", "vwind"),
    "lat": ("latitude", "lat"),
    "lon": ("longitude", "lon"),
    "time": ("time", "utc"),
    "station": ("station", "stid", "site"),
}


def _split_header(name: str) -> tuple[str, str]:
    """``"temp (deg C)"`` -> ``("temp", "deg c")``; ``"pressure(mb)"`` -> ``("pressure", "mb")``."""
    m = re.match(r"^\s*([^(\[]+?)\s*[(\[]\s*([^)\]]*)[)\]]\s*$", name)
    base, unit = (m.group(1), m.group(2)) if m else (name, "")
    return re.sub(r"[\s\-]+", "_", base.strip().lower()), unit.strip().lower()


_WIND_UNITS = {
    "kt": KNOT, "kts": KNOT, "kn": KNOT, "knot": KNOT, "knots": KNOT,
    "m/s": 1.0, "ms-1": 1.0, "m_s-1": 1.0, "mps": 1.0, "ms": 1.0, "m_s": 1.0,
    "mph": 0.44704, "km/h": 1.0 / 3.6, "kmh": 1.0 / 3.6, "kph": 1.0 / 3.6,
}  # fmt: skip


def _wind_factor(unit: str, base: str, override: str | None, notes: list[str]) -> float:
    """Metres per second per unit of the wind-speed column.

    The unit comes from the caller (*override*), else the header (``speed (kts)``), else the
    column name (``sknt`` is knots). A column with no unit at all is taken as m/s, **and the
    sounding says so**: guessing knots instead would be just as likely to be wrong.
    """
    u = re.sub(r"\s+", "_", (override or unit or "").strip().lower())
    if base == "sknt" and not override and not u:
        u = "kt"
    if not u:
        notes.append(
            "The wind speed has no unit in the file: m/s is assumed. If it is in knots (as in "
            "Wyoming and SPC files) pass wind_units='kt' (rxd sounding --wind-units kt)."
        )
        return 1.0
    if u not in _WIND_UNITS:
        raise SoundingError(f"unknown wind speed unit {u!r} (use m/s, kt, mph or km/h)")
    return _WIND_UNITS[u]


def _is_header(fields: list[str]) -> bool:
    """A row of column names: at least two of them are names this reader knows."""
    known = {a for aliases in _HEADER_KEYS.values() for a in aliases}
    return sum(1 for f in fields if _split_header(f)[0] in known) >= 2


def parse_csv(text: str, file: str = "", wind_units: str | None = None) -> Sounding:
    """CSV with a header naming the columns; units are read from the header when present.

    Recognised units: pressure ``hPa``/``mb``/``Pa``; height ``m`` (``m AGL`` is relative to the
    first level), ``ft``; temperature ``C``/``K``/``F``; wind speed ``m/s``, ``kt``/``kts``,
    ``mph``; direction in degrees. Without a unit the SPC/Wyoming defaults apply
    (hPa, m, degC, knots when the column is called ``sknt``, else m/s).
    ``lines`` starting with ``#`` are comments (the first one may name the station and time).
    """
    comments = [ln for ln in text.splitlines() if ln.startswith("#")]
    body = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if len(body) < 4:
        raise SoundingError("the CSV has no data rows")
    reader = csv.reader(body)
    header_raw = next(reader)
    if not _is_header(header_raw):
        # some files comment out the column names ("# latitude (deg), longitude (deg), ...")
        for ln in reversed(comments):
            cand = next(csv.reader([ln.lstrip("#").strip()]))
            if _is_header(cand):
                header_raw = cand
                reader = csv.reader(body)
                break
    rows = [r for r in reader if r]
    heads = [_split_header(h) for h in header_raw]

    def find(key: str) -> int | None:
        for alias in _HEADER_KEYS[key]:
            for i, (base, _) in enumerate(heads):
                if base == alias:
                    return i
        return None

    def column(key: str) -> tuple[np.ndarray, str] | None:
        i = find(key)
        if i is None:
            return None
        return _col([r[i] if i < len(r) else "" for r in rows]), heads[i][1]

    pr, he, te, dw = (column(k) for k in ("pressure", "height", "temperature", "dewpoint"))
    if te is None or (pr is None and he is None):
        raise SoundingError(
            "the CSV needs a temperature column and a pressure or height column "
            f"(found: {', '.join(h for h, _ in heads)})"
        )
    notes: list[str] = []
    if pr is not None:
        unit = pr[1]
        p_pa = pr[0] * (1.0 if unit == "pa" else 100.0)
    else:
        p_pa = np.full(len(rows), np.nan)
    z_m = None
    z_agl = False
    geopotential = False
    if he is not None:
        unit = he[1]
        z_m = he[0] * (0.3048 if unit in ("ft", "feet") else 1.0)
        z_agl = "agl" in unit
        hi = find("height")
        name = heads[hi][0] if hi is not None else ""
        # Wyoming / SPC style columns (hght, height, geopotential_height) with no hint to the
        # contrary hold geopotential metres (as in radarx); GPS altitudes and "m AGL/MSL" columns
        # are geometric.
        geopotential = (
            name in ("hght", "geopotential_height", "height", "z", "height_m")
            and not any(k in unit for k in ("agl", "msl", "gps", "geom"))
            and unit in ("", "m", "gpm")
        )
        if geopotential:
            notes.append(
                "Heights are taken as geopotential metres (the Wyoming / SPC convention) and "
                "converted to geometric height (16 m at 10 km)."
            )
    tt, tunit = te
    t_k = tt + T0 if tunit in ("", "c", "deg_c", "degc", "deg c", "celsius", "°c") else tt
    if tunit in ("f", "deg f", "degf"):
        t_k = (tt - 32.0) * 5.0 / 9.0 + T0
    elif tunit == "k":
        t_k = tt
    if dw is not None:
        dd, dunit = dw
        td_k = dd + T0 if dunit in ("", "c", "deg c", "degc", "celsius", "°c") else dd
        if dunit == "k":
            td_k = dd
        elif dunit in ("f", "deg f"):
            td_k = (dd - 32.0) * 5.0 / 9.0 + T0
    else:
        rh = column("rh")
        if rh is None:
            raise SoundingError("the CSV needs a dew point or relative humidity column")
        rhv = rh[0] / (100.0 if np.nanmax(rh[0]) > 1.5 else 1.0)
        tc = t_k - T0
        es = 6.112 * np.exp(17.67 * tc / (tc + 243.5))  # Bolton 1980 eq. 10 (hPa)
        e = np.clip(rhv, 1e-6, 1.0) * es
        ln_e = np.log(e / 6.112)
        td_k = 243.5 * ln_e / (17.67 - ln_e) + T0
        notes.append("Dew point derived from relative humidity (Bolton 1980, eq. 10).")
    uu = vv = wd = ws = None
    wdir, wspd = column("wdir"), column("wspd")
    if wdir is not None and wspd is not None:
        i = find("wspd")
        wd = wdir[0]
        ws = wspd[0] * _wind_factor(
            wspd[1], heads[i][0] if i is not None else "", wind_units, notes
        )
    else:
        ucol, vcol = column("u"), column("v")
        if ucol is not None and vcol is not None:
            f = _wind_factor(ucol[1], "", wind_units, notes)
            uu, vv = ucol[0] * f, vcol[0] * f
    lat_c, lon_c, time_c = column("lat"), column("lon"), None
    ti = find("time")
    if ti is not None:
        time_c = [r[ti].strip() if ti < len(r) else "" for r in rows]
    meta_station, meta_launch = "", ""
    si = find("station")
    if si is not None and rows:
        meta_station = rows[0][si].strip() if si < len(rows[0]) else ""
    if time_c:
        first = next((t for t in time_c if t), "")
        if re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", first):
            meta_launch = provenance.iso_utc(first) or ""
        elif re.match(r"^\d{4}-\d{2}-\d{2}$", first):
            notes.append(f"The file gives only the date ({first}); the launch hour is not in it.")
    for c in comments:
        m = re.search(r"(\d{8})\s*,?\s*(\d{6}|\d{4})\s*UTC\s*,\s*([^,]+)", c)
        if m:
            d, hm = m.group(1), m.group(2).ljust(6, "0")  # HHMM or HHMMSS
            meta_launch = f"{d[:4]}-{d[4:6]}-{d[6:]}T{hm[:2]}:{hm[2:4]}:{hm[4:]}Z"
            meta_station = m.group(3).strip()
    height_asl = z_m
    if z_agl and z_m is not None:
        base = None
        for c in comments:
            m = re.search(r"(-?\d+(?:\.\d+)?)\s*m\s*$", c)
            if m:
                base = float(m.group(1))
        if base is not None:
            height_asl = z_m + base
            notes.append(
                f"Heights are AGL in the file; {base:g} m (the station elevation in its "
                "header) was added."
            )
        else:
            notes.append(
                "Heights are above ground level and the file gives no station elevation: "
                "heights are treated as above the launch site."
            )
    built = build_profile(
        pressure_pa=p_pa,
        height_m=None if geopotential else height_asl,
        geopotential_m=height_asl if geopotential else None,
        temperature_k=t_k,
        dewpoint_k=td_k,
        u=uu,
        v=vv,
        wind_dir_deg=wd,
        wind_speed_ms=ws,
        latitude=float(np.nanmedian(lat_c[0]))
        if lat_c is not None and np.isfinite(lat_c[0]).any()
        else None,
        longitude=float(np.nanmedian(lon_c[0]))
        if lon_c is not None and np.isfinite(lon_c[0]).any()
        else None,
        time=meta_launch,
    )
    sound = _finish(
        built,
        station=meta_station,
        launch_time=meta_launch,
        source="CSV sounding file",
        fmt="csv",
        file=file,
    )
    sound.notes[:0] = notes
    sound.data_class = classify_source(*comments)
    if sound.data_class == "model":
        sound.source = "CSV model/reanalysis profile"
    if lat_c is not None and lon_c is not None and np.isfinite(lat_c[0]).any():
        la, lo = lat_c[0], lon_c[0]
        ok = np.isfinite(la) & np.isfinite(lo)
        if ok.sum() >= 2:
            sound.latitude, sound.longitude = float(la[ok][0]), float(lo[ok][0])
            drift = _drift(la[ok], lo[ok], time_c)
            # a constant position (the station, repeated on every row) is not a drift track
            sound.drift = drift if drift and drift["max_km"] >= 1.0 else None
    return sound


def _drift(lat: Any, lon: Any, times: list[str] | None) -> dict[str, Any] | None:
    la0, lo0 = math.radians(lat[0]), math.radians(lon[0])
    la, lo = np.radians(lat), np.radians(lon)
    dx = (lo - lo0) * np.cos(la0) * 6371.0088
    dy = (la - la0) * 6371.0088
    dist = np.hypot(dx, dy)
    k = int(np.nanargmax(dist))
    minutes = float("nan")
    if times:
        try:
            ts = [datetime.strptime(t, "%H:%M:%S") for t in times if t]
            span = (ts[-1] - ts[0]).total_seconds()
            minutes = (span % 86400) / 60.0
        except (ValueError, IndexError):
            minutes = float("nan")
    return {
        "max_km": float(dist[k]),
        "towards_deg": float((math.degrees(math.atan2(dx[k], dy[k])) + 360.0) % 360.0),
        "duration_min": minutes if math.isfinite(minutes) else 0.0,
        "end_latitude": float(lat[-1]),
        "end_longitude": float(lon[-1]),
    }


def parse_igra2(path: Path, file: str = "") -> Sounding:
    """IGRA2 station file (needs radarx); the first sounding in the file."""
    try:
        from radarx.io import sounding as rs
    except ImportError as exc:
        raise RadarxMissing("Reading IGRA2 files needs radarx: pip install radarx") from exc
    ds = rs.open_sounding_file(path, format="igra2")
    return from_radarx_profile(ds, file=file, fmt="igra2")


def from_radarx_profile(ds: Any, *, file: str = "", fmt: str = "") -> Sounding:
    """Wrap a radarx profile Dataset (``read_sounding`` / ``open_sounding_file``)."""
    p = np.asarray(ds["pressure"].values, dtype=float)
    built = build_profile(
        pressure_pa=p,
        height_m=np.asarray(ds["height"].values, dtype=float),
        temperature_k=np.asarray(ds["temperature"].values, dtype=float),
        dewpoint_k=np.asarray(ds["dewpoint"].values, dtype=float),
        u=np.asarray(ds["u"].values, dtype=float) if "u" in ds else None,
        v=np.asarray(ds["v"].values, dtype=float) if "v" in ds else None,
        time=str(ds["time"].values)[:19] if "time" in ds.coords else "",
    )
    attrs = ds.attrs
    declared = str(attrs.get("data_class", attrs.get("kind", ""))).lower()
    data_class = (
        "model"
        if declared in ("model", "model-analysis", "reanalysis")
        else classify_source(attrs.get("source", ""), attrs.get("title", ""))
    )
    return _finish(
        built,
        data_class=data_class,
        station=str(attrs.get("station", "")),
        station_name=str(attrs.get("station_name", "")),
        latitude=_float(ds["latitude"].values) if "latitude" in ds.coords else None,
        longitude=_float(ds["longitude"].values) if "longitude" in ds.coords else None,
        launch_time=str(attrs.get("launch_time", "")).replace("T", " ")[:19].replace(" ", "T") + "Z"
        if attrs.get("launch_time")
        else "",
        source=str(attrs.get("source", "radarx sounding")),
        fmt=fmt,
        file=file,
    )


def detect_format(text: str) -> str:
    head = text.lstrip()[:4000]
    if head.startswith("{"):
        return "iem_json"
    if "%RAW%" in text:
        return "sharppy"
    if re.search(r"\bPRES\b.*\bHGHT\b.*\bTEMP\b", text[:6000]):
        return "wyoming_text"
    if re.match(r"#\w{11} \d{4} ", head):
        return "igra2"
    return "csv"


def read_sounding_file(
    path: str | Path, fmt: str = "auto", wind_units: str | None = None
) -> Sounding:
    """Read a local sounding file. ``fmt`` is ``auto`` or one of
    ``wyoming_text``, ``sharppy``, ``iem_json``, ``csv``, ``igra2``."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise SoundingError(f"{p.name or p}: file not found")
    if p.stat().st_size > MAX_FILE_BYTES:
        raise SoundingError(
            f"{p.name}: larger than {MAX_FILE_BYTES // (1024 * 1024)} MB, too big for one sounding "
            "(an IGRA2 station file holds the whole record: cut out one sounding first)"
        )
    text = p.read_text(errors="replace")
    if "\x00" in text[:4096] or text.startswith("PK"):
        raise SoundingError(
            f"{p.name} is a binary file (a spreadsheet or archive?), not a text sounding; "
            "export it as CSV first"
        )
    fmt = detect_format(text) if fmt == "auto" else fmt
    name = p.name
    if fmt == "wyoming_text":
        return parse_wyoming_text(text, name)
    if fmt == "sharppy":
        return parse_sharppy(text, name)
    if fmt == "iem_json":
        return parse_iem_json(text, name)
    if fmt == "csv":
        return parse_csv(text, name, wind_units)
    if fmt == "igra2":
        return parse_igra2(p, name)
    raise SoundingError(f"unknown sounding format {fmt!r}")


def read_sounding_text(
    text: str, fmt: str = "auto", file: str = "", wind_units: str | None = None
) -> Sounding:
    """Like :func:`read_sounding_file` for text already in memory (used for fetched files)."""
    fmt = detect_format(text) if fmt == "auto" else fmt
    if fmt == "wyoming_text":
        return parse_wyoming_text(text, file)
    if fmt == "sharppy":
        return parse_sharppy(text, file)
    if fmt == "iem_json":
        return parse_iem_json(text, file)
    if fmt == "csv":
        return parse_csv(text, file, wind_units)
    raise SoundingError(f"unknown sounding format {fmt!r}")


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

DEFINITIONS: dict[str, str] = {
    "cape": "CAPE (J/kg): energy a lifted parcel gains from buoyancy between its level of free "
    "convection and the equilibrium level, integral of g (Tv,parcel - Tv,env)/Tv,env dz with the "
    "virtual-temperature correction (Doswell & Rasmussen 1994). Pseudo-adiabatic ascent, no "
    "condensate loading, thermodynamics of Bolton (1980) via radarx.",
    "cin": "CIN (J/kg): the same integral over the negative buoyancy from the parcel's start to "
    "its level of free convection (zero or negative: more negative = stronger cap).",
    "lcl": "LCL (m AGL): lifting condensation level, where the parcel first saturates "
    "(Bolton 1980, eq. 15).",
    "lfc": "LFC (m AGL): level of free convection, the first level at or above the LCL where the "
    "parcel is warmer than its surroundings. Empty when the parcel never becomes buoyant.",
    "el": "EL (m AGL): equilibrium level, the highest level where the parcel's buoyancy returns "
    "to zero above the LFC (the top of the sounding when still buoyant there: CAPE is then "
    "cut off).",
    "lifted_index": "Lifted index (K): environment minus parcel temperature at 500 hPa "
    "(Galway 1956); negative = unstable.",
    "shear": "Bulk shear (m/s): magnitude of the vector difference between the wind at the top and "
    "at the bottom of the layer (AGL), winds interpolated linearly in height.",
    "srh": "Storm-relative helicity (m2/s2): integral of (V - C) . vorticity over the layer "
    "(Davies-Jones et al. 1990), computed layer by layer on the sounding's own levels, with C the "
    "Bunkers (2000) storm motion named in the entry. Positive for right-turning hodographs "
    "in the Northern Hemisphere.",
    "bunkers": "Bunkers et al. (2000) internal-dynamics storm motion: the 0-6 km mean wind "
    "(height-weighted here) displaced by 7.5 m/s perpendicular to the 0-6 km shear vector "
    "(0-0.5 km to 5.5-6 km mean winds), to the right (supercell right mover) or left.",
    "freezing": "Freezing level (m AGL): height where the temperature falls through 0 degC "
    "(lowest crossing above the ground; linear interpolation between levels).",
    "wet_bulb_zero": "Wet-bulb-zero height (m AGL): where the isobaric wet-bulb temperature falls "
    "through 0 degC (lowest crossing). Not the pseudo-adiabatic wet bulb of Davies-Jones (2008).",
    "pw": "Precipitable water (mm): (1/g) times the integral of the specific humidity over "
    "pressure "
    "from the surface to the top level with a reported humidity.",
    "lapse_rate": "Environmental lapse rate (K/km): -(dT/dz) averaged over the layer (positive "
    "= temperature falls with height, the normal atmosphere). Dry adiabatic rate = 9.8 K/km; "
    "average moist adiabatic rate ≈ 6.5 K/km. Super-adiabatic (> 9.8) is absolutely unstable. "
    "Layers are 0-3 km (low-level convective) and 700-500 hPa (mid-level conditional instability).",
}

REFERENCES = {
    "bolton": "Bolton, D., 1980: The computation of equivalent potential temperature. "
    "Mon. Wea. Rev., 108, 1046-1053, doi:10.1175/1520-0493(1980)108<1046:TCOEPT>2.0.CO;2",
    "doswell": "Doswell, C. A., and E. N. Rasmussen, 1994: The effect of neglecting the virtual "
    "temperature correction on CAPE calculations. Wea. Forecasting, 9, 625-629, "
    "doi:10.1175/1520-0434(1994)009<0625:TEONTV>2.0.CO;2",
    "galway": "Galway, J. G., 1956: The lifted index as a predictor of latent instability. "
    "Bull. Amer. Meteor. Soc., 37, 528-529, doi:10.1175/1520-0477-37.10.528",
    "bunkers": "Bunkers, M. J., et al., 2000: Predicting supercell motion using a new hodograph "
    "technique. Wea. Forecasting, 15, 61-79, doi:10.1175/1520-0434(2000)015<0061:PSMUAN>2.0.CO;2",
    "daviesjones": "Davies-Jones, R., D. Burgess, and M. Foster, 1990: Test of helicity as a "
    "tornado forecast parameter. Preprints, 16th Conf. Severe Local Storms, Kananaskis Park, "
    "Alberta, Amer. Meteor. Soc., 588-592.",
    "wind_profile": "Shear, Bunkers storm motion and storm-relative helicity: "
    "radarx.retrieve.wind_profile (https://pypi.org/project/radarx/); "
    "methods are height-weighted means (Bunkers et al. 2000) and the discrete SRH sum "
    "of Davies-Jones (1984), exact for linear winds between levels.",
}

PARCELS = {
    "surface-based": "Surface-based: the lowest level of the sounding",
    "mixed-layer": "Mixed-layer: mean potential temperature and mixing ratio of the lowest 100 hPa",
    "most-unstable": "Most-unstable: the level of greatest equivalent potential temperature in "
    "the lowest 300 hPa",
}


def _num(x: Any, digits: int = 1) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, digits) + 0.0 if math.isfinite(v) else None  # + 0.0: no "-0"


def _interp_wind(z: Any, u: Any, v: Any, zq: float) -> tuple[float, float] | None:
    ok = np.isfinite(u) & np.isfinite(v) & np.isfinite(z)
    if ok.sum() < 2:
        return None
    zz, uu, vv = z[ok], u[ok], v[ok]
    if zq < zz[0] - 1e-6 or zq > zz[-1] + 1e-6:
        return None
    return float(np.interp(zq, zz, uu)), float(np.interp(zq, zz, vv))


def _layer_mean_wind(z: Any, u: Any, v: Any, z0: float, z1: float) -> tuple[float, float] | None:
    """Height-weighted mean wind over [z0, z1] (m MSL): trapezoid on the profile + end points."""
    a, b = _interp_wind(z, u, v, z0), _interp_wind(z, u, v, z1)
    if a is None or b is None:
        return None
    ok = np.isfinite(u) & np.isfinite(v) & np.isfinite(z)
    inner = ok & (z > z0) & (z < z1)
    zs = np.concatenate([[z0], z[inner], [z1]])
    us = np.concatenate([[a[0]], u[inner], [b[0]]])
    vs = np.concatenate([[a[1]], v[inner], [b[1]]])
    depth = z1 - z0
    return float(np.trapezoid(us, zs) / depth), float(np.trapezoid(vs, zs) / depth)


def bulk_shear(
    z: Any, u: Any, v: Any, ground: float, bottom: float, top: float
) -> dict[str, Any] | None:
    """Bulk shear vector between *bottom* and *top* (m AGL)."""
    lo, hi = _interp_wind(z, u, v, ground + bottom), _interp_wind(z, u, v, ground + top)
    if lo is None or hi is None:
        return None
    du, dv = hi[0] - lo[0], hi[1] - lo[1]
    spd = math.hypot(du, dv)
    return {
        "u": _num(du, 2),
        "v": _num(dv, 2),
        "speed_ms": _num(spd, 1),
        "speed_kt": _num(spd / KNOT, 0),
    }


def bunkers_motion(z: Any, u: Any, v: Any, ground: float) -> dict[str, Any] | None:
    """Bunkers et al. (2000) ID-method storm motion (right, left) and the 0-6 km mean wind."""
    mean = _layer_mean_wind(z, u, v, ground, ground + 6000.0)
    low = _layer_mean_wind(z, u, v, ground, ground + 500.0)
    high = _layer_mean_wind(z, u, v, ground + 5500.0, ground + 6000.0)
    if mean is None or low is None or high is None:
        return None
    sx, sy = high[0] - low[0], high[1] - low[1]
    mag = math.hypot(sx, sy)
    if mag < 1e-6:
        return None  # no shear: motion is the mean wind, deviation undefined
    d = 7.5
    rx, ry = mean[0] + d * sy / mag, mean[1] - d * sx / mag
    lx, ly = mean[0] - d * sy / mag, mean[1] + d * sx / mag

    def vec(a: float, b: float) -> dict[str, Any]:
        spd = math.hypot(a, b)
        return {
            "u": _num(a, 2),
            "v": _num(b, 2),
            "speed_ms": _num(spd, 1),
            "speed_kt": _num(spd / KNOT, 0),
            "from_deg": _num((math.degrees(math.atan2(-a, -b)) + 360.0) % 360.0, 0),
        }

    return {
        "mean_wind_0_6km": vec(*mean),
        "right_mover": vec(rx, ry),
        "left_mover": vec(lx, ly),
        "shear_0_6km_vector": {"u": _num(sx, 2), "v": _num(sy, 2)},
    }


def storm_relative_helicity(
    z: Any, u: Any, v: Any, ground: float, top: float, cx: float, cy: float
) -> float | None:
    """SRH (m2/s2) over 0..*top* m AGL for storm motion (cx, cy) (Davies-Jones et al. 1990)."""
    z0, z1 = ground, ground + top
    a, b = _interp_wind(z, u, v, z0), _interp_wind(z, u, v, z1)
    if a is None or b is None:
        return None
    ok = np.isfinite(u) & np.isfinite(v) & np.isfinite(z)
    inner = ok & (z > z0) & (z < z1)
    us = np.concatenate([[a[0]], u[inner], [b[0]]])
    vs = np.concatenate([[a[1]], v[inner], [b[1]]])
    total = 0.0
    for i in range(us.size - 1):
        total += (us[i + 1] - cx) * (vs[i] - cy) - (us[i] - cx) * (vs[i + 1] - cy)
    return float(total)


def precipitable_water_mm(p_pa: Any, td_k: Any, reported: Any) -> tuple[float, float] | None:
    """PW (mm) and the top pressure (hPa) of the integral; only levels with a reported dew point."""
    from radarx.io import sounding as rs

    ok = np.isfinite(reported) & np.isfinite(p_pa)
    if ok.sum() < 2:
        return None
    import xarray as xr

    q = np.asarray(
        rs.specific_humidity_from_dewpoint(xr.DataArray(td_k[ok]), xr.DataArray(p_pa[ok])).values,
        dtype=float,
    )
    pp = p_pa[ok]
    order = np.argsort(-pp)  # surface first
    pp, q = pp[order], q[order]
    # trapezoid in pressure, surface to top: PW = (1/g) integral q dp
    pw = float(np.sum(0.5 * (q[1:] + q[:-1]) * (pp[:-1] - pp[1:])) / G0)
    return pw, float(pp[-1] / 100.0)


def lapse_rate(z: Any, t: Any, ground: float, bottom: float, top: float) -> float | None:
    """Environmental lapse rate (K/km) in a layer defined by AGL heights *bottom*-*top*.

    Positive when temperature falls with height (the normal atmosphere, up to ~9.8 K/km dry
    adiabatic). The endpoints are linearly interpolated; ``None`` when the layer is not fully
    spanned by the valid temperature levels.
    """
    ok = np.isfinite(t) & np.isfinite(z)
    if ok.sum() < 2:
        return None
    zz, tt = z[ok], t[ok]
    z0, z1 = ground + bottom, ground + top
    if z0 < zz[0] - 1e-6 or z1 > zz[-1] + 1e-6:
        return None
    dz = top - bottom
    if dz == 0:
        return None
    t0 = float(np.interp(z0, zz, tt))
    t1 = float(np.interp(z1, zz, tt))
    return -(t1 - t0) / dz * 1000.0  # K/km, positive = falling with height


def _lapse_rate_pressure(z: Any, t: Any, p: Any, p_top_pa: float, p_bot_pa: float) -> float | None:
    """Lapse rate (K/km) between two pressure levels, by interpolation in log-pressure."""
    ok = np.isfinite(t) & np.isfinite(z) & np.isfinite(p) & (p > 0)
    if ok.sum() < 2:
        return None
    pp, zz, tt = p[ok], z[ok], t[ok]
    if p_bot_pa > pp[0] + 1e-3 or p_top_pa < pp[-1] - 1e-3:
        return None  # layer not covered
    lp = -np.log(pp)
    lp_bot, lp_top = -math.log(p_bot_pa), -math.log(p_top_pa)
    if lp_bot < lp[0] - 1e-9 or lp_top > lp[-1] + 1e-9:
        return None
    t_bot = float(np.interp(lp_bot, lp, tt))
    t_top = float(np.interp(lp_top, lp, tt))
    z_bot = float(np.interp(lp_bot, lp, zz))
    z_top = float(np.interp(lp_top, lp, zz))
    dz = z_top - z_bot
    if dz < 1.0:
        return None
    return -(t_top - t_bot) / dz * 1000.0  # K/km


def _wind_diag_radarx(
    ds: Any, ground: float, u: Any, v: Any, z: Any
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    """Shear, Bunkers storm motion and SRH using radarx.retrieve.wind_profile when available.

    Returns ``(shear_dict, storm_motion_dict_or_None, srh_dict)``.
    Falls back to the local implementations when radarx is not importable or raises.
    """
    try:
        from radarx.retrieve import wind_profile as wp

        def _vec(r: Any) -> dict[str, Any]:
            spd = float(r["speed"].values)
            u_v = float(r["u"].values)
            v_v = float(r["v"].values)
            return {
                "u": _num(u_v, 2),
                "v": _num(v_v, 2),
                "speed_ms": _num(spd, 1),
                "speed_kt": _num(spd / KNOT, 0),
                "from_deg": _num(float(r["direction"].values), 0),
            }

        shear: dict[str, Any] = {}
        for label, top_m in (("0-1km", 1000.0), ("0-3km", 3000.0), ("0-6km", 6000.0)):
            try:
                r = wp.bulk_shear(ds, bottom=0.0, top=top_m, ground=ground)
                shear[label] = {
                    "u": _num(float(r["shear_u"].values), 2),
                    "v": _num(float(r["shear_v"].values), 2),
                    "speed_ms": _num(float(r["shear_speed"].values), 1),
                    "speed_kt": _num(float(r["shear_speed"].values) / KNOT, 0),
                }
            except Exception:
                shear[label] = bulk_shear(z, u, v, ground, 0.0, top_m)

        storm: dict[str, Any] | None = None
        try:
            rm = wp.bunkers_storm_motion(ds, mover="right", ground=ground)
            lm = wp.bunkers_storm_motion(ds, mover="left", ground=ground)
            mn = wp.bunkers_storm_motion(ds, mover="mean", ground=ground)
            sh6 = wp.bulk_shear(ds, bottom=0.0, top=6000.0, ground=ground)
            storm = {
                "mean_wind_0_6km": _vec(mn),
                "right_mover": _vec(rm),
                "left_mover": _vec(lm),
                "shear_0_6km_vector": {
                    "u": _num(float(sh6["shear_u"].values), 2),
                    "v": _num(float(sh6["shear_v"].values), 2),
                },
            }
        except Exception:
            storm = bunkers_motion(z, u, v, ground)

        srh: dict[str, Any] = {}
        if storm:
            for side in ("right_mover", "left_mover"):
                c = storm[side]
                for top_m, key in ((1000.0, "0-1km"), (3000.0, "0-3km")):
                    try:
                        cx = ds["u"] * 0 + (c["u"] or 0.0)
                        cy = ds["v"] * 0 + (c["v"] or 0.0)
                        import xarray as xr

                        motion_ds = xr.Dataset({"u": cx, "v": cy})
                        val_da = wp.storm_relative_helicity(
                            ds, storm_motion=motion_ds, bottom=0.0, top=top_m, ground=ground
                        )
                        srh.setdefault(key, {})[side] = _num(float(val_da.values), 0)
                    except Exception:
                        val = storm_relative_helicity(
                            z, u, v, ground, top_m, c["u"] or 0.0, c["v"] or 0.0
                        )
                        srh.setdefault(key, {})[side] = _num(val, 0)

        return shear, storm, srh

    except ImportError:
        # radarx.retrieve not available: use local implementations
        shear = {
            "0-1km": bulk_shear(z, u, v, ground, 0.0, 1000.0),
            "0-3km": bulk_shear(z, u, v, ground, 0.0, 3000.0),
            "0-6km": bulk_shear(z, u, v, ground, 0.0, 6000.0),
        }
        storm = bunkers_motion(z, u, v, ground)
        srh = {}
        if storm:
            for side in ("right_mover", "left_mover"):
                c = storm[side]
                for top_m, key in ((1000.0, "0-1km"), (3000.0, "0-3km")):
                    val = storm_relative_helicity(z, u, v, ground, top_m, c["u"], c["v"])
                    srh.setdefault(key, {})[side] = _num(val, 0)
        return shear, storm, srh


def _need_radarx() -> Any:
    try:
        from radarx.io import parcel, sounding
    except ImportError as exc:
        raise RadarxMissing(
            "Parcel thermodynamics need radarx (Python >= 3.12): pip install radarx"
        ) from exc
    return parcel, sounding


def diagnose(snd: Sounding, *, virtual: bool = True) -> dict[str, Any]:
    """Thermodynamic and kinematic parameters of *snd*, each labelled (see ``definitions``).

    Heights are metres above the launch site (AGL) unless a key says ``_msl``. A value that cannot
    be computed from this sounding (no winds, no crossing, no buoyancy) is ``None``.
    """
    parcel_mod, sounding_mod = _need_radarx()
    ds = snd.profile
    z = np.asarray(ds["height"].values, dtype=float)
    ground = float(z[0])
    p = np.asarray(ds["pressure"].values, dtype=float)
    t = np.asarray(ds["temperature"].values, dtype=float)
    td = np.asarray(ds["dewpoint"].values, dtype=float)
    u = np.asarray(ds["u"].values, dtype=float)
    v = np.asarray(ds["v"].values, dtype=float)
    reported = np.asarray(ds["dewpoint_reported"].values, dtype=float)
    notes = list(snd.notes)
    if z.size < 40:
        notes.append(
            f"Only {z.size} levels (mandatory / significant levels): CAPE, CIN, helicity and "
            "precipitable water depend on the vertical resolution and are approximate."
        )

    out: dict[str, Any] = {
        "station": snd.station,
        "launch_time": snd.launch_time,
        "drift_note": snd.drift_note(),
        "surface": {
            "pressure_hpa": _num(p[0] / 100.0, 1),
            "temperature_c": _num(t[0] - T0, 1),
            "dewpoint_c": _num(td[0] - T0, 1),
            "height_msl_m": _num(ground, 0),
        },
    }

    # -- parcels --------------------------------------------------------------------------------
    parcels: dict[str, Any] = {}
    for name, label in PARCELS.items():
        try:
            r = parcel_mod.parcel_diagnostics(ds, name, virtual=virtual)
        except ValueError as exc:
            parcels[name] = {"parcel": name, "label": label, "error": str(exc)}
            continue

        def agl(key: str, r: Any = r) -> float | None:
            val = _num(r[key].values)
            return None if val is None else round(val - ground, 0)

        parcels[name] = {
            "parcel": name,
            "label": label,
            "cape": _num(r["cape"].values, 0),
            "cin": _num(r["cin"].values, 0),
            "lcl_agl_m": agl("lcl_height"),
            "lfc_agl_m": agl("lfc_height"),
            "el_agl_m": agl("el_height"),
            "lcl_hpa": _num(float(r["lcl_pressure"].values) / 100.0, 0),
            "lfc_hpa": _num(float(r["lfc_pressure"].values) / 100.0, 0),
            "el_hpa": _num(float(r["el_pressure"].values) / 100.0, 0),
            "lifted_index": _num(r["lifted_index"].values, 1),
            "start": {
                "pressure_hpa": _num(float(r["parcel_pressure"].values) / 100.0, 1),
                "temperature_c": _num(float(r["parcel_temperature"].values) - T0, 1),
                "dewpoint_c": _num(float(r["parcel_dewpoint"].values) - T0, 1),
            },
        }
        cape = parcels[name]["cape"]
        if parcels[name]["lfc_agl_m"] is None and cape == 0:
            parcels[name]["note"] = "No level of free convection: the parcel never becomes buoyant."
        el_msl = _num(r["el_height"].values)
        if el_msl is not None and el_msl >= float(z[-1]) - 1.0 and cape:
            parcels[name]["el_at_top"] = True
            parcels[name]["note"] = (
                "The parcel is still buoyant at the top of the sounding: the equilibrium level "
                "shown is the top and CAPE is cut off there (a lower bound)."
            )
    out["parcels"] = parcels
    out["virtual_temperature"] = bool(virtual)

    # -- winds (radarx.retrieve.wind_profile when available, local fallback) --------------------
    has_wind = int(np.isfinite(u).sum()) >= 2
    if has_wind:
        top_valid = float(z[np.isfinite(u) & np.isfinite(v)][-1])
        shear_d, storm, srh_d = _wind_diag_radarx(ds, ground, u, v, z)
        out["shear"] = shear_d
        out["storm_motion"] = storm
        out["srh"] = srh_d
        if top_valid < ground + 6000.0:
            notes.append(
                f"Winds end at {top_valid - ground:.0f} m AGL: 0-6 km shear and the Bunkers storm "
                "motion are not computed."
            )
    else:
        out["shear"] = out["storm_motion"] = out["srh"] = None
        notes.append("This file has no winds: shear, storm motion and helicity are not computed.")

    # -- levels --------------------------------------------------------------------------------
    levels: dict[str, Any] = {}
    for key, kelvin in (("freezing", T0), ("minus10", T0 - 10.0), ("minus20", T0 - 20.0)):
        h = sounding_mod.isotherm_height(ds, kelvin, which="lowest")
        val = _num(h.values)
        levels[f"{key}_agl_m"] = None if val is None else round(val - ground, 0)
    try:
        wb = sounding_mod.wet_bulb_zero_height(ds, which="lowest")
        wbv = _num(wb.values)
        levels["wet_bulb_zero_agl_m"] = None if wbv is None else round(wbv - ground, 0)
    except Exception as exc:  # the kernel needs a complete humidity column
        levels["wet_bulb_zero_agl_m"] = None
        notes.append(f"Wet-bulb-zero height not computed: {exc}")
    if levels["freezing_agl_m"] is None:
        notes.append(
            "No 0 degC crossing above the lowest level: the whole profile is on one side of "
            "freezing, so there is no freezing level to report."
        )
    out["levels"] = levels

    # -- lapse rates -----------------------------------------------------------------------------
    lr: dict[str, Any] = {}
    lr["0-3km_k_per_km"] = _num(lapse_rate(z, t, ground, 0.0, 3000.0), 2)
    lr["700_500hPa_k_per_km"] = _num(
        _lapse_rate_pressure(z, t, p, 50000.0, 70000.0),
        2,  # 700-500 hPa, Pa
    )
    dry_adiabatic = 9.8  # K/km
    for key, val in lr.items():
        if val is not None and val > dry_adiabatic:
            notes.append(
                f"Layer {key.replace('_k_per_km', '')} lapse rate {val:.1f} K/km is "
                f"super-adiabatic (> {dry_adiabatic} K/km: absolutely unstable)."
            )
    out["lapse_rates"] = lr

    # -- moisture --------------------------------------------------------------------------------
    pw = precipitable_water_mm(p, td, reported)
    out["moisture"] = (
        {"precipitable_water_mm": _num(pw[0], 1), "pw_top_hpa": _num(pw[1], 0)} if pw else None
    )
    if pw and pw[1] > 400.0:
        notes.append(
            f"Humidity is reported only up to {pw[1]:.0f} hPa: precipitable water is a lower "
            "bound (the layer above is not counted)."
        )
    out["notes"] = notes
    out["definitions"] = dict(DEFINITIONS)
    out["parcel_definitions"] = dict(PARCELS)
    out["references"] = dict(REFERENCES)
    out["units"] = {"height": "m AGL (above the launch site)", "pressure": "hPa", "wind": "m/s"}
    out["library"] = {"radarx": provenance.radarx_version() or "unknown"}
    out["provenance"] = snd.layer_provenance().to_dict()
    return out


def weisman_klemp(
    qv_cap: float = 0.014, top_m: float = 20000.0, dz: float = 50.0, hodograph_radius: float = 15.0
) -> Sounding:
    """The analytic thermodynamic profile of Weisman and Klemp (1982) with an illustrative wind.

    ``theta(z) = 300 + 43 (z/12 km)^(5/4)`` K below the tropopause (12 km, 343 K, 213 K) and
    ``343 exp[g (z - 12 km) / (cp 213 K)]`` above; relative humidity ``1 - 0.75 (z/12 km)^(5/4)``
    (0.25 above 12 km); water-vapour mixing ratio limited to *qv_cap* (14 g/kg in the standard
    case); surface pressure 1000 hPa; hydrostatic pressure including the virtual-temperature
    effect. The wind is **not** part of that paper: a quarter-circle hodograph of the given radius
    over the lowest 6 km, constant above, is added so the kinematic diagnostics can be shown.
    It is a made-up (simulated) sounding for documentation, tests and the demo; never real data.

    Weisman, M. L., and J. B. Klemp, 1982: The dependence of numerically simulated convective
    storms on vertical wind shear and buoyancy. Mon. Wea. Rev., 110, 504-520,
    doi:10.1175/1520-0493(1982)110<0504:TDONSC>2.0.CO;2
    """
    cp, rd, eps = 1005.7, 287.04, 0.622
    z = np.arange(0.0, top_m + dz, dz)
    ztr, theta0, thetatr, ttr = 12000.0, 300.0, 343.0, 213.0
    low = z <= ztr
    theta = np.where(
        low,
        theta0 + (thetatr - theta0) * (np.minimum(z, ztr) / ztr) ** 1.25,
        thetatr * np.exp(G0 / (cp * ttr) * (z - ztr)),
    )
    rh = np.where(low, 1.0 - 0.75 * (np.minimum(z, ztr) / ztr) ** 1.25, 0.25)
    p = np.full(z.size, 100000.0)
    qv = np.zeros(z.size)
    for _ in range(4):  # fixed point: p -> T -> qv -> theta_v -> p
        exner = (p / 100000.0) ** (rd / cp)
        t = theta * exner
        tc = t - T0
        es = 611.2 * np.exp(17.67 * tc / (tc + 243.5))
        e = rh * es
        qv = np.minimum(eps * e / (p - e), qv_cap)
        thv = theta * (1.0 + 0.61 * qv)
        pi = np.empty(z.size)
        pi[0] = 1.0
        for k in range(1, z.size):
            pi[k] = pi[k - 1] - G0 / (cp * 0.5 * (thv[k] + thv[k - 1])) * dz
        p = 100000.0 * pi ** (cp / rd)
    t = theta * (p / 100000.0) ** (rd / cp)
    e = qv * p / (eps + qv)
    ln_e = np.log(e / 611.2)
    td = T0 + 243.5 * ln_e / (17.67 - ln_e)
    td = np.minimum(td, t)
    ang = (np.pi / 2.0) * np.minimum(z, 6000.0) / 6000.0
    u = hodograph_radius * (1.0 - np.cos(ang))
    v = hodograph_radius * np.sin(ang)
    built = build_profile(
        pressure_pa=p,
        height_m=z,
        temperature_k=t,
        dewpoint_k=td,
        u=u,
        v=v,
        time="2000-01-01T00:00:00Z",
    )
    return _finish(
        built,
        station="SYNTHETIC",
        station_name="Weisman-Klemp (1982) analytic sounding",
        launch_time="",
        source="Synthetic: Weisman and Klemp (1982) thermodynamic profile, illustrative winds",
        fmt="synthetic",
        file="weisman_klemp",
    )


def synthetic_provenance() -> layer_provenance.LayerProvenance:
    return layer_provenance.LayerProvenance(
        kind="simulated",
        source="Synthetic sounding (Weisman and Klemp 1982 thermodynamics, illustrative winds)",
        title="Example sounding",
        method="Analytic profile, not a measurement; for demonstration and testing only.",
        references=[
            "Weisman, M. L., and J. B. Klemp, 1982: Mon. Wea. Rev., 110, 504-520, "
            "doi:10.1175/1520-0493(1982)110<0504:TDONSC>2.0.CO;2"
        ],
    )


def analyse(snd: Sounding) -> dict[str, Any]:
    """Everything the Skew-T panel and ``rxd sounding`` show for *snd*."""
    result = snd.to_dict()
    if snd.fmt == "synthetic":
        result["provenance"] = synthetic_provenance().to_dict()
    result["diagnostics"] = diagnose(snd)
    result["diagnostics"]["provenance"] = result["provenance"]
    return result


def analyse_file(
    path: str | Path, fmt: str = "auto", wind_units: str | None = None
) -> dict[str, Any]:
    """Read a sounding file and compute everything the Skew-T panel and ``rxd sounding`` show."""
    return analyse(read_sounding_file(path, fmt, wind_units))


def summary_lines(diag: Mapping[str, Any]) -> list[str]:
    """Plain-text lines of the main numbers (the ``rxd sounding`` table)."""
    lines: list[str] = []
    st = diag.get("surface") or {}
    lines.append(
        f"surface: {st.get('pressure_hpa')} hPa  {st.get('temperature_c')} degC  "
        f"Td {st.get('dewpoint_c')} degC  ({st.get('height_msl_m')} m above sea level)"
    )
    for name, p in (diag.get("parcels") or {}).items():
        if "error" in p:
            lines.append(f"{name}: {p['error']}")
            continue
        lines.append(
            f"{name:<14} CAPE {_fmt(p['cape'], 'J/kg')}  CIN {_fmt(p['cin'], 'J/kg')}  "
            f"LCL {_fmt(p['lcl_agl_m'], 'm AGL')}  LFC {_fmt(p['lfc_agl_m'], 'm AGL')}  "
            f"EL {_fmt(p['el_agl_m'], 'm AGL')}  LI {_fmt(p['lifted_index'], 'K')}"
        )
    for key, s in (diag.get("shear") or {}).items():
        lines.append(f"bulk shear {key:<5} {_fmt(s and s['speed_ms'], 'm/s')}")
    sm = diag.get("storm_motion")
    if sm:
        r = sm["right_mover"]
        lines.append(
            f"Bunkers right mover: from {r['from_deg']:.0f} deg at {r['speed_ms']} m/s "
            f"(u {r['u']}, v {r['v']} m/s)"
        )
    for key, s in (diag.get("srh") or {}).items():
        lines.append(
            f"SRH {key:<5} right {_fmt(s['right_mover'], 'm2/s2')}  "
            f"left {_fmt(s['left_mover'], 'm2/s2')}"
        )
    lv = diag.get("levels") or {}
    lines.append(
        f"freezing level {_fmt(lv.get('freezing_agl_m'), 'm AGL')}   "
        f"wet-bulb zero {_fmt(lv.get('wet_bulb_zero_agl_m'), 'm AGL')}   "
        f"-20 degC {_fmt(lv.get('minus20_agl_m'), 'm AGL')}"
    )
    lr = diag.get("lapse_rates") or {}
    if any(v is not None for v in lr.values()):
        lines.append(
            f"lapse rate 0-3km {_fmt(lr.get('0-3km_k_per_km'), 'K/km')}   "
            f"700-500hPa {_fmt(lr.get('700_500hPa_k_per_km'), 'K/km')}"
        )
    mo = diag.get("moisture")
    if mo:
        lines.append(
            f"precipitable water {_fmt(mo['precipitable_water_mm'], 'mm')} "
            f"(to {mo['pw_top_hpa']} hPa)"
        )
    return lines


def _fmt(value: Any, unit: str) -> str:
    return "n/a" if value is None else f"{value:g} {unit}"

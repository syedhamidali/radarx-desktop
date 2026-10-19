"""Radiosondes of the PERiLS collection around the window: readers, common CSV, diagnostics, drift.

Readers (all OBSERVED, one launch each): UIUC FARM mobile sondes (EOL sounding format, 1 s data with
position), UAH and ULM sondes (text with position and time per level), operational WYO (CSV, mandatory
and significant levels, no per-level time or position). The profile is built with the engine's
``build_profile`` (cleaning: levels without T or p dropped, sorted by height, nothing smoothed) and
the diagnostics are the engine's own ``sounding.diagnose`` (radarx parcel functions, Bunkers 2000,
SRH after Davies-Jones et al. 1990), so they are the same numbers as in the app's Skew-T panel.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import thermo

UTC = timezone.utc
KT = 0.514444


@dataclass
class Sonde:
    id: str
    site: str
    source: str  # instrument / network
    launch: datetime
    lat: float
    lon: float
    t_s: np.ndarray  # seconds since launch per level
    p_hpa: np.ndarray
    z_asl_m: np.ndarray  # geometric height ASL where known (else NaN)
    t_c: np.ndarray
    td_c: np.ndarray
    wspd_ms: np.ndarray
    wdir_deg: np.ndarray
    lat_lv: np.ndarray
    lon_lv: np.ndarray
    station_elev_m: float | None = None
    notes: list[str] = field(default_factory=list)
    credit: str = ""
    file: str = ""
    z_is_geopotential: bool = False


def _f(s: str) -> float:
    try:
        v = float(s)
    except ValueError:
        return float("nan")
    return float("nan") if v <= -998.0 else v


def read_uiuc_eol(path: str | Path) -> Sonde:
    """UIUC FARM mobile radiosonde, EOL sounding format 1.1 (GRAW DFM-09), 1 s resolution."""
    path = Path(path)
    lines = path.read_text(errors="replace").splitlines()
    meta = {}
    for ln in lines[:12]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            meta[k.strip()] = v.strip()
    m = re.search(
        r"(\d{4}),\s*(\d{2}),\s*(\d{2}),\s*(\d{2}):(\d{2}):(\d{2})",
        meta["UTC Launch Time (y,m,d,h,m,s)"],
    )
    launch = datetime(*map(int, m.groups()), tzinfo=UTC)
    site = meta.get("Launch Site", path.parent.parent.parent.name)
    rows = []
    for ln in lines:
        p = ln.split()
        if len(p) >= 15 and re.match(r"^-?\d+\.\d+$", p[0]):
            rows.append(p)
    # columns: 0 t, 1-3 hh mm ss, 4 P, 5 T, 6 Td, 7 RH, 8 U, 9 V, 10 Wspd, 11 Dir, 12 dZ, 13 GeoPoAlt, 14 Lon, 15 Lat, 16 GPSAlt
    rows17 = [r for r in rows if len(r) >= 17]
    a = np.array([[_f(x) for x in r[:17]] for r in rows17])
    t_s = (a[:, 1] * 3600 + a[:, 2] * 60 + a[:, 3]) - (
        launch.hour * 3600 + launch.minute * 60 + launch.second
    )
    t_s = np.where(t_s < -3600, t_s + 86400, t_s)
    gps = a[:, 16]
    geo = a[:, 13]
    z = np.where(np.isfinite(gps), gps, geo)
    keep = t_s >= 0
    lon0 = float(np.nanmedian(a[keep, 14][:5]))
    lat0 = float(np.nanmedian(a[keep, 15][:5]))
    return Sonde(
        id=f"UIUC_{site}_{launch:%Y%m%d_%H%M}",
        site=site,
        source="UIUC FARM mobile radiosonde (GRAW DFM-09)",
        launch=launch,
        lat=lat0,
        lon=lon0,
        t_s=t_s[keep],
        p_hpa=a[keep, 4],
        z_asl_m=z[keep],
        t_c=a[keep, 5],
        td_c=a[keep, 6],
        wspd_ms=a[keep, 10],
        wdir_deg=a[keep, 11],
        lat_lv=a[keep, 15],
        lon_lv=a[keep, 14],
        notes=["GPS height used where present (else geopotential height)."],
        credit="UIUC FARM (PERiLS 2022), quality controlled level 1",
        file=path.name,
    )


def read_uah_ulm(path: str | Path) -> Sonde:
    """UAH or ULM text sounding: lat, lon, UTC, height AGL, pressure, T, RH, Td, wind speed (kts UAH / m s-1 ULM), dir."""
    path = Path(path)
    text = path.read_text(errors="replace").splitlines()
    head = [ln for ln in text if ln.startswith("#")]
    cols = next(ln for ln in text if "latitude" in ln.lower()).lstrip("# ").lower()
    uah = "UAH" in head[0]
    elev = (
        float(re.search(r"(\d+)\s*m\s*$", head[1].strip()).group(1))
        if re.search(r"(\d+)\s*m\s*$", head[1].strip())
        else None
    )
    d = re.search(r"(\d{8})", head[1]).group(1)
    stamp = re.search(r"\d{8},\s*(\d{4,6})", head[1]).group(1).ljust(6, "0")
    launch = datetime.strptime(d + stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    site = head[1].split("UTC,")[1].split(",")[0].strip()
    rows = []
    for ln in text:
        if ln.startswith("#") or ln.startswith("%") or not ln.strip() or "latitude" in ln.lower():
            continue
        p = ln.split(",")
        if len(p) >= 10 and re.match(r"^-?\d+\.\d+$", p[0].strip()):
            rows.append(p)
    wind_scale = KT if "kts" in cols else 1.0
    lat = np.array([_f(r[0]) for r in rows])
    lon = np.array([_f(r[1]) for r in rows])
    tt = []
    for r in rows:
        hh, mm, ss = (int(x) for x in r[2].strip().split(":"))
        tt.append(hh * 3600 + mm * 60 + ss)
    t_s = np.array(tt, float) - (launch.hour * 3600 + launch.minute * 60 + launch.second)
    t_s = np.where(t_s < -3600, t_s + 86400, t_s)
    hagl = np.array([_f(r[3]) for r in rows])
    return Sonde(
        id=f"{'UAH' if uah else 'ULM'}_{re.sub('[^A-Za-z0-9]+', '', site)}_{launch:%Y%m%d_%H%M}",
        site=site,
        source="UAH radiosonde (iMet)" if uah else "ULM radiosonde",
        launch=launch,
        lat=float(lat[0]),
        lon=float(lon[0]),
        t_s=t_s,
        p_hpa=np.array([_f(r[4]) for r in rows]),
        z_asl_m=hagl + (elev or 0.0),
        t_c=np.array([_f(r[5]) for r in rows]),
        td_c=np.array([_f(r[7]) for r in rows]),
        wspd_ms=np.array([_f(r[8]) for r in rows]) * wind_scale,
        wdir_deg=np.array([_f(r[9]) for r in rows]),
        lat_lv=lat,
        lon_lv=lon,
        station_elev_m=elev,
        notes=[
            f"Heights in the file are AGL; {elev:g} m (site elevation in the header) added."
            if elev
            else "Heights AGL, site elevation unknown."
        ],
        credit="UAH / NSSL / ULM PERiLS 2022 (NCAR/EOL)"
        if not uah
        else "UAH (PERiLS 2022, NCAR/EOL 610.012 family)",
        file=path.name,
    )


def read_wyo(path: str | Path, launch_hour_utc: float | None = None) -> Sonde:
    """Operational sounding as CSV (University of Wyoming archive layout): mandatory and significant levels."""
    path = Path(path)
    rows = list(csv.DictReader(path.open()))
    stn = rows[0]["station"]
    m = re.match(r"(\d{8})_(\d{2})(\d{2})(\d{2})_", path.name)
    nominal = datetime.strptime(m.group(1) + m.group(2) + m.group(3), "%Y%m%d%H%M").replace(
        tzinfo=UTC
    )
    g = lambda k: np.array([_f(r[k]) for r in rows])  # noqa: E731
    wspd = g("speed")
    return Sonde(
        id=f"WYO_{stn}_{nominal:%Y%m%d_%H%M}",
        site=stn,
        source="operational radiosonde (NWS)",
        launch=nominal,
        lat=float(rows[0]["latitude"]),
        lon=float(rows[0]["longitude"]),
        t_s=np.full(len(rows), np.nan),
        p_hpa=g("pressure"),
        z_asl_m=g("height"),
        t_c=g("temperature"),
        td_c=g("dewpoint"),
        wspd_ms=wspd * KT,  # the engine and Wyoming use knots: SKNT
        wdir_deg=g("direction"),
        lat_lv=np.full(len(rows), np.nan),
        lon_lv=np.full(len(rows), np.nan),
        station_elev_m=float(rows[0]["elevation"]),
        notes=[
            "Nominal synoptic time (the balloons leave about 45 min earlier); no per-level time or position (drift unknown).",
            "Wind speed in the file taken as knots (Wyoming/SPC convention).",
        ],
        credit="NWS operational radiosonde network (University of Wyoming archive CSV)",
        file=path.name,
        z_is_geopotential=True,
    )


# ---------------------------------------------------------------------------
# profile and diagnostics
# ---------------------------------------------------------------------------


def to_engine(snd: Sonde):
    """The engine's ``Sounding`` (cleaned profile) for ``snd``."""
    import engine.sounding as eng

    built = eng.build_profile(
        pressure_pa=snd.p_hpa * 100.0,
        temperature_k=snd.t_c + 273.15,
        dewpoint_k=snd.td_c + 273.15,
        height_m=None if snd.z_is_geopotential else snd.z_asl_m,
        geopotential_m=snd.z_asl_m if snd.z_is_geopotential else None,
        wind_dir_deg=snd.wdir_deg,
        wind_speed_ms=snd.wspd_ms,
        latitude=snd.lat,
        longitude=snd.lon,
        time=snd.launch.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    s = eng._finish(
        built,
        station=snd.site,
        launch_time=snd.launch.strftime("%Y-%m-%dT%H:%M:%SZ"),
        latitude=snd.lat,
        longitude=snd.lon,
        source=snd.source,
        fmt="perils",
        file=snd.file,
    )
    s.notes[:0] = list(snd.notes)
    return s


def diagnostics(snd: Sonde) -> dict[str, Any]:
    """The engine's diagnostics plus the -10 and -20 degC heights ASL and parcel paths for a Skew-T."""
    import engine.sounding as eng

    e = to_engine(snd)
    d = eng.diagnose(e)
    ds = e.profile
    h = np.asarray(ds["height"].values, float)
    t = np.asarray(ds["temperature"].values, float) - 273.15
    heights = {
        f"{k}_asl_m": _crossing(h, t, lv)
        for k, lv in (("freezing", 0.0), ("minus10", -10.0), ("minus20", -20.0))
    }
    d["isotherms_asl_m"] = heights
    d["top_height_asl_m"] = float(h[-1])
    d["n_levels"] = int(h.size)
    paths = {}
    for pname, pr in d["parcels"].items():
        st = pr.get("start")
        if st and st.get("pressure_hpa") is not None:
            ps, ts = thermo.parcel_path(
                st["pressure_hpa"],
                st["temperature_c"],
                st["dewpoint_c"],
                p_top_hpa=max(100.0, float(ds["pressure"].values[-1]) / 100.0),
            )
            paths[pname] = {
                "p_hpa": [round(float(x), 1) for x in ps[::2]],
                "t_c": [round(float(x), 2) for x in ts[::2]],
            }
    d["parcel_paths"] = paths
    d["parcel_path_note"] = (
        "Parcel paths for drawing: dry adiabat to the LCL then a liquid-only pseudo-adiabat (Bolton 1980 "
        "relations, RK4 in pressure). CAPE and CIN in 'parcels' are the radarx kernel's, not integrals of these paths."
    )
    return d


def _crossing(h: np.ndarray, t_c: np.ndarray, level: float) -> float | None:
    """Height (m ASL) of the lowest crossing of ``level`` (degC) by the profile, linear in height."""
    for i in range(len(h) - 1):
        a, b = t_c[i] - level, t_c[i + 1] - level
        if np.isfinite(a) and np.isfinite(b) and a * b <= 0 and a != b:
            return float(h[i] + (h[i + 1] - h[i]) * a / (a - b))
    return None


def common_csv(snd: Sonde, window_start: datetime) -> str:
    """Common CSV text: height ASL, pressure, T, Td, wind speed/direction, lat/lon, seconds since the window start."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(
        [
            "height_asl_m",
            "pressure_hpa",
            "temperature_c",
            "dewpoint_c",
            "wind_speed_ms",
            "wind_dir_deg",
            "lat",
            "lon",
            "t_s",
        ]
    )
    z = snd.z_asl_m
    order = np.argsort(np.where(np.isfinite(z), z, 1e9), kind="stable")
    off = (snd.launch - window_start).total_seconds()

    def fmt(x, d=2):
        return "" if not np.isfinite(x) else f"{x:.{d}f}"

    for i in order:
        if not np.isfinite(snd.p_hpa[i]) or not np.isfinite(snd.t_c[i]):
            continue
        w.writerow(
            [
                fmt(z[i], 1),
                fmt(snd.p_hpa[i], 2),
                fmt(snd.t_c[i]),
                fmt(snd.td_c[i]),
                fmt(snd.wspd_ms[i]),
                fmt(snd.wdir_deg[i], 0),
                fmt(snd.lat_lv[i], 5),
                fmt(snd.lon_lv[i], 5),
                fmt(snd.t_s[i] + off, 1),
            ]
        )
    return out.getvalue()


def environment_profile(snd: Sonde) -> tuple[np.ndarray, np.ndarray, float | None, float | None]:
    """(height ASL m, temperature K, freezing level m ASL, -20 degC level m ASL) of the cleaned profile."""
    ds = to_engine(snd).profile
    h = np.asarray(ds["height"].values, float)
    t = np.asarray(ds["temperature"].values, float)
    return h, t, _crossing(h, t - 273.15, 0.0), _crossing(h, t - 273.15, -20.0)


def drift(snd: Sonde, window_start: datetime, every: int = 15) -> list[list[float]]:
    """``[[t_s, lat, lon, alt_m], ...]`` (t_s since the window start; every ``every``-th level)."""
    off = (snd.launch - window_start).total_seconds()
    ok = np.isfinite(snd.lat_lv) & np.isfinite(snd.lon_lv) & np.isfinite(snd.t_s)
    idx = np.nonzero(ok)[0][::every]
    return [
        [
            round(float(snd.t_s[i] + off), 1),
            round(float(snd.lat_lv[i]), 5),
            round(float(snd.lon_lv[i]), 5),
            round(float(snd.z_asl_m[i]), 1) if np.isfinite(snd.z_asl_m[i]) else None,
        ]
        for i in idx
    ]

"""Surface stations and disdrometers (OBSERVED) for ``stations/stations.json`` and ``stations/<id>_dsd.npz``.

Sources read here (all from disk, read only):

* **PIPS** (Purdue/Penn State probes, 4 units in IOP2): 10 s netCDF with the Parsivel size-velocity matrix,
  the quality-controlled DSD (``ND_qc``), Dm, rain rate and reflectivity derived by the owner, wind,
  temperature, dew point, pressure.
* **StickNet** (TTU, 24 probes): 10 s CSV (T, RH, P, wind speed, gust, direction); dew point and
  theta-e derived here (``thermo.py``).
* **MAPNet / RaDAPS** surface (Vaisala WXT, 1 Hz text) incl. its rain and hail sensor.
* **Mobile mesonets** (DOW7, DOW8, UIUC FARM SCOUT1-3): 1 Hz CSV with position; wind corrected for vehicle motion
  by the owners (1-min and 3-s columns).
* **UIUC FARM Parsivels** on the scouts: rain rate and reflectivity (the spectra are not used).

Every station carries its ``credit``, ``qc`` notes, and which quantities are original and which derived.
"""

from __future__ import annotations

import csv
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import thermo

UTC = timezone.utc
MARGIN_S = (
    300.0  # data this far before and after the window are kept (context for the rain / wind change)
)
LARGE_D_MM = 8.0  # a drop this big does not survive (break-up); particles >= this are hail / graupel candidates


def _clean(a, nd=3):
    out = []
    for v in np.asarray(a, float):
        out.append(None if not math.isfinite(v) else round(float(v), nd))
    return out


def _series(t_s, **cols) -> dict[str, list]:
    s: dict[str, list] = {"t_s": [round(float(x), 2) for x in t_s]}
    for k, (vals, nd) in cols.items():
        a = np.asarray(vals, float)
        if np.isfinite(a).any():
            s[k] = _clean(a, nd)
    return s


def _epoch_s(times) -> np.ndarray:
    """Seconds since 1970 (UTC) of a pandas datetime Series, whatever its resolution."""
    import pandas as pd

    t = pd.to_datetime(times)
    if getattr(t.dt, "tz", None) is not None:
        t = t.dt.tz_convert("UTC").dt.tz_localize(None)
    return ((t - pd.Timestamp("1970-01-01")) / pd.Timedelta(seconds=1)).to_numpy(float)


def despike(a: np.ndarray, threshold: float, size: int = 7) -> tuple[np.ndarray, int]:
    """Values further than ``threshold`` from the running median (``size`` samples) become NaN: single-sample
    dropouts of the humidity, temperature or pressure sensors (they otherwise make spikes in the dew point and
    theta-e). Returns the cleaned array and the number of samples removed."""
    from scipy import ndimage

    a = np.asarray(a, float)
    ok = np.isfinite(a)
    if ok.sum() < size:
        return a, 0
    filled = np.where(ok, a, np.nanmedian(a))
    med = ndimage.median_filter(filled, size=size, mode="nearest")
    bad = ok & (np.abs(a - med) > threshold)
    out = a.copy()
    out[bad] = np.nan
    return out, int(bad.sum())


def _window_mask(t_abs: np.ndarray, start: datetime, end: datetime) -> np.ndarray:
    s = start.timestamp() - MARGIN_S
    e = end.timestamp() + MARGIN_S
    return (t_abs >= s) & (t_abs <= e)


def dsd_moments(nd_m3mm: np.ndarray, d_mm: np.ndarray, dd_mm: np.ndarray) -> dict[str, np.ndarray]:
    """Dm (mm), log10 Nw (mm-1 m-3), LWC (g m-3) and Dmax (mm) from N(D) (m-3 mm-1) on bins (centre, width).

    Moments ``M_n = sum N(D) D^n dD``; ``Dm = M4 / M3``; ``Nw = (4^4 / 6) M3^5 / M4^4`` (Testud et al. 2001,
    with M_n in mm^n m-3); ``LWC = (pi / 6) 1e-3 M3`` g m-3.
    """
    nd = np.asarray(nd_m3mm, float)
    nd = np.where(np.isfinite(nd), nd, 0.0)
    m3 = (nd * d_mm**3 * dd_mm).sum(axis=-1)
    m4 = (nd * d_mm**4 * dd_mm).sum(axis=-1)
    with np.errstate(invalid="ignore", divide="ignore"):
        dm = np.where(m3 > 0, m4 / m3, np.nan)
        nw = np.where(m3 > 0, (4.0**4 / 6.0) * m3**5 / m4**4, np.nan)
        lw = np.pi / 6.0 * 1e-3 * m3
    dmax = np.full(nd.shape[:-1], np.nan)
    for i in np.ndindex(*nd.shape[:-1]):
        nz = np.nonzero(nd[i] > 0)[0]
        if nz.size:
            dmax[i] = d_mm[nz[-1]]
    return {
        "dm_mm": dm,
        "log10_nw": np.where(np.isfinite(nw) & (nw > 0), np.log10(np.maximum(nw, 1e-30)), np.nan),
        "lwc_gm3": lw,
        "dmax_mm": dmax,
    }


def raindrop_fall_speed(d_mm):
    """Terrain-free fall speed of a raindrop (m/s), Atlas et al. (1973): 9.65 - 10.3 exp(-0.6 D)."""
    return 9.65 - 10.3 * np.exp(-0.6 * np.asarray(d_mm, float))


# ---------------------------------------------------------------------------
# PIPS
# ---------------------------------------------------------------------------


def read_pips(
    path10: str | Path, start: datetime, end: datetime, out_dir: Path | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One PIPS: the station record and its hail candidate events. Writes ``<id>_dsd.npz`` if ``out_dir``."""
    import netCDF4

    path10 = Path(path10)
    with netCDF4.Dataset(path10) as ds:
        pid = ds.getncattr("probe_name")
        loc = eval_loc(ds.getncattr("location"))
        t0 = datetime.strptime(ds.getncattr("starting_time"), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        tt = np.asarray(ds["time"][:], float)
        t_abs = t0.timestamp() + tt
        m = _window_mask(t_abs, start, end)
        idx = np.nonzero(m)[0]
        if idx.size == 0:
            raise ValueError(f"{pid}: no data in the window")
        sl = slice(int(idx[0]), int(idx[-1]) + 1)

        def g(name):
            if name not in ds.variables:
                return np.full(idx.size, np.nan)
            return np.ma.filled(np.ma.asarray(ds[name][sl]).astype(float), np.nan)

        t_s = t_abs[sl] - start.timestamp()
        diam = np.asarray(ds["diameter"][:], float)
        dlo, dhi = (
            np.asarray(ds["min_diameter"][:], float),
            np.asarray(ds["max_diameter"][:], float),
        )
        fall = np.asarray(ds["fallspeed"][:], float)
        nd = g("ND_qc")
        vd = np.nan_to_num(
            np.ma.filled(np.ma.asarray(ds["VD_matrix_qc"][sl]).astype(float), np.nan)
        )  # (t, fall, diam) counts
        mom = dsd_moments(np.nan_to_num(nd), diam, dhi - dlo)
        rr = g("rainrate_derived_qc")
        rr_raw = g("precipintensity")
        dbz = g("reflectivity_derived_qc")
        dm43 = g("Dm43_qc") * 1000.0
        d0 = g("D0_qc") * 1000.0
        temp = g("fasttemp")
        td = g("dewpoint")
        p = g("pressure")
        rec = {
            "id": pid,
            "name": f"{pid} (Purdue/Penn State Probe for Interactive Precipitation Studies)",
            "type": "PIPS: Parsivel2 disdrometer + surface meteorology",
            "lat": loc[0],
            "lon": loc[1],
            "alt_m": round(loc[2], 1),
            "class": "observed",
            "instrument": "Parsivel2 (OTT) in a PIPS housing, 10 s samples; Dm, rain rate, reflectivity from the owner's QC'd DSD",
            "series": _series(
                t_s,
                rain_rate_mmh=(rr, 2),
                rain_rate_parsivel_mmh=(rr_raw, 2),
                dbz=(dbz, 2),
                dm_mm=(dm43, 3),
                d0_mm=(d0, 3),
                log10_nw=(mom["log10_nw"], 3),
                lwc_gm3=(mom["lwc_gm3"], 4),
                dmax_mm=(mom["dmax_mm"], 2),
                wind_speed_ms=(g("windspd"), 2),
                wind_gust_ms=(g("windgust"), 2),
                wind_dir_deg=(g("winddirabs"), 0),
                temperature_c=(temp, 2),
                dewpoint_c=(td, 2),
                relative_humidity_pct=(g("RH"), 1),
                pressure_hpa=(p, 2),
                theta_e_k=(thermo.theta_e_k(temp, td, p), 2),
                mixing_ratio_gkg=(thermo.mixing_ratio(td, p), 2),
            ),
            "dsd": None,
            "credit": "PIPS data: Purdue / Penn State (PERiLS 2022); DSD retrievals by the data owner (Syed, UAH)",
            "qc": (
                "Parsivel QC of the owner (strong wind, splashing, margin, rain-only options in the file attributes: "
                f"{ds.getncattr('strongwindQC')}, {ds.getncattr('splashingQC')}, {ds.getncattr('marginQC')}, "
                f"{ds.getncattr('rainonlyQC')}); the DSD and Dm are the QC'd ones (ND_qc); log10 Nw, LWC, Dmax derived here from ND_qc."
            ),
            "moving": False,
        }
        # -- size-velocity evidence of large particles -----------------------------------------------
        events: list[dict[str, Any]] = []
        big = diam >= LARGE_D_MM
        counts_big = vd[:, :, big].sum(axis=(1, 2))
        for k in range(vd.shape[0]):
            if counts_big[k] >= 2:
                sub = vd[k][:, big]
                fi, di = np.nonzero(sub > 0)
                if fi.size:
                    w = sub[fi, di]
                    dmean = float(np.average(diam[big][di], weights=w))
                    vmean = float(np.average(fall[fi], weights=w))
                    dmax = float(diam[big][di].max())
                    events.append(
                        {
                            "station": pid,
                            "t_s": round(float(t_s[k]), 1),
                            "kind": "hail_candidate",
                            "max_d_mm": round(dmax, 1),
                            "n_large": int(counts_big[k]),
                            "mean_d_large_mm": round(dmean, 1),
                            "mean_fall_ms_large": round(vmean, 1),
                            "raindrop_fall_ms_expected": round(
                                float(raindrop_fall_speed(dmean)), 1
                            ),
                            "evidence": (
                                f"{int(counts_big[k])} particles of {LARGE_D_MM:g} mm or more in 10 s (largest {dmax:g} mm); "
                                "raindrops this large break up, so they are hail or graupel candidates (or splash / wind artefacts)"
                            ),
                        }
                    )
        rec["large_particles"] = {
            "threshold_mm": LARGE_D_MM,
            "counts_per_10s": [int(x) for x in counts_big],
            "max_diameter_in_window_mm": float(np.nanmax(mom["dmax_mm"]))
            if np.isfinite(mom["dmax_mm"]).any()
            else None,
        }
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(
                out_dir / f"{pid}_dsd.npz",
                t_s=t_s,
                diameter_mm=diam,
                diameter_min_mm=dlo,
                diameter_max_mm=dhi,
                fallspeed_ms=fall,
                nd_m3mm=np.nan_to_num(nd).astype(np.float32),
                counts=np.nan_to_num(vd).astype(np.float32),
                rain_rate_mmh=rr,
                dm_mm=dm43,
                note=np.array(
                    "nd_m3mm = ND_qc (m-3 mm-1, QC'd by the owner); counts (t, fall-speed bin, diameter bin) = VD_matrix_qc"
                ),
            )
            rec["dsd"] = f"stations/{pid}_dsd.npz"
    return rec, events


def eval_loc(text: str) -> tuple[float, float, float]:
    m = re.findall(r"-?\d+\.\d+", text)
    return float(m[0]), float(m[1]), float(m[2])


# ---------------------------------------------------------------------------
# StickNet
# ---------------------------------------------------------------------------


def read_sticknet_locations(path: str | Path) -> dict[str, tuple[float, float, float]]:
    out = {}
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            out[r["ID"].zfill(5)] = (
                float(r["Latitude"]),
                float(r["Longitude"]),
                float(r["Elevation"]),
            )
    return out


def read_sticknet(
    path: str | Path, loc: tuple[float, float, float], start: datetime, end: datetime
) -> dict[str, Any] | None:
    import pandas as pd

    d = pd.read_csv(path, parse_dates=["Time"])
    t_abs = _epoch_s(d["Time"])
    m = _window_mask(t_abs, start, end)
    if not m.any():
        return None
    d = d[m]
    t_s = t_abs[m] - start.timestamp()
    T, n_t = despike(d["T"].to_numpy(float), 3.0)
    rh, n_rh = despike(d["RH"].to_numpy(float), 12.0)
    P, n_p = despike(d["P"].to_numpy(float), 1.5)
    td = thermo.dewpoint_c(T, rh)
    sid = Path(path).name.split("_")[1]
    rec = {
        "id": f"StickNet_{sid}",
        "name": f"TTU StickNet {sid}",
        "type": "StickNet surface station (about 2 m)",
        "lat": loc[0],
        "lon": loc[1],
        "alt_m": loc[2],
        "class": "observed",
        "instrument": "Texas Tech University StickNet (temperature, relative humidity, pressure, wind speed / direction, 10 s)",
        "series": _series(
            t_s,
            temperature_c=(T, 2),
            relative_humidity_pct=(rh, 1),
            dewpoint_c=(td, 2),
            pressure_hpa=(P, 2),
            wind_speed_ms=(d["WS"].to_numpy(float), 2),
            wind_gust_ms=(
                d["WSMAX"].to_numpy(float) if "WSMAX" in d else np.full(len(d), np.nan),
                2,
            ),
            wind_dir_deg=(d["WD"].to_numpy(float), 0),
            theta_e_k=(thermo.theta_e_k(T, td, P), 2),
            mixing_ratio_gkg=(thermo.mixing_ratio(td, P), 2),
        ),
        "credit": "TTU StickNet (PERiLS 2022, NCAR/EOL 610.028), 10 s file prepared by the data owner; dew point and theta-e derived here (Bolton 1980)",
        "qc": (
            "As delivered (level 2/3 products are not used: this is the 10 s file); single-sample dropouts removed "
            f"(running median of 7 samples; T > 3 K, RH > 12 %, p > 1.5 hPa away): {n_t} T, {n_rh} RH, {n_p} p samples; "
            "WSMAX = 10 s maximum wind (gust) where the file has it"
        ),
        "moving": False,
    }
    return rec


# ---------------------------------------------------------------------------
# RaDAPS / MAPNet WXT
# ---------------------------------------------------------------------------

_WXT = re.compile(r"(\w{2})=([-+]?\d+\.?\d*)([A-Z#]?)")


def read_wxt(
    path: str | Path, loc: tuple[float, float, float], start: datetime, end: datetime
) -> dict[str, Any] | None:
    """UAH MAPNet RaDAPS surface (Vaisala WXT 1 Hz): ``2022,090,000000, 1R0,Dm=086D,Sm=0.4M,Ta=63.1F,...``."""
    rows = []
    for ln in Path(path).read_text(errors="replace").splitlines():
        p = ln.split(",", 3)
        if len(p) < 4 or not p[0].strip().isdigit():
            continue
        y, doy, hms = int(p[0]), int(p[1]), p[2].strip()
        t = datetime(y, 1, 1, tzinfo=UTC) + timedelta(
            days=doy - 1, hours=int(hms[:2]), minutes=int(hms[2:4]), seconds=int(hms[4:6])
        )
        kv = {m.group(1): (float(m.group(2)), m.group(3)) for m in _WXT.finditer(p[3])}
        rows.append((t.timestamp(), kv))
    if not rows:
        return None
    t_abs = np.array([r[0] for r in rows])
    m = _window_mask(t_abs, start, end)
    if not m.any():
        return None
    sel = [rows[i] for i in np.nonzero(m)[0]]
    t_s = np.array([r[0] for r in sel]) - start.timestamp()

    def col(key, f=lambda x: x):
        return np.array([f(r[1][key][0]) if key in r[1] else np.nan for r in sel])

    Tc = col("Ta", lambda f: (f - 32.0) * 5.0 / 9.0)
    rh = col("Ua")
    P = col("Pa")
    td = thermo.dewpoint_c(Tc, rh)
    rec = {
        "id": "RaDAPS_WXT",
        "name": "UAH MAPNet RaDAPS surface station (Vaisala WXT)",
        "type": "mobile profiling site (fixed during IOP2): surface weather + hail / rain sensor",
        "lat": loc[0],
        "lon": loc[1],
        "alt_m": loc[2],
        "class": "observed",
        "instrument": "Vaisala WXT536 (1 Hz) with piezo rain and hail sensor",
        "series": _series(
            t_s,
            temperature_c=(Tc, 2),
            relative_humidity_pct=(rh, 1),
            dewpoint_c=(td, 2),
            pressure_hpa=(P, 2),
            wind_speed_ms=(col("Sm"), 2),
            wind_dir_deg=(col("Dm"), 0),
            rain_rate_mmh=(col("Ri"), 2),
            hail_intensity_hits_cm2h=(col("Hi"), 2),
            theta_e_k=(thermo.theta_e_k(Tc, td, P), 2),
        ),
        "credit": "UAH MAPNet RaDAPS surface data (PERiLS 2022, NCAR/EOL 610.017)",
        "qc": "As delivered; temperature converted from degF; position from the field log book (33.595558 N, 88.987904 W)",
        "moving": False,
    }
    return rec


# ---------------------------------------------------------------------------
# mobile mesonets and FARM disdrometers
# ---------------------------------------------------------------------------


def read_mesonet(
    path: str | Path, platform: str, start: datetime, end: datetime
) -> dict[str, Any] | None:
    """UIUC FARM mobile mesonet (L1 QC csv, 1 Hz) with position."""
    import pandas as pd

    lines = Path(path).read_text(errors="replace").splitlines()
    h = next(i for i, ln in enumerate(lines) if ln.startswith("Time,"))
    d = pd.read_csv(
        path, skiprows=[*list(range(0, h)), h + 1], names=None, header=0, na_values=["NaN", "nan"]
    )
    d["Time"] = pd.to_datetime(d["Time"])
    t_abs = _epoch_s(d["Time"])
    m = _window_mask(t_abs, start, end)
    if not m.any():
        return None
    d = d[m]
    t_s = t_abs[m] - start.timestamp()
    T, _ = despike(d["Temperature"].to_numpy(float), 3.0, 31)
    rh, _ = despike(d["Relative_Humidity"].to_numpy(float), 12.0, 31)
    P, _ = despike(
        d["Corr_Pressure"].to_numpy(float)
        if "Corr_Pressure" in d
        else d["Pressure"].to_numpy(float),
        1.5,
        31,
    )
    td = thermo.dewpoint_c(T, rh)
    lat = d["Latitude"].to_numpy(float)
    lon = d["Longitude"].to_numpy(float)
    moving = (
        float(
            np.nanmax(np.hypot((lat - np.nanmedian(lat)) * 111.2, (lon - np.nanmedian(lon)) * 92.4))
        )
        > 1.0
    )
    ws = d["3-sec_Bld_Wnd_Spd_Corr"].to_numpy(float)
    wd = d["3-sec_Bld_Wnd_Dir_Corr"].to_numpy(float)
    rec = {
        "id": f"Mesonet_{platform}",
        "name": f"UIUC FARM mobile mesonet {platform}",
        "type": "mobile mesonet (vehicle mounted)",
        "lat": round(float(np.nanmedian(lat)), 5),
        "lon": round(float(np.nanmedian(lon)), 5),
        "alt_m": None,
        "class": "observed",
        "instrument": "UIUC FARM vehicle mesonet (1 Hz T, RH, P, wind with vehicle-motion correction by the owner)",
        "series": _series(
            t_s,
            temperature_c=(T, 2),
            relative_humidity_pct=(rh, 1),
            dewpoint_c=(td, 2),
            pressure_hpa=(P, 2),
            wind_speed_ms=(ws, 2),
            wind_dir_deg=(wd, 0),
            theta_e_k=(thermo.theta_e_k(T, td, P), 2),
            lat=(lat, 5),
            lon=(lon, 5),
        ),
        "credit": "UIUC FARM (PERiLS 2022), quality controlled level 1 mesonet data",
        "qc": "Level 1 QC by the owner; the 3-second corrected wind columns are used; position given as the median over the window",
        "moving": bool(moving),
    }
    return rec


def read_farm_disdrometer(
    path: str | Path, start: datetime, end: datetime, loc: tuple[float, float] | None
) -> dict[str, Any] | None:
    """UIUC FARM Parsivel (csv, 10 s): rain intensity and reflectivity only."""
    import pandas as pd

    d = pd.read_csv(path, skiprows=[1], na_values=["-9.999"])
    d.columns = [c.strip() for c in d.columns]
    tc = next(c for c in d.columns if c.startswith("Date/Time"))
    d[tc] = pd.to_datetime(d[tc].str.replace("UTC", "").str.strip())
    t_abs = _epoch_s(d[tc])
    m = _window_mask(t_abs, start, end)
    if not m.any():
        return None
    d = d[m]
    t_s = t_abs[m] - start.timestamp()
    name = str(d["Station Name"].iloc[0]).strip()
    rec = {
        "id": f"{name}",
        "name": f"UIUC FARM Parsivel {name}",
        "type": "Parsivel disdrometer on a FARM scout",
        "lat": loc[0] if loc else None,
        "lon": loc[1] if loc else None,
        "alt_m": None,
        "class": "observed",
        "instrument": "OTT Parsivel (10 s): rain intensity and radar reflectivity as delivered (spectra not used here)",
        "series": _series(
            t_s,
            rain_rate_mmh=(d["PARSIVEL Rain Intensity"].to_numpy(float), 2),
            dbz=(d["Reflectivity"].to_numpy(float), 2),
        ),
        "credit": "UIUC FARM (PERiLS 2022)",
        "qc": "As delivered (-9.999 = missing); position from the scout mesonet of the same vehicle",
        "moving": True,
    }
    return rec


def hourly_accumulation_mm(t_s: list[float], rr_mmh: list[float | None]) -> float | None:
    """Rain accumulated (mm) over the samples by integrating the rate (trapezoid over the sampled span)."""
    t = np.array(t_s, float)
    r = np.array([np.nan if v is None else v for v in rr_mmh], float)
    ok = np.isfinite(r)
    if ok.sum() < 2:
        return None
    return float(np.trapezoid(r[ok], t[ok]) / 3600.0)

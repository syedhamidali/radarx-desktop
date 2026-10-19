"""Rain-gauge ingestion, quality control, radar-gauge adjustment and validation.

Everything works on **local files**; nothing is downloaded (a data provider can be added later and
will feed the same :class:`GaugeSet`).

Pipeline
--------
1. :func:`read_gauges` reads CSV / JSON / GeoJSON / NOAA hourly (HPD) and daily (GHCN-Daily) CSV /
   raw METAR-ASOS text into a :class:`GaugeSet`: a station table and a table of records
   ``(station, time_end [UTC], interval_s, depth_mm)``. A record is the *depth accumulated over the
   interval ending at* ``time_end`` (the NOAA and ASOS convention); other conventions are converted
   on reading (``time_convention``). Local times are converted to UTC (``tz``).
2. :meth:`GaugeSet.quality_control` flags missing values, negative values, duplicates (identical
   ones are merged, conflicting ones are excluded), stuck gauges (a run of identical positive
   values) and implausible intensities. Flagged records are kept with their reason and never used.
3. :meth:`GaugeSet.totals` sums the valid records that lie wholly inside the radar period; a station
   whose records do not cover the period is not used (``min_coverage``).
4. :func:`pair_with_radar` samples the radar accumulation at each gauge (mean of the cells within
   ``radius_cells``) and :func:`adjust` applies a documented adjustment:

   ``mfb``      mean-field bias, ``F = sum(G) / sum(R)`` over gauges with rain (Smith &
                Krajewski 1991);
   ``brandes``  spatially varying: a Gaussian-weighted mean of ``ln(G/R)`` of the nearby gauges,
                shrunk towards the mean-field bias (in the spirit of Brandes 1975);
   ``ked``      kriging with external drift: the gauge field is interpolated with the radar
                accumulation as the drift (Wackernagel 1998; applied to radar-gauge merging by
                Goudenhoofd & Delobbe 2009), exponential variogram fitted to the residuals.

5. :func:`validate` gives bias, RMSE, correlation and the Kling-Gupta efficiency (Gupta et al.
   2009), overall and by amount class and by range from the radar, plus the scatter and residual
   map data. :func:`cross_validate` repeats it leaving each gauge out, which is the honest skill of
   an adjustment (the in-sample fit of a method that interpolates the gauges is meaningless).

An adjusted field is labelled ``ADJUSTED`` with the method, the gauge set and the parameters; the
unadjusted ``RETRIEVED`` field is never overwritten.
"""

from __future__ import annotations

import itertools
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from engine import provenance as prov
from engine.hydro.qpe import QpeError


class GaugeError(QpeError):
    """A user-facing problem with a gauge file or an adjustment."""


UNITS_TO_MM = {
    "mm": 1.0,
    "cm": 10.0,
    "m": 1000.0,
    "in": 25.4,
    "inch": 25.4,
    "inches": 25.4,
    "0.01in": 0.254,
    "hundredths_in": 0.254,
    "0.1mm": 0.1,
    "tenths_mm": 0.1,
}

#: sentinels used for "missing" in NOAA/COOP/ASOS files, in the unit of the file
MISSING_SENTINELS = (-9999.0, -999.0, 99999.0, 9999.0, 999.99, 999.9, 25399.75, 99.99)

_ALIASES = {
    "station": (
        "station",
        "station_id",
        "stationid",
        "id",
        "site",
        "site_no",
        "gauge",
        "stn",
        "sid",
    ),
    "name": ("name", "station_name", "stationname"),
    "lat": ("latitude", "lat", "y"),
    "lon": ("longitude", "lon", "lng", "long", "x"),
    "elev": ("elevation", "elev", "altitude", "elevation_m"),
    "time": (
        "time",
        "datetime",
        "date_time",
        "date",
        "valid",
        "timestamp",
        "utc_time",
        "obs_time",
        "observation_time",
        "time_end",
        "end",
    ),
    "value": (
        "precip",
        "prcp",
        "hpcp",
        "p01i",
        "precipitation",
        "rain",
        "rainfall",
        "value",
        "amount",
        "depth",
        "precip_mm",
        "rain_mm",
        "mm",
        "inches",
    ),
    "flag_m": ("measurement flag", "measurement_flag", "mflag"),
    "flag_q": ("quality flag", "quality_flag", "qflag"),
}


# ---------------------------------------------------------------------------
# The data model
# ---------------------------------------------------------------------------


@dataclass
class GaugeSet:
    """Stations and their records (depth in mm over the interval ending at ``time_end``)."""

    stations: Any  # pandas.DataFrame: station, name, lat, lon, elevation_m
    records: Any  # pandas.DataFrame: station, time_end, interval_s, depth_mm, flag
    source: str = ""
    notes: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.stations)

    # -- quality control --------------------------------------------------------------------
    def quality_control(
        self,
        *,
        stuck_n: int = 6,
        max_rate_mm_h: float = 300.0,
        max_depth_mm: float | None = None,
    ) -> GaugeSet:
        """Flag bad records (a copy). ``flag`` is empty for a good record, else the reason.

        Rules, in order: ``missing`` (NaN or a NOAA sentinel), ``negative``, ``duplicate``
        (identical value for the same station and time: one is kept), ``duplicate_conflict`` (same
        time, different values: all excluded), ``implausible`` (intensity above ``max_rate_mm_h``
        or depth above ``max_depth_mm``), ``stuck`` (``stuck_n`` or more identical positive values
        in a row at one station, usually a blocked or frozen gauge).
        """
        import pandas as pd

        rec = self.records.copy()
        rec["flag"] = rec["flag"].fillna("").astype(object)
        d = rec["depth_mm"]
        free = rec["flag"] == ""
        miss = d.isna() | d.isin(MISSING_SENTINELS)
        rec.loc[free & miss, "flag"] = "missing"
        free = rec["flag"] == ""
        rec.loc[free & (d < 0), "flag"] = "negative"
        free = rec["flag"] == ""
        # duplicates
        key = ["station", "time_end"]
        grp = rec[free].groupby(key)["depth_mm"]
        nun = grp.transform("nunique")
        cnt = grp.transform("count")
        idx = rec[free].index
        conflict = idx[(nun > 1).to_numpy()]
        rec.loc[conflict, "flag"] = "duplicate_conflict"
        free = rec["flag"] == ""
        dup = rec[free].duplicated(subset=key, keep="first")
        rec.loc[dup[dup].index, "flag"] = "duplicate"
        _ = cnt
        free = rec["flag"] == ""
        rate = rec["depth_mm"] / (rec["interval_s"] / 3600.0)
        bad = free & (rate > max_rate_mm_h)
        if max_depth_mm is not None:
            bad |= free & (rec["depth_mm"] > max_depth_mm)
        rec.loc[bad, "flag"] = "implausible"
        # stuck runs per station
        rec = rec.sort_values(["station", "time_end"], kind="stable").reset_index(drop=True)
        good = rec["flag"] == ""
        for _station, g in rec[good & (rec["depth_mm"] > 0)].groupby("station"):
            vals = g["depth_mm"].to_numpy()
            if vals.size < stuck_n:
                continue
            run_id = np.concatenate([[0], np.cumsum(vals[1:] != vals[:-1])])
            for rid in np.unique(run_id):
                where = np.flatnonzero(run_id == rid)
                if where.size >= stuck_n:
                    rec.loc[g.index[where], "flag"] = "stuck"
        return GaugeSet(self.stations, pd.DataFrame(rec), self.source, self.notes)

    def qc_summary(self) -> dict[str, int]:
        flags = self.records["flag"].fillna("")
        out = {"records": len(flags), "good": int((flags == "").sum())}
        for k, v in flags[flags != ""].value_counts().items():
            out[str(k)] = int(v)
        return out

    def valid(self) -> Any:
        return self.records[self.records["flag"].fillna("") == ""]

    # -- totals over a period -------------------------------------------------------------------
    def totals(
        self,
        start: Any,
        end: Any,
        *,
        min_coverage: float = 0.9,
        prorate: bool = False,
        tolerance_s: float = 0.0,
    ) -> Any:
        """Gauge accumulation (mm) over ``[start, end]`` per station, with its coverage.

        Only valid records whose whole interval lies inside the period are summed (``prorate=True``
        also takes the overlapping part of the records at the edges, assuming a uniform intensity
        inside the record - an assumption, not a measurement). A station is returned when the
        records cover at least ``min_coverage`` of the period; ``coverage`` says how much.

        ``tolerance_s`` widens the window on both sides: radar scans rarely start exactly on the
        hour of the gauge records, so a record that begins up to ``tolerance_s`` before
        ``start`` (or ends that long after ``end``) still counts. ``span_start`` / ``span_end``
        give the window actually summed, so the mismatch is never hidden.
        """
        import pandas as pd

        t0 = (
            pd.Timestamp(np.datetime64(start, "ns"))
            if not isinstance(start, pd.Timestamp)
            else start
        )
        t1 = pd.Timestamp(np.datetime64(end, "ns")) if not isinstance(end, pd.Timestamp) else end
        if t1 <= t0:
            raise GaugeError("the period for the gauge totals has no length")
        rec = self.valid().copy()
        rec["t_start"] = rec["time_end"] - pd.to_timedelta(rec["interval_s"], unit="s")
        rows = []
        period = (t1 - t0).total_seconds()
        tol = pd.Timedelta(seconds=float(tolerance_s))
        for sid, g in rec.groupby("station"):
            inside = g[(g["t_start"] >= t0 - tol) & (g["time_end"] <= t1 + tol)]
            depth = float(inside["depth_mm"].sum())
            covered = float(inside["interval_s"].sum())
            if prorate:
                edge = g[(g["time_end"] > t0) & (g["t_start"] < t1) & ~g.index.isin(inside.index)]
                for _, r in edge.iterrows():
                    ov = (min(r["time_end"], t1) - max(r["t_start"], t0)).total_seconds()
                    depth += r["depth_mm"] * ov / r["interval_s"]
                    covered += ov
            cov = min(covered / period, 1.0)
            if cov >= min_coverage and len(inside):
                rows.append(
                    {
                        "station": sid,
                        "depth_mm": depth,
                        "coverage": cov,
                        "n": len(inside),
                        "span_start": inside["t_start"].min(),
                        "span_end": inside["time_end"].max(),
                    }
                )
        out = pd.DataFrame(
            rows, columns=["station", "depth_mm", "coverage", "n", "span_start", "span_end"]
        )
        return out.merge(self.stations, on="station", how="left")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _find(columns: Sequence[str], key: str, mapping: dict[str, str] | None) -> str | None:
    if mapping and key in mapping:
        name = mapping[key]
        for c in columns:
            if c == name or c.lower() == str(name).lower():
                return c
        raise GaugeError(
            f"column {name!r} (for {key}) is not in the file; columns: {', '.join(columns)}"
        )
    low = {c.lower().strip(): c for c in columns}
    for alias in _ALIASES[key]:
        if alias in low:
            return low[alias]
    if key == "value":  # precip_in, rain_mm, prcp_tenths ... : a column that starts like one
        for name, col in low.items():
            if name.startswith(("precip", "prcp", "rain", "hpcp")):
                return col
    return None


def _units_from_name(column: str | None) -> str:
    """Unit hinted by a column name (``precip_in``, ``p01i`` -> in; ``rain_mm`` -> mm)."""
    name = (column or "").lower()
    if name == "inches":
        return "in"
    if name.endswith(("_in", "_inch", "_inches")) or name in ("p01i", "p05i"):
        return "in"
    if name.endswith("_cm"):
        return "cm"
    return "mm"


def _to_utc(series: Any, tz: str | None) -> Any:
    import pandas as pd

    s = pd.to_datetime(series, errors="coerce", utc=False, format="mixed")
    if getattr(s.dt, "tz", None) is not None:
        return s.dt.tz_convert("UTC").dt.tz_localize(None)
    if tz and tz.upper() not in ("UTC", "Z", "GMT"):
        try:
            return (
                s.dt.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
                .dt.tz_convert("UTC")
                .dt.tz_localize(None)
            )
        except Exception as exc:
            raise GaugeError(f"unknown time zone {tz!r}: {exc}") from exc
    return s


def _finish(
    df: Any,
    cols: dict[str, str | None],
    *,
    units: str,
    interval_s: float | None,
    time_convention: str,
    tz: str | None,
    stations_file: Any,
    source: str,
    notes: list[str],
    extra_flags: Any = None,
) -> GaugeSet:
    import pandas as pd

    if units not in UNITS_TO_MM:
        raise GaugeError(f"unknown units {units!r}; use one of {', '.join(UNITS_TO_MM)}")
    if cols["time"] is None or cols["value"] is None:
        raise GaugeError(
            "need a time column and a precipitation column; give them with the column mapping "
            f"(columns found: {', '.join(map(str, df.columns))})"
        )
    t = _to_utc(df[cols["time"]], tz)
    if t.isna().all():
        raise GaugeError(f"could not read any time from column {cols['time']!r}")
    sid = (
        df[cols["station"]].astype(str).str.strip()
        if cols["station"]
        else pd.Series("gauge", index=df.index)
    )
    val = pd.to_numeric(df[cols["value"]], errors="coerce")
    if val.notna().sum() == 0 and df[cols["value"]].astype(str).str.upper().eq("T").any():
        val = val.fillna(0.0)
    raw = df[cols["value"]].astype(str).str.strip().str.upper()
    trace = raw.eq("T")
    miss = val.isin(MISSING_SENTINELS)
    val = val.where(~miss)
    val = val.mask(trace, 0.0)  # trace (< 0.01 in): counted as zero, a stated convention
    if trace.any():
        notes.append(f"{int(trace.sum())} trace record(s) 'T' were taken as 0 mm")
    depth = val * UNITS_TO_MM[units]
    rec = pd.DataFrame({"station": sid, "time": t, "depth_mm": depth})
    rec["flag"] = ""
    if extra_flags is not None:
        rec.loc[extra_flags[extra_flags != ""].index.intersection(rec.index), "flag"] = extra_flags[
            extra_flags != ""
        ]
    rec = rec.dropna(subset=["time"])
    # interval
    if interval_s is None:
        gaps = (
            rec.sort_values(["station", "time"])
            .groupby("station")["time"]
            .diff()
            .dt.total_seconds()
        )
        med = float(gaps.dropna().median()) if gaps.notna().any() else 3600.0
        if not math.isfinite(med) or med <= 0:
            med = 3600.0
        interval_s = med
        notes.append(f"record interval inferred from the data: {interval_s / 60:.0f} min")
    rec["interval_s"] = float(interval_s)
    shift = {"end": 0.0, "start": float(interval_s), "centre": float(interval_s) / 2.0}
    if time_convention not in shift:
        raise GaugeError("time_convention must be 'end', 'start' or 'centre'")
    rec["time_end"] = rec["time"] + pd.to_timedelta(shift[time_convention], unit="s")
    rec = rec.drop(columns="time")
    # stations
    stations = _stations_from(df, cols, rec, stations_file)
    return GaugeSet(
        stations,
        rec[["station", "time_end", "interval_s", "depth_mm", "flag"]].reset_index(drop=True),
        source=source,
        notes=tuple(notes),
    )


def _stations_from(df: Any, cols: dict[str, str | None], rec: Any, stations_file: Any) -> Any:
    import pandas as pd

    if stations_file is not None:
        st = _read_station_table(stations_file)
    elif cols["lat"] and cols["lon"]:
        sid = (
            df[cols["station"]].astype(str).str.strip()
            if cols["station"]
            else pd.Series("gauge", index=df.index)
        )
        st = (
            pd.DataFrame(
                {
                    "station": sid,
                    "lat": pd.to_numeric(df[cols["lat"]], errors="coerce"),
                    "lon": pd.to_numeric(df[cols["lon"]], errors="coerce"),
                    "name": df[cols["name"]].astype(str) if cols["name"] else sid,
                    "elevation_m": pd.to_numeric(df[cols["elev"]], errors="coerce")
                    if cols["elev"]
                    else np.nan,
                }
            )
            .dropna(subset=["lat", "lon"])
            .drop_duplicates("station")
        )
    else:
        raise GaugeError(
            "the file has no station coordinates: add latitude/longitude columns or give a "
            "stations file (columns station, lat, lon)"
        )
    st = st[(st["lat"].between(-90, 90)) & (st["lon"].between(-180, 180))]
    missing = sorted(set(rec["station"]) - set(st["station"]))
    if missing:
        rec.loc[rec["station"].isin(missing), "flag"] = "no_location"
    return st.reset_index(drop=True)


def _read_station_table(path: Any) -> Any:
    import pandas as pd

    p = Path(path)
    if p.suffix.lower() in (".json", ".geojson"):
        doc = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and doc.get("type") == "FeatureCollection":
            rows = []
            for f in doc["features"]:
                c = f["geometry"]["coordinates"]
                pr = f.get("properties", {})
                rows.append(
                    {
                        "station": str(pr.get("station", pr.get("id", ""))),
                        "lon": c[0],
                        "lat": c[1],
                        "name": pr.get("name", ""),
                    }
                )
            df = pd.DataFrame(rows)
        else:
            df = pd.DataFrame(doc["stations"] if isinstance(doc, dict) else doc)
    else:
        df = pd.read_csv(p)
    cols = {k: _find(list(df.columns), k, None) for k in ("station", "name", "lat", "lon", "elev")}
    if not (cols["station"] and cols["lat"] and cols["lon"]):
        raise GaugeError(f"{p.name} needs columns station, latitude and longitude")
    return pd.DataFrame(
        {
            "station": df[cols["station"]].astype(str).str.strip(),
            "lat": pd.to_numeric(df[cols["lat"]], errors="coerce"),
            "lon": pd.to_numeric(df[cols["lon"]], errors="coerce"),
            "name": df[cols["name"]].astype(str)
            if cols["name"]
            else df[cols["station"]].astype(str),
            "elevation_m": pd.to_numeric(df[cols["elev"]], errors="coerce")
            if cols["elev"]
            else np.nan,
        }
    ).dropna(subset=["lat", "lon"])


def _noaa_measurement_flags(df: Any, col: str | None, tcol: str) -> tuple[Any, Any]:
    """HPD measurement flags: '[' ... ']' wrap a period whose total is on the ']' record."""
    import pandas as pd

    flags = pd.Series("", index=df.index, dtype=object)
    if col is None:
        return flags, None
    mf = df[col].fillna("").astype(str).str.strip()
    # '[' ... ']' (and '{' ... '}') wrap a period that is accumulated or deleted: the hours inside
    # have no value of their own and the total on the closing record spans several hours, so none
    # of these records is used as an hourly value
    flags[mf.isin(["[", "{"])] = "accumulating"
    flags[mf.isin(["]", "}"])] = "accumulated_total"
    return flags, mf


def read_gauges(
    path: str | Path,
    *,
    fmt: str = "auto",
    mapping: dict[str, str] | None = None,
    units: str | None = None,
    interval_s: float | None = None,
    time_convention: str = "end",
    tz: str | None = "UTC",
    stations: str | Path | None = None,
    year: int | None = None,
    month: int | None = None,
) -> GaugeSet:
    """Read gauge records from a local file.

    Parameters
    ----------
    fmt : {"auto", "csv", "json", "noaa_hourly", "noaa_daily", "metar"}
        ``noaa_hourly``: NCEI Hourly Precipitation Data (HPD) CSV (``STATION, ... DATE, HPCP``,
        inches, hourly totals ending at ``DATE``). ``noaa_daily``: GHCN-Daily CSV (``PRCP``, daily
        totals, mm for metric / inches for standard downloads). ``metar``: raw METAR/SPECI text,
        the ``Pnnnn`` group (hundredths of an inch since the previous hour) and a station table
        with coordinates (``stations=``); ``year`` and ``month`` complete the ``DDHHMMZ`` stamp.
    mapping : dict
        Column names for ``station``, ``lat``, ``lon``, ``time``, ``value``, ``name``, ``elev``.
    units : str
        Unit of the values (``mm``, ``in``, ``0.01in``, ``0.1mm`` ...). Default: ``in`` for
        ``noaa_hourly`` and ``metar``, ``mm`` otherwise.
    interval_s : float
        Accumulation interval of every record in seconds (default: inferred from the data;
        ``noaa_hourly`` 3600, ``noaa_daily`` 86400, ``metar`` 3600).
    time_convention : {"end", "start", "centre"}
        Whether the time stamp is the end, the start or the middle of the accumulation interval.
    tz : str
        Time zone of naive time stamps (``UTC`` or an IANA name like ``America/Chicago``).
    """
    import pandas as pd

    p = Path(path).expanduser()
    if not p.is_file():
        raise GaugeError(f"gauge file not found: {path}. Check the path.")
    suffix = p.suffix.lower()
    if fmt == "auto":
        if suffix in (".json", ".geojson"):
            fmt = "json"
        elif suffix in (".txt", ".metar", ".sao"):
            fmt = "metar"
        else:
            head = p.read_text(encoding="utf-8", errors="ignore")[:2000].upper()
            if "HPCP" in head:
                fmt = "noaa_hourly"
            elif "PRCP" in head and "STATION" in head and "ELEVATION" in head:
                fmt = "noaa_daily"
            else:
                fmt = "csv"
    notes: list[str] = []
    if fmt == "metar":
        return _read_metar(p, stations, year, month, units or "in", time_convention, notes)
    if fmt == "json":
        df = _json_to_frame(p)
    else:
        try:
            df = pd.read_csv(
                p, dtype=str, keep_default_na=False, na_values=[""], skipinitialspace=True
            )
        except Exception as exc:
            raise GaugeError(f"cannot read {p.name} as CSV: {exc}") from exc
    columns = list(df.columns)
    cols = {k: _find(columns, k, mapping) for k in _ALIASES}
    extra = None
    if fmt == "noaa_hourly":
        cols["value"] = cols["value"] or _find(columns, "value", {"value": "HPCP"})
        units = units or "in"
        interval_s = interval_s or 3600.0
        notes.append(
            f"NOAA hourly (HPD): values in {units}, each the total of the hour ending at DATE "
            "(UTC unless tz is given)"
        )
        extra, _ = _noaa_measurement_flags(df, cols["flag_m"], cols["time"] or "DATE")
        if cols["flag_q"]:
            qf = df[cols["flag_q"]].fillna("").astype(str).str.strip()
            extra = extra.where(qf.isin(["", "nan", "<NA>"]), "source_quality_flag")
    elif fmt == "noaa_daily":
        cols["value"] = cols["value"] or _find(columns, "value", {"value": "PRCP"})
        units = units or "mm"
        interval_s = interval_s or 86400.0
        if time_convention == "end":
            time_convention = "start"  # a DATE is the day, whose total ends 24 h later
        notes.append(
            "NOAA daily (GHCN-Daily): DATE is the observation day; the totals are not aligned to "
            "UTC (the observer's day, often 07:00 local); use them only for periods of whole days"
        )
    else:
        units = units or _units_from_name(cols["value"])
    gs = _finish(
        df,
        cols,
        units=units,
        interval_s=interval_s,
        time_convention=time_convention,
        tz=tz,
        stations_file=stations,
        source=p.name,
        notes=notes,
        extra_flags=extra,
    )
    return gs


def _json_to_frame(p: Path) -> Any:
    import pandas as pd

    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise GaugeError(f"{p.name} is not valid JSON: {exc}") from exc
    if isinstance(doc, dict) and doc.get("type") == "FeatureCollection":
        rows = []
        for f in doc["features"]:
            c = (f.get("geometry") or {}).get("coordinates") or [None, None]
            rows.append({**(f.get("properties") or {}), "longitude": c[0], "latitude": c[1]})
        return pd.DataFrame(rows).astype(object)
    if isinstance(doc, dict) and "observations" in doc:
        obs = pd.DataFrame(doc["observations"])
        st = pd.DataFrame(doc.get("stations", []))
        if not st.empty:
            key = _find(list(obs.columns), "station", None) or "station"
            skey = _find(list(st.columns), "station", None) or "station"
            obs = obs.merge(st, left_on=key, right_on=skey, how="left", suffixes=("", "_st"))
        return obs.astype(object)
    if isinstance(doc, dict):
        doc = doc.get("records") or doc.get("data") or [doc]
    return pd.DataFrame(doc).astype(object)


_METAR_P = re.compile(r"\sP(\d{4})(?=\s|$)")
_METAR_STAMP = re.compile(r"^(?:METAR\s+|SPECI\s+)?([A-Z][A-Z0-9]{3})\s+(\d{2})(\d{2})(\d{2})Z")


def _read_metar(
    p: Path,
    stations: Any,
    year: int | None,
    month: int | None,
    units: str,
    time_convention: str,
    notes: list[str],
) -> GaugeSet:
    import pandas as pd

    if stations is None:
        raise GaugeError(
            "METAR text carries no coordinates: give a stations file (station, lat, lon) with "
            "stations=... (nothing is downloaded)"
        )
    if year is None or month is None:
        raise GaugeError("METAR time stamps are DDHHMMZ: give year= and month=")
    rows = []
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = _METAR_STAMP.match(line.strip())
        if not m:
            continue
        sid, dd, hh, mm = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
        pm = _METAR_P.search(" " + line + " ")
        try:
            t = pd.Timestamp(year=year, month=month, day=dd, hour=hh, minute=mm)
        except ValueError:
            continue
        rows.append(
            {
                "station": sid,
                "time": t,
                "value": float(pm.group(1)) / 100.0 if pm else np.nan,
                "has_p": bool(pm),
            }
        )
    if not rows:
        raise GaugeError(f"no METAR reports found in {p.name}")
    df = pd.DataFrame(rows)
    # A METAR without a P group at a routine hour means no precipitation was reported (0), at
    # other times (SPECI) it is unknown: only keep the reports on the hour as zeros.
    on_hour = df["time"].dt.minute.between(45, 59) | (df["time"].dt.minute <= 5)
    df.loc[~df["has_p"] & on_hour, "value"] = 0.0
    df["time"] = df["time"].dt.round("h")
    df = df[df["time"].notna()].drop_duplicates(["station", "time"], keep="last")
    notes.append(
        "METAR/ASOS: the Pnnnn group is the precipitation (hundredths of an inch) since the "
        "previous hourly report; a routine report without a P group is taken as 0; reports are "
        "rounded to the hour. ASOS tipping buckets under-catch snow and heavy rain."
    )
    cols = {
        "station": "station",
        "time": "time",
        "value": "value",
        "lat": None,
        "lon": None,
        "name": None,
        "elev": None,
        "flag_m": None,
        "flag_q": None,
    }
    return _finish(
        df,
        cols,
        units=units,
        interval_s=3600.0,
        time_convention=time_convention,
        tz="UTC",
        stations_file=stations,
        source=p.name,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Pairing with the radar
# ---------------------------------------------------------------------------


@dataclass
class Pairs:
    """Gauge accumulations paired with the radar accumulation at the gauge."""

    station: np.ndarray
    lon: np.ndarray
    lat: np.ndarray
    x: np.ndarray  # grid metres
    y: np.ndarray
    gauge: np.ndarray  # mm
    radar: np.ndarray  # mm
    range_km: np.ndarray
    coverage: np.ndarray
    radar_sigma: np.ndarray | None = None

    def __len__(self) -> int:
        return int(self.gauge.size)

    def subset(self, mask: np.ndarray) -> Pairs:
        return Pairs(
            *(
                getattr(self, f)[mask] if getattr(self, f) is not None else None
                for f in (
                    "station",
                    "lon",
                    "lat",
                    "x",
                    "y",
                    "gauge",
                    "radar",
                    "range_km",
                    "coverage",
                    "radar_sigma",
                )
            )
        )


def pair_with_radar(
    totals: Any,
    result: Any,
    *,
    radius_cells: int = 1,
    max_gap_cells: float = 1.5,
    field: str = "accumulation",
) -> Pairs:
    """Sample ``result.<field>`` at each gauge of ``totals`` (output of ``GaugeSet.totals``).

    The radar value is the mean of the finite cells within ``radius_cells`` (3 x 3 cells for 1):
    a point gauge sees a few hundred square centimetres, but radar and gauge positions and the beam
    height above the gauge differ by about a cell, so a small window is the honest comparison.
    Gauges outside the grid or in cells without data are dropped.
    """
    import pyproj

    grid = result.grid
    tr = pyproj.Transformer.from_crs("EPSG:4326", grid.proj4(), always_xy=True)
    x, y = tr.transform(totals["lon"].to_numpy(float), totals["lat"].to_numpy(float))
    ix = np.rint((x - grid.x[0]) / grid.cell_m).astype(int)
    iy = np.rint((y - grid.y[0]) / grid.cell_m).astype(int)
    ny, nx = grid.shape
    arr = getattr(result, field)
    rng = result.range_km
    sig = getattr(result, "accumulation_sigma", None)
    keep, vals, rr, ss = [], [], [], []
    for k in range(len(x)):
        if not (0 <= ix[k] < nx and 0 <= iy[k] < ny):
            continue
        j0, j1 = max(iy[k] - radius_cells, 0), min(iy[k] + radius_cells + 1, ny)
        i0, i1 = max(ix[k] - radius_cells, 0), min(ix[k] + radius_cells + 1, nx)
        w = arr[j0:j1, i0:i1]
        fin = np.isfinite(w)
        if not fin.any():
            continue
        keep.append(k)
        vals.append(float(w[fin].mean()))
        rr.append(
            float(np.nanmean(rng[j0:j1, i0:i1])) if np.isfinite(rng[j0:j1, i0:i1]).any() else np.nan
        )
        ss.append(
            float(np.nanmean(sig[j0:j1, i0:i1]))
            if sig is not None and np.isfinite(sig[j0:j1, i0:i1]).any()
            else np.nan
        )
    keep_a = np.array(keep, dtype=int)
    if keep_a.size == 0:
        raise GaugeError("no gauge lies inside the radar accumulation (check the coordinates)")
    t = totals.iloc[keep_a]
    return Pairs(
        station=t["station"].to_numpy(str),
        lon=t["lon"].to_numpy(float),
        lat=t["lat"].to_numpy(float),
        x=x[keep_a],
        y=y[keep_a],
        gauge=t["depth_mm"].to_numpy(float),
        radar=np.array(vals),
        range_km=np.array(rr),
        coverage=t["coverage"].to_numpy(float),
        radar_sigma=np.array(ss),
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def kge(sim: np.ndarray, obs: np.ndarray) -> float:
    """Kling-Gupta efficiency ``1 - sqrt((r-1)^2 + (a-1)^2 + (b-1)^2)`` (Gupta et al. 2009)."""
    sim, obs = np.asarray(sim, float), np.asarray(obs, float)
    if sim.size < 2 or obs.std() == 0 or sim.std() == 0 or obs.mean() == 0:
        return math.nan
    r = float(np.corrcoef(sim, obs)[0, 1])
    alpha = float(sim.std() / obs.std())
    beta = float(sim.mean() / obs.mean())
    return 1.0 - math.sqrt((r - 1) ** 2 + (alpha - 1) ** 2 + (beta - 1) ** 2)


def metrics(radar: np.ndarray, gauge: np.ndarray) -> dict[str, Any]:
    """Skill of ``radar`` against ``gauge``: n, bias, relative bias, MAE, RMSE, r, KGE."""
    r_, g_ = np.asarray(radar, float), np.asarray(gauge, float)
    ok = np.isfinite(r_) & np.isfinite(g_)
    r_, g_ = r_[ok], g_[ok]
    n = int(r_.size)
    if n == 0:
        return {"n": 0}
    err = r_ - g_
    out: dict[str, Any] = {
        "n": n,
        "gauge_mean_mm": float(g_.mean()),
        "radar_mean_mm": float(r_.mean()),
        "bias_mm": float(err.mean()),
        "relative_bias": float(r_.sum() / g_.sum() - 1.0) if g_.sum() > 0 else None,
        "mae_mm": float(np.abs(err).mean()),
        "rmse_mm": float(math.sqrt((err**2).mean())),
        "correlation": float(np.corrcoef(r_, g_)[0, 1])
        if n > 2 and r_.std() > 0 and g_.std() > 0
        else None,
        "kge": kge(r_, g_) if n > 2 else None,
    }
    for k, v in list(out.items()):
        if isinstance(v, float) and not math.isfinite(v):
            out[k] = None
    return out


def validate(
    pairs: Pairs,
    *,
    amount_bins: Sequence[float] = (0.0, 1.0, 5.0, 25.0, 50.0, 100.0, math.inf),
    range_bins: Sequence[float] = (0.0, 50.0, 100.0, 150.0, 230.0),
    radar: np.ndarray | None = None,
) -> dict[str, Any]:
    """Overall and stratified skill, scatter and residual-map data of radar against gauges."""
    rad = pairs.radar if radar is None else radar
    out: dict[str, Any] = {"overall": metrics(rad, pairs.gauge)}

    def strat(values: np.ndarray, edges: Sequence[float], label: str) -> list[dict[str, Any]]:
        rows = []
        for lo, hi in itertools.pairwise(edges):
            m = (values >= lo) & (values < hi)
            if m.sum() == 0:
                continue
            rows.append(
                {
                    label: f"{lo:g}-{hi:g}" if math.isfinite(hi) else f">={lo:g}",
                    **metrics(rad[m], pairs.gauge[m]),
                }
            )
        return rows

    out["by_amount_mm"] = strat(pairs.gauge, amount_bins, "gauge_class_mm")
    out["by_range_km"] = strat(pairs.range_km, range_bins, "range_class_km")
    out["points"] = [
        {
            "station": str(s),
            "lon": float(lo),
            "lat": float(la),
            "gauge_mm": float(g),
            "radar_mm": float(r),
            "residual_mm": float(r - g),
            "range_km": None if not math.isfinite(rk) else float(rk),
        }
        for s, lo, la, g, r, rk in zip(
            pairs.station, pairs.lon, pairs.lat, pairs.gauge, rad, pairs.range_km, strict=True
        )
    ]
    return out


# ---------------------------------------------------------------------------
# Adjustment
# ---------------------------------------------------------------------------

METHODS = ("mfb", "brandes", "ked")
MIN_PAIRS = {"mfb": 3, "brandes": 5, "ked": 8}


def _usable(pairs: Pairs, min_amount: float) -> np.ndarray:
    return (
        np.isfinite(pairs.gauge)
        & np.isfinite(pairs.radar)
        & (pairs.gauge >= min_amount)
        & (pairs.radar >= min_amount)
    )


class Adjuster:
    """Fit on gauge/radar pairs, predict the adjusted accumulation anywhere radar exists."""

    def __init__(
        self,
        method: str,
        *,
        min_amount: float = 1.0,
        length_km: float | None = None,
        shrink: float = 1.0,
        factor_limits: tuple[float, float] = (0.2, 5.0),
    ) -> None:
        if method not in METHODS:
            raise GaugeError(f"unknown adjustment {method!r}; choose from {', '.join(METHODS)}")
        self.method, self.min_amount, self.length_km = method, float(min_amount), length_km
        self.shrink, self.limits = float(shrink), factor_limits
        self.params: dict[str, Any] = {}

    # -- fit ---------------------------------------------------------------------------------
    def fit(self, pairs: Pairs) -> Adjuster:
        use = _usable(pairs, self.min_amount)
        n = int(use.sum())
        if n < MIN_PAIRS[self.method]:
            raise GaugeError(
                f"only {n} gauge(s) with rain (>= {self.min_amount:g} mm in both) but the "
                f"'{self.method}' adjustment needs at least {MIN_PAIRS[self.method]}; use more "
                "gauges, "
                "a lower min_amount or the 'mfb' method"
            )
        g, r = pairs.gauge[use], pairs.radar[use]
        self._x, self._y = pairs.x[use], pairs.y[use]
        self._g, self._r = g, r
        self.mfb = float(np.clip(g.sum() / r.sum(), *self.limits))
        self.params = {"n_pairs": n, "mean_field_bias": self.mfb}
        if self.method == "mfb":
            rng = np.random.default_rng(0)
            boots = [
                float(g[i].sum() / r[i].sum()) for i in (rng.integers(0, n, n) for _ in range(300))
            ]
            self.params["mfb_ci90"] = [
                float(np.percentile(boots, 5)),
                float(np.percentile(boots, 95)),
            ]
        elif self.method == "brandes":
            d = np.hypot(self._x[:, None] - self._x[None, :], self._y[:, None] - self._y[None, :])
            np.fill_diagonal(d, np.inf)
            nn = np.median(d.min(axis=1)) / 1000.0
            self.length = float(self.length_km or np.clip(1.5 * nn, 10.0, 60.0))
            self.lnratio = np.log(np.clip(g / r, *self.limits))
            self.params.update(
                length_km=self.length, shrink=self.shrink, median_nearest_gauge_km=float(nn)
            )
        else:
            self._fit_ked()
        return self

    def _fit_ked(self) -> None:
        from scipy.optimize import least_squares

        g, r, x, y = self._g, self._r, self._x, self._y
        design = np.column_stack([np.ones_like(r), r])
        beta, *_ = np.linalg.lstsq(design, g, rcond=None)
        res = g - design @ beta
        d = np.hypot(x[:, None] - x[None, :], y[:, None] - y[None, :]) / 1000.0
        iu = np.triu_indices(len(g), 1)
        h, sv = d[iu], 0.5 * (res[:, None] - res[None, :])[iu] ** 2
        # empirical variogram in bins
        edges = np.quantile(h, np.linspace(0, 1, 9))
        centers, gam = [], []
        for lo, hi in itertools.pairwise(edges):
            m = (h >= lo) & (h <= hi)
            if m.sum() >= 3:
                centers.append(h[m].mean())
                gam.append(sv[m].mean())
        centers, gam = np.array(centers), np.array(gam)
        var = float(res.var()) + 1e-6

        def model(p, hh):
            c0, c, a = p
            return c0 + c * (1.0 - np.exp(-hh / a))

        if centers.size >= 3:
            p0 = [0.1 * var, 0.9 * var, max(float(centers.max()) / 3.0, 1.0)]
            sol = least_squares(
                lambda p: model(p, centers) - gam,
                p0,
                bounds=([0.0, 1e-9, 1.0], [10 * var, 10 * var, 500.0]),
            )
            c0, c, a = (float(v) for v in sol.x)
        else:
            c0, c, a = 0.1 * var, 0.9 * var, 30.0
        self._vario = (c0, c, a)
        self._beta = beta
        n = len(g)
        gam_m = np.where(d > 0, c0 + c * (1.0 - np.exp(-d / a)), 0.0)
        kmat = np.zeros((n + 2, n + 2))
        kmat[:n, :n] = gam_m
        kmat[:n, n] = kmat[n, :n] = 1.0
        kmat[:n, n + 1] = kmat[n + 1, :n] = r
        self._kinv = np.linalg.pinv(kmat)
        self.params.update(
            variogram={"model": "exponential", "nugget": c0, "partial_sill": c, "range_km": a},
            drift={"intercept": float(beta[0]), "slope": float(beta[1])},
        )

    # -- predict ---------------------------------------------------------------------------------
    def predict_points(self, x: np.ndarray, y: np.ndarray, radar: np.ndarray) -> np.ndarray:
        """Adjusted values at points (same units as ``radar``)."""
        radar = np.asarray(radar, float)
        if self.method == "mfb":
            return radar * self.mfb
        if self.method == "brandes":
            return radar * self._factor_points(x, y)
        return self._ked(x, y, radar)[0]

    def _factor_points(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        d2 = (
            (np.asarray(x)[:, None] - self._x[None, :]) ** 2
            + (np.asarray(y)[:, None] - self._y[None, :]) ** 2
        ) / 1e6
        w = np.exp(-d2 / (2.0 * self.length**2))
        w0 = self.shrink  # weight of the mean-field bias, in units of one gauge at distance 0
        lnf = (w @ self.lnratio + w0 * math.log(self.mfb)) / (w.sum(axis=1) + w0)
        return np.clip(np.exp(lnf), *self.limits)

    def _ked(
        self, x: np.ndarray, y: np.ndarray, radar: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        c0, c, a = self._vario
        n = len(self._g)
        out = np.full(len(radar), np.nan)
        sd = np.full(len(radar), np.nan)
        ok = np.isfinite(radar)
        idx = np.flatnonzero(ok)
        for s in range(0, idx.size, 20000):
            sel = idx[s : s + 20000]
            d = (
                np.hypot(
                    np.asarray(x)[sel, None] - self._x[None, :],
                    np.asarray(y)[sel, None] - self._y[None, :],
                )
                / 1000.0
            )
            rhs = np.empty((sel.size, n + 2))
            rhs[:, :n] = np.where(d > 0, c0 + c * (1.0 - np.exp(-d / a)), 0.0)
            rhs[:, n] = 1.0
            rhs[:, n + 1] = radar[sel]
            sol = rhs @ self._kinv.T
            lam = sol[:, :n]
            out[sel] = lam @ self._g
            var = np.einsum("ij,ij->i", sol, rhs)
            sd[sel] = np.sqrt(np.maximum(var, 0.0))
        return np.maximum(out, 0.0), sd

    def adjust_field(self, result: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
        """``(adjusted, factor, sigma)`` on the whole grid of a :class:`QpeResult`.

        Where the radar saw no rain (< 0.01 mm) the adjusted value is 0: an adjustment never
        creates rain the radar did not see. ``factor`` is adjusted / radar where the radar has at
        least 0.1 mm, else 1.
        """
        grid = result.grid
        radar = result.accumulation.astype(np.float64)
        xx, yy = np.meshgrid(grid.x, grid.y)
        sigma = None
        if self.method == "mfb":
            adj = radar * self.mfb
        elif self.method == "brandes":
            adj = np.full(radar.shape, np.nan)
            ok = np.isfinite(radar)
            f = self._factor_points(xx[ok], yy[ok])
            adj[ok] = radar[ok] * f
        else:
            ok = np.isfinite(radar)
            vals, sd = self._ked(xx[ok], yy[ok], radar[ok])
            adj = np.full(radar.shape, np.nan)
            adj[ok] = vals
            sigma = np.full(radar.shape, np.nan)
            sigma[ok] = sd
        adj = np.where(np.isfinite(radar) & (radar < 0.01), 0.0, adj)
        with np.errstate(invalid="ignore", divide="ignore"):
            factor = np.where(np.isfinite(radar) & (radar >= 0.1), adj / radar, 1.0)
        factor = np.where(np.isfinite(radar), factor, np.nan)
        return (
            adj.astype(np.float32),
            factor.astype(np.float32),
            (sigma.astype(np.float32) if sigma is not None else None),
        )


def cross_validate(pairs: Pairs, method: str = "mfb", **kwargs: Any) -> dict[str, Any]:
    """Leave-one-gauge-out skill of an adjustment, next to the skill of the unadjusted radar."""
    n = len(pairs)
    pred = np.full(n, np.nan)
    skipped = 0
    for i in range(n):
        train = pairs.subset(np.arange(n) != i)
        try:
            ad = Adjuster(method, **kwargs).fit(train)
        except GaugeError:
            skipped += 1
            continue
        pred[i] = ad.predict_points(
            np.array([pairs.x[i]]), np.array([pairs.y[i]]), np.array([pairs.radar[i]])
        )[0]
    ok = np.isfinite(pred)
    return {
        "method": method,
        "left_out": int(ok.sum()),
        "skipped": skipped,
        "unadjusted": metrics(pairs.radar[ok], pairs.gauge[ok]),
        "adjusted": metrics(pred[ok], pairs.gauge[ok]),
        "predictions": pred.tolist(),
    }


def adjust(
    result: Any,
    gauges: GaugeSet,
    *,
    method: str = "mfb",
    min_coverage: float = 0.9,
    min_amount: float = 1.0,
    radius_cells: int = 1,
    prorate: bool = False,
    tolerance_s: float = 300.0,
    qc: bool = True,
    cross_validation: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Adjust a :class:`QpeResult` to gauges in place and return the report.

    Fills ``result.accumulation_adjusted``, ``adjustment_factor`` and ``result.adjusted`` (the
    ADJUSTED provenance), re-derives the basin series if there are any, and returns
    ``{"pairs", "qc", "validation_before", "validation_after", "cross_validation", "adjustment"}``.
    """
    if qc:
        gauges = gauges.quality_control()
    totals = gauges.totals(
        result.start,
        result.end,
        min_coverage=min_coverage,
        prorate=prorate,
        tolerance_s=tolerance_s,
    )
    if totals.empty:
        raise GaugeError(
            "no gauge has valid records covering the radar period "
            f"({prov.iso_utc(result.start)} to {prov.iso_utc(result.end)}); check the time zone "
            "and the accumulation interval"
        )
    pairs = pair_with_radar(totals, result, radius_cells=radius_cells)
    ad = Adjuster(method, min_amount=min_amount, **kwargs).fit(pairs)
    adj, factor, sigma = ad.adjust_field(result)
    result.accumulation_adjusted = adj
    result.adjustment_factor = factor
    after = ad.predict_points(pairs.x, pairs.y, pairs.radar)
    report: dict[str, Any] = {
        "pairs": len(pairs),
        "qc": gauges.qc_summary(),
        "validation_before": validate(pairs),
        "validation_after_in_sample": validate(pairs, radar=after),
    }
    if cross_validation and len(pairs) >= 4:
        report["cross_validation"] = cross_validate(pairs, method, min_amount=min_amount, **kwargs)
    import pandas as pd

    t0 = pd.Timestamp(np.datetime64(result.start, "ns"))
    t1 = pd.Timestamp(np.datetime64(result.end, "ns"))
    mismatch = max(
        float((totals["span_start"] - t0).abs().dt.total_seconds().max()),
        float((totals["span_end"] - t1).abs().dt.total_seconds().max()),
    )
    report["window_mismatch_s"] = mismatch
    fac = factor[np.isfinite(factor)]
    result.adjusted = {
        "type": "ADJUSTED",
        "method": {
            "mfb": "mean-field bias",
            "brandes": "Brandes-type spatial ratio",
            "ked": "kriging with external drift",
        }[method],
        "method_key": method,
        "gauge_source": gauges.source,
        "gauges_used": [str(s) for s in pairs.station],
        "n_gauges": len(pairs),
        "period": [prov.iso_utc(result.start), prov.iso_utc(result.end)],
        "parameters": {
            **ad.params,
            "min_amount_mm": min_amount,
            "radius_cells": radius_cells,
            "min_coverage": min_coverage,
            "window_mismatch_s": mismatch,
        },
        "factor_percentiles": [float(np.percentile(fac, p)) for p in (5, 50, 95)]
        if fac.size
        else None,
        "note": (
            "Gauges are point measurements with their own errors (wind undercatch, siting); the "
            "adjusted field is an estimate that merges gauge and radar, not an observation."
        ),
    }
    if sigma is not None:
        report["adjustment_sigma_mm_mean"] = float(np.nanmean(sigma))
    report["adjustment"] = result.adjusted
    if result.basin_series is not None:
        result.basin_series.apply_factor(
            np.where(np.isfinite(factor), factor, 1.0), result.adjusted, result
        )
    return report

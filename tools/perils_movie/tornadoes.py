"""Tornadoes of the day: survey / report fields (OBSERVED, after the event) kept apart from derived ones.

Sources (CSV tables prepared for the PERiLS analysis, read only):

* ``perils_tornadoes_nsed.csv``: NWS damage-survey events (NCEI Storm Events, "NSED"): begin / end time (UTC),
  EF rating, begin and end latitude / longitude, a narrative (cut to 300 characters in the table).
* ``perils_spc_tornadoes.csv``: SPC tornado table: start time, rating, start / end point, length (mi),
  width (yd). Matched to the survey event by start time and place to add the width.

A survey gives the begin and end point and time, not the track: ``path`` is a straight line between them at
constant speed and is labelled INTERPOLATED (``path_kind``). The maximum wind of the EF rating is the
published range of the scale (EF0 65-85 mph ... EF3 136-165 mph), an estimate from damage, not a
measurement: ``max_wind_mph`` carries the upper bound of the range and ``wind_note`` says so.
"""

from __future__ import annotations

import csv
import itertools
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc
EF_WIND_MPH = {
    "EF0": (65, 85),
    "EF1": (86, 110),
    "EF2": (111, 135),
    "EF3": (136, 165),
    "EF4": (166, 200),
    "EF5": (200, 250),
}


def _t(s: str) -> datetime:
    return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def _km(lat1, lon1, lat2, lon2) -> float:
    return math.hypot(
        (lat2 - lat1) * 111.2, (lon2 - lon1) * 111.2 * math.cos(math.radians(0.5 * (lat1 + lat2)))
    )


def read_tables(
    nsed_csv: str | Path, spc_csv: str | Path | None
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    with open(nsed_csv, encoding="utf-8", errors="replace", newline="") as fh:
        nsed = list(csv.DictReader(fh))
    spc: list[dict[str, str]] = []
    if spc_csv:
        with open(spc_csv, newline="") as fh:
            spc = list(csv.DictReader(fh))
    return nsed, spc


def build(
    nsed: list[dict[str, str]],
    spc: list[dict[str, str]],
    window_start: datetime,
    window_end: datetime,
    region: tuple[float, float, float, float],
    time_margin_s: float = 3600.0,
    n_points: int = 13,
) -> list[dict[str, Any]]:
    """Tornado records overlapping [window - margin, window + margin] inside ``region`` (south, west, north, east)."""
    s0 = window_start - timedelta(seconds=time_margin_s)
    e0 = window_end + timedelta(seconds=time_margin_s)
    south, west, north, east = region
    out = []
    for r in nsed:
        if r.get("iop") != "Y1IOP2":
            continue
        t0, t1 = _t(r["begin"]), _t(r["end"])
        if t1 < s0 or t0 > e0:
            continue
        blat, blon, elat, elon = (
            float(r["blat"]),
            float(r["blon"]),
            float(r["elat"]),
            float(r["elon"]),
        )
        if not (south <= blat <= north and west <= blon <= east):
            continue
        # SPC match: start within 15 min and 25 km
        best, bd = None, 1e9
        for q in spc:
            try:
                qt = _t(q["start"])
            except ValueError:
                continue
            if abs((qt - t0).total_seconds()) <= 900:
                d = _km(blat, blon, float(q["slat"]), float(q["slon"]))
                if d < bd:
                    best, bd = q, d
        spc_ok = best is not None and bd <= 25.0
        dur = max((t1 - t0).total_seconds(), 1.0)
        path = []
        for k in range(n_points):
            f = k / (n_points - 1)
            path.append(
                [
                    round((t0 - window_start).total_seconds() + f * dur, 1),
                    round(blat + f * (elat - blat), 5),
                    round(blon + f * (elon - blon), 5),
                ]
            )
        ef = r["ef"].strip()
        lo, hi = EF_WIND_MPH.get(ef, (None, None))
        length_km = _km(blat, blon, elat, elon)
        rec: dict[str, Any] = {
            "id": f"NSED_{r['event_id']}",
            "rating": ef,
            "start": t0.isoformat().replace("+00:00", "Z"),
            "end": t1.isoformat().replace("+00:00", "Z"),
            "path": path,
            "path_kind": "INTERPOLATED: straight line between the surveyed begin and end points at constant speed",
            "width_m": round(float(best["wid_yd"]) * 0.9144, 0)
            if spc_ok and best.get("wid_yd")
            else None,
            "length_km_from_endpoints": round(length_km, 2),
            "length_mi_spc": float(best["len_mi"]) if spc_ok and best.get("len_mi") else None,
            "max_wind_mph": hi,
            "wind_note": f"upper bound of the EF-scale range for {ef} ({lo}-{hi} mph): a damage-based estimate, not a measurement",
            "begin": {"lat": blat, "lon": blon, "time": t0.isoformat().replace("+00:00", "Z")},
            "end_point": {"lat": elat, "lon": elon, "time": t1.isoformat().replace("+00:00", "Z")},
            "survey_narrative_start": r.get("narrative", "")[:300],
            "episode_id": r.get("episode_id"),
            "source": "NWS damage survey, NCEI Storm Events (NSED) table prepared for PERiLS"
            + ("; SPC tornado table for the width" if spc_ok else ""),
            "credit": "NOAA NWS / NCEI Storm Events Database; NOAA SPC tornado database",
            "class": "observed (survey)",
            "overlaps_window": not (t1 < window_start or t0 > window_end),
            "spc_match": {"om": best.get("om"), "distance_km": round(bd, 1)} if spc_ok else None,
        }
        out.append(rec)
    out.sort(key=lambda d: d["start"])
    return out


def position_at(rec: dict[str, Any], t_s: float) -> tuple[float, float] | None:
    """Interpolated surveyed position (lat, lon) at ``t_s`` seconds after the window start, None outside the life."""
    p = rec["path"]
    if t_s < p[0][0] - 1e-6 or t_s > p[-1][0] + 1e-6:
        return None
    for a, b in itertools.pairwise(p):
        if a[0] <= t_s <= b[0]:
            f = 0.0 if b[0] == a[0] else (t_s - a[0]) / (b[0] - a[0])
            return a[1] + f * (b[1] - a[1]), a[2] + f * (b[2] - a[2])
    return p[-1][1], p[-1][2]

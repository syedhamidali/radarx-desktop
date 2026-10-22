"""Cloud data access: AWS NEXRAD Level II (archive + real-time) and NEXRAD ARCO.

All S3 listing/downloading uses boto3 (synchronous, no asyncio) to avoid
event-loop conflicts when called from run_in_executor thread-pool threads.
"""

from __future__ import annotations

import copy
import logging
import re
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_NEXRAD_L2_BUCKET = "unidata-nexrad-level2"
_NEXRAD_ARCO_BUCKET = "nexrad-arco"

# Network behaviour: fail fast instead of hanging an executor thread forever.
_CONNECT_TIMEOUT_S = 10
_READ_TIMEOUT_S = 60
_STATION_RE = re.compile(r"^[A-Z0-9]{4}$")

_S3_CLIENT: Any = None
_S3_LOCK = threading.Lock()


def _s3():
    """Return a shared anonymous boto3 S3 client (clients are thread-safe; creating one
    costs tens of milliseconds, so it is created once per process)."""
    global _S3_CLIENT
    if _S3_CLIENT is None:
        with _S3_LOCK:
            if _S3_CLIENT is None:
                import boto3
                from botocore import UNSIGNED
                from botocore.config import Config

                _S3_CLIENT = boto3.client(
                    "s3",
                    config=Config(
                        signature_version=UNSIGNED,
                        connect_timeout=_CONNECT_TIMEOUT_S,
                        read_timeout=_READ_TIMEOUT_S,
                        retries={"max_attempts": 3, "mode": "standard"},
                        max_pool_connections=32,
                    ),
                    region_name="us-east-1",
                )
    return _S3_CLIENT


def _normalize_station(station: str) -> str:
    st = (station or "").strip().upper()
    if not _STATION_RE.match(st):
        raise ValueError(f"Invalid station id: {station!r} (expected 4 letters, e.g. KLOT)")
    return st


# ── Station registry ──────────────────────────────────────────────────────────
_NEXRAD_STATIONS = [
    "KABR",
    "KABX",
    "KAKQ",
    "KAMA",
    "KAMX",
    "KAPX",
    "KARX",
    "KATX",
    "KBBX",
    "KBGM",
    "KBHX",
    "KBIS",
    "KBLX",
    "KBMX",
    "KBOX",
    "KBRO",
    "KBUF",
    "KBYX",
    "KCAE",
    "KCBW",
    "KCBX",
    "KCCX",
    "KCLE",
    "KCLX",
    "KCRP",
    "KCXX",
    "KCYS",
    "KDAX",
    "KDDC",
    "KDFX",
    "KDGX",
    "KDIX",
    "KDLH",
    "KDMX",
    "KDOX",
    "KDTX",
    "KDVN",
    "KDYX",
    "KEAX",
    "KEMX",
    "KENX",
    "KEOX",
    "KEPZ",
    "KESX",
    "KEVX",
    "KEWX",
    "KEYX",
    "KFCX",
    "KFDR",
    "KFDX",
    "KFFC",
    "KFSD",
    "KFSX",
    "KFTG",
    "KFWS",
    "KGGW",
    "KGJX",
    "KGLD",
    "KGRB",
    "KGRK",
    "KGRR",
    "KGSP",
    "KGWX",
    "KGYX",
    "KHDX",
    "KHGX",
    "KHNX",
    "KHPX",
    "KHTX",
    "KICT",
    "KICX",
    "KILN",
    "KILX",
    "KIND",
    "KINX",
    "KIWA",
    "KIWX",
    "KJAX",
    "KJGX",
    "KJKL",
    "KLBB",
    "KLCH",
    "KLGX",
    "KLIX",
    "KLNX",
    "KLOT",
    "KLRX",
    "KLSX",
    "KLTX",
    "KLVX",
    "KLWX",
    "KLZK",
    "KMAF",
    "KMAX",
    "KMBX",
    "KMHX",
    "KMKX",
    "KMLB",
    "KMOB",
    "KMPX",
    "KMQT",
    "KMRX",
    "KMSX",
    "KMTX",
    "KMUX",
    "KMVX",
    "KMXX",
    "KNKX",
    "KNQA",
    "KOAX",
    "KOHX",
    "KOKX",
    "KOTX",
    "KPAH",
    "KPBZ",
    "KPDT",
    "KPOE",
    "KPUX",
    "KRAX",
    "KRGX",
    "KRIW",
    "KRLX",
    "KRTX",
    "KSFX",
    "KSGF",
    "KSHV",
    "KSJT",
    "KSOX",
    "KSRX",
    "KTBW",
    "KTFX",
    "KTLH",
    "KTLX",
    "KTWX",
    "KTYX",
    "KUDX",
    "KUEX",
    "KVAX",
    "KVBX",
    "KVNX",
    "KVTX",
    "KVWX",
    "KYUX",
    "PABC",
    "PACG",
    "PAEC",
    "PAHG",
    "PAIH",
    "PAKC",
    "PAPD",
    "PGUA",
    "PHKI",
    "PHKM",
    "PHMO",
    "PHWA",
    "TJUA",
]

# Radar site coordinates (latitude deg N, longitude deg E, ground elevation m) for every
# station above.  Source: NOAA NCEI HOMR NEXRAD station list (elevation converted from
# feet); cross-checked against the Py-ART nexrad_common table (all agree within 0.01 deg
# except KFDX and KIWX, where the newer NCEI values are used).
_NEXRAD_SITES: dict[str, tuple[float, float, float]] = {
    "KABR": (45.45583, -98.41333, 422),
    "KABX": (35.14972, -106.82388, 1814),
    "KAKQ": (36.98405, -77.00736, 78),
    "KAMA": (35.23333, -101.70927, 1129),
    "KAMX": (25.61108, -80.41267, 34),
    "KAPX": (44.90635, -84.71953, 476),
    "KARX": (43.82278, -91.19111, 414),
    "KATX": (48.19461, -122.49569, 196),
    "KBBX": (39.49564, -121.63161, 67),
    "KBGM": (42.19969, -75.98472, 519),
    "KBHX": (40.49858, -124.29216, 767),
    "KBIS": (46.77083, -100.76055, 535),
    "KBLX": (45.85378, -108.60680, 1129),
    "KBMX": (33.17242, -86.77017, 231),
    "KBOX": (41.95578, -71.13686, 71),
    "KBRO": (25.91600, -97.41897, 27),
    "KBUF": (42.94879, -78.73678, 241),
    "KBYX": (24.59750, -81.70317, 27),
    "KCAE": (33.94872, -81.11828, 105),
    "KCBW": (46.03925, -67.80643, 262),
    "KCBX": (43.49022, -116.23603, 967),
    "KCCX": (40.92317, -78.00372, 758),
    "KCLE": (41.41322, -81.85987, 262),
    "KCLX": (32.65553, -81.04219, 70),
    "KCRP": (27.78402, -97.51125, 43),
    "KCXX": (44.51100, -73.16643, 131),
    "KCYS": (41.15192, -104.80603, 1888),
    "KDAX": (38.50111, -121.67783, 44),
    "KDDC": (37.76083, -99.96889, 814),
    "KDFX": (29.27314, -100.28033, 365),
    "KDGX": (32.27994, -89.98444, 186),
    "KDIX": (39.94709, -74.41073, 70),
    "KDLH": (46.83694, -92.20972, 470),
    "KDMX": (41.73120, -93.72287, 334),
    "KDOX": (38.82577, -75.44012, 50),
    "KDTX": (42.70000, -83.47167, 371),
    "KDVN": (41.61167, -90.58083, 259),
    "KDYX": (32.53850, -99.25433, 482),
    "KEAX": (38.81025, -94.26447, 333),
    "KEMX": (31.89365, -110.63025, 1621),
    "KENX": (42.58656, -74.06408, 590),
    "KEOX": (31.46056, -85.45939, 164),
    "KEPZ": (31.87306, -106.69800, 1286),
    "KESX": (35.70135, -114.89165, 1508),
    "KEVX": (30.56503, -85.92167, 67),
    "KEWX": (29.70406, -98.02861, 234),
    "KEYX": (35.09785, -117.56075, 876),
    "KFCX": (37.02440, -80.27397, 904),
    "KFDR": (34.36219, -98.97667, 401),
    "KFDX": (34.63417, -103.61888, 1432),
    "KFFC": (33.36355, -84.56595, 296),
    "KFSD": (43.58778, -96.72944, 456),
    "KFSX": (34.57433, -111.19844, 2290),
    "KFTG": (39.78664, -104.54580, 1710),
    "KFWS": (32.57300, -97.30315, 237),
    "KGGW": (48.20636, -106.62469, 727),
    "KGJX": (39.06217, -108.21376, 3079),
    "KGLD": (39.36694, -101.70027, 1133),
    "KGRB": (44.49863, -88.11111, 251),
    "KGRK": (30.72183, -97.38294, 184),
    "KGRR": (42.89389, -85.54489, 267),
    "KGSP": (34.88331, -82.21983, 326),
    "KGWX": (33.89692, -88.32919, 180),
    "KGYX": (43.89131, -70.25636, 144),
    "KHDX": (33.07700, -106.12003, 1301),
    "KHGX": (29.47190, -95.07873, 35),
    "KHNX": (36.31418, -119.63213, 104),
    "KHPX": (36.73697, -87.28558, 187),
    "KHTX": (34.93056, -86.08361, 567),
    "KICT": (37.65444, -97.44306, 427),
    "KICX": (37.59105, -112.86218, 3279),
    "KILN": (39.42048, -83.82145, 357),
    "KILX": (40.15050, -89.33679, 223),
    "KIND": (39.70750, -86.28028, 270),
    "KINX": (36.17513, -95.56416, 228),
    "KIWA": (33.28923, -111.66991, 435),
    "KIWX": (41.35861, -85.70000, 322),
    "KJAX": (30.48463, -81.70190, 49),
    "KJGX": (32.67568, -83.35083, 188),
    "KJKL": (37.59083, -83.31306, 445),
    "KLBB": (33.65414, -101.81416, 1030),
    "KLCH": (30.12531, -93.21589, 42),
    "KLGX": (47.11694, -124.10666, 112),
    "KLIX": (30.33667, -89.82542, 55),
    "KLNX": (41.95794, -100.57622, 949),
    "KLOT": (41.60444, -88.08444, 232),
    "KLRX": (40.73955, -116.80270, 2102),
    "KLSX": (38.69861, -90.68278, 220),
    "KLTX": (33.98915, -78.42911, 44),
    "KLVX": (37.97528, -85.94389, 254),
    "KLWX": (38.97611, -77.48750, 123),
    "KLZK": (34.83650, -92.26219, 198),
    "KMAF": (31.94346, -102.18925, 903),
    "KMAX": (42.08117, -122.71736, 2305),
    "KMBX": (48.39306, -100.86444, 485),
    "KMHX": (34.77591, -76.87619, 44),
    "KMKX": (42.96790, -88.55067, 312),
    "KMLB": (28.11319, -80.65408, 45),
    "KMOB": (30.67944, -88.24000, 88),
    "KMPX": (44.84889, -93.56553, 336),
    "KMQT": (46.53111, -87.54833, 465),
    "KMRX": (36.16861, -83.40194, 437),
    "KMSX": (47.04100, -113.98622, 2432),
    "KMTX": (41.26278, -112.44777, 2010),
    "KMUX": (37.15522, -121.89844, 1082),
    "KMVX": (47.52778, -97.32556, 330),
    "KMXX": (32.53665, -85.78975, 171),
    "KNKX": (32.91902, -117.04180, 321),
    "KNQA": (35.34472, -89.87333, 133),
    "KOAX": (41.32037, -96.36682, 385),
    "KOHX": (36.24722, -86.56250, 206),
    "KOKX": (40.86553, -72.86392, 61),
    "KOTX": (47.68042, -117.62677, 746),
    "KPAH": (37.06833, -88.77194, 154),
    "KPBZ": (40.53172, -80.21797, 386),
    "KPDT": (45.69065, -118.85293, 482),
    "KPOE": (31.15528, -92.97611, 144),
    "KPUX": (38.45955, -104.18135, 1635),
    "KRAX": (35.66552, -78.48975, 141),
    "KRGX": (39.75406, -119.46202, 2559),
    "KRIW": (43.06609, -108.47730, 1717),
    "KRLX": (38.31111, -81.72278, 370),
    "KRTX": (45.71504, -122.96500, 527),
    "KSFX": (43.10560, -112.68613, 1383),
    "KSGF": (37.23524, -93.40042, 419),
    "KSHV": (32.45083, -93.84125, 118),
    "KSJT": (31.37128, -100.49250, 611),
    "KSOX": (33.81773, -117.63600, 947),
    "KSRX": (35.29042, -94.36189, 225),
    "KTBW": (27.70550, -82.40178, 37),
    "KTFX": (47.45958, -111.38533, 1160),
    "KTLH": (30.39758, -84.32894, 54),
    "KTLX": (35.33336, -97.27776, 390),
    "KTWX": (38.99695, -96.23255, 431),
    "KTYX": (43.75569, -75.67986, 597),
    "KUDX": (44.12472, -102.83000, 974),
    "KUEX": (40.32083, -98.44194, 627),
    "KVAX": (30.89028, -83.00181, 101),
    "KVBX": (34.83855, -120.39791, 413),
    "KVNX": (36.74062, -98.12772, 383),
    "KVTX": (34.41202, -119.17875, 856),
    "KVWX": (38.26025, -87.72453, 190),
    "KYUX": (32.49528, -114.65671, 73),
    "PABC": (60.79194, -161.87638, 59),
    "PACG": (56.85278, -135.52916, 83),
    "PAEC": (64.51139, -165.29500, 27),
    "PAHG": (60.72591, -151.35146, 109),
    "PAIH": (59.46077, -146.30344, 40),
    "PAKC": (58.67944, -156.62944, 44),
    "PAPD": (65.03511, -147.50143, 825),
    "PGUA": (13.45583, 144.81111, 118),
    "PHKI": (21.89389, -159.55250, 104),
    "PHKM": (20.12528, -155.77777, 1209),
    "PHMO": (21.13278, -157.18027, 440),
    "PHWA": (19.09500, -155.56888, 445),
    "TJUA": (18.11567, -66.07817, 902),
}

_ARCO_STATIONS: set[str] | None = None
_ARCO_STATIONS_LOCK = threading.Lock()
_ARCO_STATIONS_FAILED_AT = 0.0  # monotonic time of the last failed listing
_ARCO_FAILURE_RETRY_S = 60.0
_ARCO_REPOS: dict[str, Any] = {}  # station_upper → icechunk.Repository (cached)
_ARCO_REPO_LOCKS: dict[str, threading.Lock] = {}
_ARCO_REPO_LOCKS_GUARD = threading.Lock()

# (station, YYYY-MM-DD) → (expires_at_monotonic | None, files).  Past UTC days are
# immutable on S3 so they never expire; "today" is cached briefly.
_DAY_CACHE: OrderedDict[tuple[str, str], tuple[float | None, list[dict[str, Any]]]] = OrderedDict()
_DAY_CACHE_LOCK = threading.Lock()
_DAY_CACHE_MAX = 64
_TODAY_TTL_S = 20.0


def list_nexrad_stations() -> list[dict[str, Any]]:
    """Return full NEXRAD station list, marking which have ARCO data."""
    arco = _get_arco_stations()
    out: list[dict[str, Any]] = []
    for s in _NEXRAD_STATIONS:
        lat, lon, elev = _NEXRAD_SITES[s]
        out.append(
            {"id": s, "arco": s in arco, "latitude": lat, "longitude": lon, "elevation_m": elev}
        )
    return out


def nearby_stations(
    lat: float, lon: float, radius_km: float, limit: int = 6, *, exclude: str | None = None
) -> list[dict[str, Any]]:
    """NEXRAD stations within *radius_km* of (lat, lon), nearest first.

    Each item is {"id", "latitude", "longitude", "distance_km", "bearing_deg"} where
    the distance is geodesic (WGS84) and the bearing is the initial azimuth (0-360, deg
    clockwise from north) from the query point to the station.  *exclude* drops one
    station id (typically the centre site itself).  limit <= 0 means no limit.
    """
    from pyproj import Geod

    if not (-90.0 <= lat <= 90.0) or not (-360.0 <= lon <= 360.0):
        raise ValueError(f"Invalid coordinates: lat={lat!r}, lon={lon!r}")
    geod = Geod(ellps="WGS84")
    skip = (exclude or "").strip().upper()
    found: list[dict[str, Any]] = []
    for sid, (slat, slon, _elev) in _NEXRAD_SITES.items():
        if sid == skip:
            continue
        az, _back, dist_m = geod.inv(lon, lat, slon, slat)
        dist_km = dist_m / 1000.0
        if dist_m < 1.0:
            az = 0.0  # coincident points: bearing is undefined
        if dist_km <= radius_km:
            found.append(
                {
                    "id": sid,
                    "latitude": slat,
                    "longitude": slon,
                    "distance_km": round(dist_km, 2),
                    "bearing_deg": round(az % 360.0, 1),
                }
            )
    found.sort(key=lambda r: (r["distance_km"], r["id"]))
    return found[:limit] if limit and limit > 0 else found


_L2_NAME_TIME_RE = re.compile(r"^[A-Z0-9]{4}(\d{8})_(\d{6})")


def scan_time(name: str) -> datetime | None:
    """UTC start time encoded in a Level II file name (KLOT20240101_000500_V06)."""
    m = _L2_NAME_TIME_RE.match(name.rsplit("/", 1)[-1])
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def nearest_scan_to_time(
    files: list[dict[str, Any]], target: datetime, *, max_gap: timedelta | None = None
) -> dict[str, Any] | None:
    """The file whose encoded scan time is closest to *target* (naive = UTC).

    Files whose names carry no parsable time are ignored.  Ties go to the earlier scan.
    Returns None when nothing qualifies or the best candidate is further than *max_gap*.
    """
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    best: tuple[float, datetime, dict[str, Any]] | None = None
    for f in files:
        name = f.get("name", "")
        t = None if name.endswith("_MDM") else scan_time(name)
        if t is None:
            continue
        key = (abs((t - target).total_seconds()), t)
        if best is None or key < (best[0], best[1]):
            best = (key[0], t, f)
    if best is None:
        return None
    if max_gap is not None and best[0] > max_gap.total_seconds():
        return None
    return best[2]


def _get_arco_stations() -> set[str]:
    """Stations present in the ARCO bucket.

    Cached for the process lifetime on success.  A failed listing is *not* cached
    (it used to poison the list with {"KLOT"} until restart); the fallback is
    returned and the listing is retried after a minute.
    """
    global _ARCO_STATIONS, _ARCO_STATIONS_FAILED_AT
    cached = _ARCO_STATIONS
    if cached is not None:
        return cached
    with _ARCO_STATIONS_LOCK:
        if _ARCO_STATIONS is not None:
            return _ARCO_STATIONS
        failed_at = _ARCO_STATIONS_FAILED_AT
        if failed_at and time.monotonic() - failed_at < _ARCO_FAILURE_RETRY_S:
            return {"KLOT"}
        try:
            paginator = _s3().get_paginator("list_objects_v2")
            found: set[str] = set()
            for page in paginator.paginate(Bucket=_NEXRAD_ARCO_BUCKET, Delimiter="/"):
                found.update(cp["Prefix"].rstrip("/") for cp in page.get("CommonPrefixes", []))
        except Exception as exc:
            logger.warning("Could not list ARCO stations: %s", exc)
            _ARCO_STATIONS_FAILED_AT = time.monotonic()
            return {"KLOT"}
        if not found:
            _ARCO_STATIONS_FAILED_AT = time.monotonic()
            return {"KLOT"}
        _ARCO_STATIONS = found
        return found


# ── NEXRAD Level II: archive ──────────────────────────────────────────────────


def _parse_date(date: str) -> datetime:
    try:
        return datetime.strptime(date.strip(), "%Y-%m-%d")
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid date {date!r}: expected YYYY-MM-DD") from exc


def list_nexrad_l2_files(
    station: str, date: str, *, use_cache: bool = True
) -> list[dict[str, Any]]:
    """List Level II files for a station on a given UTC date (YYYY-MM-DD).

    Results are cached: past days indefinitely (S3 archive days are immutable),
    the current UTC day for a few seconds.
    """
    st = _normalize_station(station)
    dt = _parse_date(date)
    day_key = dt.strftime("%Y-%m-%d")
    key = (st, day_key)

    if use_cache:
        with _DAY_CACHE_LOCK:
            entry = _DAY_CACHE.get(key)
            if entry is not None and (entry[0] is None or entry[0] > time.monotonic()):
                _DAY_CACHE.move_to_end(key)
                return copy.deepcopy(entry[1])

    prefix = f"{dt.year}/{dt.month:02d}/{dt.day:02d}/{st}/"
    paginator = _s3().get_paginator("list_objects_v2")

    files: list[dict[str, Any]] = []
    try:
        for page in paginator.paginate(Bucket=_NEXRAD_L2_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                name = obj["Key"].rsplit("/", 1)[-1]
                if not name or name.endswith("_MDM") or not name.startswith(st):
                    continue
                files.append(
                    {
                        "name": name,
                        "path": f"s3://{_NEXRAD_L2_BUCKET}/{obj['Key']}",
                        "size": obj.get("Size", 0),
                        "last_modified": str(obj.get("LastModified", "")),
                    }
                )
    except Exception as exc:
        raise RuntimeError(f"Could not list files for {st} on {day_key}: {exc}") from exc

    files.sort(key=lambda f: f["name"])

    is_past = dt.date() < datetime.now(UTC).date()
    expires = None if is_past else time.monotonic() + _TODAY_TTL_S
    # Don't pin an empty listing of a past day forever: the station may simply not have
    # uploaded yet (or the user typed the wrong day); keep it short-lived instead.
    if is_past and not files:
        expires = time.monotonic() + _TODAY_TTL_S
    with _DAY_CACHE_LOCK:
        _DAY_CACHE[key] = (expires, copy.deepcopy(files))
        _DAY_CACHE.move_to_end(key)
        while len(_DAY_CACHE) > _DAY_CACHE_MAX:
            _DAY_CACHE.popitem(last=False)
    return files


# ── NEXRAD Level II: real-time ────────────────────────────────────────────────


def get_latest_nexrad_l2(station: str) -> dict[str, Any]:
    """Return the most recent Level II file for a station (today or yesterday, UTC)."""
    st = _normalize_station(station)
    now = datetime.now(UTC)
    last_exc: Exception | None = None
    for delta_days in (0, 1):
        date_str = (now - timedelta(days=delta_days)).strftime("%Y-%m-%d")
        try:
            files = list_nexrad_l2_files(st, date_str, use_cache=False)
        except Exception as exc:
            last_exc = exc
            continue
        if files:
            return files[-1]
    if last_exc is not None:
        raise RuntimeError(f"No recent Level II files found for {st}: {last_exc}") from last_exc
    raise RuntimeError(f"No recent Level II files found for {st}")


_OFFLINE_ERROR_NAMES = frozenset(
    {
        "EndpointConnectionError",
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "ConnectionClosedError",
        "ProxyConnectionError",
        "SSLError",
        "NoCredentialsError",
        "ConnectionError",
        "gaierror",
    }
)
_INFO_TTL_S = 20.0
_INFO_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_INFO_CACHE_LOCK = threading.Lock()


def _is_offline_error(exc: BaseException | None) -> bool:
    """True when *exc* (or anything it was raised from) is a network-level failure."""
    seen = 0
    while exc is not None and seen < 8:
        if type(exc).__name__ in _OFFLINE_ERROR_NAMES:
            return True
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def latest_scan_info(station: str, *, use_cache: bool = True) -> dict[str, Any]:
    """Describe the newest Level II volume of *station* without downloading it.

    One S3 listing (today's folder, else yesterday's, UTC) - metadata only.  Never raises: the
    result always carries ``status``: ``"ok"`` | ``"not_found"`` (listed, no scan) | ``"offline"``
    (network failure) | ``"error"`` (bad station or any other failure) plus a ``message``.

    On ``"ok"``: ``name``, ``path`` (s3:// URL, exactly what ``open_nexrad_l2`` takes), ``size``
    (bytes, from the listing), ``scan_time`` (UTC, parsed from the file name = volume start),
    ``last_modified`` (when the object was written to S3), ``now`` (this machine's UTC clock) and
    ``age_s`` (``now - scan_time``, clamped to >= 0).  Nothing is inferred beyond what the
    listing says: no scan means no age.
    """
    try:
        st = _normalize_station(station)
    except ValueError as exc:
        return {"status": "error", "station": str(station), "message": str(exc)}

    now_mono = time.monotonic()
    if use_cache:
        with _INFO_CACHE_LOCK:
            hit = _INFO_CACHE.get(st)
            if hit is not None and hit[0] > now_mono:
                return _with_age(copy.deepcopy(hit[1]))

    now = datetime.now(UTC)
    last_exc: BaseException | None = None
    listed_ok = False
    result: dict[str, Any] | None = None
    for delta_days in (0, 1):
        day = (now - timedelta(days=delta_days)).strftime("%Y-%m-%d")
        try:
            files = list_nexrad_l2_files(st, day, use_cache=False)
        except Exception as exc:
            last_exc = exc
            continue
        listed_ok = True
        timed = [(scan_time(f["name"]), f) for f in files]
        timed = [(t, f) for t, f in timed if t is not None]
        if timed:
            t, f = max(timed, key=lambda tf: tf[0])
            result = {
                "status": "ok",
                "station": st,
                "name": f["name"],
                "path": f["path"],
                "size": int(f.get("size") or 0),
                "scan_time": t.isoformat().replace("+00:00", "Z"),
                "last_modified": f.get("last_modified", ""),
                "source": f"s3://{_NEXRAD_L2_BUCKET}/",
            }
            break

    if result is None:
        if not listed_ok and last_exc is not None:
            offline = _is_offline_error(last_exc)
            result = {
                "status": "offline" if offline else "error",
                "station": st,
                "message": (
                    "Could not reach the AWS public bucket (offline or blocked)."
                    if offline
                    else f"Listing failed: {last_exc}"
                ),
            }
        else:
            result = {
                "status": "not_found",
                "station": st,
                "message": f"No Level II volume for {st} today or yesterday (UTC).",
            }
    if result["status"] in ("ok", "not_found"):
        with _INFO_CACHE_LOCK:
            _INFO_CACHE[st] = (now_mono + _INFO_TTL_S, copy.deepcopy(result))
            while len(_INFO_CACHE) > 256:
                _INFO_CACHE.pop(next(iter(_INFO_CACHE)))
    return _with_age(result)


def _with_age(info: dict[str, Any]) -> dict[str, Any]:
    """Stamp *info* with this machine's UTC clock and, for an ok result, the scan's age."""
    now = datetime.now(UTC)
    info["now"] = now.isoformat().replace("+00:00", "Z")
    if info.get("status") == "ok":
        t = datetime.fromisoformat(info["scan_time"].replace("Z", "+00:00"))
        info["age_s"] = max(0.0, (now - t).total_seconds())
    return info


# ── NEXRAD ARCO (icechunk / Zarr v3) ─────────────────────────────────────────


def _repo_lock(station_upper: str) -> threading.Lock:
    with _ARCO_REPO_LOCKS_GUARD:
        lock = _ARCO_REPO_LOCKS.get(station_upper)
        if lock is None:
            lock = _ARCO_REPO_LOCKS[station_upper] = threading.Lock()
        return lock


def _open_icechunk_session(station: str):
    """Open a readonly icechunk session for a NEXRAD station.

    The Repository object is cached per station so the expensive S3 manifest
    download only happens once per process lifetime (guarded by a per-station
    lock so concurrent callers don't each spend ~30 s opening it).  Each call
    still creates a fresh readonly_session("main") so callers see the latest snapshot.
    """
    try:
        import icechunk
    except ImportError as exc:
        raise RuntimeError("icechunk is required: pip install icechunk") from exc
    station_upper = _normalize_station(station)
    repo = _ARCO_REPOS.get(station_upper)
    if repo is None:
        with _repo_lock(station_upper):
            repo = _ARCO_REPOS.get(station_upper)
            if repo is None:
                storage = icechunk.s3_storage(
                    bucket=_NEXRAD_ARCO_BUCKET,
                    prefix=station_upper,
                    region="us-east-1",
                    anonymous=True,
                )
                try:
                    logger.info(
                        "Opening icechunk repository for %s (first time, may take ~30s)…",
                        station_upper,
                    )
                    repo = icechunk.Repository.open(storage)
                except Exception as exc:
                    raise RuntimeError(f"Could not open ARCO store for {station}: {exc}") from exc
                _ARCO_REPOS[station_upper] = repo
                logger.info("icechunk repository for %s cached", station_upper)
    return repo.readonly_session("main")


def list_arco_scans(station: str, limit: int = 50) -> list[dict[str, Any]]:
    """List available VCP types in the ARCO icechunk store for a station.

    Each 'scan' corresponds to one Volume Coverage Pattern (VCP) type,
    e.g. VCP-12, VCP-34. Each VCP contains all sweeps and all historical
    scan times along the vcp_time dimension.  At most *limit* VCPs are returned
    (``limit <= 0`` means no limit).
    """
    import zarr

    station_upper = _normalize_station(station)
    session = _open_icechunk_session(station_upper)
    try:
        root = zarr.open_group(session.store, mode="r")
        # groups() enumerates group children directly instead of fetching each key's
        # metadata separately and isinstance-checking it.
        vcps = sorted(name for name, _ in root.groups() if not name.startswith("."))
    except Exception as exc:
        raise RuntimeError(f"Could not list ARCO VCPs for {station}: {exc}") from exc

    if limit and limit > 0:
        vcps = vcps[:limit]
    return [
        {"key": vcp, "path": f"icechunk://{_NEXRAD_ARCO_BUCKET}/{station_upper}/{vcp}"}
        for vcp in vcps
    ]

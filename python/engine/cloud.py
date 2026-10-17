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
from datetime import datetime, timedelta, timezone
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
    return [{"id": s, "arco": s in arco} for s in _NEXRAD_STATIONS]


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

    is_past = dt.date() < datetime.now(timezone.utc).date()
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
    now = datetime.now(timezone.utc)
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

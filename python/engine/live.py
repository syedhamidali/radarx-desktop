"""The live map: the newest NEXRAD Level II volume of a few radars, merged into one composite.

Two steps, so the app can ask the user before anything is downloaded:

1. :func:`check` lists the newest volume of every station (one S3 listing each, metadata only,
   through :func:`engine.cloud.latest_scan_info`) and says which of them the cache already holds.
2. :func:`update` downloads the volumes the app names (and only those), keeps **one volume per
   station** in a :class:`LiveCache` (older volumes of a station are dropped, the whole cache is
   bounded by a station count and a byte cap), and builds the composite with
   :func:`engine.mosaic.build_mosaic`.

Nothing here touches the network on its own: ``check`` lists, ``update`` downloads exactly the
``path`` values it is given. The composite is RETRIEVED (a merged, gridded product); the volumes
behind it are OBSERVED. The provenance of the frame is the mosaic's.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from engine.cloud import _normalize_station, latest_scan_info

logger = logging.getLogger(__name__)

#: Most stations one live session may hold (the app allows the same number of default radars).
MAX_STATIONS = 8
#: Cap on the sum of the listed sizes of the held volumes (bytes).
MAX_BYTES = 768 * 1024 * 1024
#: Default grid for the live composite: coarser than the mosaic panel's 1 km so a frame of
#: several radars builds in seconds; stated in the frame's ``grid`` and provenance.
DEFAULT_GRID: dict[str, Any] = {"resolution_m": 2000.0, "max_range_km": 230.0, "extent": "auto"}
DEFAULT_MERGE: dict[str, Any] = {"enabled": True}

ProgressFn = Callable[[int, str], None]


class LiveError(Exception):
    """A live update could not be made (bad request, nothing to merge, download failure)."""


@dataclass
class HeldVolume:
    station: str
    name: str
    path: str
    size: int
    scan_time: str | None
    reader: Any
    opened_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "station": self.station,
            "name": self.name,
            "path": self.path,
            "size": int(self.size),
            "scan_time": self.scan_time,
        }


class LiveCache:
    """One open volume per station, newest wins, bounded by count and bytes.

    ``hold`` replaces the station's previous volume (its reader is closed) and then evicts the
    least recently opened *other* stations while the cache is over ``max_stations`` or
    ``max_bytes``. The volume just held is never evicted by its own insertion.
    """

    def __init__(self, max_stations: int = MAX_STATIONS, max_bytes: int = MAX_BYTES) -> None:
        self.max_stations = max(1, int(max_stations))
        self.max_bytes = max(0, int(max_bytes))
        self._held: dict[str, HeldVolume] = {}
        self._lock = threading.Lock()

    def get(self, station: str) -> HeldVolume | None:
        with self._lock:
            return self._held.get(station.upper())

    def holds(self, station: str, name: str) -> bool:
        h = self.get(station)
        return h is not None and h.name == name

    def hold(self, vol: HeldVolume) -> list[str]:
        """Keep *vol* (replacing the station's older volume). Returns the evicted station ids."""
        evicted: list[str] = []
        to_close: list[Any] = []
        with self._lock:
            old = self._held.pop(vol.station, None)
            if old is not None and old.reader is not vol.reader:
                to_close.append(old.reader)
            self._held[vol.station] = vol
            while len(self._held) > self.max_stations or self.nbytes_locked() > self.max_bytes:
                victims = [h for h in self._held.values() if h.station != vol.station]
                if not victims:
                    break
                victim = min(victims, key=lambda h: h.opened_at)
                self._held.pop(victim.station)
                to_close.append(victim.reader)
                evicted.append(victim.station)
        for r in to_close:
            _close_quietly(r)
        return evicted

    def drop(self, station: str) -> None:
        with self._lock:
            h = self._held.pop(station.upper(), None)
        if h is not None:
            _close_quietly(h.reader)

    def clear(self) -> None:
        with self._lock:
            held = list(self._held.values())
            self._held.clear()
        for h in held:
            _close_quietly(h.reader)

    def keep_only(self, stations: list[str]) -> list[str]:
        """Drop every station not in *stations*; returns what was dropped."""
        keep = {s.upper() for s in stations}
        with self._lock:
            gone = [h for s, h in self._held.items() if s not in keep]
            for h in gone:
                self._held.pop(h.station)
        for h in gone:
            _close_quietly(h.reader)
        return [h.station for h in gone]

    def nbytes_locked(self) -> int:
        return sum(int(h.size) for h in self._held.values())

    @property
    def nbytes(self) -> int:
        with self._lock:
            return self.nbytes_locked()

    def stations(self) -> list[str]:
        with self._lock:
            return list(self._held)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "stations": [h.as_dict() for h in self._held.values()],
                "bytes": self.nbytes_locked(),
                "max_bytes": self.max_bytes,
                "max_stations": self.max_stations,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._held)


def _close_quietly(reader: Any) -> None:
    try:
        reader.close()
    except Exception as exc:  # pragma: no cover - best effort
        logger.debug("live: closing a reader failed: %s", exc)


#: The engine's one live cache (one live map per app).
LIVE = LiveCache()


def normalize_stations(stations: Any) -> list[str]:
    """Upper-case, de-duplicated station ids in the given order; at most MAX_STATIONS."""
    if not isinstance(stations, list) or not stations:
        raise LiveError("'stations' must be a non-empty list of station ids")
    out: list[str] = []
    for s in stations:
        if not isinstance(s, str):
            raise LiveError("'stations' must be a list of strings")
        st = _normalize_station(s)  # ValueError for a bad id
        if st not in out:
            out.append(st)
    if len(out) > MAX_STATIONS:
        raise LiveError(f"at most {MAX_STATIONS} radars on the live map")
    return out


def check(stations: list[str], cache: LiveCache = LIVE) -> list[dict[str, Any]]:
    """Newest volume of every station (metadata only) and whether the cache already holds it.

    One entry per station, in order, each a :func:`latest_scan_info` result (``status`` ok /
    not_found / offline / error, ``name``, ``path``, ``size``, ``scan_time``, ``age_s``) plus
    ``held`` (True when this very volume is already open in the live cache).
    """
    out: list[dict[str, Any]] = []
    for st in stations:
        info = latest_scan_info(st)
        info["held"] = bool(info.get("status") == "ok" and cache.holds(st, str(info.get("name"))))
        out.append(info)
    return out


def _open_s3(path: str) -> Any:
    from engine.reader import RadarReader

    reader = RadarReader()
    reader.open_s3_file(path)
    return reader


def update(
    wanted: list[dict[str, Any]],
    *,
    cache: LiveCache = LIVE,
    variable: str = "DBZH",
    product: str = "composite",
    grid: dict[str, Any] | None = None,
    merge: dict[str, Any] | None = None,
    progress: ProgressFn | None = None,
    opener: Callable[[str], Any] = _open_s3,
) -> tuple[Any, list[dict[str, Any]]]:
    """Download what is missing, keep the newest volume per station, build the composite.

    *wanted* is a list of ``{station, path, name, size, scan_time?}``: exactly the files the user
    agreed to. A station whose volume is already held is not downloaded again. A station whose
    download fails is reported and left out; the composite is built from the rest (at least one).
    Returns ``(MosaicRecord, radars)`` where ``radars`` is one status dict per wanted station.
    """
    from engine import mosaic

    report = progress or (lambda _p, _m: None)
    if not isinstance(wanted, list) or not wanted:
        raise LiveError("'files' must be a non-empty list")
    plan: list[tuple[str, dict[str, Any]]] = []
    for item in wanted:
        if not isinstance(item, dict):
            raise LiveError("'files' entries must be objects")
        try:
            st = _normalize_station(str(item.get("station", "")))
        except ValueError as exc:
            raise LiveError(str(exc)) from exc
        path = item.get("path")
        name = item.get("name")
        if not isinstance(path, str) or not path.startswith("s3://") or not isinstance(name, str):
            raise LiveError(f"{st}: 'path' (s3://...) and 'name' are required")
        if any(p[0] == st for p in plan):
            raise LiveError(f"{st} is listed twice")
        plan.append((st, item))
    if len(plan) > MAX_STATIONS:
        raise LiveError(f"at most {MAX_STATIONS} radars on the live map")

    cache.keep_only([st for st, _ in plan])
    radars: list[dict[str, Any]] = []
    n = len(plan)
    for i, (st, item) in enumerate(plan):
        name = str(item["name"])
        status: dict[str, Any] = {"station": st, "name": name, "scan_time": item.get("scan_time")}
        if cache.holds(st, name):
            status["status"] = "ok"
            status["downloaded"] = False
            radars.append(status)
            continue
        report(int(5 + 60 * i / n), f"Downloading {name} ({i + 1}/{n})")
        try:
            reader = opener(str(item["path"]))
        except Exception as exc:
            logger.warning("live: %s download failed: %s", st, exc)
            status["status"] = "error"
            status["message"] = str(exc)[:300]
            cache.drop(st)  # its older volume would be a stale neighbour of the newer ones
            radars.append(status)
            continue
        size = item.get("size")
        evicted = cache.hold(
            HeldVolume(
                station=st,
                name=name,
                path=str(item["path"]),
                size=int(size) if isinstance(size, (int, float)) else 0,
                scan_time=item.get("scan_time") if isinstance(item.get("scan_time"), str) else None,
                reader=reader,
            )
        )
        if evicted:
            logger.info("live: evicted %s (cache bound)", ", ".join(evicted))
        status["status"] = "ok"
        status["downloaded"] = True
        radars.append(status)

    pairs = []
    for st, _ in plan:
        h = cache.get(st)
        if h is not None and h.reader is not None:
            pairs.append((st, h.reader))
    if not pairs:
        failed = "; ".join(f"{r['station']}: {r.get('message', 'failed')}" for r in radars)
        raise LiveError(f"No radar volume could be opened ({failed})")

    report(70, f"Merging {len(pairs)} radar{'s' if len(pairs) != 1 else ''}")
    g = {**DEFAULT_GRID, **(grid or {})}
    m = {**DEFAULT_MERGE, **(merge or {})}

    def _progress(p: int, msg: str) -> None:
        report(70 + int(p * 0.28), msg)

    record = mosaic.build_mosaic(
        pairs,
        variable,
        product,
        g,
        m,
        progress=_progress,
        keep_volume=False,
    )
    return record, radars


def frame_header(
    record: Any, radars: list[dict[str, Any]], cache: LiveCache = LIVE
) -> dict[str, Any]:
    """The JSON header of a ``live_frame`` message (without ``type`` / ``request_id``)."""
    from engine import layer_provenance

    h = record.header()
    return {
        **h,
        "provenance": layer_provenance.for_mosaic_header(h),
        "stations": radars,
        "cache": cache.status(),
    }

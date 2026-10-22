"""The live map: the newest volume of a few radars, merged into one composite.

Two networks are live: **NEXRAD** (Level II from the public Unidata bucket) and **DWD** (the German
Weather Service's Open Data volumes, through :mod:`engine.radar_networks`). A radar is named by one
unambiguous id: a NEXRAD station is its bare four-letter id (``KTLX``, the form older settings
already use), a DWD radar is ``dwd:ess``. Nothing else is claimed to work (see docs/LIVE.md).

Two steps, so the app can ask the user before anything is downloaded:

1. :func:`check` lists the newest volume of every radar (one listing each, metadata only: an S3
   listing for NEXRAD, the two folder indexes for DWD) and says which of them the cache already
   holds.
2. :func:`update` downloads the volumes the app names (and only those), keeps **one volume per
   station** in a :class:`LiveCache` (older volumes of a station are dropped, the whole cache is
   bounded by a station count and a byte cap), and builds the composite with
   :func:`engine.mosaic.build_mosaic`.

Nothing here touches the network on its own: ``check`` lists, ``update`` downloads exactly the
``path`` values it is given. The composite is RETRIEVED (a merged, gridded product); the volumes
behind it are OBSERVED. The provenance of the frame is the mosaic's.

The downloaded file of every held volume is kept on disk (in a private folder of the system temp
area) for as long as the cache holds the volume, so :func:`pin_volumes` can hand the same file to a
normal open (2D / 3D viewers, Mosaic, Multi-Doppler, export): no new download, and the opened file
survives the next live update. A pin is released when the file is closed (:func:`release_reader`);
at most :data:`MAX_PINNED` volumes can be pinned, and the disk they use is reported.

The picture is drawn for the eye, not for analysis: reflectivity below a display floor (5 dBZ by
default, the NWS convention) is not drawn and the image is smoothed. The grid values in the
cache, in the exports and in the hover read-out are not changed by either.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import math
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from engine.cloud import _normalize_station, latest_scan_info

logger = logging.getLogger(__name__)

#: Most stations one live session may hold (the app allows the same number of default radars).
MAX_STATIONS = 8
#: Cap on the sum of the listed sizes of the held volumes (bytes).
MAX_BYTES = 768 * 1024 * 1024
#: Most held volumes that may be open as normal files at once (each is a pinned file on disk).
MAX_PINNED = 8
#: Grid of the live composite: as fine as the machine budget allows for few radars (1 km up to
#: four radars), coarser for more so a frame still builds in seconds. The resolution actually used
#: is stated in the frame's ``grid`` and its provenance (the resource governor may coarsen it).
GRID_BY_RADARS: tuple[tuple[int, float], ...] = ((4, 1000.0), (6, 1500.0), (MAX_STATIONS, 2000.0))
#: Cells the live grid may have: radars spread far apart get a coarser grid, not a longer wait.
MAX_LIVE_CELLS = 2_500_000
DEFAULT_GRID: dict[str, Any] = {"max_range_km": 230.0, "extent": "auto"}
DEFAULT_MERGE: dict[str, Any] = {"enabled": True}
#: The NWS convention: reflectivity below about 5 dBZ (clear-air returns, insects, noise) is not
#: drawn. A display choice only: the grid values are not changed, and the setting is visible.
DEFAULT_FLOOR_DBZ = 5.0
MIN_FLOOR_DBZ = -20.0
MAX_FLOOR_DBZ = 40.0
#: Largest PNG (pixels) drawn for the map; the smoothing factor is lowered to fit.
MAX_DISPLAY_PIXELS = 4_000_000
#: Ground distance one drawn pixel should cover at most; finer grids are smoothed by 2 or 3.
DISPLAY_PIXEL_M = 500.0
MAX_SMOOTHING = 3

ProgressFn = Callable[[int, str], None]


class LiveError(Exception):
    """A live update could not be made (bad request, nothing to merge, download failure)."""


# ---------------------------------------------------------------------------
# Where the files live
# ---------------------------------------------------------------------------

_ROOT_LOCK = threading.Lock()
_ROOT: Path | None = None


def _root() -> Path:
    """The live map's private folder in the system temp area (made on first use)."""
    global _ROOT
    with _ROOT_LOCK:
        if _ROOT is None or not _ROOT.is_dir():
            _sweep_stale_roots()
            _ROOT = Path(tempfile.mkdtemp(prefix=f"{_ROOT_PREFIX}{os.getpid()}-"))
            atexit.register(_cleanup_root, _ROOT)
        return _ROOT


_ROOT_PREFIX = "radarx-live-"


def _sweep_stale_roots() -> None:
    """Delete the folders of engines that no longer run (killed or crashed before their exit
    cleanup), so held and pinned volumes never pile up on disk. POSIX only (a pid probe)."""
    if os.name != "posix":
        return
    try:
        candidates = list(Path(tempfile.gettempdir()).glob(f"{_ROOT_PREFIX}*"))
    except OSError:
        return
    for d in candidates:
        parts = d.name[len(_ROOT_PREFIX) :].split("-", 1)
        if len(parts) != 2 or not parts[0].isdigit() or not d.is_dir():
            continue
        pid = int(parts[0])
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue  # alive, or not ours to signal


def _cleanup_root(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)


def held_dir() -> Path:
    """Folder of the files the live cache holds (deleted with the volume)."""
    d = _root() / "held"
    d.mkdir(exist_ok=True)
    return d


def pinned_dir() -> Path:
    """Folder of the pinned copies of volumes that were opened as files (deleted on close)."""
    d = _root() / "pinned"
    d.mkdir(exist_ok=True)
    return d


def _remove(path: str | os.PathLike[str] | None) -> None:
    """Delete a file (and its private folder when it is empty); never raises."""
    if not path:
        return
    p = Path(path)
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:  # e.g. still open on Windows: the folder goes at exit
        logger.debug("live: could not remove %s: %s", p, exc)
        return
    with contextlib.suppress(OSError):
        p.parent.rmdir()  # a pin sits in its own folder; the shared folder is not empty


# ---------------------------------------------------------------------------
# The cache of held volumes
# ---------------------------------------------------------------------------


@dataclass
class HeldVolume:
    station: str
    name: str
    path: str
    size: int
    scan_time: str | None
    reader: Any
    opened_at: float = field(default_factory=time.time)
    #: The downloaded file kept on disk (None when the opener did not keep one).
    local_path: str | None = None
    #: The network the volume came from ("nexrad" or "dwd").
    provider: str = "nexrad"

    @property
    def on_disk(self) -> bool:
        return bool(self.local_path and Path(self.local_path).is_file())

    @property
    def openable(self) -> bool:
        """True when the volume can be handed to a normal open without a new download: its file is
        still on disk (NEXRAD), or it was assembled from several files and is held in memory (DWD),
        in which case the opened file shares the same data (no copy, no disk)."""
        if self.on_disk:
            return True
        return self.local_path is None and getattr(self.reader, "datatree", None) is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "station": self.station,
            "provider": self.provider,
            "name": self.name,
            "path": self.path,
            "size": int(self.size),
            "scan_time": self.scan_time,
            "on_disk": self.on_disk,
            "openable": self.openable,
        }


def site_key(station: str) -> str:
    """The key of a radar id: a bare NEXRAD id is upper case, ``dwd:ess`` is lower case."""
    s = str(station).strip()
    return s.lower() if ":" in s else s.upper()


class LiveCache:
    """One open volume per station, newest wins, bounded by count and bytes.

    ``hold`` replaces the station's previous volume (its reader is closed and its kept file
    deleted) and then evicts the least recently opened *other* stations while the cache is over
    ``max_stations`` or ``max_bytes``. The volume just held is never evicted by its own insertion.
    A volume that was opened as a file keeps its own pinned copy (see :class:`PinRegistry`), so
    none of this touches it.
    """

    def __init__(self, max_stations: int = MAX_STATIONS, max_bytes: int = MAX_BYTES) -> None:
        self.max_stations = max(1, int(max_stations))
        self.max_bytes = max(0, int(max_bytes))
        self._held: dict[str, HeldVolume] = {}
        self._lock = threading.Lock()

    def get(self, station: str) -> HeldVolume | None:
        with self._lock:
            return self._held.get(site_key(station))

    def holds(self, station: str, name: str) -> bool:
        h = self.get(station)
        return h is not None and h.name == name

    def hold(self, vol: HeldVolume) -> list[str]:
        """Keep *vol* (replacing the station's older volume). Returns the evicted station ids."""
        evicted: list[str] = []
        to_drop: list[HeldVolume] = []
        with self._lock:
            old = self._held.pop(vol.station, None)
            if old is not None and old.reader is not vol.reader:
                to_drop.append(old)
            self._held[vol.station] = vol
            while len(self._held) > self.max_stations or self.nbytes_locked() > self.max_bytes:
                victims = [h for h in self._held.values() if h.station != vol.station]
                if not victims:
                    break
                victim = min(victims, key=lambda h: h.opened_at)
                self._held.pop(victim.station)
                to_drop.append(victim)
                evicted.append(victim.station)
        for h in to_drop:
            _discard(h)
        return evicted

    def drop(self, station: str) -> None:
        with self._lock:
            h = self._held.pop(site_key(station), None)
        if h is not None:
            _discard(h)

    def clear(self) -> None:
        with self._lock:
            held = list(self._held.values())
            self._held.clear()
        for h in held:
            _discard(h)

    def keep_only(self, stations: list[str]) -> list[str]:
        """Drop every station not in *stations*; returns what was dropped."""
        keep = {site_key(s) for s in stations}
        with self._lock:
            gone = [h for s, h in self._held.items() if s not in keep]
            for h in gone:
                self._held.pop(h.station)
        for h in gone:
            _discard(h)
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


def _discard(h: HeldVolume) -> None:
    """Let go of a held volume: close its reader and delete the file the cache kept."""
    _close_quietly(h.reader)
    _remove(h.local_path)


#: The engine's one live cache (one live map per app).
LIVE = LiveCache()


# ---------------------------------------------------------------------------
# Opening held volumes as normal files (pinning)
# ---------------------------------------------------------------------------


@dataclass
class PinnedVolume:
    """A held volume that was handed to a normal open, released when that file is closed.

    A NEXRAD volume owns a file (``path``: a hard link to the held file, or a copy). A DWD volume
    is assembled in memory from many small files and has no single file: the opened file shares
    its data (``tree``), which costs no disk.
    """

    station: str
    name: str
    path: str | None  # the pinned file; None for a volume held in memory
    source: str  # where it came from (what the opened file reports as its origin)
    size: int  # bytes on disk while the live cache no longer holds the volume (0 for memory)
    copied: bool  # True when the file had to be copied (no hard link possible)
    scan_time: str | None = None
    file_id: str | None = None
    provider: str = "nexrad"
    tree: Any = None  # the shared datatree of a volume held in memory
    volume_size: int = 0  # the size of the volume as listed (what was downloaded)

    @property
    def key(self) -> tuple[str, str]:
        return (self.station, self.name)

    def as_dict(self) -> dict[str, Any]:
        return {
            "station": self.station,
            "provider": self.provider,
            "name": self.name,
            "size": int(self.size),
            "volume_size": int(self.volume_size),
            "kind": "memory" if self.path is None else "file",
            "scan_time": self.scan_time,
            "file_id": self.file_id,
            "copied": self.copied,
        }


class PinRegistry:
    """The volumes opened as files, bounded in number; each owns a file until it is released."""

    def __init__(self, max_pinned: int = MAX_PINNED) -> None:
        self.max_pinned = max(1, int(max_pinned))
        self._pins: dict[tuple[str, str], PinnedVolume] = {}
        self._lock = threading.Lock()

    def get(self, station: str, name: str) -> PinnedVolume | None:
        with self._lock:
            return self._pins.get((site_key(station), name))

    def add(self, pin: PinnedVolume) -> None:
        with self._lock:
            if len(self._pins) >= self.max_pinned and pin.key not in self._pins:
                raise LiveError(
                    f"At most {self.max_pinned} live volumes can be open as files at once; "
                    "close one of them first."
                )
            self._pins[pin.key] = pin

    def bind(self, key: tuple[str, str], file_id: str) -> None:
        with self._lock:
            if key in self._pins:
                self._pins[key].file_id = file_id

    def release(self, key: tuple[str, str]) -> bool:
        with self._lock:
            pin = self._pins.pop(key, None)
        if pin is None:
            return False
        _remove(pin.path)
        return True

    def clear(self) -> None:
        with self._lock:
            pins = list(self._pins.values())
            self._pins.clear()
        for p in pins:
            _remove(p.path)

    def __len__(self) -> int:
        with self._lock:
            return len(self._pins)

    @property
    def nbytes(self) -> int:
        with self._lock:
            return sum(int(p.size) for p in self._pins.values())

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "count": len(self._pins),
                "max": self.max_pinned,
                "bytes": sum(int(p.size) for p in self._pins.values()),
                "volumes": [p.as_dict() for p in self._pins.values()],
            }


#: The volumes of the live map that are open as files.
PINS = PinRegistry()


def pin_volumes(
    stations: list[str],
    cache: LiveCache = LIVE,
    pins: PinRegistry | None = None,
) -> list[PinnedVolume]:
    """Pin the held volume of every station: one file each, in the order given.

    A volume that is already open as a file is returned as it is (opening it again would only
    duplicate the data). A station without a held volume, or whose kept file is gone, is an error
    that says what to do; nothing is downloaded here. If one station fails, the pins made by this
    call are released again.
    """
    if not isinstance(stations, list) or not stations:
        raise LiveError("'stations' must be a non-empty list of station ids")
    if pins is None:
        pins = PINS
    out: list[PinnedVolume] = []
    made: list[PinnedVolume] = []
    try:
        for raw in stations:
            st, _prov, _site = parse_site_id(str(raw))
            h = cache.get(st)
            if h is None:
                raise LiveError(
                    f"{st}: the live map holds no volume of this radar. Update the live map first "
                    "(a radar without a recent volume has nothing to open)."
                )
            existing = pins.get(st, h.name)
            if existing is not None and (existing.path is None or Path(existing.path).is_file()):
                out.append(existing)
                continue
            if h.local_path is None and getattr(h.reader, "datatree", None) is not None:
                # held in memory (DWD): the opened file shares the data, no file, no disk
                pin = PinnedVolume(
                    station=st,
                    name=h.name,
                    path=None,
                    source=str(getattr(h.reader, "_path", None) or h.path),
                    size=0,
                    copied=False,
                    scan_time=h.scan_time,
                    provider=h.provider,
                    tree=h.reader.datatree,
                    volume_size=int(h.size),
                )
                pins.add(pin)
                made.append(pin)
                out.append(pin)
                continue
            src = h.local_path
            if not src or not Path(src).is_file():
                raise LiveError(
                    f"{st}: the file of {h.name} is no longer on disk. Press Update now to fetch "
                    "it again (the app asks first)."
                )
            folder = pinned_dir() / uuid.uuid4().hex[:12]
            folder.mkdir()
            dst = folder / h.name
            copied = False
            try:
                os.link(src, dst)
            except OSError:
                try:
                    shutil.copy2(src, dst)
                except OSError as exc:
                    _remove(dst)
                    raise LiveError(f"{st}: could not keep a copy of {h.name}: {exc}") from exc
                copied = True
            try:
                size = dst.stat().st_size
            except OSError:
                size = int(h.size)
            pin = PinnedVolume(
                station=st,
                name=h.name,
                path=str(dst),
                source=h.path,
                size=size,
                copied=copied,
                scan_time=h.scan_time,
                provider=h.provider,
                volume_size=int(h.size),
            )
            try:
                pins.add(pin)
            except LiveError:
                _remove(dst)
                raise
            made.append(pin)
            out.append(pin)
    except Exception:
        for p in made:
            pins.release(p.key)
        raise
    return out


def open_pinned(pin: PinnedVolume) -> tuple[Any, dict[str, Any]]:
    """Open a pinned volume as an ordinary file: ``(RadarReader, schema)``.

    The reader reports the original source (the ``s3://`` URL, or the DWD volume name) as its
    origin, as a file opened from the Cloud data panel or the radar map does, carries the credit of
    its network, and remembers its pin so that closing the file releases it
    (:func:`release_reader`). A volume held in memory is adopted from the shared datatree.
    """
    from engine.reader import NEXRAD_L2_CREDIT, RadarReader

    reader = RadarReader()
    if pin.path is None:
        from engine import radar_networks

        schema = reader.adopt_datatree(
            pin.tree, pin.source, "odim", source_type=f"{pin.provider}_opendata"
        )
        reader._source_credit = radar_networks.get_provider(pin.provider).credit()
        reader._live_pin = pin.key
        return reader, schema
    schema = reader.open_file(pin.path)
    reader._path = pin.source
    schema["source"] = pin.source
    schema["source_type"] = "nexrad_l2_s3"
    reader._source_credit = dict(NEXRAD_L2_CREDIT)
    reader._live_pin = pin.key
    return reader, schema


def release_reader(reader: Any, pins: PinRegistry | None = None) -> bool:
    """Release the pin of a closed file (no-op for a reader that did not come from the live map)."""
    key = getattr(reader, "_live_pin", None)
    if key is None:
        return False
    reader._live_pin = None
    _close_quietly(reader)
    return (PINS if pins is None else pins).release(key)


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


#: The networks the live map can use. Others are not claimed (docs/LIVE.md says why).
LIVE_NETWORKS = ("nexrad", "dwd")


def parse_site_id(raw: str) -> tuple[str, str, str]:
    """``(id, network, site)`` of a radar id.

    A bare four-letter id is a NEXRAD station (``ktlx`` -> ``KTLX``: the form saved settings
    already use, so nothing needs migrating); ``nexrad:KTLX`` is the same radar written out;
    ``dwd:ess`` is a German radar. Anything else raises ``ValueError`` (a network that is not live
    is refused by name, never guessed).
    """
    s = str(raw).strip()
    if ":" not in s:
        st = _normalize_station(s)
        return st, "nexrad", st
    network, _, site = s.partition(":")
    network = network.strip().lower()
    if network == "nexrad":
        st = _normalize_station(site)
        return st, "nexrad", st
    if network == "dwd":
        from engine import radar_networks

        code = radar_networks.get_provider("dwd").check_site(site)
        return f"dwd:{code}", "dwd", code
    raise ValueError(
        f"Radar network {network!r} is not available on the live map "
        f"(live networks: {', '.join(LIVE_NETWORKS)})"
    )


def normalize_stations(stations: Any) -> list[str]:
    """Unambiguous radar ids, de-duplicated, in the given order; at most MAX_STATIONS."""
    if not isinstance(stations, list) or not stations:
        raise LiveError("'stations' must be a non-empty list of station ids")
    out: list[str] = []
    for s in stations:
        if not isinstance(s, str):
            raise LiveError("'stations' must be a list of strings")
        st = parse_site_id(s)[0]  # ValueError for a bad id
        if st not in out:
            out.append(st)
    if len(out) > MAX_STATIONS:
        raise LiveError(f"at most {MAX_STATIONS} radars on the live map")
    return out


def latest(station_id: str) -> dict[str, Any]:
    """The newest volume of one radar (a listing, nothing downloaded). Never raises.

    The same shape for every network: ``station`` (the live id), ``provider``, ``status`` (ok /
    not_found / offline / error), and on ok ``name``, ``size`` (bytes, as listed), ``scan_time``,
    ``age_s``, plus ``path`` (the s3:// URL, NEXRAD) or ``key`` (the volume key, DWD).
    """
    try:
        st, network, site = parse_site_id(station_id)
    except ValueError as exc:
        return {"status": "error", "station": str(station_id), "message": str(exc)}
    if network == "nexrad":
        info = latest_scan_info(site)
        return {**info, "station": st, "provider": "nexrad"}
    from engine import radar_networks

    raw = radar_networks.get_provider("dwd").latest_scan_info(site)
    info = {k: v for k, v in raw.items() if k not in ("files", "site", "slot")}
    if isinstance(info.get("name"), str):  # the volume name says which radar it is
        info["name"] = info["name"].replace("DWD ", f"DWD {site.upper()} ", 1)
    return {**info, "station": st, "provider": "dwd"}


def check(stations: list[str], cache: LiveCache = LIVE) -> list[dict[str, Any]]:
    """Newest volume of every radar (metadata only) and whether the cache already holds it.

    One entry per radar, in order, each a :func:`latest` result plus ``held`` (True when this very
    volume is already in the live cache).
    """
    out: list[dict[str, Any]] = []
    for st in stations:
        info = latest(st)
        info["held"] = bool(info.get("status") == "ok" and cache.holds(st, str(info.get("name"))))
        out.append(info)
    return out


def credit_of(network: str) -> dict[str, str]:
    """Source, attribution and licence of a live network (travels into labels and exports)."""
    if network == "dwd":
        from engine import radar_networks

        return radar_networks.get_provider("dwd").credit()
    from engine.reader import NEXRAD_L2_CREDIT

    return dict(NEXRAD_L2_CREDIT)


def _open_s3(path: str) -> Any:
    """Download one volume into the live folder (kept while the cache holds it) and open it."""
    from engine.reader import RadarReader

    reader = RadarReader()
    result = reader.open_s3_file(path, keep_dir=str(held_dir()))
    reader._live_file = result.get("local_path")
    return reader


def _open_dwd(site: str, key: str) -> Any:
    """Download one DWD volume (its 20 sweep files) and assemble it; nothing is kept on disk."""
    from engine import radar_networks
    from engine.reader import RadarReader

    reader = RadarReader()
    radar_networks.get_provider("dwd").open(reader, site, key)
    return reader


def requested_resolution_m(n_radars: int) -> float:
    """The finest grid asked for with *n_radars* radars (1 km for up to four, then coarser)."""
    for limit, res in GRID_BY_RADARS:
        if n_radars <= limit:
            return res
    return GRID_BY_RADARS[-1][1]


def fit_resolution_m(
    sites: list[tuple[float, float]],
    wanted_m: float,
    max_range_km: float,
    max_cells: int = MAX_LIVE_CELLS,
) -> float:
    """Coarsen *wanted_m* in 250 m steps until the grid around the radars has at most *max_cells*.

    The extent is the box around every radar's range disc (``sites`` are ``(lat, lon)``); that is
    an estimate, the mosaic computes the exact one. The resource governor may still coarsen the
    grid further when the machine's memory or time budget needs it; both changes are stated.
    """
    if not sites:
        return wanted_m
    lats = [s[0] for s in sites]
    lons = [s[1] for s in sites]
    mid = math.radians(max(abs(min(lats)), abs(max(lats))))
    h_km = (max(lats) - min(lats)) * 111.2 + 2.0 * max_range_km
    w_km = (max(lons) - min(lons)) * 111.2 * max(math.cos(mid), 0.05) + 2.0 * max_range_km
    area_m2 = h_km * w_km * 1.0e6 * 1.1
    res = max(250.0, float(wanted_m))
    while area_m2 / (res * res) > max_cells and res < 5000.0:
        res += 250.0
    return min(res, 5000.0)


def _site_of(reader: Any) -> tuple[float, float] | None:
    from engine import mosaic

    try:
        tree = reader.datatree
        lat, lon, _alt = mosaic._site(tree, reader.get_sweep_dataset(0, mask=False))
        return lat, lon
    except Exception as exc:
        logger.debug("live: no site position for a volume: %s", exc)
        return None


def update(
    wanted: list[dict[str, Any]],
    *,
    cache: LiveCache = LIVE,
    variable: str = "DBZH",
    product: str = "composite",
    grid: dict[str, Any] | None = None,
    merge: dict[str, Any] | None = None,
    floor_dbz: float | None = DEFAULT_FLOOR_DBZ,
    progress: ProgressFn | None = None,
    opener: Callable[[str], Any] = _open_s3,
    dwd_opener: Callable[[str, str], Any] = _open_dwd,
) -> tuple[Any, list[dict[str, Any]]]:
    """Download what is missing, keep the newest volume per radar, build the composite.

    *wanted* is a list of ``{station, name, size, scan_time?, path | key}``: exactly the volumes the
    user agreed to (``path`` is the s3:// URL of a NEXRAD volume, ``key`` the volume key of a DWD
    one). A radar whose volume is already held is not downloaded again. A radar whose download
    fails is reported and left out; the composite is built from the rest (at least one).
    Returns ``(MosaicRecord, radars)`` where ``radars`` is one status dict per wanted radar.
    """
    from engine import mosaic

    report = progress or (lambda _p, _m: None)
    if not isinstance(wanted, list) or not wanted:
        raise LiveError("'files' must be a non-empty list")
    plan: list[tuple[str, dict[str, Any]]] = []
    networks: dict[str, str] = {}
    sites: dict[str, str] = {}
    for item in wanted:
        if not isinstance(item, dict):
            raise LiveError("'files' entries must be objects")
        try:
            st, network, site = parse_site_id(str(item.get("station", "")))
        except ValueError as exc:
            raise LiveError(str(exc)) from exc
        path = item.get("path")
        name = item.get("name")
        if not isinstance(name, str):
            raise LiveError(f"{st}: 'name' is required")
        if network == "nexrad" and not (isinstance(path, str) and path.startswith("s3://")):
            raise LiveError(f"{st}: 'path' (s3://...) and 'name' are required")
        if network == "dwd" and not (
            isinstance(item.get("key"), str) and re.fullmatch(r"\d{12}", item["key"])
        ):
            raise LiveError(f"{st}: 'key' (the 12-digit volume key) and 'name' are required")
        if any(p[0] == st for p in plan):
            raise LiveError(f"{st} is listed twice")
        plan.append((st, item))
        networks[st] = network
        sites[st] = site
    if len(plan) > MAX_STATIONS:
        raise LiveError(f"at most {MAX_STATIONS} radars on the live map")

    cache.keep_only([st for st, _ in plan])
    radars: list[dict[str, Any]] = []
    n = len(plan)
    for i, (st, item) in enumerate(plan):
        name = str(item["name"])
        status: dict[str, Any] = {
            "station": st,
            "provider": networks[st],
            "name": name,
            "scan_time": item.get("scan_time"),
        }
        if cache.holds(st, name):
            status["status"] = "ok"
            status["downloaded"] = False
            radars.append(status)
            continue
        report(int(5 + 60 * i / n), f"Downloading {name} ({i + 1}/{n})")
        try:
            if networks[st] == "dwd":
                reader = dwd_opener(sites[st], str(item["key"]))
            else:
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
                path=str(item.get("path") or f"{st}:{item.get('key')}"),
                size=int(size) if isinstance(size, (int, float)) else 0,
                scan_time=item.get("scan_time") if isinstance(item.get("scan_time"), str) else None,
                reader=reader,
                local_path=getattr(reader, "_live_file", None),
                provider=networks[st],
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
    if g.get("resolution_m") is None:
        sites = [s for s in (_site_of(rd) for _, rd in pairs) if s is not None]
        g["resolution_m"] = fit_resolution_m(
            sites, requested_resolution_m(len(pairs)), float(g.get("max_range_km", 230.0))
        )
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
    used = {st: networks[st] for st, _ in pairs}
    record._live_networks = sorted(set(used.values()))
    finish_record(record, floor_dbz, [credit_of(nw) for nw in record._live_networks])
    reach = {st: _range_km(rd) for st, rd in pairs}
    for r in radars:
        if reach.get(r["station"]) is not None:
            r["range_km"] = reach[r["station"]]
    return record, radars


def _range_km(reader: Any) -> float | None:
    """How far the radar's lowest sweep sees (km), capped at the composite's range; for the ring."""
    try:
        ds = reader.get_sweep_dataset(0, mask=False)
        return round(min(float(np.nanmax(np.asarray(ds["range"].values))) / 1000.0, 230.0), 1)
    except Exception as exc:
        logger.debug("live: no range for a volume: %s", exc)
        return None


# ---------------------------------------------------------------------------
# The picture: display floor, smoothing, credit
# ---------------------------------------------------------------------------


def clamp_floor(floor_dbz: Any) -> float | None:
    """A display floor in dBZ within the allowed range; ``None`` means every echo is drawn."""
    if floor_dbz is None:
        return None
    if isinstance(floor_dbz, bool) or not isinstance(floor_dbz, (int, float)):
        raise LiveError("'floor_dbz' must be a number or null")
    if not math.isfinite(float(floor_dbz)):
        raise LiveError("'floor_dbz' must be finite")
    return float(min(MAX_FLOOR_DBZ, max(MIN_FLOOR_DBZ, float(floor_dbz))))


def smoothing_factor(shape: tuple[int, int], cell_m: float) -> int:
    """How many drawn pixels per grid cell and axis: 1 to 3, fewer when the PNG would be huge."""
    ny, nx = shape
    f = max(1, min(MAX_SMOOTHING, round(cell_m / DISPLAY_PIXEL_M)))
    while f > 1 and ny * nx * f * f > MAX_DISPLAY_PIXELS:
        f -= 1
    return int(f)


def _upsample(image: np.ndarray, f: int) -> np.ndarray:
    """Bilinear upsampling by *f* that does not smear echo into empty cells (NaN-aware)."""
    if f <= 1:
        return image
    from engine import mosaic

    ny, nx = image.shape
    fx = (np.arange(nx * f, dtype=np.float32) + 0.5) / f - 0.5
    fy = (np.arange(ny * f, dtype=np.float32) + 0.5) / f - 0.5
    gx, gy = np.meshgrid(fx, fy)
    return mosaic._bilinear_nan(np.asarray(image, dtype=np.float32), gx, gy)


def render_display_png(record: Any, floor_dbz: float | None) -> tuple[bytes, dict[str, Any]]:
    """The picture of a composite: smoothed, with echo below *floor_dbz* not drawn.

    Display only: ``record.image`` (the grid every export, probe and statistic uses) is not
    changed. Returns ``(png_bytes, display_info)``.
    """
    from engine import mosaic

    floor = clamp_floor(floor_dbz)
    image = np.asarray(record.image, dtype=np.float32)
    f = smoothing_factor(image.shape, float(record.cell_m))
    up = _upsample(image, f)
    if floor is not None and str(record.units).lower().startswith("dbz"):
        with np.errstate(invalid="ignore"):
            up = np.where(up >= floor, up, np.float32(np.nan))
    else:
        floor = None
    lut, _ = mosaic._build_lut(record.cmap)
    png = mosaic.render_png(up, lut, record.vmin, record.vmax)
    info = {
        "floor_dbz": floor,
        "smoothing": int(f),
        "png_size": [int(up.shape[1]), int(up.shape[0])],
        "note": (
            (f"Echo below {floor:g} dBZ is not drawn. " if floor is not None else "")
            + "The picture is smoothed for display"
            + (f" ({f} drawn pixels per grid cell)" if f > 1 else "")
            + "; the grid values are not changed."
        ),
    }
    return png, info


def finish_record(
    record: Any,
    floor_dbz: float | None = DEFAULT_FLOOR_DBZ,
    network_credits: list[dict[str, str]] | None = None,
) -> None:
    """Give a live composite its picture and the credit and notes that travel with exports.

    *network_credits* is one ``{source, attribution, licence}`` per network in the composite (NEXRAD
    alone when not given); they are joined into the file attributes, so a composite of NEXRAD and
    DWD volumes says both, with each licence.
    """
    if network_credits is None:
        network_credits = getattr(record, "_live_credits", None) or [credit_of("nexrad")]
    record._live_credits = network_credits
    png, info = render_display_png(record, floor_dbz)
    record.png = png
    record.display = info
    res_m = float(record.resolution_m)
    record.extra_attrs = {
        "data_source": "; ".join(c["source"] for c in network_credits),
        "data_credit": "; ".join(c["attribution"] for c in network_credits),
        "data_licence": "; ".join(f"{c['source']}: {c['licence']}" for c in network_credits),
        "live_composite": (
            f"Live composite: column maximum of {record.variable} merged from the newest volume "
            f"of each radar on a {res_m:g} m grid. Retrieved product, not an official product."
        ),
        "display_note": info["note"] + " The values in this file are the merged grid, unfloored.",
    }


def restyle(mosaic_id: str, floor_dbz: float | None) -> Any:
    """Draw an already built composite again with another display floor (no new data)."""
    from engine import mosaic

    record = mosaic.MOSAICS.get(mosaic_id)
    finish_record(record, floor_dbz)
    return record


def frame_header(
    record: Any, radars: list[dict[str, Any]], cache: LiveCache = LIVE
) -> dict[str, Any]:
    """The JSON header of a ``live_frame`` message (without ``type`` / ``request_id``)."""
    from engine import layer_provenance

    h = record.header()
    network_credits = getattr(record, "_live_credits", None) or [credit_of("nexrad")]
    return {
        **h,
        "provenance": layer_provenance.for_mosaic_header(h),
        "stations": radars,
        "cache": cache.status(),
        "pinned": PINS.status(),
        "display": getattr(record, "display", {}) or {},
        "credit": network_credits[0],
        "credits": network_credits,
    }

"""Disk-cached, polite tile proxy for the storm-clip renderer.

The clip page asks the local server for ``/tiles/<source>/<z>/<x>/<y>``; a tile is served from the
cache folder when it is there and fetched once from the tile server otherwise. So a re-render (or
an offline run) uses the files already downloaded, and the tile servers see each tile at most once
from this machine, one request at a time with a short pause and a descriptive User-Agent (usage
policies of shared tile services ask for exactly that; this is not for bulk downloads).

Sources (see docs/STORM_CLIP.md for the terms):
  eox        Sentinel-2 cloudless 2021 by EOX, CC BY-NC-SA 4.0 (attribution required, non-commercial)
  terrarium  Terrain Tiles on AWS (Terrarium elevation PNG)
"""

# ruff: noqa: E501

from __future__ import annotations

import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = (
    "radarx-desktop-storm-clip/0.1 (non-commercial research tool; "
    "https://github.com/syedhamidali/radarx-desktop)"
)

SOURCES: dict[str, dict[str, str]] = {
    "eox": {
        "url": "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2021_3857/default/g/{z}/{y}/{x}.jpg",
        "ext": "jpg",
        "ctype": "image/jpeg",
    },
    "terrarium": {
        "url": "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png",
        "ext": "png",
        "ctype": "image/png",
    },
}

# highest zoom served per source (a request above it is refused, not forwarded)
MAX_ZOOM = {"eox": 13, "terrarium": 13}
MIN_INTERVAL_S = 0.12  # pause between two requests to a tile server


def default_cache_dir() -> Path:
    env = os.environ.get("XRADAR_TILE_CACHE")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".cache" / "radarx-desktop" / "tiles"


class TileCache:
    """Cache folder plus the (serialised, rate limited) fetch of missing tiles."""

    def __init__(self, root: Path | str | None = None, fetch=None, offline: bool = False):
        self.root = Path(root).expanduser() if root else default_cache_dir()
        self._fetch = fetch or self._http_fetch
        self.offline = offline
        self._lock = threading.Lock()
        self._last = 0.0
        self.hits = 0
        self.downloads = 0
        self.failures: dict[str, int] = {}  # host -> failed requests

    # -- addressing ------------------------------------------------------------------------
    def valid(self, source: str, z: int, x: int, y: int) -> bool:
        if source not in SOURCES or not (0 <= z <= MAX_ZOOM[source]):
            return False
        n = 2**z
        return 0 <= x < n and 0 <= y < n

    def url_for(self, source: str, z: int, x: int, y: int) -> str:
        return SOURCES[source]["url"].format(z=z, x=x, y=y)

    def path_for(self, source: str, z: int, x: int, y: int) -> Path:
        return self.root / source / str(z) / str(x) / f"{y}.{SOURCES[source]['ext']}"

    # -- fetching --------------------------------------------------------------------------
    @staticmethod
    def _http_fetch(url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.read()

    def get(self, source: str, z: int, x: int, y: int) -> tuple[bytes, str] | None:
        """The tile bytes and content type, from the cache or fetched once; None when unavailable."""
        if not self.valid(source, z, x, y):
            return None
        ctype = SOURCES[source]["ctype"]
        path = self.path_for(source, z, x, y)
        if path.is_file():
            self.hits += 1
            return path.read_bytes(), ctype
        if self.offline:
            return None
        url = self.url_for(source, z, x, y)
        host = urllib.parse.urlparse(url).netloc
        with self._lock:  # one request at a time, with a pause
            if path.is_file():  # another thread got it meanwhile
                self.hits += 1
                return path.read_bytes(), ctype
            wait = MIN_INTERVAL_S - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            data = None
            for attempt in range(2):
                try:
                    data = self._fetch(url)
                    break
                except (urllib.error.URLError, OSError, TimeoutError):
                    time.sleep(0.6 * (attempt + 1))
            self._last = time.monotonic()
        if not data:
            self.failures[host] = self.failures.get(host, 0) + 1
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        self.downloads += 1
        return data, ctype

    def parse(self, url_path: str) -> tuple[str, int, int, int] | None:
        """``/tiles/<source>/<z>/<x>/<y>`` -> (source, z, x, y); None for anything else."""
        parts = url_path.split("?", 1)[0].strip("/").split("/")
        if len(parts) != 5 or parts[0] != "tiles":
            return None
        try:
            return parts[1], int(parts[2]), int(parts[3]), int(parts[4].split(".")[0])
        except ValueError:
            return None

    def summary(self) -> str:
        s = f"tiles: {self.hits} from the cache, {self.downloads} downloaded ({self.root})"
        if self.failures:
            s += "; FAILED hosts: " + ", ".join(f"{h} ({n})" for h, n in self.failures.items())
        return s

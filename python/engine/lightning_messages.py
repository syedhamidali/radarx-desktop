"""WebSocket message of the lightning layer: read local GLM / LMA files for the viewer.

``ConnectionHandler`` (python/server.py) inherits :class:`LightningMessages`, which adds:

``lightning_open``  -> ``lightning_result`` | ``lightning_error``

Request: ``{type: "lightning_open", request_id, paths: [absolute paths], box?: [south, west, north, east],
t0?, t1? (ISO UTC), parallax_to_km? (GLM), good_only? (GLM), max_chi2?, min_stations?, link?: {distance_m,
time_s, min_points, max_duration_s} (LMA)}``. The answer carries the columns as base64 float32
(see :func:`engine.lightning_layer.to_wire`) and the provenance of the layer (class OBSERVED).

Files are read from the engine's disk only, nothing is downloaded. Not available in web mode (a public
service must not read the server's disk); a web visitor's own file would first have to be uploaded.
"""

# ruff: noqa: E501
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("xradar-server.lightning")

_LINK_KEYS = {"distance_m", "time_s", "min_points", "max_duration_s"}


def _number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def parse_request(msg: dict[str, Any]) -> tuple[list[Path], dict[str, Any]]:
    """Validate a ``lightning_open`` message: the file list and the keyword arguments of the reader."""
    paths = msg.get("paths")
    if (
        not isinstance(paths, list)
        or not paths
        or not all(isinstance(p, str) and p.strip() and "\x00" not in p for p in paths)
    ):
        raise ValueError("lightning_open needs a list of file paths")
    files = [Path(p).expanduser() for p in paths]
    for p in files:
        if not p.is_absolute() or ".." in p.parts:
            raise ValueError("lightning_open: the paths must be absolute")
    kw: dict[str, Any] = {}
    box = msg.get("box")
    if box is not None:
        if not (isinstance(box, list) and len(box) == 4 and all(_number(v) for v in box)):
            raise ValueError("box must be [south, west, north, east] in degrees")
        south, west, north, east = (float(v) for v in box)
        if not (-90 <= south < north <= 90 and -180 <= west <= 180 and -180 <= east <= 180):
            raise ValueError("box is outside -90..90 / -180..180 degrees")
        kw["box"] = (south, west, north, east)
    for key in ("t0", "t1"):
        if msg.get(key) not in (None, ""):
            if not isinstance(msg[key], str):
                raise ValueError(f"{key} must be an ISO time in UTC")
            kw[key] = msg[key]
    if msg.get("parallax_to_km") is not None:
        v = msg["parallax_to_km"]
        if not _number(v) or not 0 <= v <= 40:
            raise ValueError("parallax_to_km must be a height from 0 to 40 km")
        kw["parallax_to_km"] = float(v)
    if msg.get("max_chi2") is not None:
        v = msg["max_chi2"]
        if not _number(v) or not 0 < v <= 100:
            raise ValueError("max_chi2 must be a positive number")
        kw["max_chi2"] = float(v)
    if msg.get("min_stations") is not None:
        v = msg["min_stations"]
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 60:
            raise ValueError("min_stations must be a whole number from 0 to 60")
        kw["min_stations"] = v
    if msg.get("good_only") is not None:
        kw["good_only"] = bool(msg["good_only"])
    link = msg.get("link")
    if link is not None:
        if not isinstance(link, dict) or set(link) - _LINK_KEYS:
            raise ValueError(f"link takes only {sorted(_LINK_KEYS)}")
        for key in ("distance_m", "time_s"):
            if key in link and not (_number(link[key]) and 0 < link[key] <= 1e6):
                raise ValueError(f"link.{key} must be a positive number")
        mp = link.get("min_points", 1)
        if not (isinstance(mp, int) and not isinstance(mp, bool) and 1 <= mp <= 1000):
            raise ValueError("link.min_points must be a whole number from 1 to 1000")
        mdur = link.get("max_duration_s")
        if mdur is not None and not (_number(mdur) and 0 < mdur <= 3600):
            raise ValueError("link.max_duration_s must be a positive number of seconds (or null)")
        kw["link"] = dict(link)
    return files, kw


class LightningMessages:
    """Mixin for ``ConnectionHandler`` (uses ``self.send``, ``self.session``)."""

    def _lightning_handlers(self) -> dict[str, Callable[[dict[str, Any]], Awaitable[None]]]:
        return {"lightning_open": self._handle_lightning_open}

    async def _handle_lightning_open(self, msg: dict[str, Any]) -> None:
        rid = msg.get("request_id")
        if getattr(self, "session", None) is not None:
            await self.send(  # type: ignore[attr-defined]
                {
                    "type": "lightning_error",
                    "request_id": rid,
                    "message": "Lightning files are read from the engine's disk, which is available in the desktop app only.",
                }
            )
            return
        loop = asyncio.get_running_loop()

        def run() -> dict[str, Any]:
            from engine import lightning_layer

            files, kw = parse_request(msg)
            return lightning_layer.to_wire(lightning_layer.read_lightning(files, **kw))

        try:
            wire = await loop.run_in_executor(None, run)
            await self.send({"type": "lightning_result", "request_id": rid, **wire})  # type: ignore[attr-defined]
        except Exception as exc:
            logger.info("lightning_open failed: %s", exc)
            text = (
                str(exc)
                if isinstance(exc, ValueError | ImportError | FileNotFoundError)
                else f"{type(exc).__name__}: {exc}"
            )
            await self.send({"type": "lightning_error", "request_id": rid, "message": text})  # type: ignore[attr-defined]

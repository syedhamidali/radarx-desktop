"""Plain data classes of the environment framework: what is asked, what it would cost, what came.

* :class:`Request`   -- *what* (provider, product, area, time window, variables, extras).
* :class:`FileSpec`  -- one file a request needs (stable cache key, size and checksum if known).
* :class:`Estimate`  -- what a request would download: sources, sizes, destination, licence.
* :class:`Approval`  -- the explicit "yes" of the user for exactly one estimate.
* :class:`FetchResult` -- the files on disk after a fetch, with attribution and provenance.

Everything has ``to_dict()`` so the sidecar can send it to the UI as JSON.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from engine.environment.errors import InvalidRequest

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
#: Hard caps on request sizes so a malformed UI message cannot build a huge object.
MAX_VARIABLES = 64
MAX_PARAMS_BYTES = 16_384


def _num(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequest(f"{label} must be a number, got {value!r}")
    v = float(value)
    if not math.isfinite(v):
        raise InvalidRequest(f"{label} must be finite")
    return v


def parse_time(value: Any, label: str = "time") -> str:
    """Normalise an ISO-8601 time to ``YYYY-MM-DDTHH:MM:SSZ`` (UTC); naive times are UTC."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise InvalidRequest(f"{label}: {value!r} is not an ISO-8601 time") from exc
    else:
        raise InvalidRequest(f"{label} must be an ISO-8601 string")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Request:
    """What to fetch. ``bbox`` is ``(west, south, east, north)`` degrees (no 180 degree wrap)."""

    provider: str
    product: str = ""
    bbox: tuple[float, float, float, float] | None = None
    time_start: str | None = None
    time_end: str | None = None
    variables: tuple[str, ...] = ()
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not _ID_RE.match(self.provider):
            raise InvalidRequest(f"bad provider id {self.provider!r}")
        if not isinstance(self.product, str) or len(self.product) > 128:
            raise InvalidRequest("product must be a short text")
        if self.bbox is not None:
            if len(self.bbox) != 4:
                raise InvalidRequest("bbox must be (west, south, east, north)")
            west, south, east, north = (
                _num(v, n)
                for v, n in zip(self.bbox, ("west", "south", "east", "north"), strict=True)
            )
            if not (-90 <= south < north <= 90):
                raise InvalidRequest("bbox: need -90 <= south < north <= 90")
            if not (-180 <= west < east <= 180):
                raise InvalidRequest(
                    "bbox: need -180 <= west < east <= 180 (split an area that crosses 180 degrees)"
                )
            object.__setattr__(self, "bbox", (west, south, east, north))
        start = parse_time(self.time_start, "time_start") if self.time_start else None
        end = parse_time(self.time_end, "time_end") if self.time_end else None
        if start and end and end < start:
            raise InvalidRequest("time_end is before time_start")
        object.__setattr__(self, "time_start", start)
        object.__setattr__(self, "time_end", end)
        if len(self.variables) > MAX_VARIABLES or not all(
            isinstance(v, str) and 0 < len(v) <= 64 for v in self.variables
        ):
            raise InvalidRequest("variables must be a short list of names")
        object.__setattr__(self, "variables", tuple(self.variables))
        try:
            blob = json.dumps(dict(self.params), sort_keys=True, default=str)
        except (TypeError, ValueError) as exc:
            raise InvalidRequest(f"params are not JSON-serialisable: {exc}") from exc
        if len(blob) > MAX_PARAMS_BYTES:
            raise InvalidRequest("params are too large")
        object.__setattr__(self, "params", json.loads(blob))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Request:
        if not isinstance(data, Mapping):
            raise InvalidRequest("the request must be an object")
        bbox = data.get("bbox")
        variables = data.get("variables") or ()
        if isinstance(variables, str) or not isinstance(variables, (list, tuple)):
            raise InvalidRequest("variables must be a list of names")
        params = data.get("params") or {}
        if not isinstance(params, Mapping):
            raise InvalidRequest("params must be an object")
        if bbox is not None and not isinstance(bbox, (list, tuple)):
            raise InvalidRequest("bbox must be a list of four numbers")
        return cls(
            provider=str(data.get("provider", "")),
            product=str(data.get("product") or ""),
            bbox=tuple(bbox) if bbox is not None else None,  # type: ignore[arg-type]
            time_start=data.get("time_start"),
            time_end=data.get("time_end"),
            variables=tuple(variables),
            params=params,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "product": self.product,
            "bbox": list(self.bbox) if self.bbox else None,
            "time_start": self.time_start,
            "time_end": self.time_end,
            "variables": list(self.variables),
            "params": dict(self.params),
        }

    def digest(self) -> str:
        """Stable hash of the request: the same question always has the same digest."""
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(frozen=True)
class FileSpec:
    """One file of a request. ``key`` is the stable cache identity (provider-unique)."""

    key: str
    name: str
    url: str | None = None
    size: int | None = None  # bytes, None = unknown
    sha256: str | None = None  # published checksum, when the source gives one
    meta: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "url": self.url,
            "size": self.size,
            "sha256": self.sha256,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> FileSpec:
        return cls(
            key=str(d["key"]),
            name=str(d["name"]),
            url=d.get("url"),
            size=d.get("size"),
            sha256=d.get("sha256"),
            meta=dict(d.get("meta") or {}),
        )


@dataclass(frozen=True)
class InventoryItem:
    """One product a provider offers (for the UI picker). ``variables`` are the names it serves."""

    product: str
    title: str
    description: str = ""
    variables: tuple[str, ...] = ()
    resolution: str = ""
    coverage: str = ""
    cadence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "variables": list(self.variables)}


@dataclass
class Estimate:
    """What a request would do, before anything is downloaded."""

    request: Request
    provider_id: str
    provider_name: str
    files: list[FileSpec]
    cached: list[bool]  # parallel to ``files``
    destination: str
    attribution: str
    licence: str
    source: str  # human-readable origin, e.g. a host name or a home page
    offline: bool = False
    needs_network: bool = True
    can_fetch: bool = True
    reason: str = ""  # why ``can_fetch`` is false
    notes: list[str] = field(default_factory=list)
    #: Upper bound per file of unknown size that the framework will accept while downloading.
    unknown_size_cap: int = 50 * 1024 * 1024

    @property
    def total_bytes(self) -> int:
        return sum(f.size or 0 for f in self.files)

    @property
    def cached_bytes(self) -> int:
        return sum(f.size or 0 for f, c in zip(self.files, self.cached, strict=True) if c)

    @property
    def network_files(self) -> list[FileSpec]:
        return [f for f, c in zip(self.files, self.cached, strict=True) if not c]

    @property
    def network_bytes(self) -> int:
        """Bytes that would be downloaded (known sizes only; see :attr:`size_exact`)."""
        return sum(f.size or 0 for f in self.network_files)

    @property
    def size_exact(self) -> bool:
        """False when some file to download has no known size (the figure is a lower bound)."""
        return all(f.size is not None for f in self.network_files)

    @property
    def max_network_bytes(self) -> int:
        """Most the download may transfer: known sizes plus the cap for every unknown one."""
        unknown = sum(1 for f in self.network_files if f.size is None)
        return self.network_bytes + unknown * self.unknown_size_cap

    @property
    def requires_approval(self) -> bool:
        return self.needs_network and bool(self.network_files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "provider_name": self.provider_name,
            "request": self.request.to_dict(),
            "digest": self.request.digest(),
            "files": [
                {"name": f.name, "url": f.url, "size": f.size, "cached": c}
                for f, c in zip(self.files, self.cached, strict=True)
            ],
            "n_files": len(self.files),
            "total_bytes": self.total_bytes,
            "cached_bytes": self.cached_bytes,
            "network_bytes": self.network_bytes,
            "size_exact": self.size_exact,
            "max_network_bytes": self.max_network_bytes,
            "destination": self.destination,
            "attribution": self.attribution,
            "licence": self.licence,
            "source": self.source,
            "offline": self.offline,
            "needs_network": self.needs_network,
            "requires_approval": self.requires_approval,
            "can_fetch": self.can_fetch,
            "reason": self.reason,
            "notes": list(self.notes),
        }

    def approve(self) -> Approval:
        """The user's explicit consent for *this* estimate (call it only after showing it)."""
        return Approval(
            request_digest=self.request.digest(),
            max_bytes=self.max_network_bytes,
            destination=self.destination,
        )


@dataclass(frozen=True)
class Approval:
    """Consent to download one request, up to ``max_bytes``, into ``destination``."""

    request_digest: str
    max_bytes: int
    destination: str
    granted_at: float = field(default_factory=time.time)

    def covers(self, request: Request, needed_bytes: int, destination: str) -> bool:
        return (
            self.request_digest == request.digest()
            and needed_bytes <= self.max_bytes
            and self.destination == destination
        )


@dataclass
class FetchResult:
    """The outcome of a fetch: where the files are and what they are."""

    request: Request
    paths: list[Path]
    from_cache: list[bool]
    bytes_downloaded: int
    attribution: str
    licence: str
    provenance: dict[str, Any]
    checksums: dict[str, str] = field(default_factory=dict)  # file name -> sha256

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "paths": [str(p) for p in self.paths],
            "from_cache": list(self.from_cache),
            "bytes_downloaded": self.bytes_downloaded,
            "attribution": self.attribution,
            "licence": self.licence,
            "provenance": self.provenance,
            "checksums": dict(self.checksums),
        }

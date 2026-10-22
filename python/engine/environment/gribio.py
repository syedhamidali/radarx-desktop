"""Decode GRIB2 messages with ecCodes (an optional dependency) and crop them to an area.

``pip install "radarx-desktop-engine[grib]"`` brings ``eccodes`` (Apache-2.0; the wheel carries
the ecCodes library). It is optional so the installer stays small: downloading and caching
GRIB2 messages needs nothing, only *reading* them does, and the error says so.

What is decoded: the values of one message as a 2-D ``float32`` array (missing = NaN), its
latitude/longitude, the units and names ecCodes reports, the reference (run) time and the valid
time. The area crop is done **after** decoding (GRIB2 cannot be decoded in parts), on the grid, as
the smallest rectangle of grid cells that contains the box; nothing is interpolated.
"""

from __future__ import annotations

import gzip
import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from engine.environment.errors import EnvironmentDataError, InvalidRequest

INSTALL_HINT = (
    "Reading GRIB2 data needs the optional ecCodes package: "
    'pip install "radarx-desktop-engine[grib]" (or: pip install eccodes).'
)


class GribUnavailable(EnvironmentDataError, ImportError):  # noqa: N818
    """ecCodes is not installed (the files stay in the cache and can be read once it is)."""


def require_eccodes() -> Any:
    try:
        import eccodes
    except ImportError as exc:
        raise GribUnavailable(INSTALL_HINT) from exc
    except Exception as exc:  # the binary library failed to load
        raise GribUnavailable(f"{INSTALL_HINT} ({type(exc).__name__}: {exc})") from exc
    return eccodes


@dataclass
class Grid:
    """Where the values are. ``lat`` / ``lon`` are 1-D for a regular lat/lon grid, else 2-D."""

    kind: str  # "regular_ll", "lambert", ...
    lat: np.ndarray
    lon: np.ndarray  # degrees east, -180..180
    description: str = ""
    spacing: str = ""

    @property
    def shape(self) -> tuple[int, int]:
        if self.lat.ndim == 1:
            return (self.lat.size, self.lon.size)
        return tuple(self.lat.shape)  # type: ignore[return-value]


@dataclass
class GribField:
    short_name: str
    name: str
    units: str
    level_type: str
    level: float | None
    values: np.ndarray  # (ny, nx) float32, NaN = missing
    grid: Grid
    reference_time: datetime
    valid_time: datetime
    step_hours: float = 0.0
    keys: dict[str, Any] = field(default_factory=dict)


#: GRIB2 code table 4.4 (unit of the forecast time) -> length of one unit
_STEP_UNITS = {0: timedelta(minutes=1), 1: timedelta(hours=1), 2: timedelta(days=1),
               10: timedelta(hours=3), 11: timedelta(hours=6), 12: timedelta(hours=12),
               13: timedelta(seconds=1)}  # fmt: skip


def _times(ec: Any, gid: Any) -> tuple[datetime, datetime]:
    """Reference (run) time and valid time. Built from the second-resolution keys, because the
    ``dataTime`` / ``validityTime`` keys cannot hold seconds (MRMS files are stamped 03:00:39)."""
    ref = datetime(
        *(int(ec.codes_get(gid, k)) for k in ("year", "month", "day", "hour", "minute", "second")),
        tzinfo=UTC,
    )
    unit = _STEP_UNITS.get(int(_get(ec, gid, "indicatorOfUnitOfTimeRange", 1) or 1))
    lead = int(_get(ec, gid, "forecastTime", 0) or 0)
    return ref, ref + (unit or timedelta(hours=1)) * lead


def _lon180(lon: np.ndarray) -> np.ndarray:
    return ((np.asarray(lon, dtype=float) + 180.0) % 360.0) - 180.0


def _get(ec: Any, gid: Any, key: str, default: Any = None) -> Any:
    try:
        return ec.codes_get(gid, key)
    except Exception:
        return default


def _grid(ec: Any, gid: Any, nj: int, ni: int, *, geometry: bool) -> Grid:
    kind = str(_get(ec, gid, "gridType", "unknown"))
    if kind == "regular_ll":
        lat0 = float(ec.codes_get(gid, "latitudeOfFirstGridPointInDegrees"))
        lat1 = float(ec.codes_get(gid, "latitudeOfLastGridPointInDegrees"))
        lon0 = float(ec.codes_get(gid, "longitudeOfFirstGridPointInDegrees"))
        di = float(ec.codes_get(gid, "iDirectionIncrementInDegrees"))
        dj = float(ec.codes_get(gid, "jDirectionIncrementInDegrees"))
        sgn = 1.0 if lat1 >= lat0 else -1.0
        lat = lat0 + sgn * dj * np.arange(nj)
        lon = _lon180(lon0 + di * np.arange(ni))
        return Grid(
            kind, lat, lon, f"regular latitude/longitude grid {ni} x {nj}", f"{dj:g} degrees"
        )
    if not geometry:
        return Grid(kind, np.zeros(0), np.zeros(0), kind)
    lat = np.asarray(ec.codes_get_array(gid, "latitudes"), dtype=float).reshape(nj, ni)
    lon = _lon180(np.asarray(ec.codes_get_array(gid, "longitudes"), dtype=float).reshape(nj, ni))
    dx = _get(ec, gid, "DxInMetres")
    spacing = f"{float(dx) / 1000.0:g} km" if dx else ""
    names = {"lambert": "Lambert conformal"}
    return Grid(kind, lat, lon, f"{names.get(kind, kind)} grid {ni} x {nj}", spacing)


def iter_fields(
    source: Path | bytes,
    *,
    geometry: bool = True,
    bbox: tuple[float, float, float, float] | None = None,
) -> Iterator[GribField]:
    """Decode every GRIB message in a file (``.gz`` is unpacked in memory) or a bytes object.

    ``geometry=False`` skips latitude/longitude of curvilinear grids (a caller that already has
    them for this grid saves 30 MB of work per message). ``bbox`` crops a regular latitude/
    longitude grid right after decoding, so a large field (98 million values) is never kept whole
    as 32-bit numbers.
    """
    ec = require_eccodes()
    if isinstance(source, (bytes, bytearray, memoryview)):
        raw = bytes(source)
    else:
        raw = Path(source).read_bytes()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
    offset = 0
    while True:
        start = raw.find(b"GRIB", offset)
        if start < 0:
            return
        if len(raw) < start + 16:
            return
        length = int.from_bytes(raw[start + 8 : start + 16], "big")
        if length <= 0 or start + length > len(raw):
            raise InvalidRequest("A GRIB2 message in the file is truncated or damaged.")
        message = raw[start : start + length]
        offset = start + length
        gid = ec.codes_new_from_message(message)
        try:
            ni, nj = int(ec.codes_get(gid, "Ni")), int(ec.codes_get(gid, "Nj"))
            ec.codes_set(gid, "missingValue", float("nan"))
            values = np.asarray(ec.codes_get_values(gid)).reshape(nj, ni)
            grid = _grid(ec, gid, nj, ni, geometry=geometry)
            ref, valid = _times(ec, gid)
            level = _get(ec, gid, "level")
            if grid.kind == "regular_ll" and grid.lat.size > 1 and grid.lat[0] > grid.lat[-1]:
                grid = Grid(
                    grid.kind, grid.lat[::-1].copy(), grid.lon, grid.description, grid.spacing
                )
                values = values[::-1]  # south to north, so every regular grid reads the same way
            if bbox is not None and grid.lat.ndim == 1:
                rows, cols = window(grid, bbox)
                grid = crop_grid(grid, rows, cols)
                values = values[rows, cols]
            values = values.astype(np.float32)
            yield GribField(
                short_name=str(_get(ec, gid, "shortName", "")),
                name=str(_get(ec, gid, "name", "")),
                units=str(_get(ec, gid, "units", "")),
                level_type=str(_get(ec, gid, "typeOfLevel", "")),
                level=float(level) if level is not None else None,
                values=values,
                grid=grid,
                reference_time=ref,
                valid_time=valid,
                step_hours=(valid - ref) / timedelta(hours=1),
                keys={
                    "discipline": _get(ec, gid, "discipline"),
                    "parameterCategory": _get(ec, gid, "parameterCategory"),
                    "parameterNumber": _get(ec, gid, "parameterNumber"),
                    "packingType": _get(ec, gid, "packingType"),
                    "uvRelativeToGrid": _get(ec, gid, "uvRelativeToGrid"),
                    "LoVInDegrees": _get(ec, gid, "LoVInDegrees"),
                    "Latin1InDegrees": _get(ec, gid, "Latin1InDegrees"),
                    "Latin2InDegrees": _get(ec, gid, "Latin2InDegrees"),
                },
            )
        finally:
            ec.codes_release(gid)


def read_first(
    source: Path | bytes,
    *,
    geometry: bool = True,
    bbox: tuple[float, float, float, float] | None = None,
) -> GribField:
    for fld in iter_fields(source, geometry=geometry, bbox=bbox):
        return fld
    raise InvalidRequest("No GRIB2 message found in the file.")


def clean_units(units: str, grib_name: str = "") -> str:
    """Units as CF writes them: ecCodes says ``m s**-1``, CF says ``m s-1``. NCEP stores radar
    reflectivity in dBZ although the GRIB2 table calls the unit ``dB``; the name decides."""
    text = str(units or "").replace("**", "").strip()
    if text.lower() == "unknown":  # ecCodes' word for a parameter it has no table entry for
        return ""
    if text == "dB" and grib_name in REFLECTIVITY_NAMES:
        return "dBZ"
    return text


#: Reflectivity parameters (HRRR index names) whose values are dBZ.
REFLECTIVITY_NAMES = frozenset({"REFC", "REFD", "MAXREF"})


# -- cropping ------------------------------------------------------------------------------------


def window(grid: Grid, bbox: tuple[float, float, float, float] | None) -> tuple[slice, slice]:
    """``(rows, cols)`` slices of the smallest rectangle of cells holding every cell centre inside
    *bbox* ``(west, south, east, north)``. Raises if the box does not touch the grid.
    No interpolation: the crop is whole grid cells."""
    ny, nx = grid.shape
    if bbox is None:
        return slice(0, ny), slice(0, nx)
    west, south, east, north = bbox
    if grid.lat.ndim == 1:
        rows = np.nonzero((grid.lat >= south) & (grid.lat <= north))[0]
        cols = np.nonzero((grid.lon >= west) & (grid.lon <= east))[0]
    else:
        inside = (grid.lat >= south) & (grid.lat <= north) & (grid.lon >= west) & (grid.lon <= east)
        rows = np.nonzero(inside.any(axis=1))[0]
        cols = np.nonzero(inside.any(axis=0))[0]
    if rows.size == 0 or cols.size == 0:
        raise InvalidRequest(
            f"The area west {west:g}, south {south:g}, east {east:g}, north {north:g} has no grid "
            "cell centre inside this data's coverage (or is smaller than one cell). Widen the area."
        )
    return slice(int(rows[0]), int(rows[-1]) + 1), slice(int(cols[0]), int(cols[-1]) + 1)


def crop_grid(grid: Grid, rows: slice, cols: slice) -> Grid:
    if grid.lat.ndim == 1:
        return Grid(grid.kind, grid.lat[rows], grid.lon[cols], grid.description, grid.spacing)
    return Grid(
        grid.kind, grid.lat[rows, cols], grid.lon[rows, cols], grid.description, grid.spacing
    )


def _haversine_km(lat1: float, lon1: float, lat2: Any, lon2: Any) -> Any:
    p1, p2 = math.radians(lat1), np.radians(lat2)
    a = (
        np.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * np.cos(p2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371.0 * np.arcsin(np.sqrt(a))


def nearest_cell(grid: Grid, latitude: float, longitude: float) -> tuple[int, int, float]:
    """``(row, col, distance_km)`` of the grid cell centre nearest a point (great-circle)."""
    lon = ((float(longitude) + 180.0) % 360.0) - 180.0
    lat = float(latitude)
    if grid.lat.ndim == 1:  # a regular grid: the axes are independent
        j = int(np.argmin(np.abs(grid.lat - lat)))
        dlon = np.abs(((grid.lon - lon) + 180.0) % 360.0 - 180.0)
        i = int(np.argmin(dlon))
        return j, i, float(_haversine_km(lat, lon, grid.lat[j], grid.lon[i]))
    d = _haversine_km(lat, lon, grid.lat, grid.lon)
    j, i = np.unravel_index(int(np.nanargmin(d)), d.shape)
    return int(j), int(i), float(d[j, i])

"""Tiny synthetic GRIB2 files and an in-memory "HRRR bucket" for the provider tests.

The messages are made with ecCodes from its built-in sample (a few hundred bytes each), so the
tests exercise the real decoder, the real index/byte-range logic and the real HTTP client with no
network. They are fixtures, not weather: values are simple, known functions of the position.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import Message
from typing import Any

import numpy as np

# (name in the index) -> (discipline, category, number, units); HRRR/NCEP parameter numbers
PARAMS: dict[str, tuple[int, int, int]] = {
    "CAPE": (0, 7, 6),
    "CIN": (0, 7, 7),
    "TMP": (0, 0, 0),
    "DPT": (0, 0, 6),
    "HGT": (0, 3, 5),
    "PRES": (0, 3, 0),
    "UGRD": (0, 2, 2),
    "VGRD": (0, 2, 3),
    "RH": (0, 1, 1),
}

#: grid of the lambert fixtures (a small window of the HRRR projection)
LAMBERT = {
    "nx": 24,
    "ny": 16,
    "dx": 3000.0,
    "lov": 262.5,
    "latin": 38.5,
    "lat0": 35.0,
    "lon0": 266.0,  # degrees east, 0..360 as NCEP writes it
}


def have_eccodes() -> bool:
    try:
        import eccodes  # noqa: F401
    except Exception:
        return False
    return True


def level_text(kind: str, value: float) -> str:
    return {
        "surface": "surface",
        "isobaric": f"{value:g} mb",
        "height": f"{value:g} m above ground",
    }[kind]


def message(
    name: str,
    values: np.ndarray,
    *,
    kind: str = "surface",
    level: float = 0.0,
    run: datetime,
    fxx: int = 0,
    grid: str = "lambert",
    uv_grid_relative: bool = False,
    lat_first: float | None = None,
    lon_first: float | None = None,
    step: float = 0.01,
) -> bytes:
    """One GRIB2 message holding *values* (shape ny x nx) on a small grid."""
    import eccodes as ec

    ny, nx = values.shape
    gid = ec.codes_grib_new_from_samples("GRIB2")
    try:
        if grid == "lambert":
            ec.codes_set(gid, "gridType", "lambert")
            ec.codes_set(gid, "Nx", nx)
            ec.codes_set(gid, "Ny", ny)
            ec.codes_set(gid, "LaDInDegrees", LAMBERT["latin"])
            ec.codes_set(gid, "LoVInDegrees", LAMBERT["lov"])
            ec.codes_set(gid, "Latin1InDegrees", LAMBERT["latin"])
            ec.codes_set(gid, "Latin2InDegrees", LAMBERT["latin"])
            ec.codes_set(gid, "DxInMetres", LAMBERT["dx"])
            ec.codes_set(gid, "DyInMetres", LAMBERT["dx"])
            ec.codes_set(gid, "latitudeOfFirstGridPointInDegrees", LAMBERT["lat0"])
            ec.codes_set(gid, "longitudeOfFirstGridPointInDegrees", LAMBERT["lon0"])
            ec.codes_set(gid, "scanningMode", 64)
            ec.codes_set(gid, "uvRelativeToGrid", 1 if uv_grid_relative else 0)
        else:  # regular lat/lon, north to south like NOAA's MRMS
            ec.codes_set(gid, "Ni", nx)
            ec.codes_set(gid, "Nj", ny)
            la0 = 40.0 if lat_first is None else lat_first
            lo0 = 270.0 if lon_first is None else lon_first
            d = step
            ec.codes_set(gid, "latitudeOfFirstGridPointInDegrees", la0)
            ec.codes_set(gid, "longitudeOfFirstGridPointInDegrees", lo0)
            ec.codes_set(gid, "latitudeOfLastGridPointInDegrees", la0 - d * (ny - 1))
            ec.codes_set(gid, "longitudeOfLastGridPointInDegrees", lo0 + d * (nx - 1))
            ec.codes_set(gid, "iDirectionIncrementInDegrees", d)
            ec.codes_set(gid, "jDirectionIncrementInDegrees", d)
            ec.codes_set(gid, "jScansPositively", 0)
        disc, cat, num = PARAMS[name]
        ec.codes_set(gid, "discipline", disc)
        ec.codes_set(gid, "parameterCategory", cat)
        ec.codes_set(gid, "parameterNumber", num)
        if kind == "surface":
            ec.codes_set(gid, "typeOfFirstFixedSurface", 1)
        elif kind == "isobaric":
            ec.codes_set(gid, "typeOfFirstFixedSurface", 100)
            ec.codes_set(gid, "scaleFactorOfFirstFixedSurface", 0)
            ec.codes_set(gid, "scaledValueOfFirstFixedSurface", int(level * 100))
        elif kind == "height":
            ec.codes_set(gid, "typeOfFirstFixedSurface", 103)
            ec.codes_set(gid, "scaleFactorOfFirstFixedSurface", 0)
            ec.codes_set(gid, "scaledValueOfFirstFixedSurface", int(level))
        ec.codes_set(gid, "dataDate", int(run.strftime("%Y%m%d")))
        ec.codes_set(gid, "dataTime", run.hour * 100 + run.minute)
        ec.codes_set(gid, "stepUnits", 1)
        ec.codes_set(gid, "forecastTime", fxx)
        ec.codes_set(gid, "bitsPerValue", 24)
        ec.codes_set_values(gid, np.asarray(values, dtype=float).ravel())
        return bytes(ec.codes_get_message(gid))
    finally:
        ec.codes_release(gid)


class _Resp(io.BytesIO):
    def __init__(self, body: bytes, headers: dict[str, str], status: int = 200) -> None:
        super().__init__(body)
        self.headers = headers
        self.status = status


class Bucket:
    """An in-memory file server with ``HEAD``, whole-file ``GET`` and ``Range`` support."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.files: dict[str, bytes] = {}
        self.log: list[tuple[str, str, str | None]] = []  # (method, url, Range)
        self.ignore_range = False

    def put(self, path: str, body: bytes) -> None:
        self.files[path] = body

    def opener(self, req: urllib.request.Request, timeout: float) -> Any:
        url = req.full_url
        rng = req.get_header("Range")
        self.log.append((req.get_method(), url, rng))
        path = url[len(self.base) :]
        if not url.startswith(self.base) or path not in self.files:
            raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)  # type: ignore[arg-type]
        body = self.files[path]
        if req.get_method() == "HEAD":
            return _Resp(b"", {"Content-Length": str(len(body))})
        if rng and not self.ignore_range:
            a, b = rng.removeprefix("bytes=").split("-")
            a, b = int(a), int(b) if b else len(body) - 1
            part = body[a : b + 1]
            return _Resp(part, {"Content-Length": str(len(part))}, 206)
        return _Resp(body, {"Content-Length": str(len(body))})

    def downloaded_bytes(self) -> int:
        """Bytes served for GET requests (the sum of the range lengths)."""
        total = 0
        for method, url, rng in self.log:
            if method != "GET":
                continue
            body = self.files[url[len(self.base) :]]
            if rng:
                a, b = rng.removeprefix("bytes=").split("-")
                total += int(b) - int(a) + 1
            else:
                total += len(body)
        return total


def hrrr_file(
    messages: list[tuple[str, str, str, bytes]], run: datetime, fxx: int = 0
) -> tuple[bytes, str]:
    """``(grib bytes, idx text)`` for ``[(name, level_text, step_text, message_bytes)]``."""
    body = b""
    lines = []
    for n, (name, level, step, msg) in enumerate(messages, start=1):
        lines.append(f"{n}:{len(body)}:d={run:%Y%m%d%H}:{name}:{level}:{step}:")
        body += msg
    return body, "\n".join(lines) + "\n"


def lambert_axes() -> tuple[np.ndarray, np.ndarray]:
    """Latitude and longitude (-180..180) of the fixture grid, from ecCodes."""
    import eccodes as ec

    vals = np.zeros((LAMBERT["ny"], LAMBERT["nx"]))
    gid = ec.codes_new_from_message(message("TMP", vals, run=datetime(2021, 12, 11, tzinfo=UTC)))
    lat = np.asarray(ec.codes_get_array(gid, "latitudes")).reshape(vals.shape)
    lon = np.asarray(ec.codes_get_array(gid, "longitudes")).reshape(vals.shape)
    ec.codes_release(gid)
    return lat, ((lon + 180.0) % 360.0) - 180.0


def make_hrrr_bucket(
    run: datetime,
    fxx: int = 0,
    *,
    extra: Callable[[], list[tuple[str, str, str, bytes]]] | None = None,
) -> Bucket:
    """A bucket holding one surface file with a handful of messages and one pressure file."""
    base = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
    ny, nx = LAMBERT["ny"], LAMBERT["nx"]
    yy, xx = np.mgrid[0:ny, 0:nx].astype(float)
    kw = {"run": run, "fxx": fxx}
    anl = "anl" if fxx == 0 else f"{fxx} hour fcst"
    sfc = [
        ("CAPE", "surface", anl, message("CAPE", 1000 + 10 * yy + xx, **kw)),
        ("CIN", "surface", anl, message("CIN", 50 + 0 * yy, **kw)),
        ("TMP", "2 m above ground", anl,
         message("TMP", 280 + 0.1 * xx, kind="height", level=2, **kw)),
        ("UGRD", "10 m above ground", anl,
         message("UGRD", 10 + 0 * yy, kind="height", level=10, uv_grid_relative=True, **kw)),
        ("VGRD", "10 m above ground", anl,
         message("VGRD", 0 * yy, kind="height", level=10, uv_grid_relative=True, **kw)),
    ]  # fmt: skip
    if extra:
        sfc += extra()
    day = f"hrrr.{run:%Y%m%d}/conus/hrrr.t{run:%H}z"
    bucket = Bucket(base + "/")
    body, idx = hrrr_file(sfc, run, fxx)
    bucket.put(f"{day}.wrfsfcf{fxx:02d}.grib2", body)
    bucket.put(f"{day}.wrfsfcf{fxx:02d}.grib2.idx", idx.encode())
    levels = (850.0, 700.0, 500.0)
    prs = []
    for lv in levels:
        prs.append(
            (
                "TMP",
                f"{lv:g} mb",
                anl,
                message("TMP", 260 + 0 * yy - (1000 - lv) / 10, kind="isobaric", level=lv, **kw),
            )
        )
        prs.append(
            (
                "UGRD",
                f"{lv:g} mb",
                anl,
                message("UGRD", 20 + 0 * yy, kind="isobaric", level=lv, **kw),
            )
        )
    body, idx = hrrr_file(prs, run, fxx)
    bucket.put(f"{day}.wrfprsf{fxx:02d}.grib2", body)
    bucket.put(f"{day}.wrfprsf{fxx:02d}.grib2.idx", idx.encode())
    return bucket


def run_time(hour: int = 3) -> datetime:
    return datetime(2021, 12, 11, hour, tzinfo=UTC)


__all__ = [
    "LAMBERT",
    "Bucket",
    "have_eccodes",
    "hrrr_file",
    "lambert_axes",
    "make_hrrr_bucket",
    "message",
    "run_time",
    "timedelta",
]

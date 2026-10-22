"""ERA5 reanalysis (ECMWF / Copernicus C3S) from the public Google ARCO-ERA5 Zarr store, read by
slicing the store with HTTP byte ranges.

The store (``gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3``, which despite
its name is in the Zarr v2 layout) holds hourly global fields on a 0.25 degree grid. A chunk is one
variable at one hour for the **whole globe and, for pressure-level variables, all 37 levels**: 2 MB
for a surface field, 85 MB for a pressure-level field. Downloading chunks whole would make a
regional profile cost hundreds of MB, so this provider reads only what is asked for:

1. the first bytes of the chunk give the Blosc block table (``bloscblocks``);
2. the levels and latitude rows requested are consecutive bytes of the decompressed chunk, which
   live in a few Blosc blocks of 512 kB each (about 290 kB compressed);
3. exactly those blocks are fetched with one ``Range`` request per level, kept in the cache as a
   self-describing piece, and decoded with the ordinary Blosc decoder.

A request for 7 pressure levels of 5 variables around a point is therefore tens of MB, not 400 MB,
and the estimate shown before the download is exact. Longitudes cannot be cut this way (a row of
the grid is 1440 values in a row), so the area is cut to whole latitude rows when fetching and to
the longitude columns after decoding.

Time: the requested time is matched to the **nearest hour** (ERA5 is hourly) and the offset is
stated; nothing is interpolated in time. The store's time axis is read and checked.
ERA5-Land (0.1 degree, land only) is not in this store and is not offered.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from engine import layer_provenance
from engine.environment import bloscblocks, derive, gribio
from engine.environment.errors import FetchFailed, InvalidRequest
from engine.environment.provider import Provider
from engine.environment.request import FetchResult, FileSpec, InventoryItem, Request

STORE = (
    "https://storage.googleapis.com/gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"
)
EPOCH = datetime(1900, 1, 1, tzinfo=UTC)
NLAT, NLON = 721, 1440
HEAD_BYTES = 4096  # enough for the block table of a chunk of 150 MB in 512 kB blocks (1.2 kB)

#: short name -> store variable
ALIASES: dict[str, str] = {
    "t2m": "2m_temperature",
    "d2m": "2m_dewpoint_temperature",
    "u10": "10m_u_component_of_wind",
    "v10": "10m_v_component_of_wind",
    "sp": "surface_pressure",
    "msl": "mean_sea_level_pressure",
    "cape": "convective_available_potential_energy",
    "cin": "convective_inhibition",
    "tcwv": "total_column_water_vapour",
    "orography": "geopotential_at_surface",
    "z": "geopotential",
    "t": "temperature",
    "u": "u_component_of_wind",
    "v": "v_component_of_wind",
    "q": "specific_humidity",
    "r": "relative_humidity",
    "w": "vertical_velocity",
}
SINGLE_LEVEL_EXAMPLES = (
    "t2m", "d2m", "u10", "v10", "sp", "msl", "cape", "cin", "tcwv", "orography",
    "volumetric_soil_water_layer_1", "soil_temperature_level_1", "snow_depth",
)  # fmt: skip
PRESSURE_LEVEL_VARS = ("z", "t", "u", "v", "q", "r", "w")
#: ERA5 pressure levels, hPa
LEVELS = (1, 2, 3, 5, 7, 10, 20, 30, 50, 70, 100, 125, 150, 175, 200, 225, 250, 300, 350, 400,
          450, 500, 550, 600, 650, 700, 750, 775, 800, 825, 850, 875, 900, 925, 950,
          975, 1000)  # fmt: skip
DEFAULT_PROFILE_LEVELS = (1000, 950, 900, 850, 800, 750, 700, 650, 600, 550, 500, 450, 400, 350,
                          300, 250, 200, 150, 100)  # fmt: skip
PROFILE_SURFACE = ("sp", "orography", "t2m", "d2m", "u10", "v10")


def hours_since_epoch(t: datetime) -> int:
    return int((t - EPOCH) / timedelta(hours=1))


class Era5Selection:
    def __init__(self, target: datetime) -> None:
        self.target = target
        self.valid = (target + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
        self.index = hours_since_epoch(self.valid)
        self.offset_s = (self.valid - target).total_seconds()

    def describe(self) -> str:
        mins = round(self.offset_s / 60)
        near = "the same" if mins == 0 else f"{mins:+d} min away"
        return (
            f"reanalysis valid {self.valid:%Y-%m-%d %H:%M} UTC; requested {self.target:%H:%M} UTC, "
            f"nearest hourly analysis is {near} (not interpolated in time)"
        )


def row_range(bbox: tuple[float, float, float, float] | None) -> tuple[int, int]:
    """First and last latitude row (0 = 90 N, 0.25 degree steps) whose centre is in the box."""
    if bbox is None:
        return 0, NLAT - 1
    _w, south, _e, north = bbox
    lo = int(np.ceil((90.0 - north) / 0.25 - 1e-9))
    hi = int(np.floor((90.0 - south) / 0.25 + 1e-9))
    lo, hi = max(lo, 0), min(hi, NLAT - 1)
    if hi < lo:
        raise InvalidRequest(
            "The area is smaller than one ERA5 grid cell (0.25 degree) and holds no cell centre. "
            "Widen the area."
        )
    return lo, hi


def columns_for(bbox: tuple[float, float, float, float] | None) -> np.ndarray:
    """Indices of the longitude columns (0..359.75 east) inside the box, ordered west to east."""
    lon = np.arange(NLON) * 0.25
    lon180 = ((lon + 180.0) % 360.0) - 180.0
    if bbox is None:
        idx = np.arange(NLON)
    else:
        west, _s, east, _n = bbox
        idx = np.nonzero((lon180 >= west - 1e-9) & (lon180 <= east + 1e-9))[0]
        if idx.size == 0:
            raise InvalidRequest("The area holds no ERA5 grid column. Widen the area.")
    return idx[np.argsort(lon180[idx], kind="stable")]


class Era5Provider(Provider):
    id: ClassVar[str] = "era5"
    name: ClassVar[str] = "ERA5 reanalysis (ECMWF/Copernicus, Google ARCO-ERA5)"
    kind: ClassVar[str] = "model"
    attribution: ClassVar[str] = (
        "Contains modified Copernicus Climate Change Service information. ERA5 reanalysis by "
        "ECMWF for the Copernicus Climate Change Service (C3S); analysis-ready cloud-optimised "
        "copy ARCO-ERA5 by Google LLC (bucket gcp-public-data-arco-era5)."
    )
    licence: ClassVar[str] = (
        "Copernicus Climate Change Service licence: free to use, with the credit above; the "
        "terms are on https://apps.ecmwf.int/datasets/licences/copernicus/ (not reproduced "
        "here, read them before redistributing). Neither ECMWF nor the European Commission is "
        "responsible for any use of the information."
    )
    homepage: ClassVar[str] = "https://cloud.google.com/storage/docs/public-datasets/era5"
    description: ClassVar[str] = (
        "Global hourly reanalysis on a 0.25 degree grid from 1940: surface fields, 37 pressure "
        "levels and profiles. A model-based estimate of the atmosphere (environment, not storm "
        "scale), never an observation. ERA5-Land is not offered."
    )
    unknown_size_cap: ClassVar[int] = 4 * 1024 * 1024

    def inventory(self, query: dict[str, Any] | None = None) -> list[InventoryItem]:
        common = {
            "resolution": "0.25 degree regular grid (about 28 km in latitude)",
            "coverage": "global, 1940 to a few days ago (ERA5T)",
            "cadence": "hourly",
        }
        return [
            InventoryItem(
                product="single",
                title="ERA5 single-level fields",
                description="Surface and column fields: 2 m and 10 m values, pressure, CAPE, CIN, "
                "soil moisture and temperature, snow, ... Any store variable name also works "
                "(about 2 MB per field and hour, less with a small area).",
                variables=SINGLE_LEVEL_EXAMPLES,
                **common,
            ),
            InventoryItem(
                product="pressure",
                title="ERA5 pressure-level fields",
                description="Geopotential, temperature, wind, humidity, vertical velocity on the "
                "37 pressure levels. Only the levels and latitude rows asked for are downloaded "
                "(params levels, in hPa).",
                variables=PRESSURE_LEVEL_VARS,
                **common,
            ),
            InventoryItem(
                product="profile",
                title="ERA5 vertical profile at a point",
                description="The column at the grid cell nearest a point (params lat, lon; levels "
                "optional), read as a sounding.",
                variables=("z", "t", "u", "v", "q"),
                **common,
            ),
        ]

    # -- planning --------------------------------------------------------------------------

    def _json(self, env: Any, path: str) -> Any:
        try:
            return json.loads(env.http.get_bytes(f"{STORE}/{path}", max_bytes=2 * 1024 * 1024))
        except FetchFailed as exc:
            if "404" in str(exc):
                raise InvalidRequest(f"{path.split('/')[0]!r} is not in the ERA5 store.") from exc
            raise

    def _small_chunk(self, env: Any, array: str, dtype: str) -> np.ndarray:
        from numcodecs import Blosc

        raw = env.http.get_bytes(f"{STORE}/{array}/0", max_bytes=1024 * 1024)
        return np.frombuffer(bytes(Blosc().decode(raw)), dtype=dtype)

    def _check_axes(self, env: Any, sel: Era5Selection, need_levels: bool) -> None:
        """Read the store's time, latitude and longitude axes and check what the plan assumes."""
        from numcodecs import Blosc

        root = self._json(env, ".zattrs")
        stop = str(root.get("valid_time_stop_era5t") or root.get("valid_time_stop") or "")
        start = str(root.get("valid_time_start") or "")
        day = f"{sel.valid:%Y-%m-%d}"
        if (start and day < start) or (stop and day > stop):
            raise InvalidRequest(f"ERA5 in this store covers {start} to {stop}; {day} is outside.")
        chunk = self._json(env, "time/.zarray")["chunks"][0]
        raw = env.http.get_bytes(f"{STORE}/time/{sel.index // chunk}", max_bytes=1024 * 1024)
        hours = np.frombuffer(bytes(Blosc().decode(raw)), dtype="<i8")
        if hours[sel.index % chunk] != sel.index:
            raise InvalidRequest(
                "The time axis of the ERA5 store is not hourly since 1900; stopped."
            )
        lat = self._small_chunk(env, "latitude", "<f4")
        lon = self._small_chunk(env, "longitude", "<f4")
        if not (
            lat.size == NLAT and lat[0] == 90 and lat[-1] == -90 and lon.size == NLON
            and lon[0] == 0 and np.isclose(lon[1], 0.25)
        ):  # fmt: skip
            raise InvalidRequest("The ERA5 store's grid is not the expected 0.25 degree grid.")
        if need_levels:
            lev = self._small_chunk(env, "level", "<i8")
            if tuple(int(v) for v in lev) != LEVELS:
                raise InvalidRequest(
                    "The pressure levels of the ERA5 store are not the expected 37."
                )

    @staticmethod
    def _levels(params: Any, default: tuple[int, ...] | None) -> list[int]:
        raw = params.get("levels")
        if raw is None and default is not None:
            raw = list(default)
        if raw is None:
            raise InvalidRequest(
                "Name the pressure levels (params levels, in hPa, e.g. [850, 500])."
            )
        if not isinstance(raw, list) or not raw or len(raw) > 37:
            raise InvalidRequest("levels must be a list of pressures in hPa.")
        try:
            levels = [int(v) for v in raw]
        except (TypeError, ValueError) as exc:
            raise InvalidRequest("levels must be whole numbers in hPa.") from exc
        bad = [v for v in levels if v not in LEVELS]
        if bad:
            raise InvalidRequest(
                f"ERA5 has no level {bad[0]} hPa. Levels: {', '.join(map(str, LEVELS))}."
            )
        return sorted(dict.fromkeys(levels), reverse=True)

    def _wanted(
        self, request: Request
    ) -> tuple[list[tuple[str, int | None]], tuple[int, int], Any]:
        """``([(store variable, level or None)], (row0, row1), point)``."""
        product = request.product or "single"
        params = request.params
        point = None
        if product == "profile":
            lat, lon = params.get("lat"), params.get("lon")
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lon)):
                raise InvalidRequest("A profile needs params lat and lon (degrees).")
            if not (-90 <= lat <= 90 and -180 <= lon <= 360):
                raise InvalidRequest("lat/lon are outside the globe.")
            j = int(min(max(round((90.0 - float(lat)) / 0.25), 0), NLAT - 1))
            levels = self._levels(params, DEFAULT_PROFILE_LEVELS)
            wanted: list[tuple[str, int | None]] = [(ALIASES[a], None) for a in PROFILE_SURFACE]
            wanted += [(ALIASES[a], lv) for lv in levels for a in ("z", "t", "u", "v", "q")]
            return wanted, (j, j), (float(lat), float(lon))
        if product not in ("single", "pressure"):
            raise InvalidRequest(f"Unknown ERA5 product {product!r}: single, pressure, profile.")
        if not request.variables:
            raise InvalidRequest(
                "Name the variables (for example t2m, cape, or z t u v q for pressure levels)."
            )
        names = [ALIASES.get(v, v) for v in request.variables]
        rows = row_range(request.bbox)
        if product == "single":
            return [(n, None) for n in names], rows, point
        levels = self._levels(params, None)
        return [(n, lv) for n in names for lv in levels], rows, point

    def plan(self, request: Request, env: Any) -> list[FileSpec]:
        if not request.time_start:
            raise InvalidRequest(
                "Give the time you want (time_start, UTC); the nearest hour is used."
            )
        sel = Era5Selection(
            datetime.strptime(request.time_start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        )
        wanted, (j0, j1), point = self._wanted(request)
        product = request.product or "single"
        self._check_axes(env, sel, any(lv is not None for _n, lv in wanted))
        specs: list[FileSpec] = []
        info: dict[str, tuple[dict, dict, Any, int]] = {}
        for name, level in wanted:
            if name not in info:
                za = self._json(env, f"{name}/.zarray")
                zt = self._json(env, f"{name}/.zattrs")
                comp = za.get("compressor") or {}
                if (
                    za.get("dtype") != "<f4" or za.get("order") != "C" or za.get("filters")
                    or comp.get("id") != "blosc" or za["chunks"][0] != 1
                    or za["chunks"][-2:] != [NLAT, NLON]
                ):  # fmt: skip
                    raise InvalidRequest(
                        f"{name!r} is stored in a layout this reader does not support."
                    )
                ndim = len(za["shape"])
                if (ndim == 4) != (level is not None):
                    which = "pressure-level" if ndim == 4 else "single-level"
                    raise InvalidRequest(
                        f"{name!r} is a {which} variable: use product "
                        f"{'pressure' if ndim == 4 else 'single'}."
                    )
                key = ".".join([str(sel.index)] + ["0"] * (ndim - 1))
                url = f"{STORE}/{name}/{key}"
                head = env.http.get_bytes(
                    url, max_bytes=HEAD_BYTES, headers={"Range": f"bytes=0-{HEAD_BYTES - 1}"}
                )
                need = bloscblocks.header_length_for(head)
                if len(head) < need:
                    head = env.http.get_bytes(
                        url, max_bytes=need, headers={"Range": f"bytes=0-{need - 1}"}
                    )
                frame = bloscblocks.parse_frame(head)
                if frame.nbytes != int(np.prod(za["chunks"])) * 4:
                    raise InvalidRequest(f"{name!r}: a chunk has an unexpected size.")
                info[name] = (za, zt, frame, ndim)
            za, zt, frame, ndim = info[name]
            li = LEVELS.index(level) if level is not None else 0
            plane = NLAT * NLON * 4
            raw_a = (li * plane if ndim == 4 else 0) + j0 * NLON * 4
            raw_b = (li * plane if ndim == 4 else 0) + (j1 + 1) * NLON * 4
            header, b0, b1 = bloscblocks.container_header(
                frame, raw_a, raw_b, dtype="<f4", shape=[j1 - j0 + 1, NLON]
            )
            lvl = f"{level}hPa" if level is not None else "sfc"
            specs.append(
                FileSpec(
                    key=f"{name}/{sel.index}/{lvl}/{j0}-{j1}",
                    name=f"era5_{name}_{sel.valid:%Y%m%dT%H}_{lvl}_rows{j0}-{j1}.rxb",
                    url=f"{STORE}/{name}/{'.'.join([str(sel.index)] + ['0'] * (ndim - 1))}",
                    size=b1 - b0,  # network bytes; the piece on disk adds a small header
                    meta={
                        "range": [b0, b1 - 1],
                        "header": header.hex(),
                        "variable": name,
                        "level": level,
                        "rows": [j0, j1],
                        "long_name": zt.get("long_name", name),
                        "units": zt.get("units", ""),
                        "chunk_bytes": frame.cbytes,
                        "valid": f"{sel.valid:%Y-%m-%dT%H:%M:%SZ}",
                        "target": f"{sel.target:%Y-%m-%dT%H:%M:%SZ}",
                        "selection": sel.describe(),
                        "product": product,
                        "point": point,
                    },
                )
            )
        return specs

    def estimate_notes(self, request: Request, specs: list[FileSpec]) -> list[str]:
        if not specs:
            return []
        whole = sum({s.meta["variable"]: s.meta["chunk_bytes"] for s in specs}.values())
        mine = sum(s.size or 0 for s in specs)
        notes = [
            specs[0].meta["selection"],
            f"Only the blocks holding the chosen levels and latitude rows are requested "
            f"({mine / 1e6:.1f} MB; the whole chunks of these variables are {whole / 1e6:.0f} MB). "
            "A block covers about 90 latitude rows, so a smaller area does not shrink it further.",
        ]
        if (request.product or "single") != "profile" and request.bbox is not None:
            notes.append("Longitudes are cut after decoding; latitudes before downloading.")
        return notes

    # -- download --------------------------------------------------------------------------

    def download_file(  # type: ignore[no-untyped-def]
        self, spec, dest, env, *, max_bytes, progress, cancel
    ):
        a, b = spec.meta["range"]
        header = bytes.fromhex(spec.meta["header"])
        body = dest.with_name(dest.name + ".body")
        try:
            env.http.download(
                spec.url,
                body,
                max_bytes=b - a + 1,
                progress=progress,
                cancel=cancel,
                headers={"Range": f"bytes={a}-{b}"},
            )
            if body.stat().st_size != b - a + 1:
                raise FetchFailed(
                    f"{spec.name}: expected {b - a + 1} bytes, got {body.stat().st_size}. The "
                    "server may not support byte ranges; nothing was kept."
                )
            bloscblocks.write_container(dest, header, body)
        finally:
            body.unlink(missing_ok=True)

    # -- reading ---------------------------------------------------------------------------

    def provenance(self, request: Request, specs: list[FileSpec]) -> dict[str, Any]:
        return self._provenance(request, specs).to_dict()

    def _provenance(
        self, request: Request, specs: list[FileSpec], extra: dict[str, Any] | None = None
    ) -> layer_provenance.LayerProvenance:
        m = specs[0].meta if specs else {}
        product = request.product or "single"
        levels = sorted({s.meta["level"] for s in specs if s.meta["level"] is not None})
        return layer_provenance.LayerProvenance(
            kind="model",
            source=f"ERA5 reanalysis ({product}), ECMWF/C3S via Google ARCO-ERA5",
            title=f"ERA5 {product}",
            valid_time=m.get("valid", ""),
            resolution="0.25 degree regular latitude/longitude grid (the model's own resolution "
            "is about 31 km); cropped to the area, no regridding",
            method="Hourly ERA5 analysis fields read from the ARCO-ERA5 Zarr store by HTTP byte "
            "range (only the blocks of the chosen levels and latitudes), decoded with Blosc. "
            "ERA5 is a reanalysis: each hour is a model state corrected by observations; there "
            "is no model run or forecast hour to state. " + (m.get("selection") or ""),
            parameters={
                "product": product,
                "variables": sorted({s.meta["variable"] for s in specs}),
                "levels_hpa": levels or None,
                "requested_time": m.get("target") or None,
                "time_selection": "nearest hourly analysis; not interpolated in time",
                "bbox": list(request.bbox) if request.bbox else None,
                **(extra or {}),
            },
            uncertainty="Reanalysis field: an estimate of the atmosphere on a 0.25 degree grid "
            "(about 31 km), not a measurement. It resolves synoptic and mesoscale environments, "
            "not individual storms, outflow boundaries or the near-storm profile. Accumulated "
            "and flux variables are model forecasts; CAPE and CIN are model diagnostics. "
            "Uncertainty is not given per field by ERA5 in this store.",
            references=[
                "Hersbach, H., et al., 2020: The ERA5 global reanalysis. Q. J. R. Meteorol. Soc., "
                "146, 1999-2049.",
                "Carver, R. W., and A. Merose, 2023: ARCO-ERA5: An analysis-ready cloud-optimized "
                "reanalysis dataset. 22nd Conf. on Artificial Intelligence for Environmental "
                "Science, AMS.",
            ],
            attribution=self.attribution,
            licence=self.licence,
            inputs=sorted({s.meta["variable"] for s in specs})[:40],
        )

    def _specs_for(self, result: FetchResult, env: Any) -> list[FileSpec]:
        from engine.environment.core import Environment

        specs, _ = self._planned(result.request, env or Environment.default())
        if len(specs) != len(result.paths):
            raise FetchFailed("The cached listing no longer matches the files; fetch again.")
        return specs

    @staticmethod
    def _rows(path: Path) -> np.ndarray:
        raw = bloscblocks.decode_container(path)
        meta, _ = bloscblocks.read_container(path)
        return np.frombuffer(raw, dtype=meta["dtype"]).reshape(meta["shape"]).copy()

    def open(self, result: FetchResult, env: Any = None) -> Any:
        import xarray as xr

        specs = self._specs_for(result, env)
        request = result.request
        j0, j1 = specs[0].meta["rows"]
        cols = columns_for(request.bbox if (request.product or "single") != "profile" else None)
        lat = (90.0 - 0.25 * np.arange(j0, j1 + 1))[::-1]
        lon = (((np.arange(NLON) * 0.25 + 180.0) % 360.0) - 180.0)[cols]
        coords = {
            "latitude": (("latitude",), lat.astype("float32"), {"units": "degrees_north"}),
            "longitude": (("longitude",), lon.astype("float32"), {"units": "degrees_east"}),
        }
        by_var: dict[str, list[tuple[FileSpec, Path]]] = {}
        for spec, path in zip(specs, result.paths, strict=True):
            by_var.setdefault(spec.meta["variable"], []).append((spec, path))
        data_vars: dict[str, Any] = {}
        for name, members in by_var.items():
            first = members[0][0].meta
            attrs = {
                "long_name": first["long_name"],
                "units": gribio.clean_units(first["units"]),
                "era5_name": name,
            }
            arrays = [self._rows(p)[::-1][:, cols] for _s, p in members]
            if first["level"] is None:
                data_vars[name] = (("latitude", "longitude"), arrays[0], attrs)
            else:
                order = sorted(range(len(members)), key=lambda k: members[k][0].meta["level"])
                data_vars[name] = (
                    ("level", "latitude", "longitude"),
                    np.stack([arrays[k] for k in order]),
                    attrs,
                )
                coords["level"] = (
                    ("level",),
                    np.array([members[k][0].meta["level"] for k in order], dtype="float32"),
                    {"units": "hPa"},
                )
        ds = xr.Dataset(data_vars, coords=coords)
        ds.attrs.update(layer_provenance.attrs(self._provenance(request, specs)))
        ds.attrs["title"] = "ERA5 " + (request.product or "single")
        ds.attrs["time_selection"] = specs[0].meta["selection"]
        return ds

    def profile_at(
        self, result: FetchResult, latitude: float, longitude: float, env: Any = None
    ) -> Any:
        """The column at the grid cell nearest the requested point, as a model sounding."""
        from engine import sounding

        request = result.request
        if (request.product or "single") != "profile":
            raise InvalidRequest("Only a 'profile' request can be read as a sounding.")
        specs = self._specs_for(result, env)
        point = specs[0].meta["point"]
        if (
            abs(point[0] - latitude) > 0.13
            or abs(((point[1] - longitude + 180) % 360) - 180) > 0.13
        ):
            raise InvalidRequest(
                f"This profile was fetched for {point[0]:.2f}, {point[1]:.2f}; ask for that point "
                "or fetch another."
            )
        j0, _j1 = specs[0].meta["rows"]
        lon_idx = round((float(longitude) % 360.0) / 0.25) % NLON
        cell_lat = 90.0 - 0.25 * j0
        cell_lon = ((lon_idx * 0.25 + 180.0) % 360.0) - 180.0
        values: dict[tuple[str, int | None], float] = {}
        for spec, path in zip(specs, result.paths, strict=True):
            values[(spec.meta["variable"], spec.meta["level"])] = float(
                self._rows(path)[0, lon_idx]
            )

        def sfc(alias: str) -> float:
            return values[(ALIASES[alias], None)]

        p_sfc = sfc("sp")
        levels = sorted({lv for (_n, lv) in values if lv is not None}, reverse=True)
        p, zg, t, td, u, v = (
            [p_sfc],
            [sfc("orography")],
            [sfc("t2m")],
            [sfc("d2m")],
            [sfc("u10")],
            [sfc("v10")],
        )
        dropped = 0
        for lv in levels:
            pl = lv * 100.0
            if pl >= p_sfc - 100.0:
                dropped += 1
                continue
            q = values[(ALIASES["q"], lv)]
            p.append(pl)
            zg.append(values[(ALIASES["z"], lv)])
            t.append(values[(ALIASES["t"], lv)])
            td.append(
                float(derive.dewpoint_from_specific_humidity(max(q, 0.0), pl)) if q > 0 else np.nan
            )
            u.append(values[(ALIASES["u"], lv)])
            v.append(values[(ALIASES["v"], lv)])
        # ERA5 geopotential is in m2 s-2; the profile builder takes geopotential metres
        zg_m = np.asarray(zg) / 9.80665
        built = sounding.build_profile(
            pressure_pa=p, geopotential_m=zg_m, temperature_k=t, dewpoint_k=td, u=u, v=v,
            latitude=cell_lat, longitude=cell_lon, time=specs[0].meta["valid"],
        )  # fmt: skip
        dist = float(gribio._haversine_km(float(latitude), float(longitude), cell_lat, cell_lon))
        notes = [
            *built[1],
            f"ERA5 column at the grid cell {dist:.1f} km from the requested point "
            f"({latitude:.3f}, {longitude:.3f}); nearest cell, no horizontal interpolation. "
            f"{dropped} pressure level(s) at or below the model ground were dropped; the lowest "
            "level is the model surface (2 m temperature and dew point, 10 m wind, surface "
            "pressure, orography).",
            "Dew point above the surface is derived from specific humidity and pressure.",
        ]
        prov = self._provenance(
            request, specs, {"point": list(point), "distance_km": round(dist, 2)}
        )
        snd = sounding._finish(
            built,
            station=f"ERA5 {specs[0].meta['valid'][:13]}Z",
            station_name="ERA5 model column",
            latitude=cell_lat,
            longitude=cell_lon,
            launch_time=specs[0].meta["valid"],
            source="ERA5 reanalysis (ECMWF/C3S, ARCO-ERA5), "
            + specs[0].meta["selection"].split(";")[0],
            fmt="era5_zarr",
            file="",
            data_class="model",
            prov=prov,
        )
        snd.notes = notes
        return snd


PROVIDERS = (Era5Provider,)

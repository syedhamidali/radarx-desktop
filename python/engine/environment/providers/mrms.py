"""MRMS (Multi-Radar/Multi-Sensor, NOAA NSSL and NWS) products from the open archive on AWS
(``s3://noaa-mrms-pds``): one file per product and time on a 1 km grid over the contiguous US,
with a new file about every 2 minutes for most products.

Each request names **products** (``variables``): ``reflectivity_composite``,
``reflectivity_3d`` with the heights in ``params heights_km``, ``qpe_radar_1h``,
``qpe_gauge_1h``, ``precip_rate``, ``precip_type``, ``mesh``, ``rotation_30min``,
``echotop_18``, ``freezing_level`` ... or the product folder's own name as listed on the bucket
(``MergedReflectivityQC_05.00``). The bucket is listed (a few kB) to find the file nearest the
time asked for and its exact size; a name that is not on the bucket is an error, never a guess.

Time: the **nearest file** to the requested time is chosen (within 30 minutes); the real time of
the file and the offset are stated and nothing is interpolated. With ``time_end`` every file in
the window is fetched (a time series; the dialog shows the exact total first).

Which products are what: MRMS combines radars, and some products also use gauges or numerical
model fields. The class and the ingredients are in each layer's provenance:

* radar-only retrievals: composite and 3-D reflectivity, echo tops, VIL, MESH, rotation tracks,
  radar-only QPE, precipitation rate;
* radar with model-assisted classification: the precipitation type flag (uses model temperature);
* gauge-adjusted: the multi-sensor QPE (radar rain adjusted to hourly gauges);
* model: the freezing-level height (a numerical-model field distributed with MRMS).

Files are gzip-compressed GRIB2; reading them needs ecCodes (optional extra ``grib``). The area is
cropped after decoding, so the download does not shrink with the area.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import numpy as np

from engine import layer_provenance
from engine.environment import gribio
from engine.environment.errors import FetchFailed, InvalidRequest
from engine.environment.provider import Provider
from engine.environment.request import FetchResult, FileSpec, InventoryItem, Request

BUCKET_URL = "https://noaa-mrms-pds.s3.amazonaws.com"
REGION = "CONUS"
MAX_OFFSET = timedelta(minutes=30)
MAX_FILES = 200
_KEY_TIME = re.compile(r"_(\d{8})-(\d{6})\.grib2\.gz$")
_FOLDER = re.compile(r"^[A-Za-z0-9_.-]{3,80}$")

#: The 33 heights (km above mean sea level) of MergedReflectivityQC.
HEIGHTS_KM: tuple[float, ...] = (
    0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0,
    6.5, 7.0, 7.5, 8.0, 8.5, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0,
)  # fmt: skip

RADAR = "Radar-only: computed from the radars' measurements; no gauges, no model fields."
MODEL_CLASS = "Uses numerical-model temperature fields to classify the precipitation type."
GAUGE = "Radar rainfall adjusted to hourly rain gauges."
OBS_NOTE = (
    "Gridded product of the MRMS system: 1 km grid, every 2 minutes. It inherits the radars' "
    "errors (calibration, attenuation, beam height and blockage, range) plus those of the "
    "algorithm; coverage is poor in mountains and far from radars."
)


# alias -> (folder, title, kind, ingredients, units hint)
PRODUCTS: dict[str, tuple[str, str, str, str, str]] = {
    "reflectivity_composite": (
        "MergedReflectivityQCComposite_00.50", "Composite reflectivity (quality controlled)",
        "retrieved", RADAR, "dBZ"),
    "reflectivity_lowest": (
        "ReflectivityAtLowestAltitude_00.50", "Reflectivity at the lowest altitude",
        "retrieved", RADAR, "dBZ"),
    "precip_rate": ("PrecipRate_00.00", "Surface precipitation rate", "retrieved",
        RADAR + " " + MODEL_CLASS, "mm/h"),
    "precip_type": ("PrecipFlag_00.00", "Surface precipitation type flag", "retrieved",
        RADAR + " " + MODEL_CLASS, "category"),
    "qpe_radar_15m": ("RadarOnly_QPE_15M_00.00", "Radar-only rainfall, last 15 min", "retrieved",
        RADAR + " " + MODEL_CLASS, "mm"),
    "qpe_radar_1h": ("RadarOnly_QPE_01H_00.00", "Radar-only rainfall, last hour", "retrieved",
        RADAR + " " + MODEL_CLASS, "mm"),
    "qpe_radar_24h": ("RadarOnly_QPE_24H_00.00", "Radar-only rainfall, last 24 hours",
        "retrieved", RADAR + " " + MODEL_CLASS, "mm"),
    "qpe_gauge_1h": ("MultiSensor_QPE_01H_Pass2_00.00", "Gauge-adjusted rain, last hour",
        "retrieved", GAUGE + " " + MODEL_CLASS, "mm"),
    "qpe_gauge_24h": ("MultiSensor_QPE_24H_Pass2_00.00", "Gauge-adjusted rain, last 24 h",
        "retrieved", GAUGE + " " + MODEL_CLASS, "mm"),
    "mesh": ("MESH_00.50", "Maximum expected size of hail (MESH)", "retrieved", RADAR, "mm"),
    "posh": ("POSH_00.50", "Probability of severe hail", "retrieved", RADAR, "%"),
    "shi": ("SHI_00.50", "Severe hail index", "retrieved", RADAR, "J/m/s"),
    "vil": ("VIL_00.50", "Vertically integrated liquid", "retrieved", RADAR, "kg/m2"),
    "echotop_18": ("EchoTop_18_00.50", "Echo top, 18 dBZ", "retrieved", RADAR, "km"),
    "echotop_30": ("EchoTop_30_00.50", "Echo top, 30 dBZ", "retrieved", RADAR, "km"),
    "rotation_30min": ("RotationTrack30min_00.50", "Rotation track, 30 min",
        "retrieved", RADAR, "1e-3 1/s"),
    "rotation_60min": ("RotationTrack60min_00.50", "Rotation track, 60 min",
        "retrieved", RADAR, "1e-3 1/s"),
    "azshear_0_2km": ("MergedAzShear_0-2kmAGL_00.50", "Azimuthal shear, 0-2 km AGL", "retrieved",
        RADAR, "1e-3 1/s"),
    "azshear_3_6km": ("MergedAzShear_3-6kmAGL_00.50", "Azimuthal shear, 3-6 km AGL", "retrieved",
        RADAR, "1e-3 1/s"),
    "freezing_level": ("Model_0degC_Height_00.50", "Height of the 0 degC level (model)", "model",
        "A numerical-model field (not radar) distributed with MRMS; it is large.", "m"),
}  # fmt: skip

#: Said in every dataset: the files mark "no data" and "no coverage" below the physical range.
SPECIAL_VALUES = (
    "The files use values below the physical range (for example -999, -99, -3, -1) for no data or "
    "no coverage (which one depends on the product); they are kept as they are, nothing is "
    "filled or hidden. Mask them before computing with a field."
)

REFERENCES = [
    "Smith, T. M., et al., 2016: Multi-Radar Multi-Sensor (MRMS) severe weather and aviation "
    "products: Initial operating capabilities. Bull. Amer. Meteor. Soc., 97, 1617-1630.",
    "Zhang, J., et al., 2016: Multi-Radar Multi-Sensor (MRMS) quantitative precipitation "
    "estimation: Initial operating capabilities. Bull. Amer. Meteor. Soc., 97, 621-638.",
]
METHOD_REFERENCES = {
    "mesh": "Witt, A., et al., 1998: An enhanced hail detection algorithm for the WSR-88D. Wea. "
    "Forecasting, 13, 286-303.",
}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def key_time(key: str) -> datetime:
    m = _KEY_TIME.search(key)
    if not m:
        raise FetchFailed(f"Cannot read the time from the file name {key!r}.")
    return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def list_keys(env: Any, folder: str, day: str, hour_prefix: str) -> list[tuple[str, int]]:
    """``[(key, size)]`` of the files of *folder* whose time starts with *hour_prefix*
    (``YYYYMMDD-HH``)."""
    prefix = f"{REGION}/{folder}/{day}/MRMS_{folder}_{hour_prefix}"
    url = f"{BUCKET_URL}/?list-type=2&prefix={prefix}&max-keys=200"
    root = ET.fromstring(env.http.get_bytes(url, max_bytes=2 * 1024 * 1024))
    out = []
    for c in root.iter():
        if c.tag.endswith("Contents"):
            parts = {x.tag.rsplit("}", 1)[-1]: (x.text or "") for x in c}
            out.append((parts["Key"], int(parts["Size"])))
    return out


def expand(request: Request) -> list[tuple[str, str, float | None]]:
    """``[(alias or folder, folder, height_km)]`` the request names."""
    if not request.variables:
        raise InvalidRequest(
            "Name the products (variables), for example reflectivity_composite, mesh, "
            "rotation_30min, qpe_radar_1h; see the inventory."
        )
    out: list[tuple[str, str, float | None]] = []
    for name in request.variables:
        key = name.strip()
        if key == "reflectivity_3d":
            raw = request.params.get("heights_km")
            if raw == "all":
                heights = list(HEIGHTS_KM)
            elif isinstance(raw, list) and raw:
                heights = [float(h) for h in raw]
            else:
                raise InvalidRequest(
                    "reflectivity_3d needs params heights_km, a list (km above mean sea level) "
                    f"or 'all'. Heights: {', '.join(f'{h:g}' for h in HEIGHTS_KM)}."
                )
            bad = [h for h in heights if h not in HEIGHTS_KM]
            if bad:
                raise InvalidRequest(
                    f"MRMS has no reflectivity at {bad[0]:g} km. Heights: "
                    f"{', '.join(f'{h:g}' for h in HEIGHTS_KM)}."
                )
            out += [(key, f"MergedReflectivityQC_{h:05.2f}", h) for h in sorted(set(heights))]
        elif key in PRODUCTS:
            out.append((key, PRODUCTS[key][0], None))
        elif _FOLDER.match(key):
            out.append((key, key, None))
        else:
            raise InvalidRequest(f"{name!r} is not a product name.")
    return out


class MrmsProvider(Provider):
    id: ClassVar[str] = "mrms"
    name: ClassVar[str] = "MRMS radar products (NOAA NSSL, AWS open data)"
    kind: ClassVar[str] = "retrieved"
    attribution: ClassVar[str] = (
        "Multi-Radar/Multi-Sensor (MRMS) system, NOAA National Severe Storms Laboratory and "
        "National Weather Service, distributed through the NOAA Open Data Dissemination "
        "programme on Amazon Web Services (bucket noaa-mrms-pds)."
    )
    licence: ClassVar[str] = (
        "NOAA open data: free to use and redistribute; credit NOAA/NSSL/NWS. "
        "https://registry.opendata.aws/noaa-mrms-pds/ , "
        "https://www.noaa.gov/information-technology/open-data-dissemination"
    )
    homepage: ClassVar[str] = "https://registry.opendata.aws/noaa-mrms-pds/"
    description: ClassVar[str] = (
        "1 km, 2-minute mosaics over the contiguous US: 3-D and composite reflectivity, rainfall "
        "rate and accumulations, precipitation type, hail, rotation tracks, echo tops. Retrieved "
        "from radar (some products use gauges or model fields: stated per layer)."
    )
    unknown_size_cap: ClassVar[int] = 16 * 1024 * 1024

    def inventory(self, query: dict[str, Any] | None = None) -> list[InventoryItem]:
        common = {
            "resolution": "0.01 degree (about 1 km) grid, 7000 x 3500",
            "coverage": "contiguous United States",
            "cadence": "2 minutes",
        }
        items = [
            InventoryItem(
                product=alias, title=title, description=f"{ing} Class: {kind}. Unit: {units}.",
                variables=(alias,), **common,
            )
            for alias, (_f, title, kind, ing, units) in PRODUCTS.items()
        ]  # fmt: skip
        items.insert(
            2,
            InventoryItem(
                product="reflectivity_3d",
                title="3-D reflectivity (33 heights)",
                description=RADAR + " Choose params heights_km; one file per height "
                "(about 0.1 to 1.6 MB each).",
                variables=("reflectivity_3d",),
                **common,
            ),
        )
        return items

    # -- planning --------------------------------------------------------------------------

    def plan(self, request: Request, env: Any) -> list[FileSpec]:
        if not request.time_start:
            raise InvalidRequest(
                "Give the time you want (time_start, UTC); the nearest file is used."
            )
        start = datetime.strptime(request.time_start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        end = (
            datetime.strptime(request.time_end, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            if request.time_end
            else None
        )
        specs: list[FileSpec] = []
        for alias, folder, height in expand(request):
            lo, hi = (start, end) if end else (start - MAX_OFFSET, start + MAX_OFFSET)
            found: list[tuple[datetime, str, int]] = []
            t = lo.replace(minute=0, second=0, microsecond=0)
            while t <= hi:
                day, hour = f"{t:%Y%m%d}", f"{t:%Y%m%d-%H}"
                for key, size in list_keys(env, folder, day, hour):
                    found.append((key_time(key), key, size))
                t += timedelta(hours=1)
            if not found:
                raise InvalidRequest(
                    f"The MRMS bucket has no {folder!r} files near {start:%Y-%m-%d %H:%M} UTC. "
                    "Check the product name (inventory) and the date (the archive starts in "
                    "October 2020 and not every product exists for every date)."
                )
            if end:
                chosen = sorted(c for c in found if lo <= c[0] <= hi)
                if not chosen:
                    raise InvalidRequest(f"No {folder!r} file between the times asked for.")
                if len(chosen) > MAX_FILES:
                    raise InvalidRequest(
                        f"{len(chosen)} files are more than {MAX_FILES}: shorten the window."
                    )
            else:
                best = min(found, key=lambda c: abs(c[0] - start))
                if abs(best[0] - start) > MAX_OFFSET:
                    raise InvalidRequest(
                        f"The nearest {folder!r} file is {abs(best[0] - start)} from the time "
                        "asked for (limit 30 minutes)."
                    )
                chosen = [best]
            for when, key, size in chosen:
                off = (when - start).total_seconds()
                specs.append(
                    FileSpec(
                        key=key,
                        name=key.rsplit("/", 1)[-1],
                        url=f"{BUCKET_URL}/{key}",
                        size=size,
                        meta={
                            "alias": alias, "folder": folder, "height_km": height,
                            "valid": f"{when:%Y-%m-%dT%H:%M:%SZ}",
                            "target": f"{start:%Y-%m-%dT%H:%M:%SZ}",
                            "offset_s": off, "series": bool(end),
                        },
                    )
                )  # fmt: skip
        return specs

    def estimate_notes(self, request: Request, specs: list[FileSpec]) -> list[str]:
        if not specs:
            return []
        notes = []
        seen = set()
        for s in specs:
            m = s.meta
            if m["series"] or m["folder"] in seen:
                continue
            seen.add(m["folder"])
            if len(seen) <= 6:
                notes.append(
                    f"{m['folder']}: file of {m['valid'][11:19]} UTC, "
                    f"{m['offset_s']:+.0f} s from the time asked for (nearest file, "
                    "not interpolated)"
                )
        if request.time_end:
            notes.append(f"A series: {len(specs)} file(s) between the times asked for.")
        if request.bbox is not None:
            notes.append("Whole files are downloaded; the area is cropped after decoding.")
        return notes

    # -- provenance -----------------------------------------------------------------------

    def provenance(self, request: Request, specs: list[FileSpec]) -> dict[str, Any]:
        return self._provenance(request, specs).to_dict()

    def _provenance(
        self, request: Request, specs: list[FileSpec]
    ) -> layer_provenance.LayerProvenance:
        aliases = list(dict.fromkeys(s.meta["alias"] for s in specs))
        first = aliases[0] if aliases else ""
        info = PRODUCTS.get(first)
        kinds = {PRODUCTS[a][2] if a in PRODUCTS else "retrieved" for a in aliases}
        kind = kinds.pop() if len(kinds) == 1 else "retrieved"
        ingredients = " ".join(dict.fromkeys(PRODUCTS[a][3] for a in aliases if a in PRODUCTS))
        valid = sorted({s.meta["valid"] for s in specs})
        refs = list(REFERENCES)
        refs += [METHOD_REFERENCES[a] for a in aliases if a in METHOD_REFERENCES]
        return layer_provenance.LayerProvenance(
            kind=kind,
            source="NOAA MRMS " + (", ".join(aliases[:4]) + (" ..." if len(aliases) > 4 else "")),
            title=info[1] if info and len(aliases) == 1 else "MRMS products",
            valid_time=valid[0] if valid else "",
            valid_end=valid[-1] if len(valid) > 1 else "",
            resolution="regular latitude/longitude grid over the contiguous US, 0.01 degree (about "
            "1 km) "
            "for most products, finer for some (the spacing of each variable is in its "
            "grid_spacing_deg); cropped to the area while decoding, no regridding",
            method="Gridded MRMS products downloaded as GRIB2 files from the NOAA archive and "
            "decoded with ecCodes; the nearest file to the requested time is used and not "
            "interpolated in time. " + ingredients,
            parameters={
                "products": aliases,
                "requested_time": specs[0].meta["target"] if specs else None,
                "bbox": list(request.bbox) if request.bbox else None,
                "files": len(specs),
                "time_selection": "nearest file within 30 minutes; not interpolated"
                if not request.time_end
                else "every file in the window",
            },
            uncertainty=OBS_NOTE
            + (
                " Gauge adjustment depends on the density of gauges."
                if "qpe_gauge" in " ".join(aliases)
                else ""
            ),
            references=refs,
            attribution=self.attribution,
            licence=self.licence,
            inputs=aliases[:40],
        )

    # -- reading ---------------------------------------------------------------------------

    def open(self, result: FetchResult, env: Any = None) -> Any:
        import xarray as xr

        from engine.environment.core import Environment

        specs, _ = self._planned(result.request, env or Environment.default())
        if len(specs) != len(result.paths):
            raise FetchFailed("The cached listing no longer matches the files; fetch again.")
        groups: dict[str, list[tuple[FileSpec, Any]]] = {}
        for spec, path in zip(specs, result.paths, strict=True):
            fld = gribio.read_first(path, bbox=result.request.bbox)  # cropped while decoding
            groups.setdefault(spec.meta["alias"], []).append((spec, fld))
        coords: dict[str, Any] = {}
        data_vars: dict[str, Any] = {}
        first_grid: tuple[Any, ...] | None = None
        for alias, members in groups.items():
            members.sort(key=lambda m: (m[0].meta["height_km"] or 0, m[0].meta["valid"]))
            spec0, fld0 = members[0]
            grid = fld0.grid
            signature = (grid.lat.size, grid.lon.size, float(grid.lat[0]), float(grid.lon[0]))
            if any((m[1].grid.lat.size, m[1].grid.lon.size) != signature[:2] for m in members):
                raise FetchFailed(f"The files of {alias} are not all on the same grid.")
            name = slug(alias)
            # products are on grids of different spacing (the rotation tracks are twice as fine):
            # the first grid keeps the plain names, another grid gets names of its own
            if first_grid is None or signature == first_grid:
                first_grid = signature
                lat_dim, lon_dim = "latitude", "longitude"
            else:
                lat_dim, lon_dim = f"latitude_{name}", f"longitude_{name}"
            coords[lat_dim] = ((lat_dim,), grid.lat.astype("float32"), {"units": "degrees_north"})
            coords[lon_dim] = ((lon_dim,), grid.lon.astype("float32"), {"units": "degrees_east"})
            info = PRODUCTS.get(alias)
            file_units = gribio.clean_units(fld0.units)
            units = file_units or (info[4] if info else "dBZ" if alias == "reflectivity_3d" else "")
            attrs = {
                "long_name": info[1] if info else spec0.meta["folder"],
                "units": units,
                "units_source": "file"
                if file_units
                else "NSSL product documentation (not in the file)",
                "mrms_product": spec0.meta["folder"].split("_")[0]
                if alias == "reflectivity_3d"
                else spec0.meta["folder"],
                "valid_times": ",".join(m[0].meta["valid"] for m in members),
                "grid_spacing_deg": round(float(abs(grid.lat[1] - grid.lat[0])), 6)
                if grid.lat.size > 1
                else None,
            }
            stack = np.stack([m[1].values for m in members])
            if alias == "reflectivity_3d":
                data_vars[name] = (("height_km", lat_dim, lon_dim), stack, attrs)
                coords["height_km"] = (
                    ("height_km",),
                    np.array([m[0].meta["height_km"] for m in members], dtype="float32"),
                    {"units": "km", "long_name": "height above mean sea level"},
                )
            elif len(members) > 1:
                tn = f"time_{name}"
                data_vars[name] = ((tn, lat_dim, lon_dim), stack, attrs)
                coords[tn] = (
                    (tn,),
                    np.array([np.datetime64(m[0].meta["valid"][:-1]) for m in members]),
                )
            else:
                data_vars[name] = ((lat_dim, lon_dim), stack[0], attrs)
            members.clear()  # release the arrays as soon as they are in the dataset
        ds = xr.Dataset(data_vars, coords=coords)
        ds.attrs.update(layer_provenance.attrs(self._provenance(result.request, specs)))
        ds.attrs["title"] = "MRMS " + ", ".join(groups)
        ds.attrs["special_values"] = SPECIAL_VALUES
        ds.attrs["time_selection"] = "; ".join(
            f"{s.meta['folder']} {s.meta['valid']} ({s.meta['offset_s']:+.0f} s)"
            for s in {s.meta["folder"]: s for s in specs}.values()
        )
        return ds


PROVIDERS = (MrmsProvider,)

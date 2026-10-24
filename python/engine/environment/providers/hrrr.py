"""HRRR (High-Resolution Rapid Refresh, NOAA/NCEP): analysis and forecast fields from the open
archive on AWS (``s3://noaa-hrrr-bdp-pds``), downloaded **message by message**.

How a request becomes a download
--------------------------------
Each GRIB2 file (140 MB surface, 390 MB pressure levels, 650 MB native levels) has a small text
index next to it (``.grib2.idx``, 10 to 60 kB) listing every message with its byte offset. The
provider reads that index, finds the messages that match the requested variables and levels, and
asks the server for **only those byte ranges** (HTTP ``Range``). The estimate shows the exact
number of bytes (from the index offsets and the file size), the approval covers exactly that, and
each message is cached on its own, so a repeat of the request, or a request that shares messages
with an earlier one, downloads nothing.

The area is cropped **after** decoding (GRIB2 cannot be decoded in parts), so the download does
not get smaller with the area; it gets smaller with fewer variables and levels. The estimate says
so.

Time: a requested time is matched to the **nearest hourly valid time** of the analysis (forecast
hour 0) or of the forecast hour asked for. The chosen run, forecast hour, valid time and the offset
to the requested time are stated in the estimate and in the provenance. Nothing is interpolated in
time.

Wind components in the NCEP Lambert grid are relative to the grid; they are rotated to east and
north (``derive.grid_winds_to_earth``) when both components are read.

Variables are named either by a short alias (``cape``, ``srh_0_3km``, see :data:`ALIASES`) or by
the index's own words ``NAME:level`` (``TMP:500 mb``, ``HLCY:3000-0 m above ground``), optionally
``NAME:level:step`` when a level appears with several step texts; ``NAME:@mb`` and
``NAME:@hybrid`` mean every pressure level / every native level. Names are checked against the
index of the file: a name that is not in it is an error, never a guess.

Data: NOAA/NCEP, public, no restriction on use (NOAA Open Data Dissemination); credit NOAA/NCEP.
"""

from __future__ import annotations

import itertools
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from engine import layer_provenance
from engine.environment import derive, gribio
from engine.environment.errors import FetchFailed, InvalidRequest
from engine.environment.provider import Provider
from engine.environment.request import FetchResult, FileSpec, InventoryItem, Request

BUCKET_URL = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
IDX_MAX_BYTES = 4 * 1024 * 1024
#: Hours of forecast each cycle has: hourly runs reach 18 h, the 00/06/12/18 UTC runs 48 h.
MAX_FXX_SHORT, MAX_FXX_LONG = 18, 48

#: product name -> (file code, what it holds)
PRODUCTS: dict[str, tuple[str, str]] = {
    "surface": (
        "wrfsfc",
        "2-D fields at the surface, near the surface and for the whole column (CAPE, CIN, "
        "helicity, shear, reflectivity, 2 m and 10 m values, a few pressure levels).",
    ),
    "pressure": (
        "wrfprs",
        "3-D fields on 40 pressure levels (1000 to 50 hPa, 25 hPa apart): height, temperature, "
        "humidity, wind and more.",
    ),
    "native": (
        "wrfnat",
        "3-D fields on the model's 50 native (hybrid) levels: pressure, height, temperature, "
        "humidity, wind and the hydrometeor mixing ratios.",
    ),
}

#: alias -> (name, level, description). Checked against the file's index at planning time.
ALIASES: dict[str, tuple[str, str, str]] = {
    "reflectivity_composite": ("REFC", "entire atmosphere", "Composite reflectivity of the model."),
    "reflectivity_1km": ("REFD", "1000 m above ground", "Model reflectivity at 1 km above ground."),
    "reflectivity_4km": ("REFD", "4000 m above ground", "Model reflectivity at 4 km above ground."),
    "echo_top": ("RETOP", "cloud top", "Model echo-top height."),
    "vil": ("VIL", "entire atmosphere", "Vertically integrated liquid of the model."),
    "cape": ("CAPE", "surface", "CAPE of the surface-based parcel."),
    "cin": ("CIN", "surface", "CIN of the surface-based parcel."),
    "cape_180_0mb": ("CAPE", "180-0 mb above ground", "CAPE for the layer 180-0 hPa above ground."),
    "cin_180_0mb": ("CIN", "180-0 mb above ground", "CIN for the layer 180-0 hPa above ground."),
    "cape_255_0mb": ("CAPE", "255-0 mb above ground", "CAPE for the layer 255-0 hPa above ground."),
    "cin_255_0mb": ("CIN", "255-0 mb above ground", "CIN for the layer 255-0 hPa above ground."),
    "lifted_index": ("LFTX", "500-1000 mb", "Lifted index (500-1000 hPa)."),
    "srh_0_1km": ("HLCY", "1000-0 m above ground", "Storm-relative helicity 0-1 km."),
    "srh_0_3km": ("HLCY", "3000-0 m above ground", "Storm-relative helicity 0-3 km."),
    "storm_motion_u": ("USTM", "0-6000 m above ground", "u component of the storm motion."),
    "storm_motion_v": ("VSTM", "0-6000 m above ground", "v component of the storm motion."),
    "shear_u_0_1km": ("VUCSH", "0-1000 m above ground", "Vertical shear, u component, 0-1 km."),
    "shear_v_0_1km": ("VVCSH", "0-1000 m above ground", "Vertical shear, v component, 0-1 km."),
    "shear_u_0_6km": ("VUCSH", "0-6000 m above ground", "Vertical shear, u component, 0-6 km."),
    "shear_v_0_6km": ("VVCSH", "0-6000 m above ground", "Vertical shear, v component, 0-6 km."),
    "updraft_helicity_2_5km": (
        "MXUPHL",
        "5000-2000 m above ground",
        "Maximum updraft helicity, 2-5 km (hourly maximum).",
    ),
    "lightning": ("LTNG", "entire atmosphere", "Model lightning threat (units as in the file)."),
    "hail": ("HAIL", "surface", "Model hail size (units as in the file)."),
    "pwat": ("PWAT", "entire atmosphere (considered as a single layer)", "Precipitable water."),
    "freezing_level_height": ("HGT", "0C isotherm", "Height of the 0 degC isotherm."),
    "lcl_height": (
        "HGT",
        "level of adiabatic condensation from sfc",
        "Lifting condensation level.",
    ),
    "boundary_layer_height": ("HPBL", "surface", "Planetary boundary-layer height."),
    "t2m": ("TMP", "2 m above ground", "Temperature at 2 m."),
    "d2m": ("DPT", "2 m above ground", "Dew point at 2 m."),
    "rh2m": ("RH", "2 m above ground", "Relative humidity at 2 m."),
    "u10": ("UGRD", "10 m above ground", "u wind at 10 m (earth-relative after rotation)."),
    "v10": ("VGRD", "10 m above ground", "v wind at 10 m (earth-relative after rotation)."),
    "gust": ("GUST", "surface", "Wind gust at the surface."),
    "surface_pressure": ("PRES", "surface", "Surface pressure."),
    "mslp": ("MSLMA", "mean sea level", "Mean sea level pressure (MAPS system reduction)."),
    "terrain_height": ("HGT", "surface", "Model terrain height."),
    "visibility": ("VIS", "surface", "Visibility."),
    "precip_rate": ("PRATE", "surface", "Precipitation rate."),
    "cloud_water": ("CLMR", "@hybrid", "Cloud water mixing ratio on the native levels."),
    "rain_water": ("RWMR", "@hybrid", "Rain water mixing ratio on the native levels."),
    "snow_water": ("SNMR", "@hybrid", "Snow mixing ratio on the native levels."),
    "graupel": ("GRLE", "@hybrid", "Graupel mixing ratio on the native levels."),
    "cloud_ice": ("CIMIXR", "@hybrid", "Cloud ice mixing ratio on the native levels."),
}

#: Named sets of aliases (a bundle is expanded before the index is read).
BUNDLES: dict[str, tuple[str, ...]] = {
    "@storm_environment": (
        "cape",
        "cin",
        "srh_0_1km",
        "srh_0_3km",
        "shear_u_0_1km",
        "shear_v_0_1km",
        "shear_u_0_6km",
        "shear_v_0_6km",
        "reflectivity_composite",
    ),
    "@hydrometeors": ("cloud_water", "rain_water", "snow_water", "graupel", "cloud_ice"),
}

#: Vector pairs whose grid-relative components are rotated together.
VECTOR_PAIRS = (("UGRD", "VGRD"), ("VUCSH", "VVCSH"), ("USTM", "VSTM"), ("MAXUW", "MAXVW"))

#: Profile products: surface values and the pressure-level variables of a column.
PROFILE_SURFACE = (
    "PRES:surface",
    "HGT:surface",
    "TMP:2 m above ground",
    "DPT:2 m above ground",
    "UGRD:10 m above ground",
    "VGRD:10 m above ground",
)
PROFILE_LEVEL_VARS = ("HGT", "TMP", "DPT", "UGRD", "VGRD")
DEFAULT_PROFILE_STEP_HPA = 50

_INSTANT = re.compile(r"^(anl|\d+ (hour|min) fcst)$")
_LEVEL_MB = re.compile(r"^(\d+(?:\.\d+)?) mb$")
_LEVEL_HYBRID = re.compile(r"^(\d+) hybrid level$")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


class IdxRow:
    """One line of a ``.idx`` file."""

    __slots__ = ("end", "level", "n", "name", "offset", "step")

    def __init__(self, n: int, offset: int, name: str, level: str, step: str) -> None:
        self.n, self.offset, self.name, self.level, self.step = n, offset, name, level, step
        self.end = -1  # last byte (inclusive), filled in by parse_idx

    @property
    def size(self) -> int:
        return self.end - self.offset + 1


def parse_idx(text: str, file_bytes: int | None) -> list[IdxRow]:
    """Rows of a ``.idx`` text with the byte range of every message. The last message runs to the
    end of the file, so *file_bytes* (the size from a HEAD request) is needed for its range."""
    rows: list[IdxRow] = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) < 6:
            continue
        try:
            rows.append(IdxRow(int(parts[0]), int(parts[1]), parts[3], parts[4], parts[5]))
        except ValueError:
            continue
    if not rows:
        raise InvalidRequest(
            "The index file of this HRRR product is empty or not in the usual form."
        )
    for a, b in itertools.pairwise(rows):
        if b.offset <= a.offset:
            raise InvalidRequest("The index file lists messages out of order; not used.")
        a.end = b.offset - 1
    if file_bytes is None or file_bytes <= rows[-1].offset:
        rows.pop()  # the last message's size is unknown: do not guess it
    else:
        rows[-1].end = file_bytes - 1
    return rows


def profile_levels(params: Any) -> list[int]:
    """Pressure levels (hPa, high to low) of a profile request: ``levels`` (a list of hPa, each a
    multiple of 25 between 50 and 1000) or every ``levels_step_hpa`` (25 or 50) from 1000 to 100."""
    if params.get("levels") is not None:
        raw = params["levels"]
        if not isinstance(raw, list) or not raw or len(raw) > 40:
            raise InvalidRequest("levels must be a list of up to 40 pressures in hPa.")
        try:
            levels = sorted({int(v) for v in raw if int(v) == v}, reverse=True)
        except (TypeError, ValueError) as exc:
            raise InvalidRequest("levels must be whole numbers in hPa.") from exc
        if len(levels) != len(set(raw)) or any(v % 25 or not 50 <= v <= 1000 for v in levels):
            raise InvalidRequest("Each level must be a multiple of 25 hPa between 50 and 1000.")
        return levels
    step = params.get("levels_step_hpa", DEFAULT_PROFILE_STEP_HPA)
    if step not in (25, 50):
        raise InvalidRequest("levels_step_hpa must be 25 or 50.")
    return list(range(1000, 99, -int(step)))


def level_value(level: str) -> float | None:
    """Numeric level of ``"500 mb"`` (hPa) or ``"12 hybrid level"`` (index), else ``None``."""
    for pat in (_LEVEL_MB, _LEVEL_HYBRID):
        m = pat.match(level)
        if m:
            return float(m.group(1))
    return None


def expand_variables(variables: tuple[str, ...]) -> list[tuple[str, str | None]]:
    """``[(token, alias or None)]`` with bundles expanded; duplicates removed (order kept)."""
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for tok in variables:
        names = BUNDLES.get(tok.strip().lower(), (tok.strip(),))
        if tok.strip().startswith("@") and tok.strip().lower() not in BUNDLES:
            raise InvalidRequest(f"Unknown set {tok!r}. Known sets: {', '.join(sorted(BUNDLES))}.")
        for name in names:
            key = name.strip().lower() if name.strip().lower() in ALIASES else name.strip()
            if key not in seen:
                seen.add(key)
                out.append((key, key if key in ALIASES else None))
    return out


def _token_parts(token: str, alias: str | None) -> tuple[str, str, str | None]:
    if alias:
        name, level, _ = ALIASES[alias]
        return name, level, None
    parts = token.split(":")
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        raise InvalidRequest(
            f"Variable {token!r} is neither an alias nor of the form NAME:level (for example "
            "'TMP:500 mb' or 'HLCY:3000-0 m above ground'). Aliases: "
            f"{', '.join(sorted(ALIASES)[:12])} ..."
        )
    step = ":".join(parts[2:]).strip() or None
    return parts[0].strip(), parts[1].strip(), step


def match_rows(rows: list[IdxRow], token: str, alias: str | None, product: str) -> list[IdxRow]:
    """The index rows a variable token names (exact words; wildcards ``@mb`` / ``@hybrid``)."""
    name, level, step = _token_parts(token, alias)
    if level == "@mb":
        hit = [r for r in rows if r.name == name and _LEVEL_MB.match(r.level)]
    elif level == "@hybrid":
        hit = [r for r in rows if r.name == name and _LEVEL_HYBRID.match(r.level)]
    else:
        hit = [r for r in rows if r.name == name and r.level == level]
    if step is not None:
        hit = [r for r in hit if r.step == step]
    if not hit:
        same_name = sorted({f"{r.level}" for r in rows if r.name == name})
        extra = (
            f" Levels of {name} in this file: {', '.join(same_name[:12])}"
            f"{' ...' if len(same_name) > 12 else ''}."
            if same_name
            else f" {name} is not in this file at all (product {product!r})."
        )
        raise InvalidRequest(
            f"{token!r} is not in the index of the HRRR {product} file.{extra} "
            "Other products hold other fields: see the inventory."
        )
    if level not in ("@mb", "@hybrid") and len(hit) > 1:
        inst = [r for r in hit if _INSTANT.match(r.step)]
        if len(inst) == 1:
            return inst
        steps = ", ".join(f"'{r.step}'" for r in hit)
        raise InvalidRequest(
            f"{token!r} appears {len(hit)} times in the index (step texts: {steps}). "
            f"Say which one: '{name}:{level}:<step text>'."
        )
    return hit


class Selection:
    """Which run and forecast hour a request means, and how far that is from the time asked for."""

    def __init__(self, run: datetime, fxx: int, target: datetime | None) -> None:
        self.run, self.fxx, self.target = run, fxx, target
        self.valid = run + timedelta(hours=fxx)
        self.offset_s = (self.valid - target).total_seconds() if target else None

    @staticmethod
    def of(request: Request) -> Selection:
        p = request.params
        fxx = p.get("fxx", p.get("forecast_hour", 0))
        if isinstance(fxx, bool) or not isinstance(fxx, (int, float)) or int(fxx) != fxx or fxx < 0:
            raise InvalidRequest("fxx (forecast hour) must be a whole number, 0 or more.")
        fxx = int(fxx)
        target = _dt(request.time_start) if request.time_start else None
        if p.get("run"):
            run = _dt(str(p["run"]))
            if run.minute or run.second:
                raise InvalidRequest("A HRRR run starts on the hour (run=YYYY-MM-DDTHH:00Z).")
        elif target is not None:
            valid = (target + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
            run = valid - timedelta(hours=fxx)
        else:
            raise InvalidRequest(
                "Give the time you want (time_start, UTC: the nearest hourly valid time is used) "
                "or the model run and forecast hour (params run and fxx)."
            )
        limit = MAX_FXX_LONG if run.hour % 6 == 0 else MAX_FXX_SHORT
        if fxx > limit:
            raise InvalidRequest(
                f"The {run:%H} UTC HRRR run has forecasts to hour {limit}; {fxx} was asked for."
            )
        return Selection(run, fxx, target)

    def describe(self) -> str:
        kind = "analysis" if self.fxx == 0 else "forecast"
        text = (
            f"run {self.run:%Y-%m-%d %H} UTC, forecast hour {self.fxx} ({kind}), "
            f"valid {self.valid:%Y-%m-%d %H:%M} UTC"
        )
        if self.target is not None and self.offset_s is not None:
            mins = round(self.offset_s / 60)
            text += (
                f"; requested {self.target:%H:%M} UTC, nearest hourly valid time is "
                f"{'the same' if mins == 0 else f'{mins:+d} min away'} "
                "(not interpolated in time)"
            )
        return text


def _dt(text: Any) -> datetime:
    from engine.environment.request import parse_time

    return datetime.strptime(parse_time(str(text)), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def file_url(sel: Selection, product: str) -> str:
    code = PRODUCTS[product][0]
    return (
        f"{BUCKET_URL}/hrrr.{sel.run:%Y%m%d}/conus/hrrr.t{sel.run:%H}z.{code}f{sel.fxx:02d}.grib2"
    )


class HrrrProvider(Provider):
    id: ClassVar[str] = "hrrr"
    name: ClassVar[str] = "HRRR model analysis and forecast (NOAA, AWS open data)"
    kind: ClassVar[str] = "model"
    attribution: ClassVar[str] = (
        "High-Resolution Rapid Refresh (HRRR), NOAA/NCEP/EMC, distributed through the NOAA Open "
        "Data Dissemination programme on Amazon Web Services (bucket noaa-hrrr-bdp-pds)."
    )
    licence: ClassVar[str] = (
        "NOAA open data: free to use and redistribute; credit NOAA/NCEP. "
        "https://registry.opendata.aws/noaa-hrrr-pds/ , https://www.noaa.gov/information-technology/open-data-dissemination"
    )
    homepage: ClassVar[str] = "https://registry.opendata.aws/noaa-hrrr-pds/"
    description: ClassVar[str] = (
        "3 km model fields over the contiguous US, hourly: "
        "environment of a storm, model reflectivity, hydrometeors, profiles. A model estimate, "
        "never an observation."
    )
    unknown_size_cap: ClassVar[int] = 16 * 1024 * 1024

    # -- inventory -------------------------------------------------------------------------

    def inventory(self, query: dict[str, Any] | None = None) -> list[InventoryItem]:
        items = []
        for product, (_code, text) in PRODUCTS.items():
            names = tuple(a for a, (_n, _l, _d) in ALIASES.items() if _product_of(a) == product)
            items.append(
                InventoryItem(
                    product=product,
                    title=f"HRRR {product} fields",
                    description=text + " Variables are checked against the file's index.",
                    variables=names,
                    resolution="3 km Lambert conformal grid, 1799 x 1059 (CONUS)",
                    coverage="contiguous United States",
                    cadence="hourly runs, forecast hours 0-18 (0-48 at 00/06/12/18 UTC)",
                )
            )
        items.append(
            InventoryItem(
                product="profile",
                title="HRRR vertical profile",
                description="Surface values and the pressure levels needed for a sounding "
                "(height, temperature, dew point, wind) from two files; the column at one point "
                "is read with profile_at (levels below the model ground are dropped).",
                variables=PROFILE_LEVEL_VARS,
                resolution="pressure levels 50 hPa apart by default (see levels_step_hpa, levels)",
                coverage="contiguous United States",
                cadence="hourly",
            )
        )
        return items

    # -- planning --------------------------------------------------------------------------

    def _tokens(self, request: Request) -> list[tuple[str, str, str | None]]:
        """``[(product, token, alias)]`` the request asks for."""
        product = request.product or "surface"
        if product == "profile":
            levels = profile_levels(request.params)
            lv_tokens = [f"{v}:{lv} mb" for lv in levels for v in PROFILE_LEVEL_VARS]
            return [("surface", t, None) for t in PROFILE_SURFACE] + [
                ("pressure", t, None) for t in lv_tokens
            ]
        if product not in PRODUCTS:
            raise InvalidRequest(
                f"Unknown HRRR product {product!r}. Products: {', '.join([*PRODUCTS, 'profile'])}."
            )
        if not request.variables:
            raise InvalidRequest(
                "Name the variables to download (for example cape, srh_0_3km, 'TMP:500 mb'): the "
                "files are hundreds of MB and nothing is downloaded whole."
            )
        return [(product, tok, alias) for tok, alias in expand_variables(request.variables)]

    def plan(self, request: Request, env: Any) -> list[FileSpec]:
        sel = Selection.of(request)
        tokens = self._tokens(request)
        specs: list[FileSpec] = []
        per_product: dict[str, list[IdxRow]] = {}
        urls: dict[str, str] = {}
        sizes: dict[str, int | None] = {}
        idx_bytes = 0
        for product in dict.fromkeys(t[0] for t in tokens):
            url = file_url(sel, product)
            urls[product] = url
            try:
                text = env.http.get_bytes(url + ".idx", max_bytes=IDX_MAX_BYTES)
                size = env.http.content_length(url)
            except FetchFailed as exc:
                if "404" in str(exc):
                    raise InvalidRequest(
                        f"The HRRR archive has no {product} file for "
                        f"{sel.describe().split(';')[0]}. Check the date and hour: the open "
                        "archive does not hold every run, and a new run appears about an hour "
                        "after its start."
                    ) from exc
                raise
            idx_bytes += len(text)
            sizes[product] = size
            per_product[product] = parse_idx(text.decode("utf-8", "replace"), size)
        for product, token, alias in tokens:
            for row in match_rows(per_product[product], token, alias, product):
                code = PRODUCTS[product][0]
                lvl = level_value(row.level)
                specs.append(
                    FileSpec(
                        key=f"conus/{code}/{sel.run:%Y%m%d%H}/f{sel.fxx:02d}/{row.name}/{row.level}/{row.step}",
                        name=f"hrrr_{sel.run:%Y%m%dT%H}_{code}_f{sel.fxx:02d}_{row.name}_{slug(row.level)}.grib2",
                        url=urls[product],
                        size=row.size,
                        meta={
                            "range": [row.offset, row.end],
                            "product": product,
                            "token": token,
                            "alias": alias,
                            "name": row.name,
                            "level": row.level,
                            "level_value": lvl,
                            "step": row.step,
                            "run": f"{sel.run:%Y-%m-%dT%H:%M:%SZ}",
                            "fxx": sel.fxx,
                            "valid": f"{sel.valid:%Y-%m-%dT%H:%M:%SZ}",
                            "target": f"{sel.target:%Y-%m-%dT%H:%M:%SZ}" if sel.target else "",
                            "selection": sel.describe(),
                            "file_bytes": sizes[product],
                            "idx_bytes": idx_bytes,
                        },
                    )
                )
        # one message can be asked for twice (two tokens naming it): keep the first
        unique = {s.key: s for s in specs}
        return list(unique.values())

    def estimate_notes(self, request: Request, specs: list[FileSpec]) -> list[str]:
        if not specs:
            return []
        m = specs[0].meta
        files = {s.url: s.meta.get("file_bytes") for s in specs}
        whole = sum(int(v) for v in files.values() if v)
        notes = [
            m.get("selection", ""),
            f"Only {len(specs)} message(s) are downloaded (byte ranges of "
            f"{len(files)} file(s) of {whole / 1e6:.0f} MB in total); the index file(s) "
            f"({int(m.get('idx_bytes') or 0) / 1e3:.0f} kB) were read to plan this.",
        ]
        if request.bbox is not None:
            notes.append(
                "The area is cropped after decoding: the download is the same for any area."
            )
        return [n for n in notes if n]

    # -- download --------------------------------------------------------------------------

    def download_file(self, spec, dest, env, *, max_bytes, progress, cancel):  # type: ignore[no-untyped-def]
        a, b = spec.meta["range"]
        if not spec.url:
            raise FetchFailed(f"{spec.name} has no URL")
        env.http.download(
            spec.url,
            dest,
            max_bytes=max_bytes,
            progress=progress,
            cancel=cancel,
            headers={"Range": f"bytes={a}-{b}"},
        )
        got = dest.stat().st_size
        if got != b - a + 1:
            raise FetchFailed(
                f"{spec.name}: expected {b - a + 1} bytes of the range {a}-{b}, got {got}. "
                "The server may not support byte ranges; nothing was kept."
            )
        with open(dest, "rb") as fh:
            if fh.read(4) != b"GRIB":
                raise FetchFailed(f"{spec.name}: the bytes received are not a GRIB2 message.")

    # -- provenance ------------------------------------------------------------------------

    def provenance(self, request: Request, specs: list[FileSpec]) -> dict[str, Any]:
        return self._provenance(request, specs, None).to_dict()

    def _provenance(
        self, request: Request, specs: list[FileSpec], extra: dict[str, Any] | None
    ) -> layer_provenance.LayerProvenance:
        m = specs[0].meta if specs else {}
        product = request.product or "surface"
        names = sorted({f"{s.meta['name']}:{s.meta['level']}" for s in specs})
        params = {
            "product": product,
            "model_run": m.get("run"),
            "forecast_hour": m.get("fxx"),
            "requested_time": m.get("target") or None,
            "time_selection": "nearest hourly valid time; not interpolated in time",
            "bbox": list(request.bbox) if request.bbox else None,
            "messages": len(specs),
            **(extra or {}),
        }
        return layer_provenance.LayerProvenance(
            kind="model",
            source=f"NOAA HRRR {product} fields (hrrr.t{str(m.get('run', ''))[11:13]}z, "
            f"forecast hour {m.get('fxx')}), CONUS",
            title=f"HRRR {product}",
            run_time=m.get("run", ""),
            valid_time=m.get("valid", ""),
            resolution="3 km Lambert conformal grid (1799 x 1059 cells); cropped to the area after "
            "decoding, no regridding",
            method="Messages selected from the GRIB2 file with its .idx index and fetched by HTTP "
            "byte range; decoded with ecCodes; wind components rotated from the grid to east and "
            "north. " + (m.get("selection") or ""),
            parameters=params,
            uncertainty="Model field: an estimate of the atmosphere on a 3 km grid, not an "
            "observation. "
            + (
                "Analysis (forecast hour 0) blends the 1 h forecast with "
                "observations including radar; near a storm the analysis contains the storm. "
                if m.get("fxx") == 0
                else "Forecast: position and timing of convection are uncertain. "
            )
            + "CAPE, helicity and shear are computed by the model's post-processor, not from a "
            "sounding.",
            references=[
                "Dowell, D. C., et al., 2022: The High-Resolution Rapid Refresh (HRRR): An hourly "
                "updating convection-allowing forecast model. Part I: Motivation and system "
                "description. Wea. Forecasting, 37, 1371-1395.",
                "Benjamin, S. G., et al., 2016: A North American hourly assimilation and model "
                "forecast cycle: The Rapid Refresh. Mon. Wea. Rev., 144, 1669-1694.",
            ],
            attribution=self.attribution,
            licence=self.licence,
            inputs=names[:40],
        )

    # -- reading ---------------------------------------------------------------------------

    def _specs_for(self, result: FetchResult, env: Any) -> list[FileSpec]:
        from engine.environment.core import Environment

        env = env or Environment.default()
        specs, _ = self._planned(result.request, env)
        if len(specs) != len(result.paths):
            raise FetchFailed("The cached listing no longer matches the files; fetch again.")
        return specs

    def _decode(self, specs: list[FileSpec], paths: list[Path]) -> list[tuple[FileSpec, Any]]:
        """Decode every message once; the grid geometry is computed from the first only."""
        out = []
        geometry: gribio.Grid | None = None
        for spec, path in zip(specs, paths, strict=True):
            fld = gribio.read_first(path, geometry=geometry is None)
            if geometry is None:
                geometry = fld.grid
            elif fld.values.shape != geometry.shape:
                raise FetchFailed(f"{spec.name} is on a different grid from the first message.")
            else:
                fld.grid = geometry
            out.append((spec, fld))
        return out

    @staticmethod
    def _rotate_winds(items: list[tuple[FileSpec, Any]]) -> list[str]:
        """Rotate grid-relative vector pairs to east/north in place. Returns notes."""
        notes: list[str] = []
        by_key = {(s.meta["name"], s.meta["level"], s.meta["step"]): f for s, f in items}
        done: set[tuple[str, str, str]] = set()
        for un, vn in VECTOR_PAIRS:
            for (name, level, step), fu in list(by_key.items()):
                if name != un or (vn, level, step) not in by_key:
                    continue
                fv = by_key[(vn, level, step)]
                if not fu.keys.get("uvRelativeToGrid"):
                    continue
                k = fu.keys
                if fu.grid.kind != "lambert" or k.get("LoVInDegrees") is None:
                    notes.append(
                        f"{un}/{vn} at {level}: grid-relative, not rotated (unsupported grid)."
                    )
                    continue
                ue, ve = derive.grid_winds_to_earth(
                    fu.values,
                    fv.values,
                    fu.grid.lon,
                    k["LoVInDegrees"],
                    k["Latin1InDegrees"],
                    k["Latin2InDegrees"],
                )
                fu.values, fv.values = ue.astype(np.float32), ve.astype(np.float32)
                done |= {(un, level, step), (vn, level, step)}
        for (name, level, step), f in by_key.items():
            if (
                f.keys.get("uvRelativeToGrid")
                and (name, level, step) not in done
                and any(name in pair for pair in VECTOR_PAIRS)
            ):
                notes.append(
                    f"{name} at {level} is relative to the model grid, not to east and north: its "
                    "partner component was not requested, so it could not be rotated."
                )
        return notes

    def open(self, result: FetchResult, env: Any = None) -> Any:
        import xarray as xr

        specs = self._specs_for(result, env)
        items = self._decode(specs, result.paths)
        notes = self._rotate_winds(items)
        grid = items[0][1].grid
        rows, cols = gribio.window(grid, result.request.bbox)
        cgrid = gribio.crop_grid(grid, rows, cols)
        coords = {
            "latitude": (("y", "x"), cgrid.lat.astype("float32"), {"units": "degrees_north"}),
            "longitude": (("y", "x"), cgrid.lon.astype("float32"), {"units": "degrees_east"}),
        }
        groups: dict[str, list[tuple[FileSpec, Any]]] = {}
        for spec, fld in items:
            groups.setdefault(spec.meta["token"], []).append((spec, fld))
        data_vars: dict[str, Any] = {}
        for token, members in groups.items():
            first = members[0][0].meta
            attrs = {
                "long_name": members[0][1].name,
                "units": gribio.clean_units(members[0][1].units, first["name"]),
                "grib_name": first["name"],
                "step": first["step"],
            }
            wildcard = _token_parts(token, first["alias"])[1] in ("@mb", "@hybrid")
            if wildcard:
                members.sort(key=lambda sm: sm[0].meta["level_value"] or 0.0)
                name = first["alias"] or slug(first["name"])
                arr = np.stack([f.values[rows, cols] for _, f in members])
                lev = np.array([s.meta["level_value"] for s, _ in members], dtype="float32")
                unit = "hPa" if first["level"].endswith(" mb") else "1"
                attrs["level"] = "every " + ("pressure" if unit == "hPa" else "native") + " level"
                data_vars[name] = (("level_" + name, "y", "x"), arr, attrs)
                coords["level_" + name] = (("level_" + name,), lev, {"units": unit})
            else:
                name = first["alias"] or slug(f"{first['name']}_{first['level']}")
                attrs["level"] = first["level"]
                data_vars[name] = (("y", "x"), members[0][1].values[rows, cols], attrs)
        prov = self._provenance(result.request, specs, {"notes": notes} if notes else None)
        ds = xr.Dataset(data_vars, coords=coords)
        ds.attrs.update(layer_provenance.attrs(prov))
        ds.attrs["title"] = "HRRR " + (result.request.product or "surface")
        ds.attrs["grid"] = grid.description
        ds.attrs["time_selection"] = specs[0].meta.get("selection", "")
        return ds

    # -- vertical profile ------------------------------------------------------------------

    def profile_at(
        self, result: FetchResult, latitude: float, longitude: float, env: Any = None
    ) -> Any:
        """The column at the grid cell nearest a point, from a ``profile`` request."""
        from engine import sounding

        specs = self._specs_for(result, env)
        items = self._decode(specs, result.paths)
        self._rotate_winds(items)
        grid = items[0][1].grid
        j, i, dist_km = gribio.nearest_cell(grid, latitude, longitude)
        cell = {(s.meta["name"], s.meta["level"]): float(f.values[j, i]) for s, f in items}
        for key in (("PRES", "surface"), ("HGT", "surface"), ("TMP", "2 m above ground")):
            if key not in cell or not np.isfinite(cell[key]):
                raise InvalidRequest(f"The profile needs {key[0]}:{key[1]}; it is missing.")
        p_sfc = cell[("PRES", "surface")]
        z_sfc = cell[("HGT", "surface")]
        p, zg, t, td, u, v = [p_sfc], [z_sfc], [cell[("TMP", "2 m above ground")]], [], [], []
        td.append(cell.get(("DPT", "2 m above ground"), np.nan))
        u.append(cell.get(("UGRD", "10 m above ground"), np.nan))
        v.append(cell.get(("VGRD", "10 m above ground"), np.nan))
        levels = sorted(
            {
                s.meta["level_value"]
                for s, _ in items
                if s.meta["name"] == "TMP" and s.meta["level_value"]
            },
            reverse=True,
        )
        dropped = 0
        for lv in levels:
            pl = lv * 100.0
            if pl >= p_sfc - 100.0:  # below, at, or within 1 hPa of the model ground
                dropped += 1
                continue
            lab = f"{lv:g} mb"
            vals = [cell.get((name, lab), np.nan) for name in PROFILE_LEVEL_VARS]
            p.append(pl)
            zg.append(vals[0])
            t.append(vals[1])
            td.append(vals[2])
            u.append(vals[3])
            v.append(vals[4])
        built = sounding.build_profile(
            pressure_pa=p,
            geopotential_m=zg,
            temperature_k=t,
            dewpoint_k=td,
            u=u,
            v=v,
            latitude=float(grid.lat[j, i] if grid.lat.ndim == 2 else grid.lat[j]),
            longitude=float(grid.lon[j, i] if grid.lon.ndim == 2 else grid.lon[i]),
            time=specs[0].meta["valid"],
        )
        notes = list(built[1])
        notes.append(
            f"HRRR column at the grid cell {dist_km:.1f} km from the requested point "
            f"({latitude:.3f}, {longitude:.3f}); {dropped} pressure level(s) at or below the model "
            "ground were dropped; the lowest level is the model surface (2 m temperature and dew "
            "point, 10 m wind, surface pressure, terrain height)."
        )
        prov = self._provenance(
            result.request,
            specs,
            {"point": [latitude, longitude], "distance_km": round(dist_km, 2)},
        )
        snd = sounding._finish(
            built,
            station=f"HRRR {specs[0].meta['valid'][:13]}Z",
            station_name="HRRR model column",
            latitude=float(built[0].coords["latitude"]),
            longitude=float(built[0].coords["longitude"]),
            launch_time=specs[0].meta["valid"],
            source="NOAA HRRR " + specs[0].meta["selection"].split(";")[0],
            fmt="hrrr_grib2",
            file="",
            data_class="model",
            prov=prov,
        )
        snd.notes = notes
        return snd


def _product_of(alias: str) -> str:
    _name, _level, _ = ALIASES[alias]
    if _level == "@hybrid":
        return "native"
    return "surface"


PROVIDERS = (HrrrProvider,)

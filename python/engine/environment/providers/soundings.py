"""Observed radiosonde profiles from a public archive (Iowa Environmental Mesonet RAOB service).

One request = one sounding: ``params = {"station": "KJAN"}`` and ``time_start`` = the nominal
launch time (UTC, normally 00 or 12). The reply is a small JSON document (tens of kB); the
framework still asks first, because the user's computer is about to contact another server.

The JSON layout is the one read by ``radarx.io.sounding`` (``profiles[].profile[]`` with ``pres``,
``hght``, ``tmpc``, ``dwpc``, ``sknt``, ``drct``); parsing is
:func:`engine.sounding.parse_iem_json`.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, ClassVar

from engine import layer_provenance
from engine.environment.errors import InvalidRequest
from engine.environment.provider import Provider
from engine.environment.request import FileSpec, InventoryItem, Request

IEM_URL = "https://mesonet.agron.iastate.edu/json/raob.py?ts={ts}&station={station}"
_STATION = re.compile(r"^[A-Za-z0-9]{3,12}$")


def station_id(station: str) -> str:
    """The identifier the IEM service uses (ICAO-style, e.g. ``KJAN``); WMO/IGRA ids are
    translated with radarx's station table when radarx is installed."""
    if not _STATION.match(station or ""):
        raise InvalidRequest("station must be 3 to 12 letters or digits, e.g. KJAN or 72235")
    try:
        from radarx.io import sounding as rs

        return str(rs._station_id(station, "iem"))
    except Exception:
        return station.upper()


class IemRaobProvider(Provider):
    id: ClassVar[str] = "iem_raob"
    name: ClassVar[str] = "Radiosonde archive (Iowa Environmental Mesonet)"
    kind: ClassVar[str] = "observed"
    attribution: ClassVar[str] = (
        "Radiosonde observations of the NWS upper-air network, served by the Iowa Environmental "
        "Mesonet, Iowa State University."
    )
    licence: ClassVar[str] = (
        "Public archive; terms of use on https://mesonet.agron.iastate.edu/disclaimer.php "
        "(not reproduced here). Credit the Iowa Environmental Mesonet."
    )
    homepage: ClassVar[str] = "https://mesonet.agron.iastate.edu/archive/raob/"
    description: ClassVar[str] = "One observed sounding (mandatory and significant levels)."
    unknown_size_cap: ClassVar[int] = 2 * 1024 * 1024

    def inventory(self, query: dict[str, Any] | None = None) -> list[InventoryItem]:
        return [
            InventoryItem(
                product="sounding",
                title="Observed sounding",
                description="Pressure, height, temperature, dew point and wind at the mandatory "
                "and significant levels of one launch.",
                variables=("pressure", "height", "temperature", "dewpoint", "wind"),
                resolution="mandatory + significant levels",
                coverage="NWS upper-air stations",
                cadence="00 and 12 UTC (some stations also 06/18 UTC or special launches)",
            )
        ]

    @staticmethod
    def launch(request: Request) -> datetime:
        if not request.time_start:
            raise InvalidRequest(
                "Give the nominal launch time (time_start, UTC, e.g. 00 or 12 UTC)."
            )
        return datetime.strptime(request.time_start, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )

    def plan(self, request: Request, env: Any) -> list[FileSpec]:
        station = str(request.params.get("station", ""))
        sid = station_id(station)
        t = self.launch(request)
        ts = t.strftime("%Y%m%d%H%M")
        return [
            FileSpec(
                key=f"{sid}/{ts}",
                name=f"iem_{sid}_{ts}.json",
                url=IEM_URL.format(ts=ts, station=sid),
                size=None,
                meta={"station": sid, "launch": request.time_start},
            )
        ]

    def provenance(self, request: Request, specs: list[FileSpec]) -> dict[str, Any]:
        sid = specs[0].meta.get("station", "") if specs else ""
        return layer_provenance.sounding(
            station=sid,
            launch_time=request.time_start,
            source="Iowa Environmental Mesonet RAOB archive",
            method="Radiosonde profile from the IEM RAOB archive; geopotential heights converted "
            "to geometric metres.",
            attribution=self.attribution,
            licence=self.licence,
        ).to_dict()

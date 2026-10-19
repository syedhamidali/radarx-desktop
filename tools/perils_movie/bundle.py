"""Bundle layout, manifest and the list of sources (SPEC v1 plus the additions of docs/PERILS_BUNDLE.md)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import hailconv
import numpy as np
import pgrid
import volumes

SPEC_VERSION = "1.1"  # SPEC v1 plus additive fields (never removes or renames a v1 field)

SOURCES: list[dict[str, str]] = [
    {
        "name": "Radar grids (COW1, DOW7, DOW8, KBMX, KGWX, NOXP, SMART-R 2, UAH MAX)",
        "class": "observed",
        "instrument": "Doppler radars: WSR-88D S band (KGWX, KBMX), mobile C band (COW1, SMART-R 2), mobile X band (DOW7, DOW8, NOXP, UAH MAX); Py-ART grids on a common 500 m grid",
        "credit": "PERiLS 2022 radars: NOAA NWS (WSR-88D), CSWR (COW, DOW), NSSL (NOXP, SMART-R), UAH (MAX); gridded by the data owner",
        "licence_note": "PERiLS data: check the EOL data policy (NCAR/EOL) before redistribution; the bundle is derived data for the maintainer",
        "what": "reflectivity, radial velocity, ZDR, RHO, KDP merged into one volume per step (merge method in docs/PERILS_BUNDLE.md)",
    },
    {
        "name": "Rotation (azimuthal shear, rotational velocity, vortex candidates, debris flag)",
        "class": "retrieved",
        "instrument": "from the Doppler velocity of the radar grids (LLSD-style least squares, Smith and Elmore 2004)",
        "credit": "radarx-desktop, tools/perils_movie/rotation.py",
        "licence_note": "derived by this project",
        "what": "rotation (3-D), rotation_ll, vrot_ll, tds, context/rotation_tracks.json; evidence only",
    },
    {
        "name": "Hydrometeor class, hail indices, convection diagnostics",
        "class": "retrieved",
        "instrument": "radarx fuzzy-logic HID (Park 2009 / Dolan 2013 / Dolan and Rutledge 2009), SHI/MESH/POSH (Witt 1998), VIL, echo tops, ZDR column (Snyder 2015), Steiner-Houze-Yuter 1995 convective / stratiform, BWER candidates",
        "credit": "radarx; tools/perils_movie/hailconv.py",
        "licence_note": "derived by this project; ZDR is bias-corrected per volume from rain (estimate)",
        "what": "hid (3-D), shi, mesh_mm, posh_pct, vil, echotop*_km, zdr_column_km, conv_strat, bwer, cmax",
    },
    {
        "name": "Multi-Doppler winds",
        "class": "retrieved",
        "instrument": "radarx.retrieve.multi_doppler (variational, Gao et al. 1999 structure) from the gridded velocities of 2 to 5 radars",
        "credit": "radarx; tools/perils_movie/winds.py",
        "licence_note": "derived by this project",
        "what": "winds/step_NNNN.npz u, v, w, vorticity, divergence, storm-relative wind, error and crossing-angle mask",
    },
    {
        "name": "Lightning Mapping Array sources and flashes",
        "class": "observed",
        "instrument": "NASA/NSSL-style LMA: NALMA (14 stations, north Alabama) + NSSL deployable LMA (8 stations, Mississippi); flashes grouped by radarx cluster_flashes (Fuchs et al. 2016)",
        "credit": "Chmielewski, V., et al. 2022: PERiLS_2022: NSSL Deployable Lightning Mapping Array Data v1.1, UCAR/NCAR EOL, doi:10.26023/R0K6-AXR5-YZ00; NALMA (NASA MSFC / UAH)",
        "licence_note": "NCAR/EOL data policy: acknowledge NCAR/EOL and NSF",
        "what": "lma/flashes.json",
    },
    {
        "name": "PIPS disdrometer stations",
        "class": "observed",
        "instrument": "Probe for Interactive Precipitation Studies (Parsivel2 + surface weather), 4 units",
        "credit": "Purdue University / Penn State (PERiLS 2022); QC and DSD retrievals by the data owner",
        "licence_note": "owner's data; cite the owner",
        "what": "stations/stations.json series and stations/<id>_dsd.npz",
    },
    {
        "name": "TTU StickNet",
        "class": "observed",
        "instrument": "24 Texas Tech StickNet surface probes (T, RH, P, wind)",
        "credit": "Weiss, C., and McDonald, J. 2022: PERiLS_2022: TTU StickNet Data v1.0, UCAR/NCAR EOL, doi:10.26023/93M9-AE8F-SX07",
        "licence_note": "NCAR/EOL data policy",
        "what": "stations/stations.json",
    },
    {
        "name": "UAH MAPNet RaDAPS: surface, MRR and 915 MHz profiler",
        "class": "observed",
        "instrument": "Vaisala WXT536 surface station with hail sensor; Micro Rain Radar 24 GHz (IMProToo); 915 MHz radar wind profiler",
        "credit": "UAH / NSSTC MAPNet (PERiLS 2022, NCAR/EOL 610.017, 610.018, 610.015)",
        "licence_note": "NCAR/EOL data policy",
        "what": "stations/stations.json, profiles/mrr_RaDAPS.npz, profiles/rwp_RaDAPS.npz",
    },
    {
        "name": "UIUC FARM mobile mesonets and Parsivels",
        "class": "observed",
        "instrument": "vehicle mesonets (DOW7, DOW8, SCOUT1-3) and Parsivel disdrometers on the scouts",
        "credit": "University of Illinois Urbana-Champaign, FARM facility (PERiLS 2022)",
        "licence_note": "owner's data; cite the owner",
        "what": "stations/stations.json",
    },
    {
        "name": "Radiosondes (UIUC FARM, UAH, ULM, NWS)",
        "class": "observed",
        "instrument": "GRAW DFM-09 (UIUC), iMet (UAH), ULM sondes, operational radiosondes",
        "credit": "UIUC FARM, UAH, ULM (PERiLS 2022) and NWS",
        "licence_note": "owners' data",
        "what": "soundings/index.json, soundings/*.csv with engine/sounding.py diagnostics",
    },
    {
        "name": "MRMS merged base reflectivity and azimuthal shear",
        "class": "observed",
        "instrument": "NOAA/NSSL Multi-Radar Multi-Sensor mosaics (GRIB2 read with wgrib2); AzShear 0-2 km is a derived MRMS product",
        "credit": "NOAA National Severe Storms Laboratory MRMS (public domain, US government)",
        "licence_note": "NOAA data are public",
        "what": "context/mrms_NNNN.npz",
    },
    {
        "name": "Tornado damage survey",
        "class": "observed",
        "instrument": "NWS damage survey (begin / end point and time, EF rating); path between them interpolated",
        "credit": "NOAA NWS / NCEI Storm Events Database, SPC tornado database",
        "licence_note": "public (US government)",
        "what": "tornadoes/tornadoes.json",
    },
    {
        "name": "Surface objective analysis",
        "class": "analysis",
        "instrument": "Barnes analysis of the station observations (not a model)",
        "credit": "tools/perils_movie/context.py",
        "licence_note": "derived by this project",
        "what": "context/surface_NNNN.npz",
    },
]


def write_json(path: Path, obj: Any, indent: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=indent, default=_default, allow_nan=False))
    tmp.replace(path)


def _default(o: Any):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        f = float(o)
        return None if f != f else f
    if isinstance(o, np.ndarray):
        return [None if (isinstance(x, float) and x != x) else x for x in o.tolist()]
    if isinstance(o, (datetime,)):
        return o.isoformat()
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"not serialisable: {type(o)}")


def clean_nan(obj: Any) -> Any:
    """Replace NaN / inf floats by None recursively (JSON has no NaN)."""
    if isinstance(obj, float):
        return obj if np.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean_nan(v) for v in obj]
    if isinstance(obj, np.generic):
        return clean_nan(obj.item())
    return obj


def grid_block(origin_lat: float, origin_lon: float) -> dict[str, Any]:
    return {
        "origin_lat": origin_lat,
        "origin_lon": origin_lon,
        "x_km": [round(float(v), 3) for v in volumes.X_KM],
        "y_km": [round(float(v), 3) for v in volumes.Y_KM],
        "z_km": [round(float(v), 3) for v in volumes.Z_M / 1000.0],
        "z_reference": "height above mean sea level (each radar grid was moved from above its own site altitude to above sea level)",
        "projection": "azimuthal equidistant on a sphere of radius 6370997 m centred on (origin_lat, origin_lon) (Py-ART 'pyart_aeqd')",
        "x_axis": "east",
        "y_axis": "north",
    }


def hid_legend() -> dict[str, Any]:
    return {
        "codes": {str(k): {"abbr": a, "name": n} for k, (a, n) in hailconv.HID_LEGEND.items()},
        "methods": hailconv.HID_METHOD,
        "note": "X band has no hail class (high-density graupel is its heaviest ice); S band class RH is a rain-hail mixture",
    }


def radar_list(site_info: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for name in sorted(site_info):
        i = pgrid.RADAR_INFO.get(name, {})
        s = site_info[name]
        out.append(
            {
                "name": name,
                "lat": s["lat"],
                "lon": s["lon"],
                "alt_m": s["alt_m"],
                "type": i.get("type", "radar"),
                "band": i.get("band"),
                "description": i.get("name", name),
                "x_km": s.get("x_km"),
                "y_km": s.get("y_km"),
            }
        )
    return out

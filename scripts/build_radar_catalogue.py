"""Rebuild src/lib/data/radarCatalogue.json: weather radar sites worldwide, with a source,
licence and retrieval date per entry and what the app can do with each site's data.

    python scripts/build_radar_catalogue.py --date 2026-10-22 --cache-dir /tmp/radar-sources
    python scripts/build_radar_catalogue.py --date 2026-10-22 --cache-dir ... --offline  # reuse

Sources (every one fetched and checked by hand before it was added here):

* NOAA NCEI HOMR NEXRAD station list: the bundled ``nexradSites.json`` (no download here; see
  ``build_radar_sites.py``). Public domain (US government work).
* EUMETNET OPERA radar database (``OPERA_RADARS_DB.json``): positions, WMO/WIGOS ids, ODIM codes,
  band, polarisation, Doppler, status of the European radars. The OPERA page calls the database
  "public information, location and details of the radars"; the OPERA *data* are CC BY 4.0 via
  MeteoGate, the metadata table itself carries no separate licence text (recorded as such).
* WMO OSCAR/Surface (WIGOS station metadata), station class "weather radar": positions, WIGOS ids,
  territory and declared status of radars worldwide. Public WMO metadata repository; its terms of
  use page could not be reached when this was built (recorded as such, attribution given).
* DWD Open Data: the list of radar sites that publish volume sweeps on opendata.dwd.de (directory
  index) and, with ``--verify-dwd``, the ``where`` attributes of one ODIM sweep file per site to
  cross-check the OPERA positions (the file is read and deleted).
* FMI open data on AWS: the list of radars in the public ``fmi-opendata-radar-volume-hdf5`` bucket
  (one S3 listing of the sample day), CC BY 4.0.
* TDWR (FAA Terminal Doppler Weather Radars): the same HOMR station file as NEXRAD carries them
  (station type ``TDWR``); the Unidata real-time Level II bucket is listed once (folder names of one
  full UTC day) to say which of them wrote data that day.
* Australia: the radar site list ``radar_site_list.csv`` of the Bureau of Meteorology / NCI
  "Australian Operational Weather Radar" archive (NCI project rq0), published beside the archive
  with a CC BY 4.0 licence file; merged with the OSCAR rows (same radar within 3 km). The Bureau's
  website pages are NOT a source here: their terms allow personal or in-organisation use only, so
  only link addresses (not text, names or positions) were read off them.

Only sites whose position comes from one of these sources are written. Nothing is invented; a
site without a position is dropped and counted in the report. ``data_access`` says what the app can
do: ``open-loadable`` (a provider in the engine opens volumes anonymously), ``open-link`` (open data
exist but the app does not load them: a URL is given), ``key-required`` (free key or registration),
``restricted`` (no open volume data), ``unknown``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "lib" / "data" / "radarCatalogue.json"
NEXRAD = ROOT / "src" / "lib" / "data" / "nexradSites.json"
UA = "radarx-desktop-catalogue-build (+https://radarx.labshope.com)"

OPERA_URL = (
    "https://www.eumetnet.eu/wp-content/themes/aeron-child/observations-programme/"
    "current-activities/opera/database/OPERA_Database/OPERA_RADARS_DB.json"
)
OSCAR_URL = "https://oscar.wmo.int/surface/rest/api/search/station?stationClass=weatherRadar"
DWD_INDEX_URL = "https://opendata.dwd.de/weather/radar/sites/sweep_vol_z/"
DWD_SWEEP_DIR = "https://opendata.dwd.de/weather/radar/sites/sweep_vol_z/{site}/hdf5/filter_simple/"
FMI_LIST_URL = (
    "https://fmi-opendata-radar-volume-hdf5.s3-eu-west-1.amazonaws.com/"
    "?list-type=2&prefix=/2023/02/07/&max-keys=1000"
)

AURA_BASE = "https://thredds.nci.org.au/thredds/"
AURA_LIST_URL = AURA_BASE + "fileServer/rq0/radar_site_list.csv"
AURA_ROOT_URL = AURA_BASE + "catalog/rq0/catalog.html"
UNIDATA_L2 = "https://unidata-nexrad-level2.s3.amazonaws.com/"
HOMR_URL = "https://www.ncei.noaa.gov/access/homr/file/nexrad-stations.txt"
TDWR_PAGE = "https://www.ncei.noaa.gov/products/radar/terminal-doppler-weather-radar"

SOURCES = {
    "homr": {
        "name": "NOAA NCEI HOMR NEXRAD station list",
        "url": "https://www.ncei.noaa.gov/access/homr/file/nexrad-stations.txt",
        "licence": "Public domain (US government work, 17 U.S.C. 105); NOAA asks for credit",
        "attribution": "NOAA National Centers for Environmental Information, Historical Observing "
        "Metadata Repository",
    },
    "opera": {
        "name": "EUMETNET OPERA radar database",
        "url": OPERA_URL,
        "licence": "Public information (OPERA programme page); no separate licence text on the "
        "metadata table. OPERA radar data are CC BY 4.0 via MeteoGate.",
        "attribution": "EUMETNET OPERA radar database (Operational Programme for the Exchange of "
        "Weather Radar Information)",
    },
    "aura": {
        "name": "Bureau of Meteorology / NCI Australian operational weather radar archive: radar "
        "site list (radar_site_list.csv)",
        "url": AURA_LIST_URL,
        "licence": "CC BY 4.0 (licence file rq0_level1_license-cc4.pdf in the archive's top "
        "folder, which holds the Level 1 radar volumes this list indexes; the NCI collection "
        "record says licences vary by dataset)",
        "attribution": "Bureau of Meteorology and NCI Australia: Soderholm J., A. Protat, "
        "C. Jakob, 2019, Australian Operational Weather Radar Dataset, "
        "doi:10.25914/5cb686a8d9450",
    },
    "oscar": {
        "name": "WMO OSCAR/Surface (WIGOS station metadata), station class weather radar",
        "url": OSCAR_URL,
        "licence": "WMO public metadata repository; terms-of-use page not reachable at build time "
        "(recorded, attribution given)",
        "attribution": "World Meteorological Organization, OSCAR/Surface",
    },
}

#: Where a network's data can be found when the app cannot load it. Verified by fetching the URL
#: from the build machine on the date in the catalogue header (what was seen is in `note`).
NETWORK_ACCESS = {
    "Netherlands": {
        "data_access": "key-required",
        "url": "https://dataplatform.knmi.nl/dataset/radar-volume-full-herwijnen-1-0",
        "note": "KNMI Data Platform: radar volumes (ODIM HDF5) need a free API key; the API "
        "answers 401 without one.",
    },
    "United Kingdom": {
        "data_access": "open-link",
        "url": "https://registry.opendata.aws/met-office-uk-radar-observations/",
        "note": "Met Office UK radar observations on AWS Open Data (composite products, not "
        "single-site volumes), British Crown copyright, CC BY-SA 4.0.",
    },
    "Norway": {
        "data_access": "open-link",
        "url": "https://thredds.met.no/thredds/catalog/remotesensing/reflectivity-nordic/catalog.html",
        "note": "MET Norway THREDDS: Nordic reflectivity composites (not single-site volumes).",
    },
    "Canada": {
        "data_access": "open-link",
        "url": "https://eccc-msc.github.io/open-data/msc-data/obs_radar/readme_radar_en/",
        "note": "MSC Datamart and GeoMet publish radar products (CAPPI, DPQPE, composites as "
        "GIF/GeoTIFF/WMS), not volume scans; Open Government Licence - Canada.",
    },
    "Colombia": {
        "data_access": "open-link",
        "url": "https://registry.opendata.aws/ideam-radares/",
        "note": "IDEAM radar level II files (IRIS/Sigmet RAW) in the public AWS bucket "
        "s3-radaresideam, CC BY 4.0; not yet loaded by the app.",
    },
}
DWD_NOTE = (
    "DWD Open Data: volume sweeps (ODIM HDF5, one file per sweep and moment) for the last "
    "two days; free of charge without registration. DWD open spatial data are CC BY 4.0 with "
    "the source acknowledged (legal notice at "
    "https://www.dwd.de/EN/service/legal_notice/legal_notice_node.html)."
)
FMI_NOTE = (
    "FMI open radar data on AWS: a two-day sample (7-8 February 2023) of full polar volumes "
    "(ODIM HDF5), CC BY 4.0; there is no live feed in that bucket."
)
TDWR_NOTE = (
    "FAA Terminal Doppler Weather Radar at an airport. NOAA NCEI archives its Level II volumes "
    "and gives them away free of charge (the link opens the NCEI TDWR page). TDWR Level II files "
    "open with File > Open (tested with one Denver file); the app does not download them for "
    "you yet."
)
# HOMR truncates names at 30 characters; only the wording changes.
TDWR_NAME_TIDY = {"TJRV": "Jose Aponte De La Torre Airport"}
KEEP_UPPER = {"AFB", "JFK", "WFO"}

AURA_NOTE = (
    "Volume data: NCI's Australian radar archive (ODIM HDF5, one zip per UTC day), folder "
    "{folders}. {this_site} The app does not load these volumes yet."
)
AURA_CHECKED = (
    "Access without an account was verified on {date} (folder listing, then one volume read "
    "by byte range)."
)
AURA_UNCHECKED = "Volume data: access not verified for this site (only radar 2 was tested)."
SAWS_URL = "https://www.weathersa.co.za/"
SAWS_NOTE = (
    "South African Weather Service. No data access was verified for this radar. The link opens "
    "the SAWS home page: the address of its radar page could not be checked from the build "
    "machine (the site's certificate chain could not be verified)."
)
BOM_INFO = "https://www.bom.gov.au/australia/radar/info/"
BOM_RADAR_HOME = "https://www.bom.gov.au/australia/radar/"
#: The BoM radar information page and anchor of each radar, read off the Bureau's own radar pages
#: on 2026-10-22 (the numbers are the BoM radar ids, equal to the archive's folder numbers). Only
#: these link addresses were taken from the Bureau's website; its text is not copied.
BOM_ANCHORS = {
    2: "vic_info.shtml#melbourne02", 3: "nsw_info.shtml#appin03",
    4: "nsw_info.shtml#newcastle04", 5: "wa_info.shtml#carnarvon05",
    6: "wa_info.shtml#geraldton06", 7: "wa_info.shtml#wyndham07",
    8: "qld_info.shtml#mt-kanigan08", 14: "sa_info.shtml#mt-gambier14",
    15: "wa_info.shtml#dampier15", 16: "wa_info.shtml#pt-hedland16",
    17: "wa_info.shtml#broome17", 19: "qld_info.shtml#cairns19", 22: "qld_info.shtml#mackay22",
    23: "qld_info.shtml#gladstone23", 24: "qld_info.shtml#bowen24",
    25: "nt_info.shtml#alice-springs25", 26: "wa_info.shtml#perth-airport26",
    27: "sa_info.shtml#woomera27", 28: "nsw_info.shtml#grafton28",
    29: "wa_info.shtml#learmonth29", 31: "wa_info.shtml#albany31",
    32: "wa_info.shtml#esperance32", 33: "sa_info.shtml#ceduna33",
    36: "qld_info.shtml#mornington-is36", 37: "tas_info.shtml#hobart-airport37",
    38: "wa_info.shtml#newdegate38", 39: "wa_info.shtml#halls-creek39",
    40: "nsw_info.shtml#captains-flat40", 41: "qld_info.shtml#willis-island41",
    42: "nt_info.shtml#tindal42", 44: "wa_info.shtml#giles44",
    46: "sa_info.shtml#sellicks-hill46", 48: "wa_info.shtml#kalgoorlie48",
    49: "vic_info.shtml#yarrawonga49", 50: "qld_info.shtml#marburg50",
    52: "tas_info.shtml#west-takone52", 53: "nsw_info.shtml#moree53",
    55: "nsw_info.shtml#wagga-wagga55", 56: "qld_info.shtml#longreach56",
    58: "wa_info.shtml#south-doodlakine58", 62: "nsw_info.shtml#norfolk-island62",
    63: "nt_info.shtml#berrimah63", 64: "sa_info.shtml#buckland-park64",
    66: "qld_info.shtml#mt-stapylton66", 67: "qld_info.shtml#warrego67",
    68: "vic_info.shtml#bairnsdale68", 69: "nsw_info.shtml#blackjack-mountain69",
    70: "wa_info.shtml#serpentine70", 71: "nsw_info.shtml#terrey-hills71",
    72: "qld_info.shtml#emerald72", 73: "qld_info.shtml#hervey-range73",
    74: "qld_info.shtml#greenvale74", 75: "qld_info.shtml#mount-isa75",
    76: "tas_info.shtml#mt-koonya76", 77: "nt_info.shtml#warruwi77",
    78: "qld_info.shtml#weipa78", 79: "wa_info.shtml#watheroo79",
    93: "nsw_info.shtml#brewarrina93", 94: "nsw_info.shtml#hillston94",
    95: "vic_info.shtml#rainbow95", 96: "nsw_info.shtml#yeoval96",
    97: "vic_info.shtml#mildura97", 98: "qld_info.shtml#taroom98",
    105: "qld_info.shtml#brisbane-airport105", 107: "qld_info.shtml#richmond107",
    111: "wa_info.shtml#karratha111", 112: "nt_info.shtml#gove112",
    114: "wa_info.shtml#carnarvon114",
}  # fmt: skip
#: Spelling and wording fixes of names in the archive's site list (position and ids unchanged):
#: abbreviations spelled out, "Learmouth"/"Esperence" typos, a radar model used as a place name.
AU_NAME_FIX = {
    10: "Darwin Airport", 11: "Adelaide Airport", 20: "Townsville Airport",
    21: "Townsville (Mount Stuart)", 26: "Perth Airport", 29: "Learmonth", 32: "Esperance",
    34: "Cairns Airport", 43: "Brisbane Airport", 105: "Brisbane Airport", 47: "Rockhampton",
    62: "Norfolk Island", 51: "Melbourne Airport",
}  # fmt: skip
#: OSCAR rows that are the same radar as a list entry although OSCAR's position is farther than
#: AU_MATCH_KM away (the list's own position is used): OSCAR "Radar Townsville Hervey Range" is
#: 18.6 km from the list's Townsville (Hervey Range); the names agree and no other list entry is
#: near, so it is treated as that radar and OSCAR's position is not used. OSCAR "Wyndham"
#: (0-20000-0-94214, 4.2 km from the list's Wyndham) carries the WIGOS id the list itself gives
#: that radar.
AU_OSCAR_OVERRIDE = {"0-36-9-33321": 73, "0-20000-0-94214": 7}
AU_MATCH_KM = 3.0
AU_CLUSTER_KM = 1.5


def fetch(url: str, cache_dir: Path, name: str, *, offline: bool, limit: int = 4_000_000) -> bytes:
    path = cache_dir / name
    if path.exists():
        return path.read_bytes()
    if offline:
        sys.exit(f"offline and {name} is not in {cache_dir}")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        sys.exit(f"{url}: more than {limit} bytes, refusing")
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def num(v: object) -> float | None:
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p = math.pi / 180
    a = (
        math.sin((lat2 - lat1) * p / 2) ** 2
        + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    )
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def nexrad_sites() -> list[dict]:
    doc = json.loads(NEXRAD.read_text(encoding="utf-8"))
    out = []
    for s in doc["sites"]:
        out.append(
            {
                "id": s["id"],
                "name": s["name"],
                "country": "United States of America",
                "region": s.get("state", ""),
                "network": "NEXRAD",
                "lat": s["lat"],
                "lon": s["lon"],
                "elev_m": s.get("elev_m"),
                "band": "S",
                "polarisation": "dual",
                "status": "operational",
                "source": "homr",
                "retrieved": doc["retrieved"],
                "data_access": "open-loadable",
                "provider": "nexrad",
                "provider_site": s["id"],
            }
        )
    return out


def homr_tdwr_rows(text: str) -> list[dict]:
    """The TDWR rows of the HOMR station file (fixed-width columns taken from the dashed ruler)."""
    lines = text.splitlines()
    cols = [(m.start(), m.end()) for m in re.finditer(r"-+", lines[1])]
    out = []
    for line in lines[2:]:
        f = [line[a:b].strip() for a, b in cols]
        if len(f) < 12 or f[11] != "TDWR":
            continue
        out.append(
            {
                "id": f[1],
                "name": f[3],
                "state": f[5],
                "lat": float(f[7]),
                "lon": float(f[8]),
                "elev_ft": int(f[9]),
            }
        )
    return out


def pretty(name: str) -> str:
    return " ".join(w if w in KEEP_UPPER else w.capitalize() for w in name.split())


def tdwr_sites(text: str, date: str, day: str, active: set[str]) -> list[dict]:
    """FAA TDWR sites from HOMR. ``active`` = ids that have a folder in the Unidata real-time
    bucket for UTC day ``day``; those are marked operational, the others ``unknown`` (HOMR's
    station file carries no status)."""
    out = []
    for r in homr_tdwr_rows(text):
        seen = r["id"] in active
        out.append(
            {
                "id": r["id"],
                "name": TDWR_NAME_TIDY.get(r["id"], pretty(r["name"])),
                "country": "United States of America",
                "region": r["state"],
                "network": "TDWR",
                "lat": round(r["lat"], 5),
                "lon": round(r["lon"], 5),
                "elev_m": round(r["elev_ft"] * 0.3048),
                "band": "",
                "polarisation": "",
                "status": "operational" if seen else "unknown",
                "source": "homr",
                "retrieved": date,
                "data_access": "open-link",
                "url": TDWR_PAGE,
                "note": TDWR_NOTE
                + (
                    f" Wrote Level II volumes to the Unidata real-time bucket on {day} (UTC)."
                    if seen
                    else f" No folder in the Unidata real-time bucket on {day} (UTC); HOMR gives "
                    "no status."
                ),
            }
        )
    return out


def _d(s: str):
    from datetime import datetime

    try:
        return datetime.strptime(s.strip(), "%d/%m/%Y")
    except ValueError:
        return None


def aura_entries(csv_text: str) -> dict[int, dict]:
    """Last (newest) row of every radar id of the archive's site list."""
    import csv
    import io

    last: dict[int, dict] = {}
    for r in csv.DictReader(io.StringIO(csv_text)):
        lat, lon = num(r["site_lat"]), num(r["site_lon"])
        start = _d(r["postchange_start"])
        if lat is None or lon is None or start is None or not r["id"].strip().isdigit():
            continue
        r = dict(r, _id=int(r["id"]), _lat=lat, _lon=lon, _start=start, _end=_d(r["prechange_end"]))
        if r["_id"] not in last or start > last[r["_id"]]["_start"]:
            last[r["_id"]] = r
    return last


def australia_sites(
    csv_text: str, root_html: str, date: str, oscar_au: list[dict]
) -> tuple[list[dict], list[dict], list[str]]:
    """Rows for the Australian radars from the BoM/NCI archive's site list (one per physical
    site: the newest radar), the OSCAR rows that are not the same radar as any of them (kept, with
    a note) and a report. OSCAR contributes the WIGOS id of the matching station."""
    entries = aura_entries(csv_text)
    folders = {int(m) for m in re.findall(r'href="(\d+)/catalog\.html"', root_html)}
    order = sorted(
        entries.values(), key=lambda e: (e["_end"] is not None, -e["_start"].toordinal())
    )
    clusters: list[list[dict]] = []
    for e in order:
        for c in clusters:
            if km(e["_lat"], e["_lon"], c[0]["_lat"], c[0]["_lon"]) < AU_CLUSTER_KM:
                c.append(e)
                break
        else:
            clusters.append([e])

    matched: dict[int, list[tuple[float, dict]]] = {}
    leftover = []
    for o in oscar_au:
        best = min(entries.values(), key=lambda e: km(o["lat"], o["lon"], e["_lat"], e["_lon"]))
        d = km(o["lat"], o["lon"], best["_lat"], best["_lon"])
        target = best["_id"] if d < AU_MATCH_KM else AU_OSCAR_OVERRIDE.get(o["id"])
        if target is None:
            leftover.append(o)
            continue
        ci = next(i for i, c in enumerate(clusters) if any(e["_id"] == target for e in c))
        matched.setdefault(ci, []).append(
            (km(o["lat"], o["lon"], entries[target]["_lat"], entries[target]["_lon"]), o)
        )

    sites = []
    wigos_conflicts = 0
    for ci, c in enumerate(clusters):
        rep = c[0]
        ids = sorted(e["_id"] for e in c)
        current = rep["_end"] is None
        status = "operational" if current else "closed"
        if current and "not operational" in rep["notes"].lower():
            status = "not operational"
        name = next((AU_NAME_FIX[i] for i in [rep["_id"], *ids] if i in AU_NAME_FIX), None)
        name = name or rep["location"].strip()
        anchor_id = next((i for i in [rep["_id"], *ids] if i in BOM_ANCHORS), None)
        url = BOM_INFO + BOM_ANCHORS[anchor_id] if anchor_id else BOM_RADAR_HOME
        dop = rep["doppler"].strip().lower() == "yes"
        dp = rep["dp"].strip().lower() == "yes"
        since = rep["_start"].strftime("%Y-%m-%d")
        until = f", retired {rep['_end'].strftime('%Y-%m-%d')}" if rep["_end"] else ""
        oscar_hits = sorted(matched.get(ci, []), key=lambda t: t[0])
        csv_wigos = {e["WIGOS"].strip() for e in c if e["WIGOS"].strip()}
        wigos = oscar_hits[0][1]["wigos"] if oscar_hits else ""
        if csv_wigos and wigos not in csv_wigos:
            wigos_conflicts += 1
        folder_txt = ", ".join(f"rq0/{i}" for i in ids if i in folders) or "not found in the index"
        this_site = AURA_CHECKED.format(date=date) if 2 in ids else AURA_UNCHECKED
        if not any(i in folders for i in ids):
            this_site = "No folder for this site is listed in the archive index."
        note = (
            f"Radar {rep['radar_type'].strip()}, in service {since}{until}; Doppler "
            f"{'yes' if dop else 'not flagged'}, dual-polarisation "
            f"{'yes' if dp else 'not flagged'} (the archive's site list). "
            + AURA_NOTE.format(folders=folder_txt, this_site=this_site)
        )
        if rep["status"].strip() != "OK":
            note += f" The list marks this position '{rep['status'].strip()}' (to be confirmed)."
        if rep["notes"].strip():
            note += f" List note: {rep['notes'].strip()}."
        for dist, o in oscar_hits:
            note += (
                f" OSCAR lists it as '{o['name']}' ({o['wigos']}), declared {o['status']}, "
                f"{dist:.1f} km from this position."
            )
        sites.append(
            {
                "id": f"au-{rep['_id']}",
                "name": name,
                "country": "Australia",
                "region": "",
                "network": "BoM",
                "lat": round(rep["_lat"], 5),
                "lon": round(rep["_lon"], 5),
                "elev_m": num(rep["site_alt"]),
                "band": rep["band"].strip(),
                "polarisation": "dual" if dp else "",
                "status": status,
                "source": "aura",
                "retrieved": date,
                "wigos": wigos,
                "data_access": "open-link",
                "url": url,
                "note": note,
            }
        )
    for o in leftover:
        o.update(
            {
                "data_access": "unknown",
                "note": "In OSCAR's weather radar list but not in the Australian archive's radar "
                "list: the position and status are OSCAR's only and could not be confirmed.",
            }
        )
    report = [
        f"Australia: {len(entries)} archive radar ids -> {len(sites)} sites "
        f"({sum(s['status'] == 'operational' for s in sites)} operational); "
        f"{sum(len(v) for v in matched.values())} of {len(oscar_au)} OSCAR rows merged, "
        f"{len(leftover)} kept as OSCAR-only: {[o['id'] + ' ' + o['name'] for o in leftover]}; "
        f"list WIGOS ids disagreeing with OSCAR: {wigos_conflicts}"
    ]
    return sites, leftover, report


def south_africa_tidy(rows: list[dict]) -> None:
    """OSCAR names South African radars "Radar <place>": drop the prefix; add the SAWS link."""
    for r in rows:
        if r["country"] != "South Africa":
            continue
        r["name"] = re.sub(r"^Radar\s+", "", r["name"])
        r["url"] = SAWS_URL
        r["note"] = SAWS_NOTE


def opera_sites(raw: bytes, date: str, dwd_sites: set[str], fmi_sites: set[str]) -> list[dict]:
    rows = json.loads(raw)
    out = []
    for r in rows:
        lat, lon = num(r.get("latitude")), num(r.get("longitude"))
        if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            continue
        code = str(r.get("odimcode") or "").strip().lower()
        country = str(r.get("country") or "").strip()
        sid = code or f"opera-{r.get('number')}"
        pol = {"D": "dual", "S": "single"}.get(str(r.get("polarization") or "").strip(), "")
        site = {
            "id": sid,
            "name": str(r.get("location") or sid).strip(),
            "country": country,
            "region": "",
            "network": f"OPERA/{country}",
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "elev_m": num(r.get("heightofstation")),
            "band": str(r.get("band") or "").strip(),
            "polarisation": pol,
            "status": "operational" if str(r.get("status")) == "1" else "not operational",
            "source": "opera",
            "retrieved": date,
            "wmo": str(r.get("wmocode") or "").strip(),
            "wigos": str(r.get("wigosid") or "").strip(),
            "data_access": "unknown",
        }
        if code.startswith("de") and code[2:] in dwd_sites:
            site.update(
                {
                    "network": "DWD",
                    "data_access": "open-loadable",
                    "provider": "dwd",
                    "provider_site": code[2:],
                    "note": DWD_NOTE,
                }
            )
        elif code in fmi_sites:
            site.update(
                {
                    "network": "FMI",
                    "data_access": "open-loadable",
                    "provider": "fmi",
                    "provider_site": code,
                    "note": FMI_NOTE,
                }
            )
        elif country in NETWORK_ACCESS:
            site.update(NETWORK_ACCESS[country])
        out.append(site)
    return out


#: OSCAR territory names that OPERA and HOMR spell differently (one name per country in the map's
#: country filter); "(the)" suffixes are dropped for every territory.
TERRITORY_ALIASES = {
    "United States": "United States of America",
    "United Kingdom of Great Britain and Northern Ireland": "United Kingdom",
    "Czech Republic": "Czechia",
    "Moldova": "Republic of Moldova",
}


def territory_name(raw: str) -> str:
    name = re.sub(r"\s*\(the\)\s*$", "", raw.strip())
    return TERRITORY_ALIASES.get(name, name)


def oscar_sites(raw: bytes, date: str, taken: list[dict]) -> tuple[list[dict], int, int]:
    doc = json.loads(raw)
    out = []
    dropped_pos = 0
    dropped_dup = 0
    for r in doc.get("stationSearchResults", []):
        lat, lon = num(r.get("latitude")), num(r.get("longitude"))
        if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            dropped_pos += 1
            continue
        # A radar already listed by HOMR or OPERA (same place within 2 km) keeps that entry.
        if any(km(lat, lon, t["lat"], t["lon"]) < 2.0 for t in taken):
            dropped_dup += 1
            continue
        wigos = str(r.get("wigosId") or "").strip()
        territory = territory_name(str(r.get("territory") or ""))
        status = str(r.get("declaredStatus") or "unknown").strip().lower()
        site = {
            "id": wigos or f"oscar-{r.get('id')}",
            "name": str(r.get("name") or wigos).strip().title(),
            "country": territory,
            "region": "",
            "network": f"OSCAR/{territory}",
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "elev_m": num(r.get("elevation")),
            "band": "",
            "polarisation": "",
            "status": status,
            "source": "oscar",
            "retrieved": date,
            "wigos": wigos,
            "data_access": "unknown",
        }
        if territory in NETWORK_ACCESS:
            site.update(NETWORK_ACCESS[territory])
        out.append(site)
    return out, dropped_pos, dropped_dup


def dwd_site_list(raw: bytes) -> set[str]:
    return set(re.findall(r'href="([a-z]{3})/"', raw.decode("utf-8", "replace")))


def fmi_site_list(raw: bytes) -> set[str]:
    return set(re.findall(r"_(fi[a-z]{3})_PVOL\.h5", raw.decode("utf-8", "replace")))


def verify_dwd(sites: list[dict], cache_dir: Path, *, offline: bool) -> list[str]:
    """Compare the OPERA position of every DWD site with the ``where`` attributes of one of its
    own sweep files (downloaded into *cache_dir* and deleted after reading)."""
    import h5py

    notes = []
    for s in sites:
        if s.get("provider") != "dwd":
            continue
        code = s["provider_site"]
        index = fetch(
            DWD_SWEEP_DIR.format(site=code), cache_dir, f"dwd-index-{code}.html", offline=offline
        )
        names = re.findall(r'href="(ras07[^"]*dbzh_00-[^"]*)"', index.decode("utf-8", "replace"))
        if not names:
            notes.append(f"{code}: no sweep file listed")
            continue
        path = cache_dir / f"dwd-{code}.h5"
        try:
            fetch(
                DWD_SWEEP_DIR.format(site=code) + names[-1], cache_dir, path.name, offline=offline
            )
            with h5py.File(path, "r") as h:
                w = h["where"].attrs
                lat, lon, hgt = float(w["lat"]), float(w["lon"]), float(w["height"])
        except Exception as exc:
            notes.append(f"{code}: could not read a sweep file ({exc})")
            continue
        finally:
            if path.exists():
                path.unlink()
        d = km(lat, lon, s["lat"], s["lon"])
        notes.append(
            f"{code}: file {lat:.4f},{lon:.4f},{hgt:.0f} m; OPERA {s['lat']},{s['lon']}, {d:.2f} km"
        )
        if d > 1.0:
            s["note"] = (
                s.get("note", "") + f" Position cross-check: {d:.1f} km from the file's own."
            )
    return notes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="date of the downloads (YYYY-MM-DD)")
    ap.add_argument("--cache-dir", required=True, type=Path, help="folder for the fetched sources")
    ap.add_argument("--offline", action="store_true", help="use only files already in --cache-dir")
    ap.add_argument(
        "--verify-dwd", action="store_true", help="cross-check DWD positions with ODIM files"
    )
    ap.add_argument(
        "--tdwr-day",
        help="UTC day (YYYY-MM-DD) whose Unidata folder names say which TDWR were active "
        "(default: the day before --date)",
    )
    args = ap.parse_args()
    cache = args.cache_dir

    opera_raw = fetch(OPERA_URL, cache, "opera.json", offline=args.offline)
    oscar_raw = fetch(OSCAR_URL, cache, "oscar.json", offline=args.offline)
    dwd_raw = fetch(DWD_INDEX_URL, cache, "dwd-index.html", offline=args.offline)
    fmi_raw = fetch(FMI_LIST_URL, cache, "fmi-list.xml", offline=args.offline)

    homr_raw = fetch(HOMR_URL, cache, "homr-nexrad-stations.txt", offline=args.offline)
    day = args.tdwr_day or (date.fromisoformat(args.date) - timedelta(days=1)).isoformat()
    day_url = f"{UNIDATA_L2}?list-type=2&delimiter=/&prefix={day.replace('-', '/')}/&max-keys=1000"
    unidata_raw = fetch(day_url, cache, "unidata-l2-day.xml", offline=args.offline)
    aura_raw = fetch(AURA_LIST_URL, cache, "aura-radar_site_list.csv", offline=args.offline)
    aura_root = fetch(AURA_ROOT_URL, cache, "aura-root.html", offline=args.offline)

    dwd = dwd_site_list(dwd_raw)
    fmi = fmi_site_list(fmi_raw)
    unidata_ids = set(
        re.findall(rf"<Prefix>{day.replace('-', '/')}/([A-Z0-9]+)/</Prefix>", unidata_raw.decode())
    )
    sites = nexrad_sites()
    tdwr = tdwr_sites(homr_raw.decode("utf-8", "replace"), args.date, day, unidata_ids)
    homr_ids = {s["id"] for s in tdwr}
    extra_in_bucket = sorted(i for i in unidata_ids if i.startswith("T") and len(i) == 4)
    extra_in_bucket = [
        i for i in extra_in_bucket if i not in homr_ids and i not in {s["id"] for s in sites}
    ]
    sites += tdwr
    sites += opera_sites(opera_raw, args.date, dwd, fmi)
    oscar, dropped_pos, dropped_dup = oscar_sites(oscar_raw, args.date, sites)
    oscar_au = [s for s in oscar if s["country"] == "Australia"]
    au, au_leftover, au_report = australia_sites(
        aura_raw.decode("utf-8", "replace"),
        aura_root.decode("utf-8", "replace"),
        args.date,
        oscar_au,
    )
    south_africa_tidy(oscar)
    oscar = [s for s in oscar if s["country"] != "Australia"] + au_leftover
    sites += oscar + au
    if args.verify_dwd:
        notes = verify_dwd(sites, cache, offline=args.offline)
    else:  # keep the earlier cross-check (it needs h5py and 17 file downloads to redo)
        notes = json.loads(OUT.read_text(encoding="utf-8")).get("dwd_position_check", [])

    ids = [s["id"] for s in sites]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        sys.exit(f"duplicate ids: {sorted(dup)}")
    sites.sort(key=lambda s: (s["country"], s["id"]))
    manifest = {
        name: {"url": url, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()[:16]}
        for name, url, data in (
            ("opera.json", OPERA_URL, opera_raw),
            ("oscar.json", OSCAR_URL, oscar_raw),
            ("dwd-index.html", DWD_INDEX_URL, dwd_raw),
            ("fmi-list.xml", FMI_LIST_URL, fmi_raw),
            ("homr-nexrad-stations.txt", HOMR_URL, homr_raw),
            ("unidata-l2-day.xml", day_url, unidata_raw),
            ("aura-radar_site_list.csv", AURA_LIST_URL, aura_raw),
            ("aura-root.html", AURA_ROOT_URL, aura_root),
        )
    }
    by_access: dict[str, int] = {}
    for s in sites:
        by_access[s["data_access"]] = by_access.get(s["data_access"], 0) + 1
    doc = {
        "_comment": (
            "Weather radar site catalogue of Radarx Desktop: positions and metadata transcribed "
            "from the sources listed under 'sources' (each site names its source and retrieval "
            "date; licence and attribution are those of the source). Not an official product of "
            "any of them. Regenerate with scripts/build_radar_catalogue.py."
        ),
        "retrieved": args.date,
        "sources": SOURCES,
        "manifest": manifest,
        "counts": {
            "sites": len(sites),
            "by_access": by_access,
            "oscar_dropped_no_position": dropped_pos,
            "oscar_dropped_duplicate_of_homr_or_opera": dropped_dup,
        },
        "dwd_position_check": notes,
        "tdwr_check": {
            "unidata_day": day,
            "homr_tdwr_rows": len(tdwr),
            "active_in_unidata_that_day": sum(s["status"] == "operational" for s in tdwr),
            "in_unidata_not_in_homr_tdwr": extra_in_bucket,
        },
        "australia_check": au_report,
        "sites": sites,
    }
    head = json.dumps({k: v for k, v in doc.items() if k != "sites"}, indent=1, ensure_ascii=False)
    body = ",\n".join("  " + json.dumps(r, ensure_ascii=False) for r in sites)
    OUT.write_text(head[:-2] + ',\n "sites": [\n' + body + "\n ]\n}\n", encoding="utf-8")
    print(f"wrote {OUT}: {len(sites)} sites, {by_access}")
    print(f"OSCAR dropped: {dropped_pos} without position, {dropped_dup} duplicates of HOMR/OPERA")
    print(
        f"TDWR: {len(tdwr)} HOMR rows; active in the Unidata bucket on {day}: "
        f"{sum(s['status'] == 'operational' for s in tdwr)}; in bucket but not in HOMR: "
        f"{extra_in_bucket}"
    )
    for n in au_report:
        print(" ", n)
    for n in notes:
        print(" ", n)


if __name__ == "__main__":
    main()

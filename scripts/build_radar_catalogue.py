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
    "Australia": {
        "data_access": "open-link",
        "url": "https://thredds.nci.org.au/thredds/catalog/rq0/catalog.html",
        "note": "Australian operational radar archive (level 1b, ODIM HDF5, daily tar files) on "
        "NCI THREDDS; noncommercial share-alike licence (CC BY-NC-SA 4.0); months behind "
        "real time.",
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
    args = ap.parse_args()
    cache = args.cache_dir

    opera_raw = fetch(OPERA_URL, cache, "opera.json", offline=args.offline)
    oscar_raw = fetch(OSCAR_URL, cache, "oscar.json", offline=args.offline)
    dwd_raw = fetch(DWD_INDEX_URL, cache, "dwd-index.html", offline=args.offline)
    fmi_raw = fetch(FMI_LIST_URL, cache, "fmi-list.xml", offline=args.offline)

    dwd = dwd_site_list(dwd_raw)
    fmi = fmi_site_list(fmi_raw)
    sites = nexrad_sites()
    sites += opera_sites(opera_raw, args.date, dwd, fmi)
    oscar, dropped_pos, dropped_dup = oscar_sites(oscar_raw, args.date, sites)
    sites += oscar
    notes = verify_dwd(sites, cache, offline=args.offline) if args.verify_dwd else []

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
        "sites": sites,
    }
    head = json.dumps({k: v for k, v in doc.items() if k != "sites"}, indent=1, ensure_ascii=False)
    body = ",\n".join("  " + json.dumps(r, ensure_ascii=False) for r in sites)
    OUT.write_text(head[:-2] + ',\n "sites": [\n' + body + "\n ]\n}\n", encoding="utf-8")
    print(f"wrote {OUT}: {len(sites)} sites, {by_access}")
    print(f"OSCAR dropped: {dropped_pos} without position, {dropped_dup} duplicates of HOMR/OPERA")
    for n in notes:
        print(" ", n)


if __name__ == "__main__":
    main()

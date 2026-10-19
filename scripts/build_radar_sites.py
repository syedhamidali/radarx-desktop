"""Rebuild src/lib/data/nexradSites.json from the NOAA NCEI HOMR NEXRAD station list.

    curl -o nexrad-stations.txt https://www.ncei.noaa.gov/access/homr/file/nexrad-stations.txt
    python scripts/build_radar_sites.py nexrad-stations.txt --date 2026-10-19

Only the WSR-88D sites that the engine knows (python/engine/cloud.py) are kept, so every marker
on the map can be opened.  Coordinates are the published values rounded to 5 decimals (about
1 m), elevation is converted from feet to metres.  This is a station *catalogue*, not radar data.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "lib" / "data" / "nexradSites.json"
sys.path.insert(0, str(ROOT / "python"))

KEEP_UPPER = {"AFB", "NAS", "AAF"}
# HOMR names that are truncated or carry a data-entry suffix; only the wording changes.
TIDY = {
    "KAKQ": "Norfolk/Richmond",
    "KDGX": "Jackson/Brandon",
    "KEWX": "Austin/San Antonio",
    "KFTG": "Denver/Front Range",
    "KLGX": "Langley Hill",
    "KLSX": "St. Louis",
    "KMAF": "Midland/Odessa",
    "KRAX": "Raleigh/Durham",
    "PABC": "Bethel",
    "PGUA": "Andersen AFB",
}


def pretty(name: str) -> str:
    return " ".join(w if w in KEEP_UPPER else w.capitalize() for w in name.split())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("homr_file")
    ap.add_argument("--date", required=True, help="date the file was downloaded (YYYY-MM-DD)")
    args = ap.parse_args()

    from engine.cloud import _NEXRAD_STATIONS

    wanted = set(_NEXRAD_STATIONS)
    rows: dict[str, dict] = {}
    lines = Path(args.homr_file).read_text().splitlines()
    # fixed-width columns, taken from the dashed ruler (WBAN may be blank)
    cols = [(m.start(), m.end()) for m in re.finditer(r"-+", lines[1])]
    for line in lines[2:]:
        f = [line[a:b].strip() for a, b in cols]
        if len(f) < 9 or f[1] not in wanted:
            continue
        sid, name, st = f[1], f[3], f[5]
        rows[sid] = {
            "id": sid,
            "name": TIDY.get(sid, pretty(name)),
            "state": st,
            "lat": round(float(f[7]), 5),
            "lon": round(float(f[8]), 5),
            "elev_m": round(int(f[9]) * 0.3048),
        }
    missing = wanted - set(rows)
    if missing:
        sys.exit(f"missing in the HOMR file: {sorted(missing)}")
    doc = {
        "_comment": (
            "NEXRAD WSR-88D radar site catalogue. Source: NOAA NCEI Historical Observing Metadata "
            "Repository (HOMR), https://www.ncei.noaa.gov/access/homr/file/nexrad-stations.txt, "
            f"downloaded {args.date}. NOAA/NWS information is in the public domain "
            "(https://www.weather.gov/disclaimer); this is a transcription, not official NWS "
            "material. name/state = HOMR NAME/ST (title-cased; ten truncated names spelled out), lat/lon in degrees (WGS84, 5 "
            "decimals), elev_m = ground elevation converted from feet. Regenerate with "
            "scripts/build_radar_sites.py."
        ),
        "source": "NOAA NCEI HOMR nexrad-stations.txt",
        "retrieved": args.date,
        "sites": [rows[k] for k in sorted(rows)],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    head = json.dumps({k: v for k, v in doc.items() if k != "sites"}, indent=1, ensure_ascii=False)
    body = ",\n".join("  " + json.dumps(r, ensure_ascii=False) for r in doc["sites"])
    OUT.write_text(head[:-2] + ',\n "sites": [\n' + body + "\n ]\n}\n")
    print(f"wrote {OUT} ({len(rows)} sites)")


if __name__ == "__main__":
    main()

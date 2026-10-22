#!/usr/bin/env python3
"""Build ``src/lib/data/globeGeo.json``: the land outlines and country borders of the radar globe.

Input: two Natural Earth 110m shapefiles (public domain), read with a few lines of plain Python (no GIS
library, no network):

    ne_110m_land.shp                         physical / land polygons (their outlines are the coastlines)
    ne_110m_admin_0_boundary_lines_land.shp  cultural / country borders on land

Usage::

    python scripts/build_globe_geo.py <folder with the two .shp files> [output.json]

Output format (read by src/lib/utils/globeGeo.ts): ``{"v": 1, "unit": 0.05, "land": [ring, ...],
"borders": [line, ...]}`` where every ring/line is a flat list of integers ``[x0, y0, dx1, dy1, dx2, dy2, ...]``:
the first point in units of ``unit`` degrees (longitude, latitude), then differences to the previous point.
Rings are not closed explicitly (the last point joins the first). Nothing is invented or smoothed: the points
are Natural Earth's, rounded to 0.05 degree (about 5 km), with points that rounding made identical dropped.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

UNIT = 0.05  # degrees per integer step
ROOT = Path(__file__).resolve().parent.parent


def read_shp(path: Path) -> list[list[list[tuple[float, float]]]]:
    """Return, per record, its parts as lists of (lon, lat). Handles polyline (3) and polygon (5) only."""
    data = path.read_bytes()
    out: list[list[list[tuple[float, float]]]] = []
    pos = 100
    while pos + 8 <= len(data):
        length_words = struct.unpack(">i", data[pos + 4 : pos + 8])[0]
        body = data[pos + 8 : pos + 8 + length_words * 2]
        pos += 8 + length_words * 2
        (stype,) = struct.unpack("<i", body[:4])
        if stype == 0:
            continue
        if stype not in (3, 5):
            raise SystemExit(f"{path.name}: unsupported shape type {stype}")
        nparts, npts = struct.unpack("<ii", body[36:44])
        parts = list(struct.unpack(f"<{nparts}i", body[44 : 44 + 4 * nparts]))
        off = 44 + 4 * nparts
        pts = [struct.unpack("<dd", body[off + 16 * i : off + 16 * i + 16]) for i in range(npts)]
        parts.append(npts)
        out.append([pts[parts[i] : parts[i + 1]] for i in range(nparts)])
    return out


def quantise(points: list[tuple[float, float]], closed: bool) -> list[int] | None:
    q: list[tuple[int, int]] = []
    for lon, lat in points:
        p = (round(lon / UNIT), round(lat / UNIT))
        if not q or p != q[-1]:
            q.append(p)
    if closed and len(q) > 1 and q[0] == q[-1]:
        q.pop()  # the ring closes by itself
    if len(q) < (3 if closed else 2):
        return None
    flat = [q[0][0], q[0][1]]
    for a, b in zip(q, q[1:]):
        flat += [b[0] - a[0], b[1] - a[1]]
    return flat


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "src" / "lib" / "data" / "globeGeo.json"
    land = [r for rec in read_shp(src / "ne_110m_land.shp") for part in rec if (r := quantise(part, True))]
    borders = [
        r for rec in read_shp(src / "ne_110m_admin_0_boundary_lines_land.shp") for part in rec if (r := quantise(part, False))
    ]
    doc = {"v": 1, "unit": UNIT, "land": land, "borders": borders}
    dst.write_text(json.dumps(doc, separators=(",", ":")) + "\n", encoding="utf-8")
    npts = sum(len(r) // 2 for r in land + borders)
    print(f"{dst}: {len(land)} land rings, {len(borders)} border lines, {npts} points, {dst.stat().st_size} bytes")


if __name__ == "__main__":
    main()

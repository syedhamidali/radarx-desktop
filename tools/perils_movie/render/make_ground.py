"""A schematic ground map for the movie, drawn from local vector data (no network, no imagery, no terrain).

The offline Alabama / Mississippi domain has no cached satellite tiles, so the ground is a map of
Natural Earth (public domain; the copy that cartopy keeps in ``~/.local/share/cartopy``: land, lakes,
rivers, state lines, major roads, populated places) and, optionally, the US Census cartographic boundary
county lines (``--counties``). The image is in the scene frame of the converted bundle (azimuthal
equidistant around the volume origin, km), 2048 x 2048 px over [-half, half] around ``(cx, cy)``, written as
``ground.png``; ``convert_bundle.py`` records it in ``perils.json``.

    python make_ground.py --bundle-clip <converted bundle dir> [--half 260] [--counties <cb_2014_us_county_20m.shp>]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from convert_bundle import Geo

CREDIT = "Ground map: Natural Earth (public domain)"
CREDIT_COUNTIES = "US Census Bureau cartographic boundary files (county lines)"


def _reader(path: Path):
    import shapefile

    for enc in ("utf-8", "latin1"):
        try:
            r = shapefile.Reader(str(path), encoding=enc, encodingErrors="replace")
            len(r)
            return r
        except Exception:
            continue
    raise RuntimeError(f"cannot read {path}")


def _parts(shape):
    pts = np.asarray(shape.points, float)
    idx = [*list(shape.parts), len(pts)]
    return [pts[idx[i] : idx[i + 1]] for i in range(len(shape.parts))]


def draw(out_png: Path, geo: Geo, half: float, cx: float, cy: float, ne_dir: Path, counties: Path | None, size: int = 2048, log=print) -> dict:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection, PolyCollection

    # lon / lat box with a margin, to skip far shapes
    lat0, lon0 = geo.lat0, geo.lon0
    dlat = (half + 60) / 111.19
    dlon = (half + 60) / (111.19 * np.cos(np.radians(lat0)))
    bx = (lon0 - dlon, lat0 - dlat, lon0 + dlon, lat0 + dlat)

    def inbox(b):
        return not (b[2] < bx[0] or b[0] > bx[2] or b[3] < bx[1] or b[1] > bx[3])

    def proj(p):
        x, y = geo.xy(p[:, 1], p[:, 0])
        return np.stack([x, y], 1)

    def shapes(path: Path):
        r = _reader(path)
        for s, rec in zip(r.iterShapes(), r.iterRecords(), strict=False):
            if s.shapeType == 0 or (not inbox(s.bbox if hasattr(s, "bbox") else (0, 0, 0, 0)) and s.shapeType != 1):
                continue
            yield s, rec

    fig = plt.figure(figsize=(size / 100, size / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(cx - half, cx + half)
    ax.set_ylim(cy - half, cy + half)
    ax.set_facecolor((10 / 255, 16 / 255, 28 / 255))
    ax.axis("off")
    phys = ne_dir / "physical"
    cult = ne_dir / "cultural"

    def polys(name, face, z):
        verts = []
        for s, _ in shapes(phys / f"{name}.shp"):
            for part in _parts(s):
                verts.append(proj(part))
        if verts:
            ax.add_collection(PolyCollection(verts, facecolors=[face], edgecolors="none", zorder=z))
        return len(verts)

    n_land = polys("ne_10m_land", (24 / 255, 31 / 255, 44 / 255), 1)
    polys("ne_10m_lakes", (10 / 255, 18 / 255, 32 / 255), 2)
    # rivers
    lines = []
    for s, _rec in shapes(phys / "ne_10m_rivers_lake_centerlines.shp"):
        for part in _parts(s):
            lines.append(proj(part))
    if lines:
        ax.add_collection(LineCollection(lines, colors=[(52 / 255, 98 / 255, 150 / 255)], linewidths=1.3, zorder=3))
    credit = CREDIT
    if counties and counties.exists():
        cl = []
        for s, _ in shapes(counties):
            for part in _parts(s):
                cl.append(proj(part))
        ax.add_collection(LineCollection(cl, colors=[(64 / 255, 78 / 255, 100 / 255)], linewidths=0.7, zorder=4))
        credit += "; " + CREDIT_COUNTIES
    # state lines
    sl = []
    for s, _ in shapes(cult / "ne_10m_admin_1_states_provinces_lines.shp"):
        for part in _parts(s):
            sl.append(proj(part))
    if sl:
        ax.add_collection(LineCollection(sl, colors=[(165 / 255, 185 / 255, 215 / 255)], linewidths=1.8, zorder=5))
    # major roads
    rl = []
    try:
        for s, _rec in shapes(cult / "ne_10m_roads.shp"):
            for part in _parts(s):
                rl.append(proj(part))
    except Exception as e:  # pragma: no cover
        log(f"roads skipped: {e}")
    if rl:
        ax.add_collection(LineCollection(rl, colors=[(120 / 255, 108 / 255, 84 / 255)], linewidths=1.0, zorder=5))
    # places
    r = _reader(cult / "ne_10m_populated_places.shp")
    fields = [f[0] for f in r.fields[1:]]
    ni, pi = fields.index("NAME"), fields.index("POP_MAX")
    places = []
    for s, rec in zip(r.iterShapes(), r.iterRecords(), strict=False):
        lon, lat = s.points[0]
        if bx[0] <= lon <= bx[2] and bx[1] <= lat <= bx[3] and rec[pi] and rec[pi] >= 8000:
            x, y = geo.xy(lat, lon)
            if abs(x - cx) < half and abs(y - cy) < half:
                places.append((rec[ni], float(x), float(y), int(rec[pi])))
    places.sort(key=lambda p: -p[3])
    for name, x, y, pop in places[:40]:
        big = pop >= 100000
        ax.plot([x], [y], "o", ms=7 if big else 5, color=(215 / 255, 222 / 255, 235 / 255), zorder=7)
        ax.text(x + 2.2, y + 1.2, name, color=(215 / 255, 222 / 255, 235 / 255), fontsize=12 if big else 9, zorder=8, va="bottom")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=100, facecolor=ax.get_facecolor())
    plt.close(fig)
    log(f"ground map {out_png} ({size}x{size}, half {half:g} km, {len(places)} places, {len(rl)} road segments, {n_land} land polygons)")
    return {"file": out_png.name, "half": half, "cx": cx, "cy": cy, "size": size, "credit": credit}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clip", required=True, help="the converted bundle folder (perils.json inside); ground.png is written there")
    ap.add_argument("--half", type=float, default=260.0)
    ap.add_argument("--ne-dir", default=str(Path.home() / ".local/share/cartopy/shapefiles/natural_earth"))
    ap.add_argument("--counties", default=None, help="optional US Census county lines (.shp)")
    a = ap.parse_args(argv)
    clip = Path(a.clip)
    p = json.loads((clip / "perils.json").read_text())
    geo = Geo(p["origin"]["lat"], p["origin"]["lon"])
    meta = draw(clip / "ground.png", geo, a.half, 0.0, 0.0, Path(a.ne_dir), Path(a.counties) if a.counties else None)
    p["ground"] = meta
    (clip / "perils.json").write_text(json.dumps(p))


if __name__ == "__main__":
    main()

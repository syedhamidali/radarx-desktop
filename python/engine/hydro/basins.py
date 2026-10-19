"""Basins (watersheds): polygons, exact area weights on the grid, rainfall statistics and series.

Readers use only the standard library, numpy and pyproj (all already in the engine bundle), so no
GDAL/Fiona/GeoPandas/Shapely is needed and the installer does not grow:

* GeoJSON (``.geojson``, ``.json``): Polygon, MultiPolygon, Feature, FeatureCollection; WGS 84
  unless a legacy ``crs`` member or the ``crs=`` argument says otherwise;
* ESRI shapefile (``.shp`` with ``.dbf`` attributes and ``.prj`` projection);
* OGC GeoPackage (``.gpkg``): the first features table with polygon geometry (SQLite + WKB).

Area weights are exact
----------------------
A basin is a polygon with holes. The fraction ``f`` of every grid cell inside it is computed
analytically, not by counting pixels or by sampling: for a polygon ``P`` and a cell
``[x0, x1] x [y0, y1]``

    area(P & cell) = sum_e s_e * integral_{y0}^{y1} clamp(x_e(y), x0, x1) dy

with ``s_e = +1`` for edges that go up and ``-1`` for edges that go down (exterior rings counter-
clockwise, holes clockwise). For a straight edge the integral is closed-form (piecewise linear), so
the result is exact for any simple polygon. The weight of a cell is ``f`` times its *true* area
(the grid is azimuthal-equidistant; the small area distortion is removed, see
``QpeGrid.areal_scale``), so a basin average is area-weighted on the real ground, not a mean of
pixels. The grid area is compared with the geodesic area of the polygon (WGS 84 ellipsoid) and
reported.

Statistics of a field ``v`` over a basin use ``w = f * area`` on cells with a value:
``mean = sum(w v)/sum(w)``, ``coverage = sum(w valid)/sum(w)``, ``area above t = sum(w [v >= t])``.
"""

from __future__ import annotations

import json
import math
import sqlite3
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from engine import provenance as prov
from engine.hydro.grid import QpeGrid
from engine.hydro.qpe import QpeError

SECONDS = np.timedelta64(1, "s")


class BasinError(QpeError):
    """A user-facing problem with a basin file or a basin computation."""


# ---------------------------------------------------------------------------
# Geometry containers
# ---------------------------------------------------------------------------

Ring = np.ndarray  # (n, 2) lon/lat or x/y, closed or open
Polygon = list[Ring]  # [exterior, hole, hole, ...]


@dataclass
class Basin:
    id: str
    name: str
    polygons: list[Polygon]  # lon/lat (WGS 84), degrees
    attrs: dict[str, Any] = field(default_factory=dict)

    def bounds(self) -> tuple[float, float, float, float]:
        pts = np.concatenate([r for poly in self.polygons for r in poly[:1]])
        return (
            float(pts[:, 0].min()),
            float(pts[:, 1].min()),
            float(pts[:, 0].max()),
            float(pts[:, 1].max()),
        )

    def geodesic_area_km2(self) -> float:
        """Area on the WGS 84 ellipsoid (exterior minus holes), km2."""
        import pyproj

        geod = pyproj.Geod(ellps="WGS84")
        total = 0.0
        for poly in self.polygons:
            for k, ring in enumerate(poly):
                a, _ = geod.polygon_area_perimeter(ring[:, 0], ring[:, 1])
                total += abs(a) if k == 0 else -abs(a)
        return total / 1e6


def _signed_area(ring: Ring) -> float:
    x, y = ring[:, 0], ring[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _close(ring: Ring) -> Ring:
    ring = np.asarray(ring, dtype=np.float64)[:, :2]
    if len(ring) and not np.array_equal(ring[0], ring[-1]):
        ring = np.vstack([ring, ring[:1]])
    return ring


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def _geojson_polys(geom: dict[str, Any]) -> list[Polygon]:
    t = geom.get("type")
    if t == "Polygon":
        return [[_close(np.asarray(r, dtype=np.float64)) for r in geom["coordinates"]]]
    if t == "MultiPolygon":
        return [[_close(np.asarray(r, dtype=np.float64)) for r in p] for p in geom["coordinates"]]
    if t == "GeometryCollection":
        return [p for g in geom.get("geometries", []) for p in _geojson_polys(g)]
    return []


def _first(props: dict[str, Any], keys: Sequence[str]) -> str | None:
    lower = {str(k).lower(): k for k in props}
    for key in keys:
        if key.lower() in lower and props[lower[key.lower()]] not in (None, ""):
            return str(props[lower[key.lower()]])
    return None


ID_KEYS = ("id", "basin_id", "huc12", "huc10", "huc8", "huc", "gauge_id", "site_no", "name", "fid")
NAME_KEYS = ("name", "basin_name", "basin", "label", "title", "name_en")


def _make_basins(
    items: list[tuple[list[Polygon], dict[str, Any]]],
    id_field: str | None,
    name_field: str | None,
) -> list[Basin]:
    out: list[Basin] = []
    for n, (polys, props) in enumerate(items):
        polys = [p for p in polys if p and len(p[0]) >= 4]
        if not polys:
            continue
        bid = (
            str(props.get(id_field))
            if id_field and props.get(id_field) is not None
            else _first(props, ID_KEYS)
        )
        name = (
            str(props.get(name_field))
            if name_field and props.get(name_field) is not None
            else _first(props, NAME_KEYS)
        )
        bid = bid or f"basin_{n + 1}"
        out.append(Basin(id=bid, name=name or bid, polygons=polys, attrs=props))
    # unique ids
    seen: dict[str, int] = {}
    for b in out:
        seen[b.id] = seen.get(b.id, 0) + 1
        if seen[b.id] > 1:
            b.id = f"{b.id}_{seen[b.id]}"
    return out


def _read_geojson(path: Path) -> tuple[list[tuple[list[Polygon], dict]], str | None]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BasinError(f"cannot read the GeoJSON file {path.name}: {exc}") from exc
    crs = None
    c = doc.get("crs") if isinstance(doc, dict) else None
    if isinstance(c, dict):
        crs = (c.get("properties") or {}).get("name")
        if crs and "CRS84" in crs.upper():
            crs = "EPSG:4326"
    items: list[tuple[list[Polygon], dict]] = []

    def feat(f: dict[str, Any]) -> None:
        if f.get("type") == "FeatureCollection":
            for g in f.get("features", []):
                feat(g)
        elif f.get("type") == "Feature":
            items.append((_geojson_polys(f.get("geometry") or {}), dict(f.get("properties") or {})))
        elif f.get("type") in ("Polygon", "MultiPolygon", "GeometryCollection"):
            items.append((_geojson_polys(f), {}))

    feat(doc)
    return items, crs


def _read_dbf(path: Path) -> list[dict[str, Any]]:
    data = path.read_bytes()
    n, hlen, rlen = struct.unpack("<IHH", data[4:12])
    fields = []
    pos = 32
    while data[pos] != 0x0D and pos + 32 <= len(data):
        name = data[pos : pos + 11].split(b"\x00")[0].decode("ascii", "ignore")
        ftype = chr(data[pos + 11])
        flen = data[pos + 16]
        fields.append((name, ftype, flen))
        pos += 32
    rows = []
    for i in range(n):
        rec = data[hlen + i * rlen : hlen + (i + 1) * rlen]
        if not rec or rec[0:1] == b"*":
            rows.append({})
            continue
        row: dict[str, Any] = {}
        off = 1
        for name, ftype, flen in fields:
            raw = rec[off : off + flen]
            off += flen
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
            text = text.strip()
            if ftype in "NF":
                try:
                    row[name] = float(text) if ("." in text or "e" in text.lower()) else int(text)
                except ValueError:
                    row[name] = None
            else:
                row[name] = text
        rows.append(row)
    return rows


def _split_rings(parts: list[np.ndarray], esri_clockwise_exterior: bool = True) -> list[Polygon]:
    """Group the rings of one shapefile record into polygons (exteriors with their holes)."""
    exteriors: list[Ring] = []
    holes: list[Ring] = []
    for ring in parts:
        ring = _close(ring)
        if len(ring) < 4:
            continue
        a = _signed_area(ring)
        is_ext = (a < 0) if esri_clockwise_exterior else (a > 0)
        (exteriors if is_ext else holes).append(ring)
    if not exteriors and holes:  # a file with the opposite convention
        exteriors, holes = holes, []
    polys: list[Polygon] = [[e] for e in exteriors]
    for h in holes:
        pt = h[0]
        cands = [i for i, e in enumerate(exteriors) if _point_in_ring(pt[0], pt[1], e)]
        if cands:
            best = min(cands, key=lambda i: abs(_signed_area(exteriors[i])))
            polys[best].append(h)
    return polys


def _point_in_ring(x: float, y: float, ring: Ring) -> bool:
    xs, ys = ring[:, 0], ring[:, 1]
    x1, y1, x2, y2 = xs[:-1], ys[:-1], xs[1:], ys[1:]
    cond = (y1 > y) != (y2 > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
    return bool(np.count_nonzero(cond & (x < xint)) % 2)


def _read_shapefile(path: Path) -> tuple[list[tuple[list[Polygon], dict]], str | None]:
    data = path.read_bytes()
    if len(data) < 100 or struct.unpack(">i", data[:4])[0] != 9994:
        raise BasinError(f"{path.name} is not an ESRI shapefile (.shp)")
    props: list[dict[str, Any]] = []
    dbf = path.with_suffix(".dbf")
    if not dbf.exists():
        dbf = path.with_suffix(".DBF")
    if dbf.exists():
        props = _read_dbf(dbf)
    crs = None
    for suffix in (".prj", ".PRJ"):
        prj = path.with_suffix(suffix)
        if prj.exists():
            crs = prj.read_text(encoding="utf-8", errors="ignore").strip()
            break
    items: list[tuple[list[Polygon], dict]] = []
    pos = 100
    rec_i = 0
    while pos + 8 <= len(data):
        _, clen = struct.unpack(">ii", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + clen * 2]
        pos += 8 + clen * 2
        stype = struct.unpack("<i", body[:4])[0]
        row = props[rec_i] if rec_i < len(props) else {}
        rec_i += 1
        if stype not in (5, 15, 25):
            items.append(([], row))
            continue
        nparts, npts = struct.unpack("<ii", body[36:44])
        parts = np.frombuffer(body, dtype="<i4", count=nparts, offset=44)
        pts = np.frombuffer(body, dtype="<f8", count=npts * 2, offset=44 + 4 * nparts).reshape(
            -1, 2
        )
        bounds = [*parts, npts]
        rings = [pts[bounds[k] : bounds[k + 1]] for k in range(nparts)]
        items.append((_split_rings(rings), row))
    return items, crs


class _Wkb:
    def __init__(self, buf: bytes, pos: int = 0) -> None:
        self.b, self.p = buf, pos

    def geometry(self) -> list[Polygon]:
        order = "<" if self.b[self.p] == 1 else ">"
        self.p += 1
        (gtype,) = struct.unpack_from(order + "I", self.b, self.p)
        self.p += 4
        dims = 2
        if gtype & 0x80000000:
            dims += 1
        if gtype & 0x40000000:
            dims += 1
        if gtype & 0x20000000:
            self.p += 4  # SRID
        base = gtype & 0x0FFFFFFF
        if base >= 1000:
            kind = base // 1000
            dims = {0: 2, 1: 3, 2: 3, 3: 4}[kind]
            base %= 1000
        if base == 3:
            return [self._polygon(order, dims)]
        if base in (6, 7):
            (n,) = struct.unpack_from(order + "I", self.b, self.p)
            self.p += 4
            out: list[Polygon] = []
            for _ in range(n):
                out.extend(self.geometry())
            return out
        # points, lines: skip by parsing their length so a collection can continue
        self._skip(order, base, dims)
        return []

    def _polygon(self, order: str, dims: int) -> Polygon:
        (nr,) = struct.unpack_from(order + "I", self.b, self.p)
        self.p += 4
        rings = []
        for _ in range(nr):
            (n,) = struct.unpack_from(order + "I", self.b, self.p)
            self.p += 4
            arr = np.frombuffer(self.b, dtype=order + "f8", count=n * dims, offset=self.p)
            self.p += n * dims * 8
            rings.append(_close(arr.reshape(n, dims)[:, :2]))
        return rings

    def _skip(self, order: str, base: int, dims: int) -> None:
        if base == 1:
            self.p += 8 * dims
        elif base == 2:
            (n,) = struct.unpack_from(order + "I", self.b, self.p)
            self.p += 4 + n * 8 * dims
        else:
            raise BasinError(f"unsupported geometry type {base} in the GeoPackage")


def _read_gpkg(
    path: Path, layer: str | None
) -> tuple[list[tuple[list[Polygon], dict]], str | None]:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BasinError(f"cannot open the GeoPackage {path.name}: {exc}") from exc
    try:
        rows = con.execute(
            "SELECT table_name, column_name, srs_id FROM gpkg_geometry_columns"
        ).fetchall()
        if not rows:
            raise BasinError(f"{path.name} has no feature table")
        pick = [r for r in rows if layer is None or r[0] == layer]
        if not pick:
            raise BasinError(
                f"layer {layer!r} not in {path.name} (layers: {', '.join(r[0] for r in rows)})"
            )
        table, gcol, srs = pick[0]
        crs = None
        r = con.execute(
            "SELECT organization, organization_coordsys_id, definition FROM gpkg_spatial_ref_sys "
            "WHERE srs_id=?",
            (srs,),
        ).fetchone()
        if r:
            org, code, wkt = r
            crs = f"{org}:{code}" if org and str(org).upper() == "EPSG" else wkt
        cur = con.execute(f'SELECT * FROM "{table}"')
        names = [d[0] for d in cur.description]
        items: list[tuple[list[Polygon], dict]] = []
        for row in cur:
            rec = dict(zip(names, row, strict=True))
            blob = rec.pop(gcol, None)
            if not blob:
                continue
            blob = bytes(blob)
            if blob[:2] != b"GP":
                raise BasinError("not a GeoPackage geometry blob")
            flags = blob[3]
            env = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get((flags >> 1) & 7, 0)
            if (flags >> 4) & 1:  # empty geometry
                continue
            items.append((_Wkb(blob, 8 + env).geometry(), rec))
        return items, crs
    except sqlite3.Error as exc:
        raise BasinError(f"cannot read the GeoPackage {path.name}: {exc}") from exc
    finally:
        con.close()


def _to_wgs84(
    items: list[tuple[list[Polygon], dict]], crs: Any
) -> list[tuple[list[Polygon], dict]]:
    import pyproj

    try:
        src = pyproj.CRS.from_user_input(crs) if crs else pyproj.CRS.from_epsg(4326)
    except pyproj.exceptions.CRSError as exc:
        raise BasinError(f"unknown coordinate system {crs!r}: {exc}") from exc
    if src == pyproj.CRS.from_epsg(4326) or (src.is_geographic and src.to_epsg() in (4326, 4269)):
        return items  # lon/lat already (NAD83 differs from WGS 84 by ~1 m: below the grid)
    tr = pyproj.Transformer.from_crs(src, "EPSG:4326", always_xy=True)
    out = []
    for polys, props in items:
        new = []
        for poly in polys:
            rings = []
            for ring in poly:
                lon, lat = tr.transform(ring[:, 0], ring[:, 1])
                rings.append(np.column_stack([lon, lat]))
            new.append(rings)
        out.append((new, props))
    return out


@dataclass
class BasinSet:
    """A list of basins read from a file (always in WGS 84 lon/lat)."""

    basins: list[Basin]
    source: str = ""
    crs: str = "EPSG:4326"

    def __len__(self) -> int:
        return len(self.basins)

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        id_field: str | None = None,
        name_field: str | None = None,
        crs: str | None = None,
        layer: str | None = None,
    ) -> BasinSet:
        """Read basins from GeoJSON, a shapefile or a GeoPackage (see the module docstring)."""
        p = Path(path).expanduser()
        if not p.is_file():
            raise BasinError(f"basin file not found: {path}. Check the path.")
        ext = p.suffix.lower()
        if ext in (".geojson", ".json"):
            items, file_crs = _read_geojson(p)
        elif ext == ".shp":
            items, file_crs = _read_shapefile(p)
        elif ext == ".gpkg":
            items, file_crs = _read_gpkg(p, layer)
        else:
            raise BasinError(
                f"unsupported basin file type {ext!r}: use .geojson, .shp (with .dbf/.prj) or .gpkg"
            )
        use_crs = crs or file_crs
        if ext == ".shp" and not use_crs:
            lon_lat = all(
                _plausible_lonlat(r) for polys, _ in items for poly in polys for r in poly[:1]
            )
            if not lon_lat:
                raise BasinError(
                    f"{p.name} has no .prj and its coordinates are not degrees: give the "
                    "coordinate system (crs='EPSG:5070')"
                )
        items = _to_wgs84(items, use_crs)
        basins = _make_basins(items, id_field, name_field)
        if not basins:
            raise BasinError(f"no polygons found in {p.name}")
        return cls(basins, source=p.name, crs=str(use_crs or "EPSG:4326")[:80])

    @classmethod
    def from_polygon(
        cls, lonlat: Sequence[Sequence[float]], basin_id: str = "basin", holes: Sequence = ()
    ) -> BasinSet:
        poly = [_close(np.asarray(lonlat, dtype=float))] + [
            _close(np.asarray(h, dtype=float)) for h in holes
        ]
        return cls([Basin(id, id, [poly])], source="polygon")

    def weights(self, grid: QpeGrid) -> list[BasinWeights]:
        return [basin_weights(b, grid) for b in self.basins]

    def collector(self, grid: QpeGrid) -> BasinCollector:
        return BasinCollector(self, grid)


def _plausible_lonlat(ring: Ring) -> bool:
    return bool(np.all(np.abs(ring[:, 0]) <= 180.0) and np.all(np.abs(ring[:, 1]) <= 90.0))


# ---------------------------------------------------------------------------
# Exact area fractions
# ---------------------------------------------------------------------------


def _pos_part_integral(p: np.ndarray, q: np.ndarray, c: np.ndarray, dy: np.ndarray) -> np.ndarray:
    """Integral over a segment of length ``dy`` of ``max(x(y) - c, 0)`` with ``x`` linear p -> q."""
    u, v = p - c, q - c
    up, vp = np.maximum(u, 0.0), np.maximum(v, 0.0)
    d = v - u
    with np.errstate(divide="ignore", invalid="ignore"):
        tri = (vp * vp - up * up) / (2.0 * d)
    return dy * np.where(np.abs(d) < 1e-12, up, tri)


def polygon_fractions(polys: list[Polygon], xe: np.ndarray, ye: np.ndarray) -> np.ndarray:
    """Fraction of every cell of a regular grid that lies inside the polygons (exact).

    ``polys`` are in the grid's coordinates (rings closed); ``xe`` (nx+1) and ``ye`` (ny+1) are the
    cell edges. Returns ``(ny, nx)`` float64 in [0, 1].
    """
    ny, nx = ye.size - 1, xe.size - 1
    area = np.zeros((ny, nx), dtype=np.float64)
    cell_area = np.outer(np.diff(ye), np.diff(xe))
    for poly in polys:
        for k, ring in enumerate(poly):
            ring = _close(ring)
            sa = _signed_area(ring)
            want_ccw = k == 0
            if (sa > 0) != want_ccw:
                ring = ring[::-1]
            xa, ya = ring[:-1, 0], ring[:-1, 1]
            xb, yb = ring[1:, 0], ring[1:, 1]
            keep = yb != ya
            xa, ya, xb, yb = xa[keep], ya[keep], xb[keep], yb[keep]
            if xa.size == 0:
                continue
            s = np.sign(yb - ya)
            ylo, yhi = np.minimum(ya, yb), np.maximum(ya, yb)
            slope = (xb - xa) / (yb - ya)
            for r in range(ny):
                y0, y1 = ye[r], ye[r + 1]
                sel = (yhi > y0) & (ylo < y1)
                if not sel.any():
                    continue
                lo = np.maximum(ylo[sel], y0)
                hi = np.minimum(yhi[sel], y1)
                dy = hi - lo
                # x at the clipped ends (the edge is linear in y)
                x_lo = xa[sel] + slope[sel] * (lo - ya[sel])
                x_hi = xa[sel] + slope[sel] * (hi - ya[sel])
                ss = s[sel][:, None]
                p, q = x_lo[:, None], x_hi[:, None]
                dyy = dy[:, None]
                left = xe[None, :-1]
                right = xe[None, 1:]
                contrib = _pos_part_integral(p, q, left, dyy) - _pos_part_integral(p, q, right, dyy)
                area[r] += (ss * contrib).sum(axis=0)
    with np.errstate(invalid="ignore"):
        frac = area / cell_area
    return np.clip(frac, 0.0, 1.0)


@dataclass
class BasinWeights:
    """Exact weights of one basin on one grid (sparse: only the cells it touches)."""

    basin: Basin
    flat: np.ndarray  # flat indices into the (ny, nx) grid
    fraction: np.ndarray  # fraction of each cell inside the basin
    area_km2: np.ndarray  # true area of the basin part in each cell
    geodesic_area_km2: float

    @property
    def area_total_km2(self) -> float:
        return float(self.area_km2.sum())

    @property
    def area_error(self) -> float:
        """Relative difference of the grid area and the geodesic polygon area."""
        return (self.area_total_km2 - self.geodesic_area_km2) / max(self.geodesic_area_km2, 1e-9)


def basin_weights(basin: Basin, grid: QpeGrid) -> BasinWeights:
    """Exact cell fractions and true areas of ``basin`` on ``grid``."""
    import pyproj

    tr = pyproj.Transformer.from_crs("EPSG:4326", grid.proj4(), always_xy=True)
    polys_xy: list[Polygon] = []
    for poly in basin.polygons:
        rings = []
        for ring in poly:
            # densify long edges so the straight lines of the grid follow the geodesic closely
            ring = _densify(ring, 0.02)
            x, y = tr.transform(ring[:, 0], ring[:, 1])
            rings.append(np.column_stack([x, y]))
        polys_xy.append(rings)
    allpts = np.concatenate([r for p in polys_xy for r in p[:1]])
    half = grid.cell_m / 2.0
    xe = np.concatenate([grid.x - half, [grid.x[-1] + half]])
    ye = np.concatenate([grid.y - half, [grid.y[-1] + half]])
    i0 = max(int(np.searchsorted(xe, allpts[:, 0].min(), side="right")) - 1, 0)
    i1 = min(int(np.searchsorted(xe, allpts[:, 0].max(), side="left")), grid.x.size)
    j0 = max(int(np.searchsorted(ye, allpts[:, 1].min(), side="right")) - 1, 0)
    j1 = min(int(np.searchsorted(ye, allpts[:, 1].max(), side="left")), grid.y.size)
    nx = grid.shape[1]
    if i1 <= i0 or j1 <= j0:
        return BasinWeights(
            basin, np.array([], int), np.array([]), np.array([]), basin.geodesic_area_km2()
        )
    frac = polygon_fractions(polys_xy, xe[i0 : i1 + 1], ye[j0 : j1 + 1])
    jj, ii = np.nonzero(frac > 1e-9)
    flat = (jj + j0) * nx + (ii + i0)
    f = frac[jj, ii]
    cell_km2 = grid.cell_areas_km2().ravel()[flat]
    return BasinWeights(basin, flat.astype(np.int64), f, f * cell_km2, basin.geodesic_area_km2())


def _densify(ring: Ring, max_step_deg: float) -> Ring:
    seg = np.hypot(np.diff(ring[:, 0]), np.diff(ring[:, 1]))
    n = np.maximum(np.ceil(seg / max_step_deg).astype(int), 1)
    if int(n.max()) == 1:
        return ring
    out = []
    for k in range(len(seg)):
        t = np.arange(n[k]) / n[k]
        out.append(ring[k] + (ring[k + 1] - ring[k]) * t[:, None])
    out.append(ring[-1:])
    return np.vstack(out)


# ---------------------------------------------------------------------------
# Zonal statistics
# ---------------------------------------------------------------------------


def _wquantile(v: np.ndarray, w: np.ndarray, q: float) -> float:
    o = np.argsort(v)
    v, w = v[o], w[o]
    c = np.cumsum(w) - 0.5 * w
    c /= w.sum()
    return float(np.interp(q, c, v))


def zonal_stats(
    values: np.ndarray, bw: BasinWeights, thresholds: Sequence[float] = ()
) -> dict[str, Any]:
    """Area-weighted statistics of the 2-D field ``values`` over one basin."""
    v = np.asarray(values, dtype=np.float64).ravel()[bw.flat]
    w = bw.area_km2
    ok = np.isfinite(v)
    out: dict[str, Any] = {
        "id": bw.basin.id,
        "name": bw.basin.name,
        "area_km2": bw.area_total_km2,
        "geodesic_area_km2": bw.geodesic_area_km2,
        "coverage": float(w[ok].sum() / w.sum()) if w.sum() > 0 else 0.0,
        "cells": int(bw.flat.size),
    }
    if not ok.any():
        out.update(mean=None, max=None, min=None, std=None, p50=None, p90=None)
        for t in thresholds:
            out[f"area_ge_{t:g}_km2"] = 0.0
        return out
    vv, ww = v[ok], w[ok]
    mean = float(np.sum(vv * ww) / ww.sum())
    out.update(
        mean=mean,
        max=float(vv.max()),
        min=float(vv.min()),
        std=float(math.sqrt(max(np.sum(ww * (vv - mean) ** 2) / ww.sum(), 0.0))),
        p50=_wquantile(vv, ww, 0.5),
        p90=_wquantile(vv, ww, 0.9),
    )
    for t in thresholds:
        out[f"area_ge_{t:g}_km2"] = float(ww[vv >= t].sum())
    return out


# ---------------------------------------------------------------------------
# Time series while accumulating
# ---------------------------------------------------------------------------

#: cap on the per-cell rates kept in memory for exact post-hoc adjustment (bytes)
MAX_STORE_BYTES = 256 * 1024 * 1024


class BasinCollector:
    """Receives the composite rate at every node of an accumulation and keeps basin series."""

    def __init__(self, bset: BasinSet, grid: QpeGrid) -> None:
        self.bset, self.grid = bset, grid
        self.w = bset.weights(grid)
        self.times: list[np.datetime64] = []
        self.mean: list[list[float]] = [[] for _ in self.w]
        self.cover: list[list[float]] = [[] for _ in self.w]
        self.peak: list[list[float]] = [[] for _ in self.w]
        self.cells: list[list[np.ndarray]] = [[] for _ in self.w]
        self.store = True
        self._bytes = 0

    def add(self, t: np.datetime64, rate: np.ndarray) -> None:
        flat = np.asarray(rate, dtype=np.float32).ravel()
        self.times.append(t)
        for k, bw in enumerate(self.w):
            v = flat[bw.flat]
            ok = np.isfinite(v)
            wk = bw.area_km2
            tot = wk.sum()
            if ok.any() and tot > 0:
                self.mean[k].append(float(np.sum(v[ok] * wk[ok]) / wk[ok].sum()))
                self.cover[k].append(float(wk[ok].sum() / tot))
                self.peak[k].append(float(v[ok].max()))
            else:
                self.mean[k].append(math.nan)
                self.cover[k].append(0.0)
                self.peak[k].append(math.nan)
            if self.store:
                self.cells[k].append(v.copy())
                self._bytes += v.nbytes
        if self.store and self._bytes > MAX_STORE_BYTES:
            self.store = False
            self.cells = [[] for _ in self.w]

    def finish(self, result: Any) -> BasinSeries:
        return BasinSeries(self, result)


@dataclass
class BasinSeries:
    """Basin-average rainfall series and statistics of an accumulation."""

    times: list[np.datetime64]
    weights: list[BasinWeights]
    mean_rate: np.ndarray  # (n_basins, n_nodes) mm/h area-weighted
    coverage: np.ndarray  # (n_basins, n_nodes) fraction of the basin area with data
    peak_rate: np.ndarray  # (n_basins, n_nodes) mm/h maximum cell rate
    cum_mean: np.ndarray  # (n_basins, n_nodes) mm basin-mean accumulation so far
    stats: list[dict[str, Any]]
    exact_adjustment: bool
    cell_rates: list[np.ndarray] | None
    adjusted: dict[str, Any] | None = None
    thresholds: tuple[float, ...] = (10.0, 25.0, 50.0, 100.0)

    def __init__(self, col: BasinCollector, result: Any) -> None:
        self.times = col.times
        self.weights = col.w
        self.mean_rate = np.array(col.mean, dtype=np.float64)
        self.coverage = np.array(col.cover, dtype=np.float64)
        self.peak_rate = np.array(col.peak, dtype=np.float64)
        self.adjusted = None
        self.thresholds = (10.0, 25.0, 50.0, 100.0)
        dt = np.diff(np.array(col.times, dtype="datetime64[ns]").astype("int64")) / 1e9
        if col.store and col.cells[0]:
            self.cell_rates = [np.stack(c) for c in col.cells]
        else:
            self.cell_rates = None
        self.exact_adjustment = self.cell_rates is not None
        cum = np.zeros_like(self.mean_rate)
        for k in range(len(self.weights)):
            if self.cell_rates is not None:
                cum[k] = _cum_mean(self.cell_rates[k], self.weights[k].area_km2, dt, None)
            else:
                m = np.nan_to_num(self.mean_rate[k], nan=0.0)
                cum[k, 1:] = np.cumsum(0.5 * (m[1:] + m[:-1]) * dt / 3600.0)
        self.cum_mean = cum
        # the unadjusted series: a second adjustment starts from these, never from the first
        self._raw = (self.mean_rate.copy(), self.peak_rate.copy(), self.cum_mean.copy())
        self.stats = [
            self._stats_for(k, result.accumulation, result) for k in range(len(self.weights))
        ]

    def _stats_for(self, k: int, acc: np.ndarray, result: Any) -> dict[str, Any]:
        bw = self.weights[k]
        s = zonal_stats(acc, bw, self.thresholds)
        sig = zonal_stats(result.accumulation_sigma, bw)
        pk = int(np.nanargmax(self.mean_rate[k])) if np.isfinite(self.mean_rate[k]).any() else None
        s.update(
            accumulation_mean_mm=s.pop("mean"),
            accumulation_max_mm=s.pop("max"),
            accumulation_min_mm=s.pop("min"),
            accumulation_std_mm=s.pop("std"),
            accumulation_p50_mm=s.pop("p50"),
            accumulation_p90_mm=s.pop("p90"),
            accumulation_sigma_mm=sig["mean"],
            max_cell_rate_mm_h=float(np.nanmax(result.rate_max.ravel()[bw.flat]))
            if np.isfinite(result.rate_max.ravel()[bw.flat]).any()
            else None,
            peak_mean_rate_mm_h=float(self.mean_rate[k][pk]) if pk is not None else None,
            peak_mean_rate_time=prov.iso_utc(self.times[pk]) if pk is not None else None,
            mean_valid_fraction=zonal_stats(result.valid_fraction, bw)["mean"],
        )
        # keys with ge_ thresholds renamed to accumulation thresholds
        for t in self.thresholds:
            s[f"area_acc_ge_{t:g}mm_km2"] = s.pop(f"area_ge_{t:g}_km2")
        return s

    def summary(self) -> list[dict[str, Any]]:
        return self.stats

    def hyetograph(self, k: int, bin_minutes: float = 60.0) -> dict[str, list]:
        """Basin-mean rainfall per time bin (mm) from the cumulative series; the last bin is
        cut at the end of the period."""
        t = np.array(self.times, dtype="datetime64[ns]")
        sec = (t - t[0]).astype("int64") / 1e9
        width = bin_minutes * 60.0
        total = float(sec[-1])
        if total <= 0:
            return {"start": [], "end": [], "depth_mm": [], "intensity_mm_h": []}
        edges = np.append(np.arange(0.0, total, width), total)
        cum = np.interp(edges, sec, self.cum_mean[k])
        depth = np.diff(cum)
        hours = np.diff(edges) / 3600.0

        def stamp(e: float) -> str:
            return prov.iso_utc(t[0] + np.timedelta64(round(e * 1e9), "ns"))

        return {
            "start": [stamp(e) for e in edges[:-1]],
            "end": [stamp(e) for e in edges[1:]],
            "depth_mm": [float(d) for d in depth],
            "intensity_mm_h": [
                float(d / h) if h > 0 else 0.0 for d, h in zip(depth, hours, strict=True)
            ],
        }

    def rows(self, k: int) -> list[dict[str, Any]]:
        out = []
        for i, t in enumerate(self.times):
            out.append(
                {
                    "time": prov.iso_utc(t),
                    "mean_rate_mm_h": _nn(self.mean_rate[k][i]),
                    "peak_cell_rate_mm_h": _nn(self.peak_rate[k][i]),
                    "coverage": float(self.coverage[k][i]),
                    "cumulative_mm": _nn(self.cum_mean[k][i]),
                }
            )
        return out

    def apply_factor(self, factor: np.ndarray, label: dict[str, Any], result: Any) -> None:
        """Re-derive the basin series with a multiplicative gauge-adjustment factor field.

        Exact when the per-cell rates were kept in memory (``exact_adjustment``); otherwise the
        factor is applied as the cumulative-rate-weighted basin mean, which preserves the basin
        total but not the shape of the hyetograph (reported in ``adjusted``).
        """
        f = np.asarray(factor, dtype=np.float64).ravel()
        dt = np.diff(np.array(self.times, dtype="datetime64[ns]").astype("int64")) / 1e9
        self.mean_rate, self.peak_rate, self.cum_mean = (a.copy() for a in self._raw)
        for k, bw in enumerate(self.weights):
            fk = np.nan_to_num(f[bw.flat], nan=1.0)
            if self.cell_rates is not None:
                rates = self.cell_rates[k].astype(np.float64) * fk[None, :]
                w = bw.area_km2
                ok = np.isfinite(rates)
                num = np.where(ok, rates * w[None, :], 0.0).sum(axis=1)
                den = np.where(ok, w[None, :], 0.0).sum(axis=1)
                with np.errstate(invalid="ignore", divide="ignore"):
                    self.mean_rate[k] = np.where(den > 0, num / den, np.nan)
                self.peak_rate[k] = np.nanmax(np.where(ok, rates, -np.inf), axis=1)
                self.cum_mean[k] = _cum_mean(rates.astype(np.float32), w, dt, None)
            else:
                acc = result.accumulation.ravel()[bw.flat].astype(np.float64)
                w = bw.area_km2
                ok = np.isfinite(acc) & (acc > 0)
                eff = float(np.sum(w[ok] * acc[ok] * fk[ok]) / max(np.sum(w[ok] * acc[ok]), 1e-12))
                self.mean_rate[k] = self.mean_rate[k] * eff
                self.peak_rate[k] = self.peak_rate[k] * eff
                self.cum_mean[k] = self.cum_mean[k] * eff
        self.adjusted = {**label, "exact": self.exact_adjustment}
        adj = result.accumulation_adjusted
        for k in range(len(self.weights)):
            s2 = zonal_stats(adj, self.weights[k], self.thresholds)
            self.stats[k]["accumulation_adjusted_mean_mm"] = s2["mean"]
            self.stats[k]["accumulation_adjusted_max_mm"] = s2["max"]
            for t in self.thresholds:
                self.stats[k][f"area_adj_acc_ge_{t:g}mm_km2"] = s2[f"area_ge_{t:g}_km2"]


def _nn(x: float) -> float | None:
    return None if x is None or not math.isfinite(x) else float(x)


def _cum_mean(rates: np.ndarray, w: np.ndarray, dt: np.ndarray, _unused: Any) -> np.ndarray:
    """Basin-mean cumulative depth (mm) at every node from per-cell rates ``(n_nodes, n_cells)``."""
    rates = rates.astype(np.float64)
    ok = np.isfinite(rates)
    both = ok[1:] & ok[:-1]
    inc = np.where(both, 0.5 * (np.nan_to_num(rates[1:]) + np.nan_to_num(rates[:-1])), 0.0)
    inc = inc * dt[:, None] / 3600.0
    cum = np.vstack([np.zeros((1, rates.shape[1])), np.cumsum(inc, axis=0)])
    ever = ok.any(axis=0)
    wv = np.where(ever, w, 0.0)
    return (cum * wv[None, :]).sum(axis=1) / max(wv.sum(), 1e-12)


# ---------------------------------------------------------------------------
# Frequency exceedance hook (return period)
# ---------------------------------------------------------------------------


@dataclass
class FrequencyTable:
    """Point precipitation-frequency depths of one duration: ``depth_mm`` at ``periods`` (years)."""

    duration_h: float
    periods: np.ndarray
    depth_mm: np.ndarray
    source: str = ""

    @classmethod
    def from_json(cls, path: str | Path, basin_id: str | None = None) -> FrequencyTable:
        """``{"duration_hours": 6, "depth_mm": {"2": 50, "10": 72}}`` or a ``basins`` map."""
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BasinError(f"cannot read the frequency table {path}: {exc}") from exc
        if "basins" in doc and basin_id is not None and basin_id in doc["basins"]:
            sub = {**doc, **doc["basins"][basin_id]}
        elif "depth_mm" in doc:
            sub = doc
        else:
            raise BasinError(f"no frequency table for basin {basin_id!r} in {path}")
        try:
            dur = float(sub["duration_hours"])
            items = sorted((float(k), float(v)) for k, v in sub["depth_mm"].items())
        except (KeyError, TypeError, ValueError) as exc:
            raise BasinError(
                "a frequency table is {'duration_hours': 6, 'depth_mm': {'2': 50.0, '10': 72.0}}"
            ) from exc
        return cls(dur, np.array([i[0] for i in items]), np.array([i[1] for i in items]), str(path))

    def return_period(self, depth_mm: float) -> dict[str, Any]:
        """Return period (years) of a depth by log-log interpolation; out-of-range is flagged."""
        d = float(depth_mm)
        if not math.isfinite(d) or d <= 0 or self.periods.size < 2:
            return {"return_period_years": None, "flag": "no value"}
        if d < self.depth_mm[0]:
            return {
                "return_period_years": float(self.periods[0]),
                "flag": f"below the {self.periods[0]:g}-year depth",
            }
        if d > self.depth_mm[-1]:
            return {
                "return_period_years": float(self.periods[-1]),
                "flag": f"above the {self.periods[-1]:g}-year depth",
            }
        t = float(np.exp(np.interp(np.log(d), np.log(self.depth_mm), np.log(self.periods))))
        return {"return_period_years": t, "flag": ""}


def exceedance(series: BasinSeries, table: FrequencyTable | str | Path) -> list[dict[str, Any]]:
    """Return period of the maximum ``duration_h`` basin-mean depth of each basin.

    The depth is the largest basin-mean accumulation in any window of ``table.duration_h`` hours
    inside the period. Precipitation-frequency grids are *point* depths: an area-mean depth is
    compared with them without an areal reduction factor, which makes the return period an
    **upper** bound of the true areal return period for large basins (documented).
    """
    t = np.array(series.times, dtype="datetime64[ns]")
    sec = (t - t[0]).astype("int64") / 1e9
    out = []
    for k in range(len(series.weights)):
        tab = (
            table
            if isinstance(table, FrequencyTable)
            else FrequencyTable.from_json(table, series.weights[k].basin.id)
        )
        cum = series.cum_mean[k]
        fine = np.arange(0.0, sec[-1] + 1.0, 60.0)
        cf = np.interp(fine, sec, cum)
        w = round(tab.duration_h * 60.0)
        if sec[-1] < tab.duration_h * 3600.0 - 1:
            depth, note = float(cf[-1] - cf[0]), f"period shorter than {tab.duration_h:g} h"
        else:
            depth, note = float(np.max(cf[w:] - cf[:-w])), ""
        rp = tab.return_period(depth)
        out.append(
            {
                "id": series.weights[k].basin.id,
                "duration_h": tab.duration_h,
                "max_depth_mm": depth,
                **rp,
                "note": note,
            }
        )
    return out

"""Basins: exact area weights, readers (GeoJSON, shapefile, GeoPackage), zonal statistics, series."""

from __future__ import annotations

import json
import math
import sqlite3
import struct

import numpy as np
import pyproj
import pytest
from engine.hydro import basins as hb
from engine.hydro import grid as hgrid

LAT, LON = 36.0, -88.0


@pytest.fixture(scope="module")
def grid():
    return hgrid.make_grid((-88.9, 35.4, -87.1, 36.6), 1000.0)


def _lonlat(grid, xy_km):
    """Polygon given in km of the grid's own metric -> lon/lat (so the true geometry is known)."""
    tr = pyproj.Transformer.from_crs(grid.proj4(), "EPSG:4326", always_xy=True)
    x, y = tr.transform(
        np.array([p[0] for p in xy_km]) * 1e3, np.array([p[1] for p in xy_km]) * 1e3
    )
    return list(zip(x, y, strict=True))


def _shoelace(xy):
    xy = np.asarray(xy, float)
    x, y = xy[:, 0], xy[:, 1]
    return 0.5 * abs(float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)))


# ---------------------------------------------------------------------------
# Exact fractions
# ---------------------------------------------------------------------------


class TestPolygonFractions:
    xe = np.arange(0.0, 11.0)  # 10 x 10 unit cells
    ye = np.arange(0.0, 11.0)

    def _frac(self, ring, holes=()):
        poly = [np.asarray(ring, float)] + [np.asarray(h, float) for h in holes]
        return hb.polygon_fractions([poly], self.xe, self.ye)

    def test_cell_aligned_square_is_exact(self):
        f = self._frac([(2, 2), (6, 2), (6, 5), (2, 5)])
        assert f.sum() == pytest.approx(12.0, abs=1e-12)
        assert f[2:5, 2:6].min() == pytest.approx(1.0)
        assert f.sum() - f[2:5, 2:6].sum() == pytest.approx(0.0, abs=1e-12)

    def test_half_cell_offsets(self):
        f = self._frac([(2.5, 2.5), (5.5, 2.5), (5.5, 4.0), (2.5, 4.0)])
        assert f.sum() == pytest.approx(3.0 * 1.5, abs=1e-12)
        assert f[2, 2] == pytest.approx(0.25)  # corner cell: 0.5 x 0.5
        assert f[3, 3] == pytest.approx(1.0)

    def test_orientation_does_not_matter(self):
        a = self._frac([(1.2, 1.1), (8.3, 2.0), (6.0, 7.7)])
        b = self._frac([(1.2, 1.1), (8.3, 2.0), (6.0, 7.7)][::-1])
        assert a.sum() == pytest.approx(_shoelace([(1.2, 1.1), (8.3, 2.0), (6.0, 7.7)]), rel=1e-12)
        np.testing.assert_allclose(a, b, atol=1e-12)

    def test_triangle_area_is_exact_not_pixel_counting(self):
        tri = [(0.0, 0.0), (10.0, 0.0), (0.0, 10.0)]
        f = self._frac(tri)
        assert f.sum() == pytest.approx(50.0, abs=1e-10)
        assert (f > 0.5).sum() != 50  # a pixel count would be wrong; the weights are exact

    def test_hole_is_subtracted(self):
        outer = [(1, 1), (9, 1), (9, 9), (1, 9)]
        hole = [(3, 3), (7, 3), (7, 7), (3, 7)]
        f = self._frac(outer, [hole])
        assert f.sum() == pytest.approx(64.0 - 16.0, abs=1e-10)
        assert f[4, 4] == pytest.approx(0.0)

    def test_circle(self):
        t = np.linspace(0, 2 * math.pi, 721)[:-1]
        ring = np.column_stack([5 + 3.3 * np.cos(t), 5 + 3.3 * np.sin(t)])
        f = self._frac(ring)
        poly_area = _shoelace(ring)
        assert f.sum() == pytest.approx(poly_area, abs=1e-9)
        assert poly_area == pytest.approx(math.pi * 3.3**2, rel=1e-4)

    def test_concave_polygon(self):
        u = [(1, 1), (9, 1), (9, 9), (6, 9), (6, 4), (4, 4), (4, 9), (1, 9)]
        f = self._frac(u)
        assert f.sum() == pytest.approx(_shoelace(u), abs=1e-10)

    def test_polygon_off_the_grid_contributes_only_its_inside(self):
        f = self._frac([(8, 8), (14, 8), (14, 14), (8, 14)])
        assert f.sum() == pytest.approx(4.0, abs=1e-10)


class TestBasinWeights:
    def test_area_matches_the_geodesic_polygon(self, grid):
        poly = [(-30, -20), (35, -25), (40, 22), (-10, 30)]
        bs = hb.BasinSet.from_polygon(_lonlat(grid, poly), "b")
        w = bs.weights(grid)[0]
        assert w.area_total_km2 == pytest.approx(_shoelace(poly), rel=2e-3)
        assert abs(w.area_error) < 2e-3
        assert w.fraction.max() <= 1.0 and w.fraction.min() > 0.0

    def test_multipolygon_and_hole_in_geojson(self, grid, tmp_path):
        outer = _lonlat(grid, [(-20, -20), (20, -20), (20, 20), (-20, 20)])
        hole = _lonlat(grid, [(-5, -5), (5, -5), (5, 5), (-5, 5)])
        island = _lonlat(grid, [(30, 30), (40, 30), (40, 40), (30, 40)])
        doc = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"huc12": "123", "name": "Elk Creek"},
                    "geometry": {
                        "type": "MultiPolygon",
                        "coordinates": [
                            [
                                [list(p) for p in [*outer, outer[0]]],
                                [list(p) for p in [*hole, hole[0]]],
                            ],
                            [[list(p) for p in [*island, island[0]]]],
                        ],
                    },
                }
            ],
        }
        path = tmp_path / "b.geojson"
        path.write_text(json.dumps(doc))
        bset = hb.BasinSet.from_file(path)
        assert bset.basins[0].id == "123" and bset.basins[0].name == "Elk Creek"
        w = bset.weights(grid)[0]
        assert w.area_total_km2 == pytest.approx(1600 - 100 + 100, rel=3e-3)

    def test_projected_coordinates_are_reprojected(self, tmp_path):
        # a 20 x 20 km square in Albers (EPSG:5070) around Kentucky, as a GeoJSON with a crs member
        tr = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
        cx, cy = tr.transform(-88.0, 36.5)
        ring = [
            (cx - 1e4, cy - 1e4),
            (cx + 1e4, cy - 1e4),
            (cx + 1e4, cy + 1e4),
            (cx - 1e4, cy + 1e4),
        ]
        doc = {
            "type": "Feature",
            "properties": {},
            "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in [*ring, ring[0]]]]},
        }
        path = tmp_path / "albers.geojson"
        path.write_text(json.dumps(doc))
        with pytest.raises(hb.BasinError, match="coordinate"):
            hb.BasinSet.from_file(path, crs="EPSG:99999")
        bset = hb.BasinSet.from_file(path, crs="EPSG:5070")  # equal-area: 400 km2 exactly
        assert bset.basins[0].geodesic_area_km2() == pytest.approx(400.0, rel=2e-3)
        g = hgrid.make_grid((-88.4, 36.2, -87.6, 36.8), 1000.0)
        assert bset.weights(g)[0].area_total_km2 == pytest.approx(400.0, rel=3e-3)

    def test_shapefile_reader(self, grid, tmp_path):
        ring = _lonlat(grid, [(-10, -10), (10, -10), (10, 10), (-10, 10)])
        # an ESRI polygon: exterior ring clockwise
        pts = np.array([*ring[::-1], ring[::-1][0]])
        shp = tmp_path / "ws.shp"
        _write_shapefile(shp, [pts], [{"NAME": "Shapefile Creek", "AREA": 400}])
        shp.with_suffix(".prj").write_text(pyproj.CRS.from_epsg(4326).to_wkt())
        bset = hb.BasinSet.from_file(shp)
        assert bset.basins[0].name == "Shapefile Creek"
        assert bset.weights(grid)[0].area_total_km2 == pytest.approx(400.0, rel=3e-3)

    def test_geopackage_reader(self, grid, tmp_path):
        ring = _lonlat(grid, [(-10, -10), (10, -10), (10, 10), (-10, 10)])
        gpkg = tmp_path / "ws.gpkg"
        _write_gpkg(gpkg, ring, "GPKG Creek")
        bset = hb.BasinSet.from_file(gpkg)
        assert bset.basins[0].name == "GPKG Creek"
        assert bset.weights(grid)[0].area_total_km2 == pytest.approx(400.0, rel=3e-3)

    def test_errors_say_what_to_do(self, tmp_path):
        with pytest.raises(hb.BasinError, match="not found"):
            hb.BasinSet.from_file(tmp_path / "none.geojson")
        p = tmp_path / "x.kml"
        p.write_text("<kml/>")
        with pytest.raises(hb.BasinError, match="unsupported"):
            hb.BasinSet.from_file(p)
        (tmp_path / "e.geojson").write_text('{"type":"FeatureCollection","features":[]}')
        with pytest.raises(hb.BasinError, match="no polygons"):
            hb.BasinSet.from_file(tmp_path / "e.geojson")


def _write_shapefile(path, rings, rows):
    """Minimal polygon shapefile (.shp/.shx/.dbf) writer for the test."""
    body = b""
    for i, ring in enumerate(rings):
        n = len(ring)
        content = struct.pack("<i4d2i", 5, *ring.min(axis=0), *ring.max(axis=0), 1, n)
        content += struct.pack("<i", 0) + ring.astype("<f8").tobytes()
        body += struct.pack(">2i", i + 1, len(content) // 2) + content
    allp = np.concatenate(rings)
    header = struct.pack(">7i", 9994, 0, 0, 0, 0, 0, (100 + len(body)) // 2)
    header += struct.pack("<2i8d", 1000, 5, *allp.min(axis=0), *allp.max(axis=0), 0, 0, 0, 0)
    path.write_bytes(header + body)
    fields = [("NAME", "C", 24), ("AREA", "N", 10)]
    rlen = 1 + sum(f[2] for f in fields)
    h = struct.pack("<4BIHH20x", 3, 24, 1, 1, len(rows), 32 + 32 * len(fields) + 1, rlen)
    for name, t, ln in fields:
        h += name.encode().ljust(11, b"\0") + t.encode() + b"\0" * 4 + struct.pack("<BB14x", ln, 0)
    h += b"\r"
    for r in rows:
        h += b" " + str(r["NAME"]).encode().ljust(24) + str(r["AREA"]).encode().rjust(10)
    path.with_suffix(".dbf").write_bytes(h)


def _write_gpkg(path, ring, name):
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT, srs_id INTEGER, organization TEXT,
            organization_coordsys_id INTEGER, definition TEXT);
        CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT,
            geometry_type_name TEXT, srs_id INTEGER, z INTEGER, m INTEGER);
        CREATE TABLE basins (fid INTEGER PRIMARY KEY, geom BLOB, name TEXT);
        """
    )
    con.execute("INSERT INTO gpkg_spatial_ref_sys VALUES ('WGS 84', 4326, 'EPSG', 4326, 'x')")
    con.execute("INSERT INTO gpkg_geometry_columns VALUES ('basins','geom','POLYGON',4326,0,0)")
    pts = np.array([*ring, ring[0]], dtype="<f8")
    wkb = struct.pack("<BIi", 1, 3, 1) + struct.pack("<I", len(pts)) + pts.tobytes()
    blob = b"GP" + bytes([0, 0b00000001]) + struct.pack("<i", 4326) + wkb
    con.execute("INSERT INTO basins VALUES (1, ?, ?)", (blob, name))
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Zonal statistics
# ---------------------------------------------------------------------------


class TestZonal:
    def test_area_weighted_mean_of_a_ramp_over_a_triangle(self, grid):
        tri = [(0, 0), (10, 0), (0, 10)]
        bs = hb.BasinSet.from_polygon(_lonlat(grid, tri), "t")
        w = bs.weights(grid)[0]
        xx, _ = np.meshgrid(grid.x / 1e3, grid.y / 1e3)
        stats = hb.zonal_stats(xx, w, thresholds=(5.5,))
        assert stats["area_km2"] == pytest.approx(50.0, rel=2e-3)
        assert stats["mean"] == pytest.approx(10.0 / 3.0, abs=0.03)  # centroid of the triangle
        assert stats["coverage"] == 1.0
        # cells whose centre is >= 5.5 km cover x > 5 km (cell edges are at half km): a triangle
        # leg 4.5 km long, area 4.5^2 / 2 (the threshold acts on cell values, as documented)
        assert stats["area_ge_5.5_km2"] == pytest.approx(4.5**2 / 2.0, rel=0.01)

    def test_nan_cells_reduce_coverage_not_the_mean(self, grid):
        sq = [(-5, -5), (5, -5), (5, 5), (-5, 5)]
        w = hb.BasinSet.from_polygon(_lonlat(grid, sq), "s").weights(grid)[0]
        field = np.full(grid.shape, 4.0)
        xx, _ = np.meshgrid(grid.x / 1e3, grid.y / 1e3)
        field[xx > 0] = np.nan
        s = hb.zonal_stats(field, w)
        assert s["mean"] == pytest.approx(4.0)
        # the cell centred on x = 0 still has a value: valid for x < 0.5 km -> 5.5 of 10 km
        assert s["coverage"] == pytest.approx(0.55, abs=0.005)

    def test_cell_area_is_true_area_not_a_count(self, grid):
        w = hb.BasinSet.from_polygon(
            _lonlat(grid, [(-3, -3), (3, -3), (3, 3), (-3, 3)]), "s"
        ).weights(grid)[0]
        scale = grid.areal_scale().ravel()[w.flat]
        assert (scale >= 1.0).all()
        assert w.area_km2.sum() == pytest.approx(36.0 / scale.mean(), rel=1e-3)

    def test_weighted_percentiles(self):
        v = np.array([1.0, 2.0, 3.0, 4.0])
        w = np.array([1.0, 1.0, 1.0, 1.0])
        assert hb._wquantile(v, w, 0.5) == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# Return period hook
# ---------------------------------------------------------------------------


class TestFrequency:
    def test_log_log_interpolation(self):
        t = hb.FrequencyTable(6.0, np.array([2.0, 10.0]), np.array([50.0, 100.0]))
        r = t.return_period(50.0 * math.sqrt(2.0))
        assert r["return_period_years"] == pytest.approx(math.sqrt(20.0), rel=1e-9)
        assert t.return_period(40.0)["flag"].startswith("below")
        assert t.return_period(130.0)["flag"].startswith("above")
        assert t.return_period(float("nan"))["return_period_years"] is None

    def test_from_json_and_per_basin(self, tmp_path):
        p = tmp_path / "f.json"
        p.write_text(
            json.dumps({"duration_hours": 1, "basins": {"a": {"depth_mm": {"2": 30, "100": 90}}}})
        )
        assert hb.FrequencyTable.from_json(p, "a").depth_mm[1] == 90.0
        with pytest.raises(hb.BasinError, match="no frequency table"):
            hb.FrequencyTable.from_json(p, "zzz")

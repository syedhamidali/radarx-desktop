"""Tests for the multi-radar mosaic engine (engine/mosaic.py).

Synthetic volumes (flat fields at known values) check the merge, the products, the Web-Mercator
resampling, probing and exports; the real KATX volume (and a re-sited copy) is used for smoke tests
when available. Everything that needs ``radarx`` is skipped when it is not installed.
"""

from __future__ import annotations

import importlib.util
import io
import math
import os
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from PIL import Image

from engine import mosaic
from engine.reader import RadarReader
from tests import radar_data

HAS_RADARX = importlib.util.find_spec("radarx") is not None
needs_radarx = pytest.mark.skipif(not HAS_RADARX, reason="radarx is not installed")

ELEVATIONS = [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.5, 8.0, 10.0, 12.0, 15.0]
RE_EFF = 4.0 / 3.0 * 6371e3

KATX = str(radar_data.katx() or "/nonexistent/KATX20130717_195021_V06")
KATX_RESITED = str(Path(KATX).with_name("KATX_resited_demo.nc"))


# ---------------------------------------------------------------------------
# Synthetic volumes
# ---------------------------------------------------------------------------


def make_volume(
    lat: float,
    lon: float,
    value_fn,
    *,
    name: str = "SYN",
    n_range: int = 160,
    gate_m: float = 500.0,
    alt: float = 100.0,
    variable: str = "DBZH",
    t0: str = "2024-05-01T20:00:00",
) -> xr.DataTree:
    """Volume of ``len(ELEVATIONS)`` PPIs; ``value_fn(x_km, y_km, height_km_asl)`` per gate."""
    n_az = 360
    azimuth = np.arange(n_az) + 0.5
    range_m = gate_m / 2 + gate_m * np.arange(n_range)
    sweeps = {}
    for i, el in enumerate(ELEVATIONS):
        az = np.radians(azimuth)[:, None]
        r = range_m[None, :]
        s = r * np.cos(np.radians(el))
        height = (r * np.sin(np.radians(el)) + r**2 / (2 * RE_EFF) + alt) / 1e3
        vals = value_fn(s * np.sin(az) / 1e3, s * np.cos(az) / 1e3, height)
        vals = np.array(np.broadcast_to(vals, (n_az, n_range)), dtype=np.float32)
        sweeps[f"/sweep_{i}"] = xr.Dataset(
            {
                variable: (("azimuth", "range"), vals, {"units": "dBZ"}),
                "sweep_mode": ((), "azimuth_surveillance"),
                "sweep_fixed_angle": ((), float(el)),
            },
            coords={
                "azimuth": ("azimuth", azimuth),
                "range": ("range", range_m.astype(np.float32)),
                "elevation": ("azimuth", np.full(n_az, el)),
                "time": (
                    "azimuth",
                    np.datetime64(t0) + np.arange(n_az).astype("timedelta64[ms]") * 10,
                ),
            },
        )
    root = xr.Dataset(
        coords={"latitude": np.float64(lat), "longitude": np.float64(lon), "altitude": alt},
        attrs={"instrument_name": name},
    )
    return xr.DataTree.from_dict({"/": root, **sweeps})


def make_reader(tree: xr.DataTree) -> RadarReader:
    reader = RadarReader()
    reader._dtree = tree
    reader._format = "synthetic"
    return reader


def flat(value: float):
    return lambda x, y, h: np.full(np.broadcast(x, y, h).shape, value, dtype=np.float32)


def layered(value: float, top_km: float):
    """``value`` below ``top_km`` (asl), -10 dBZ above."""
    return lambda x, y, h: np.where(h < top_km, value, -10.0)


@pytest.fixture()
def cache():
    return mosaic.MosaicCache()


def decode(png: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))


A = (35.0, -97.0)
B = (35.0, -95.9)  # about 100 km east of A


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_echo_top_interpolates_between_levels(self):
        z = np.array([1000.0, 2000.0, 3000.0, 4000.0])
        vals = np.full((4, 1, 3), np.nan, dtype=np.float32)
        vals[:, 0, 0] = [40, 30, 10, -20]  # top between 2 km (30) and 3 km (10): 18 dBZ at 2.6 km
        vals[:, 0, 1] = [40, 40, 40, 40]  # echo to the top level: + half a level
        vals[:, 0, 2] = [5, 5, 5, 5]  # never reaches the threshold
        top, k = mosaic._echo_top(vals, z, 18.0)
        assert top[0, 0] == pytest.approx(2600.0, abs=1.0)
        assert k[0, 0] == 1
        assert top[0, 1] == pytest.approx(4500.0)
        assert np.isnan(top[0, 2])

    def test_composite_and_level_index(self):
        vals = np.full((3, 1, 2), np.nan, dtype=np.float32)
        vals[:, 0, 0] = [1, 9, 3]
        comp, idx = mosaic._composite(vals)
        assert comp[0, 0] == 9 and idx[0, 0] == 1
        assert np.isnan(comp[0, 1])

    def test_bilinear_does_not_bleed_nan(self):
        field = np.full((4, 4), np.nan, dtype=np.float32)
        field[1:3, 1:3] = 10.0
        fx = np.array([[1.5, 0.0, 5.0, 1.0]])
        fy = np.array([[1.5, 0.0, 1.5, 1.0]])
        out = mosaic._bilinear_nan(field, fx, fy)
        assert out[0, 0] == pytest.approx(10.0)  # inside the patch
        assert np.isnan(out[0, 1])  # on NaN cells
        assert np.isnan(out[0, 2])  # outside the grid
        assert out[0, 3] == pytest.approx(10.0)  # on a valid node

    def test_render_png_transparency_and_lut(self):
        lut, name = mosaic._build_lut("NWSRef")
        assert name == "NWSRef" and lut.shape == (256, 4)
        img = np.array([[np.nan, -50.0, 0.0, 200.0]], dtype=np.float32)
        rgba = decode(mosaic.render_png(img, lut, -20.0, 75.0))
        assert rgba.shape == (1, 4, 4)
        assert list(rgba[0, :, 3]) == [0, 0, 255, 255]  # NaN and below-range transparent
        assert tuple(rgba[0, 3, :3]) == tuple(lut[255, :3])  # saturates at the top colour

    def test_unknown_colormap_falls_back(self):
        lut, name = mosaic._build_lut("does_not_exist")
        assert name in ("HomeyerRainbow", "viridis") and lut.shape == (256, 4)

    def test_legend(self):
        lut, _ = mosaic._build_lut("NWSRef")
        legend = mosaic._legend(lut, -20.0, 75.0)
        assert len(legend) == 16
        assert legend[0][0] == -20.0 and legend[-1][0] == 75.0
        assert all(len(c) == 7 and c.startswith("#") for _, c in legend)

    def test_world_file_matches_corners(self):
        rec = _fake_record(image=np.zeros((10, 20), dtype=np.float32))
        lines = [float(v) for v in mosaic.world_file(rec).split()]
        dx, _, _, neg_dy, x0, y0 = lines
        west, _, east, north = rec.bbox
        assert dx == pytest.approx(mosaic.MERC_R * math.radians(east - west) / 20)
        assert neg_dy < 0
        assert x0 - dx / 2 == pytest.approx(mosaic.MERC_R * math.radians(west))
        assert y0 + abs(neg_dy) / 2 == pytest.approx(float(mosaic._merc_y(north)))

    def test_image_axes_are_mercator_uniform(self):
        lon, lat = mosaic._image_axes((-100.0, 30.0, -90.0, 40.0), 50, 80)
        assert np.allclose(np.diff(lon), np.diff(lon)[0])
        ym = mosaic._merc_y(lat)
        assert np.allclose(np.diff(ym), np.diff(ym)[0], rtol=1e-6)
        assert lat[0] > lat[-1]  # north first

    def test_unique_names(self):
        assert mosaic._unique(["A", "B", "A", "A"]) == ["A", "B", "A_2", "A_3"]

    def test_specs_validate(self):
        with pytest.raises(mosaic.MosaicError):
            mosaic.GridSpec.from_dict({"resolution_m": 100})
        with pytest.raises(mosaic.MosaicError):
            mosaic.GridSpec.from_dict({"resolution_m": 6000})
        with pytest.raises(mosaic.MosaicError):
            mosaic.MergeSpec.from_dict({"beamwidth_deg": 0})
        with pytest.raises(mosaic.MosaicError):
            mosaic.MergeSpec.from_dict({"motion": "fast"})
        spec = mosaic.MergeSpec.from_dict({"motion": {"u": 1, "v": -2}, "advect": True})
        assert spec.motion == (1.0, -2.0)

    def test_parse_bbox(self):
        assert mosaic._parse_bbox({"west": -98, "south": 34, "east": -96, "north": 36}) == (
            -98.0,
            34.0,
            -96.0,
            36.0,
        )
        for bad in (
            {"west": -96, "south": 34, "east": -98, "north": 36},
            {"west": -98, "south": 34, "east": -96},
            {"west": -98, "south": 34, "east": -96, "north": 89},
            "somewhere",
        ):
            with pytest.raises(mosaic.MosaicError):
                mosaic._parse_bbox(bad)


def _fake_record(image=None, mosaic_id="x", nbytes_pad=0) -> mosaic.MosaicRecord:
    image = np.zeros((8, 8), dtype=np.float32) if image is None else image
    return mosaic.MosaicRecord(
        mosaic_id=mosaic_id,
        product="cappi",
        variable="DBZH",
        units="dBZ",
        height_m=3000.0,
        threshold_dbz=None,
        resolution_m=1000.0,
        image=np.zeros(nbytes_pad // 4 or 1, dtype=np.float32).reshape(1, -1)
        if nbytes_pad
        else image,
        bbox=(-98.0, 34.0, -96.0, 36.0),
        origin=(35.0, -97.0),
        x0=0.0,
        y0=0.0,
        cell_m=1000.0,
        radar_values=np.zeros((1, 2, 2), dtype=np.float32),
        level_m=np.zeros((2, 2), dtype=np.float32),
        radars=[],
        overlap_fraction=0.0,
        vmin=-20.0,
        vmax=75.0,
        cmap="NWSRef",
        legend=[],
        png=b"",
        stats={},
        analysis_time=None,
        merge={},
        grid_info={},
    )


class TestCache:
    def test_lru_by_count(self):
        cache = mosaic.MosaicCache(max_items=4)
        for i in range(6):
            cache.put(_fake_record(mosaic_id=f"m{i}"))
        assert len(cache) == 4
        with pytest.raises(mosaic.MosaicError, match="Unknown or expired"):
            cache.get("m0")
        assert cache.get("m5").mosaic_id == "m5"

    def test_get_refreshes_recency(self):
        cache = mosaic.MosaicCache(max_items=2)
        cache.put(_fake_record(mosaic_id="a"))
        cache.put(_fake_record(mosaic_id="b"))
        cache.get("a")
        cache.put(_fake_record(mosaic_id="c"))
        assert cache.get("a")
        with pytest.raises(mosaic.MosaicError):
            cache.get("b")

    def test_memory_bound_keeps_newest(self):
        cache = mosaic.MosaicCache(max_items=4, max_bytes=3_000_000)
        for i in range(3):
            cache.put(_fake_record(mosaic_id=f"big{i}", nbytes_pad=2_000_000))
        assert len(cache) == 1 and cache.get("big2")


# ---------------------------------------------------------------------------
# Synthetic mosaics
# ---------------------------------------------------------------------------


GRID = {"resolution_m": 1000, "max_range_km": 80, "z_max_m": 12000, "z_step_m": 1000}


@needs_radarx
class TestSyntheticMosaic:
    def test_single_radar_cappi(self, cache):
        rd = make_reader(make_volume(*A, flat(30.0), name="RAD1"))
        reports = []
        rec = mosaic.build_mosaic(
            [("f1", rd)],
            "DBZH",
            "cappi",
            GRID,
            height_m=2000,
            progress=lambda p, m: reports.append(p),
            cache=cache,
        )
        h = rec.header()
        assert h["product"] == "cappi" and h["units"] == "dBZ" and h["height_m"] == 2000
        assert h["vmin"] == -20.0 and h["vmax"] == 75.0 and h["cmap"] == "NWSRef"
        assert len(h["legend"]) == 16
        g = h["grid"]
        assert (g["nx"], g["ny"]) == (rec.image.shape[1], rec.image.shape[0])
        assert g["west"] < -97.0 < g["east"] and g["south"] < 35.0 < g["north"]
        w, s, e, n = g["west"], g["south"], g["east"], g["north"]
        assert h["corners"] == [[w, n], [e, n], [e, s], [w, s]]
        # 80 km radius: the disc fills pi/4 of its box, minus the cone of silence
        assert 0.5 < h["stats"]["valid_fraction"] < 0.85
        assert h["stats"]["min"] == pytest.approx(30.0, abs=0.5)
        assert h["stats"]["max"] == pytest.approx(30.0, abs=0.5)
        (r,) = h["radars"]
        assert r["name"] == "RAD1" and r["file_id"] == "f1" and r["share"] == pytest.approx(1.0)
        assert r["latitude"] == pytest.approx(35.0) and r["bias_db"] is None
        assert r["volume_time"].startswith("2024-05-01T20:00:0")
        assert h["overlap_fraction"] == 0.0
        assert reports[0] <= 10 and reports == sorted(reports) and reports[-1] < 100
        # PNG: right size, transparent outside the disc, opaque in the middle
        rgba = decode(rec.png)
        assert rgba.shape[:2] == rec.image.shape
        assert rgba[0, 0, 3] == 0
        centre = rgba[rgba.shape[0] // 2, rgba.shape[1] // 2 + 10]
        assert centre[3] == 255
        assert mosaic.MOSAICS is not cache and cache.get(rec.mosaic_id) is rec

    def test_pixel_size_is_about_resolution(self, cache):
        rd = make_reader(make_volume(*A, flat(30.0)))
        rec = mosaic.build_mosaic([("f", rd)], "DBZH", "cappi", GRID, cache=cache)
        west, south, east, north = rec.bbox
        ny, nx = rec.image.shape
        px_m = (east - west) * 111_320.0 * math.cos(math.radians(35.0)) / nx
        py_m = (north - south) * 110_574.0 / ny
        assert px_m == pytest.approx(1000.0, rel=0.05)
        assert py_m == pytest.approx(1000.0, rel=0.05)

    def test_georeferencing_is_exact_for_image_corners(self, cache):
        """A storm 30 km east / 20 km north of the radar lands on its true lon/lat."""
        import pyproj

        def storm(x, y, h):
            return np.where(np.hypot(x - 30.0, y - 20.0) < 6.0, 40.0, -33.0)

        rd = make_reader(make_volume(*A, storm))
        rec = mosaic.build_mosaic([("f", rd)], "DBZH", "cappi", GRID, height_m=1500, cache=cache)
        ys, xs = np.nonzero(rec.image > 20)
        assert ys.size > 20
        west, south, east, north = rec.bbox
        ny, nx = rec.image.shape
        yt, yb = float(mosaic._merc_y(north)), float(mosaic._merc_y(south))
        lon = west + (xs.mean() + 0.5) / nx * (east - west)
        lat = float(mosaic._merc_lat(np.array(yt - (ys.mean() + 0.5) / ny * (yt - yb))))
        dist = math.hypot(30e3, 20e3)
        az = math.degrees(math.atan2(30.0, 20.0))
        want_lon, want_lat, _ = pyproj.Geod(ellps="WGS84").fwd(A[1], A[0], az, dist)
        _, _, err = pyproj.Geod(ellps="WGS84").inv(lon, lat, want_lon, want_lat)
        assert err < 1500.0

    def test_two_radars_merge_between_inputs(self, cache):
        lo = make_reader(make_volume(*A, flat(20.0), name="LOW"))
        hi = make_reader(make_volume(*B, flat(40.0), name="HIGH"))
        rec = mosaic.build_mosaic(
            [("a", lo), ("b", hi)],
            "DBZH",
            "cappi",
            GRID,
            {"time_scale_s": 0},
            height_m=2000,
            cache=cache,
        )
        assert rec.merge["applied"] is True and rec.merge["average"] == "linear"
        img = rec.image
        finite = img[np.isfinite(img)]
        assert finite.min() >= 20.0 - 0.5 and finite.max() <= 40.0 + 0.5
        # only the overlap lies strictly between the two inputs
        assert ((finite > 21) & (finite < 39)).sum() > 100
        # near each radar its own value wins
        by_name = {r.name: r for r in rec.radars}
        assert by_name["LOW"].share + by_name["HIGH"].share == pytest.approx(1.0)
        assert 0.3 < by_name["LOW"].share < 0.7
        assert 0.1 < rec.overlap_fraction < 0.9
        # probe next to LOW is ~20, next to HIGH ~40, far outside NaN -> None
        near_a = mosaic.probe(rec.mosaic_id, -97.0, 35.3, cache)
        near_b = mosaic.probe(rec.mosaic_id, -95.9, 35.3, cache)
        assert near_a["value"] == pytest.approx(20.0, abs=1.5)
        assert near_b["value"] == pytest.approx(40.0, abs=1.5)
        assert mosaic.probe(rec.mosaic_id, -80.0, 10.0, cache)["value"] is None

    def test_outside_coverage_is_nan(self, cache):
        rd = make_reader(make_volume(*A, flat(30.0)))
        rec = mosaic.build_mosaic([("f", rd)], "DBZH", "composite", GRID, cache=cache)
        assert np.isnan(rec.image[0, 0]) and np.isnan(rec.image[-1, -1])
        assert np.isfinite(rec.image[rec.image.shape[0] // 2, rec.image.shape[1] // 2 + 10])

    def test_cells_without_any_echo_stay_empty_after_the_merge(self, cache):
        """radarx gives covered cells without echo a low 'no echo' value for the blend; where no
        radar measured anything the mosaic must stay transparent, not show a flat plate."""

        def blob(x, y, h):  # one small echo, nothing elsewhere (undefined = no echo)
            return np.where(np.hypot(x - 10.0, y) < 12.0, 35.0, np.nan).astype(np.float32)

        r1 = make_reader(make_volume(*A, blob, name="ONE"))
        r2 = make_reader(make_volume(*B, blob, name="TWO"))
        rec = mosaic.build_mosaic(
            [("a", r1), ("b", r2)], "DBZH", "composite", GRID, {"time_scale_s": 0}, cache=cache
        )
        finite = rec.image[np.isfinite(rec.image)]
        assert finite.size > 0 and finite.max() == pytest.approx(35.0, abs=1.0)
        assert finite.min() > 0.0, "a no-echo fill value leaked into the mosaic"
        # only the two small echoes are drawn, not the radars' whole coverage discs
        assert np.isfinite(rec.image).mean() < 0.1

    def test_unmerged_takes_maximum(self, cache):
        lo = make_reader(make_volume(*A, flat(20.0), name="LOW"))
        hi = make_reader(make_volume(*B, flat(40.0), name="HIGH"))
        rec = mosaic.build_mosaic(
            [("a", lo), ("b", hi)],
            "DBZH",
            "cappi",
            GRID,
            {"enabled": False},
            height_m=2000,
            cache=cache,
        )
        assert rec.merge["applied"] is False
        assert np.nanmax(rec.image) == pytest.approx(40.0, abs=0.5)
        # in the overlap the max (40) is shown, so HIGH wins every contested cell
        by = {r.name: r for r in rec.radars}
        assert by["HIGH"].share > by["LOW"].share

    def test_composite_is_column_max(self, cache):
        # echo only above 6 km asl: cappi at 2 km sees nothing, the composite sees it
        rd = make_reader(make_volume(*A, lambda x, y, h: np.where(h > 6.0, 35.0, -33.0)))
        cappi = mosaic.build_mosaic([("f", rd)], "DBZH", "cappi", GRID, height_m=2000, cache=cache)
        comp = mosaic.build_mosaic([("f", rd)], "DBZH", "composite", GRID, cache=cache)
        assert cappi.stats["valid_fraction"] == 0.0 and cappi.stats["max"] is None
        assert comp.stats["max"] == pytest.approx(35.0, abs=0.5)
        assert comp.stats["valid_fraction"] > 0.1

    def test_echo_top_height(self, cache):
        rd = make_reader(make_volume(*A, layered(40.0, 6.0)))
        rec = mosaic.build_mosaic(
            [("f", rd)], "DBZH", "echo_top", GRID, threshold_dbz=18.0, cache=cache
        )
        assert rec.units == "km" and rec.vmin == 0.0 and rec.vmax == 12.0
        assert rec.threshold_dbz == 18.0 and rec.height_m is None
        # 40 km out the beams bracket the 6 km layer top; close in the cone of silence truncates it
        centre = rec.image[rec.image.shape[0] // 2, rec.image.shape[1] // 2 + 40]
        assert 4.5 < centre < 7.5
        assert 0.0 < np.nanmax(rec.image) < 12.0

    def test_echo_top_needs_reflectivity(self, cache):
        rd = make_reader(make_volume(*A, flat(5.0), variable="VRADH"))
        with pytest.raises(mosaic.MosaicError, match="reflectivity"):
            mosaic.build_mosaic([("f", rd)], "VRADH", "echo_top", GRID, cache=cache)

    def test_explicit_extent(self, cache):
        rd = make_reader(make_volume(*A, flat(30.0)))
        box = {"west": -97.5, "south": 34.6, "east": -96.5, "north": 35.4}
        rec = mosaic.build_mosaic(
            [("f", rd)], "DBZH", "cappi", {**GRID, "extent": box}, cache=cache
        )
        assert rec.bbox == (-97.5, 34.6, -96.5, 35.4)
        g = rec.header()["grid"]
        assert (g["west"], g["south"], g["east"], g["north"]) == (-97.5, 34.6, -96.5, 35.4)
        assert rec.stats["valid_fraction"] > 0.9  # the box is well inside the 80 km disc
        # the cell under the radar is above the cone of silence at 3 km? the box centre is the site
        assert np.isfinite(rec.image).sum() > 0.9 * rec.image.size

    def test_variable_alias_and_cmap(self, cache):
        rd = make_reader(make_volume(*A, flat(30.0), variable="reflectivity"))
        rec = mosaic.build_mosaic([("f", rd)], "DBZH", "cappi", GRID, cache=cache)
        assert rec.stats["max"] == pytest.approx(30.0, abs=0.5)
        vel = make_reader(make_volume(*A, flat(5.0), variable="VRADH"))
        rv = mosaic.build_mosaic([("f", vel)], "VRADH", "cappi", GRID, cache=cache)
        assert rv.cmap == "NWSVel" and rv.vmin == -30.0

    def test_missing_variable(self, cache):
        rd = make_reader(make_volume(*A, flat(30.0)))
        with pytest.raises(mosaic.MosaicError, match="ZDR"):
            mosaic.build_mosaic([("f", rd)], "ZDR", "cappi", GRID, cache=cache)

    def test_bad_requests(self, cache):
        rd = make_reader(make_volume(*A, flat(30.0)))
        with pytest.raises(mosaic.MosaicError, match="at least one"):
            mosaic.build_mosaic([], "DBZH", "cappi", GRID, cache=cache)
        with pytest.raises(mosaic.MosaicError, match="product"):
            mosaic.build_mosaic([("f", rd)], "DBZH", "mystery", GRID, cache=cache)
        with pytest.raises(mosaic.MosaicError, match="vmax"):
            mosaic.build_mosaic([("f", rd)], "DBZH", "cappi", GRID, vmin=10, vmax=5, cache=cache)
        with pytest.raises(mosaic.MosaicError, match=r"closed|open"):
            mosaic.build_mosaic([("f", RadarReader())], "DBZH", "cappi", GRID, cache=cache)

    def test_huge_grid_is_refused(self, cache):
        rd = make_reader(make_volume(*A, flat(30.0)))
        box = {"west": -130.0, "south": 20.0, "east": -60.0, "north": 55.0}
        with pytest.raises(mosaic.MosaicError, match="increase the resolution"):
            mosaic.build_mosaic(
                [("f", rd)],
                "DBZH",
                "cappi",
                {**GRID, "resolution_m": 250, "extent": box},
                cache=cache,
            )

    def test_calibrate_and_advect_options(self, cache):
        lo = make_reader(make_volume(*A, flat(30.0), name="R0"))
        hi = make_reader(make_volume(*B, flat(33.0), name="R1"))
        rec = mosaic.build_mosaic(
            [("a", lo), ("b", hi)],
            "DBZH",
            "cappi",
            {**GRID, "z_max_m": 8000},
            {"calibrate": True, "advect": True},
            height_m=2000,
            cache=cache,
        )
        by = {r.name: r for r in rec.radars}
        assert by["R0"].bias_db == pytest.approx(0.0, abs=1e-6)  # the reference
        assert by["R1"].bias_db == pytest.approx(3.0, abs=0.7)
        assert rec.merge["calibrate"] is True and rec.merge["advect"] is False
        assert any("storm motion" in w for w in rec.warnings)
        # corrected radars agree: the merged field is ~30 dBZ wherever it is valid
        assert np.nanmax(rec.image) < 31.5

    def test_advection_with_motion(self, cache):
        a = make_reader(make_volume(*A, flat(30.0)))
        b = make_reader(make_volume(*B, flat(30.0), t0="2024-05-01T20:04:00"))
        rec = mosaic.build_mosaic(
            [("a", a), ("b", b)],
            "DBZH",
            "cappi",
            GRID,
            {"advect": True, "motion": [10, 5]},
            cache=cache,
        )
        assert rec.merge["advect"] is True and not rec.warnings

    def test_duplicate_names_are_made_unique(self, cache):
        a = make_reader(make_volume(*A, flat(30.0), name="SAME"))
        b = make_reader(make_volume(*B, flat(30.0), name="SAME"))
        rec = mosaic.build_mosaic([("a", a), ("b", b)], "DBZH", "cappi", GRID, cache=cache)
        assert [r.name for r in rec.radars] == ["SAME", "SAME_2"]

    def test_chunked_bands_equal_single_band(self, cache, monkeypatch):
        lo = make_reader(make_volume(*A, lambda x, y, h: 25 + 10 * np.sin(x / 9) * np.cos(y / 7)))
        hi = make_reader(make_volume(*B, flat(35.0)))
        pairs = [("a", lo), ("b", hi)]
        whole = mosaic.build_mosaic(pairs, "DBZH", "composite", GRID, cache=cache)
        monkeypatch.setattr(mosaic, "BAND_BUDGET_BYTES", 4_000_000)
        banded = mosaic.build_mosaic(pairs, "DBZH", "composite", GRID, cache=cache)
        assert np.allclose(whole.image, banded.image, equal_nan=True, atol=1e-4)
        assert [r.share for r in whole.radars] == pytest.approx([r.share for r in banded.radars])

    def test_radarx_missing(self, monkeypatch, cache):
        import builtins

        real = builtins.__import__

        def fake(name, *a, **k):
            if name.startswith("radarx"):
                raise ImportError("no radarx")
            return real(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake)
        rd = make_reader(make_volume(*A, flat(30.0)))
        with pytest.raises(mosaic.MosaicError, match="pip install radarx"):
            mosaic.build_mosaic([("f", rd)], "DBZH", "cappi", GRID, cache=cache)


@needs_radarx
class TestProbeAndExport:
    @pytest.fixture()
    def rec(self, cache):
        lo = make_reader(make_volume(*A, flat(20.0), name="LOW"))
        hi = make_reader(make_volume(*B, flat(40.0), name="HIGH"))
        return mosaic.build_mosaic(
            [("a", lo), ("b", hi)], "DBZH", "cappi", GRID, height_m=2000, cache=cache
        )

    def test_probe_agrees_with_image(self, rec, cache):
        west, south, east, north = rec.bbox
        ny, nx = rec.image.shape
        yt, yb = float(mosaic._merc_y(north)), float(mosaic._merc_y(south))
        hits = 0
        for row, col in [(ny // 2, nx // 2), (ny // 2, nx // 3), (ny // 3, nx // 2)]:
            lon = west + (col + 0.5) / nx * (east - west)
            lat = float(mosaic._merc_lat(np.array(yt - (row + 0.5) / ny * (yt - yb))))
            res = mosaic.probe(rec.mosaic_id, lon, lat, cache)
            want = float(rec.image[row, col])
            if math.isfinite(want):
                assert res["value"] == pytest.approx(want, abs=1e-4)
                hits += 1
            else:
                assert res["value"] is None
        assert hits >= 2

    def test_probe_radar_details(self, rec, cache):
        res = mosaic.probe(rec.mosaic_id, -96.45, 35.2, cache)
        assert res["units"] == "dBZ" and res["lon"] == -96.45
        names = [r["name"] for r in res["radars"]]
        assert names == ["LOW", "HIGH"]
        low, high = res["radars"]
        assert low["value"] == pytest.approx(20.0, abs=0.5)
        assert high["value"] == pytest.approx(40.0, abs=0.5)
        # both radars are ~50 km away; 2 km asl at 50 km is ~1.5 deg (4/3 earth)
        assert 40 < low["range_km"] < 60 and 40 < high["range_km"] < 60
        assert 0.5 < low["elevation_deg"] < 2.5 and 0.5 < high["elevation_deg"] < 2.5
        assert 20.0 < res["value"] < 40.0

    def test_probe_unknown_mosaic(self, cache):
        with pytest.raises(mosaic.MosaicError, match="Unknown or expired"):
            mosaic.probe("nope", 0.0, 0.0, cache)

    def test_netcdf_round_trip(self, rec, tmp_path):
        path = tmp_path / "m.nc"
        mosaic.write_netcdf(rec, path)
        with xr.open_dataset(path) as ds:
            name = "DBZH_cappi"
            assert ds[name].dims == ("y", "x")  # Web-Mercator metres, 2-D lat/lon alongside
            assert ds.attrs["Conventions"].startswith("CF-")
            assert ds[name].attrs["units"] == "dBZ" and ds[name].attrs["height_m"] == 2000.0
            assert float(ds.y[0]) < float(ds.y[-1])  # south -> north
            assert ds.lat.dims == ("y", "x") and float(ds.lat[0, 0]) < float(ds.lat[-1, 0])
            assert float(ds.lon[0, 0]) == pytest.approx(rec.bbox[0], abs=0.05)
            np.testing.assert_allclose(ds[name].values[::-1], rec.image, equal_nan=True)
            assert list(ds.radar_name.values) == ["LOW", "HIGH"]
            assert ds.radar_share.sum() == pytest.approx(1.0)
            assert ds.crs.attrs["grid_mapping_name"] == "mercator"

    def test_png_export_helpers(self, rec):
        assert len(mosaic.world_file(rec).split()) == 6
        assert "3857" in mosaic.projection_wkt() or "Mercator" in mosaic.projection_wkt()


# ---------------------------------------------------------------------------
# Real data
# ---------------------------------------------------------------------------


@needs_radarx
@pytest.mark.skipif(
    not (os.path.exists(KATX) and os.path.exists(KATX_RESITED)), reason="KATX test data missing"
)
class TestRealKatx:
    @pytest.fixture(scope="class")
    def readers(self):
        r1, r2 = RadarReader(), RadarReader()
        r1.open_file(KATX)
        r2.open_file(KATX_RESITED)
        return [("a", r1), ("b", r2)]

    def test_single_radar_all_products(self, readers, cache):
        one = readers[:1]
        cappi = mosaic.build_mosaic(one, "DBZH", "cappi", {"resolution_m": 2000}, cache=cache)
        comp = mosaic.build_mosaic(one, "DBZH", "composite", {"resolution_m": 2000}, cache=cache)
        top = mosaic.build_mosaic(one, "DBZH", "echo_top", {"resolution_m": 2000}, cache=cache)
        assert cappi.radars[0].name == "KATX"
        assert comp.stats["valid_fraction"] > 0 and top.stats["valid_fraction"] > 0
        # a column maximum is never below the constant-altitude value
        c0, c1 = cappi.radar_values[0], comp.radar_values[0]
        both = np.isfinite(c0) & np.isfinite(c1) & (c0 >= -20)
        assert both.sum() > 20
        assert (c1[both] >= c0[both] - 1e-3).all()
        assert top.stats["min"] >= 0.5 and top.stats["max"] <= 15.0
        # NEXRAD stores -33 dBZ for "no echo": it must not count as data
        assert comp.stats["min"] >= -20.0

    def test_two_radar_overlap_and_probe(self, readers, cache):
        rec = mosaic.build_mosaic(readers, "DBZH", "composite", {"resolution_m": 2000}, cache=cache)
        names = [r.name for r in rec.radars]
        assert names == ["KATX", "KATX_2"]
        assert rec.overlap_fraction > 0.3
        assert sum(r.share for r in rec.radars) == pytest.approx(1.0)
        assert all(0.2 < r.coverage_fraction < 1.0 for r in rec.radars)
        # the storm north-east of the radar: the merged value is bounded by both inputs there
        both = np.isfinite(rec.radar_values).all(axis=0)
        assert both.sum() > 100
        # probe a bright cell of the image
        iy, ix = np.unravel_index(np.nanargmax(rec.image), rec.image.shape)
        west, south, east, north = rec.bbox
        ny, nx = rec.image.shape
        yt, yb = float(mosaic._merc_y(north)), float(mosaic._merc_y(south))
        lon = west + (ix + 0.5) / nx * (east - west)
        lat = float(mosaic._merc_lat(np.array(yt - (iy + 0.5) / ny * (yt - yb))))
        res = mosaic.probe(rec.mosaic_id, lon, lat, cache)
        assert res["value"] == pytest.approx(float(rec.image[iy, ix]), abs=1e-4)
        vals = [r["value"] for r in res["radars"] if r["value"] is not None]
        assert vals and min(vals) - 3.0 <= res["value"] <= max(vals) + 3.0


def decode_volume(rec: mosaic.MosaicRecord) -> np.ndarray:
    """uint8 codes -> float (NaN = no data), the way the 3-D view reads them."""
    assert rec.volume is not None and rec.volume_range is not None
    lo, hi = rec.volume_range
    out = lo + (rec.volume.astype(np.float32) - 1.0) * (hi - lo) / 254.0
    out[rec.volume == 0] = np.nan
    return out


@needs_radarx
class TestVolume:
    def test_composite_keeps_the_merged_volume(self, cache):
        rd = make_reader(make_volume(*A, layered(40.0, 6.0)))
        rec = mosaic.build_mosaic([("f", rd)], "DBZH", "composite", GRID, cache=cache)
        z = np.arange(1000.0, 12001.0, 1000.0)
        assert rec.volume.dtype == np.uint8 and rec.volume.shape[0] == z.size
        assert rec.volume.shape[1:] == rec.level_m.shape
        np.testing.assert_allclose(rec.volume_z_m, z)
        assert rec.volume_range == (-20.0, 75.0) and rec.header()["has_volume"] is True
        vol = decode_volume(rec)
        ny, nx = vol.shape[1:]
        col = vol[:, ny // 2, nx // 2 + 40]  # 40 km east of the radar
        # echo (40 dBZ) up to the layer top at 6 km, nothing above
        assert np.nanmax(col[:5]) == pytest.approx(40.0, abs=0.5)
        assert not (col[8:] > 0).any()  # -10 dBZ above the layer, never an echo
        # the composite is the column maximum of this very volume
        assert np.nanmax(vol) == pytest.approx(np.nanmax(rec.image), abs=0.5)
        comp = np.max(np.where(np.isfinite(vol), vol, -np.inf), axis=0)
        assert np.isfinite(comp).sum() > 100

    def test_codes_round_trip_and_range(self):
        buf = np.array([[[np.nan, -30.0, -20.0, 0.0, 27.5, 75.0, 90.0]]], dtype=np.float16)
        codes = mosaic._quantise_volume(buf, -20.0, 75.0)[0, 0]
        assert list(codes[:2]) == [0, 0]  # NaN and below the range: no data
        assert codes[2] == 1 and codes[-2] == 255 and codes[-1] == 255  # saturates
        back = -20.0 + (codes[3:5].astype(float) - 1.0) * 95.0 / 254.0
        assert back == pytest.approx([0.0, 27.5], abs=0.2)

    def test_no_echo_rule_holds_on_every_level(self, cache):
        """Covered cells where no radar measured an echo are empty on all levels."""

        def blob(x, y, h):
            return np.where(np.hypot(x - 10.0, y) < 12.0, 35.0, np.nan).astype(np.float32)

        r1 = make_reader(make_volume(*A, blob, name="ONE"))
        r2 = make_reader(make_volume(*B, blob, name="TWO"))
        rec = mosaic.build_mosaic(
            [("a", r1), ("b", r2)], "DBZH", "composite", GRID, {"time_scale_s": 0}, cache=cache
        )
        vol = decode_volume(rec)
        finite = vol[np.isfinite(vol)]
        assert finite.size > 0 and finite.max() == pytest.approx(35.0, abs=1.0)
        assert finite.min() > 0.0, "a no-echo fill value leaked into the 3-D volume"
        per_level = np.isfinite(vol).mean(axis=(1, 2))
        assert per_level.max() < 0.1  # small echoes, not the radars' coverage discs

    def test_cappi_can_keep_the_volume_and_keeps_its_image(self, cache):
        rd = make_reader(make_volume(*A, layered(30.0, 5.0)))
        with_vol = mosaic.build_mosaic(
            [("f", rd)], "DBZH", "cappi", GRID, height_m=2500, cache=cache, keep_volume=True
        )
        without = mosaic.build_mosaic(
            [("f", rd)], "DBZH", "cappi", GRID, height_m=2500, cache=cache, keep_volume=False
        )
        assert without.volume is None and "not kept" in (without.volume_note or "")
        np.testing.assert_array_equal(with_vol.image, without.image)
        # the 2.5 km cappi level is not one of the volume levels (1 km steps)
        assert with_vol.volume.shape[0] == 12
        assert 2500.0 not in with_vol.volume_z_m.tolist()

    def test_too_large_a_volume_is_skipped_with_a_note(self, cache, monkeypatch):
        monkeypatch.setattr(mosaic, "VOLUME_MAX_VOXELS", 1000)
        rd = make_reader(make_volume(*A, flat(30.0)))
        rec = mosaic.build_mosaic([("f", rd)], "DBZH", "composite", GRID, cache=cache)
        assert rec.volume is None and rec.header()["has_volume"] is False
        assert any("3-D volume" in w for w in rec.warnings)
        assert np.isfinite(rec.image).any()  # the 2-D product is unaffected
        with pytest.raises(mosaic.MosaicError, match="not kept"):
            mosaic.volume_message(rec.mosaic_id, cache)

    def test_volume_counts_towards_the_cache_bound(self, cache):
        rd = make_reader(make_volume(*A, flat(30.0)))
        rec = mosaic.build_mosaic([("f", rd)], "DBZH", "composite", GRID, cache=cache)
        small = mosaic.build_mosaic(
            [("f", rd)], "DBZH", "composite", GRID, cache=cache, keep_volume=False
        )
        assert rec.nbytes - small.nbytes == rec.volume.nbytes

    def test_volume_message_geometry(self, cache):
        a = make_reader(make_volume(*A, flat(25.0), name="WEST"))
        b = make_reader(make_volume(*B, flat(25.0), name="EAST"))
        rec = mosaic.build_mosaic([("a", a), ("b", b)], "DBZH", "composite", GRID, cache=cache)
        header, vol = mosaic.volume_message(rec.mosaic_id, cache)
        g = header["grid"]
        assert vol.shape == (g["nz"], g["ny"], g["nx"]) and vol is rec.volume
        assert g["cell_m"] == 1000.0 and len(g["z"]) == g["nz"]
        assert header["dtype"] == "uint8" and header["scale"]["nodata"] == 0
        assert header["scale"]["step"] == pytest.approx(95.0 / 254.0)
        west, east = header["radars"]
        assert west["name"] == "WEST" and east["name"] == "EAST"
        # B is ~100 km east of A, both on the same latitude (the origin is the box centre)
        assert east["x_m"] - west["x_m"] == pytest.approx(100e3, rel=0.03)
        assert east["y_m"] == pytest.approx(west["y_m"], abs=2000.0)
        for r in header["radars"]:  # the radars sit inside the grid
            assert g["x0"] <= r["x_m"] <= g["x0"] + g["nx"] * g["cell_m"]
            assert g["y0"] <= r["y_m"] <= g["y0"] + g["ny"] * g["cell_m"]

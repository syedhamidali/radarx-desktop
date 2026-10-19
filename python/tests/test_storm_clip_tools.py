"""Tests of the data side of the storm-clip tool (tools/storm_clip/clipdata.py): time steps,
storm finding and tracking. Synthetic volumes only; no radar files are needed."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools" / "storm_clip"
sys.path.insert(0, str(TOOLS))

import clipdata
import clipwinds
import lightning_obs
import tilecache

UTC = timezone.utc


def touch(root: Path, radar: str, when: datetime) -> Path:
    d = root / radar
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{radar}{when:%Y%m%d_%H%M%S}_V06"
    f.write_bytes(b"x")
    return f


class TestSteps:
    def test_volume_time_from_name(self):
        t = clipdata.parse_volume_time("/x/KHPX20211211_020457_V06")
        assert t == datetime(2021, 12, 11, 2, 4, 57, tzinfo=UTC)
        assert clipdata.parse_volume_time("notes.txt") is None

    def test_list_volumes_sorted_and_read_only(self, tmp_path):
        t0 = datetime(2021, 12, 11, 2, 0, 0, tzinfo=UTC)
        touch(tmp_path, "KAAA", t0 + timedelta(minutes=6))
        touch(tmp_path, "KAAA", t0)
        (tmp_path / "results").mkdir()
        vols = clipdata.list_volumes(tmp_path)
        assert list(vols) == ["KAAA"]
        assert [v[0] for v in vols["KAAA"]] == [t0, t0 + timedelta(minutes=6)]

    def test_each_radar_contributes_its_nearest_volume(self, tmp_path):
        t0 = datetime(2021, 12, 11, 2, 0, 0, tzinfo=UTC)
        for k in range(3):
            touch(tmp_path, "REF1", t0 + timedelta(minutes=5.5 * k))
            touch(tmp_path, "OTH2", t0 + timedelta(minutes=5.5 * k + 2))
        touch(tmp_path, "FAR3", t0 + timedelta(minutes=1))
        steps = clipdata.choose_steps(clipdata.list_volumes(tmp_path), "REF1")
        assert len(steps) == 3
        assert steps[0].files["OTH2"][1] == pytest.approx(120.0)
        # FAR3 only has one volume: it is too far from the later steps and is left out of them
        assert "FAR3" in steps[0].files and "FAR3" not in steps[2].files
        assert [s.time for s in steps] == sorted(s.time for s in steps)

    def test_steps_that_repeat_the_volumes_of_most_radars_are_dropped(self, tmp_path):
        t0 = datetime(2021, 12, 11, 2, 0, 0, tzinfo=UTC)
        for k in range(3):
            touch(tmp_path, "REF1", t0 + timedelta(minutes=5.5 * k))
        # the other radars stop after the second volume: step 3 only changes the reference radar
        for k in range(2):
            touch(tmp_path, "OTH2", t0 + timedelta(minutes=5.5 * k + 1))
            touch(tmp_path, "OTH3", t0 + timedelta(minutes=5.5 * k + 2))
        steps = clipdata.choose_steps(clipdata.list_volumes(tmp_path), "REF1")
        assert len(steps) == 2

    def test_best_reference_is_the_radar_with_the_most_steps(self, tmp_path):
        t0 = datetime(2021, 12, 11, 2, 0, 0, tzinfo=UTC)
        for k in range(4):
            touch(tmp_path, "LONG", t0 + timedelta(minutes=5 * k))
            touch(tmp_path, "BOTH", t0 + timedelta(minutes=5 * k + 1))
        for k in range(2):
            touch(tmp_path, "SHRT", t0 + timedelta(minutes=5 * k + 2))
        ref, steps = clipdata.best_reference(clipdata.list_volumes(tmp_path))
        assert ref in ("LONG", "BOTH") and len(steps) == 4

    def test_needs_two_steps(self, tmp_path):
        touch(tmp_path, "REF1", datetime(2021, 12, 11, 2, 0, 0, tzinfo=UTC))
        with pytest.raises(ValueError, match="two"):
            clipdata.choose_steps(clipdata.list_volumes(tmp_path), "REF1")


def blob_volume(centres, nz=8, ny=60, nx=60, lo=-20.0, hi=75.0):
    """uint8 volume of Gaussian echo columns; ``centres`` = [(ix, iy, peak_dbz, top, sigma)]."""
    dbz = np.full((nz, ny, nx), np.nan, dtype=np.float32)
    jj, ii = np.mgrid[0:ny, 0:nx]
    for cx, cy, peak, top, sig in centres:
        g = np.exp(-(((ii - cx) ** 2 + (jj - cy) ** 2) / (2 * sig**2)))
        for k in range(top + 1):
            v = peak * g - 8.0 * (top - k) * 0 - 30.0 * (1 - g)
            dbz[k] = np.fmax(dbz[k], np.where(v > -10, v, np.nan))
    codes = np.zeros(dbz.shape, dtype=np.uint8)
    ok = np.isfinite(dbz) & (dbz >= lo)
    codes[ok] = np.clip(np.rint((dbz[ok] - lo) * 254.0 / (hi - lo)) + 1, 1, 255).astype(np.uint8)
    return codes


class TestStorm:
    Z = np.arange(1.0, 9.0)  # km

    def cells(self, centres, **kw):
        vol = blob_volume(centres)
        return clipdata.find_cells(vol, (-20.0, 75.0), self.Z, 0.0, 0.0, 1000.0, **kw)

    def test_finds_cells_and_ranks_the_strong_tall_large_one_first(self):
        cells = self.cells([(15, 15, 62, 7, 4.0), (45, 40, 56, 3, 5.0)])
        assert len(cells) == 2
        top = cells[0]
        assert top.max_dbz > 55 and top.top_km >= 7.0
        assert top.x_km == pytest.approx(15.0, abs=1.5) and top.y_km == pytest.approx(15.0, abs=1.5)
        assert cells[0].score > cells[1].score

    def test_small_echoes_below_the_area_limit_are_ignored(self):
        assert self.cells([(30, 30, 60, 5, 0.6)], min_area_km2=20.0) == []

    def test_a_weaker_echo_is_no_cell(self):
        assert self.cells([(30, 30, 38, 5, 4.0)], threshold_dbz=45.0) == []

    def test_tracking_follows_the_moving_storm_not_a_stronger_stationary_one(self):
        # the tracked cell moves 6 px (km) east per step; another cell stays put
        steps = []
        for k in range(3):
            steps.append(self.cells([(10 + 6 * k, 20, 61, 7, 4.0), (45, 45, 58, 4, 5.0)]))
        track = clipdata.track_storm(steps, [0.0, 330.0, 660.0])
        assert [round(c.x_km) for c in track] == [10, 16, 22]
        u, v = clipdata.storm_motion([(c.x_km, c.y_km) for c in track], [0.0, 330.0, 660.0])
        assert u == pytest.approx(12.0 * 1000 / 660.0, rel=0.1) and v == pytest.approx(0.0, abs=0.5)

    def test_a_lost_storm_is_carried_along_its_motion(self):
        steps = [
            self.cells([(10, 20, 61, 7, 4.0)]),
            self.cells([(16, 20, 61, 7, 4.0)]),
            [],  # nothing found in the last step
        ]
        track = clipdata.track_storm(steps, [0.0, 330.0, 660.0])
        assert len(track) == 3
        assert track[2].x_km == pytest.approx(22.0, abs=1.5)

    def test_no_cell_in_the_first_step_is_an_error(self):
        with pytest.raises(ValueError, match="first time step"):
            clipdata.track_storm([[], []], [0.0, 1.0])

    def test_linear_track_is_a_steady_motion_through_jumpy_cells(self):
        # a storm moving 6 km east per step whose found cell jumps about (and a repeated volume)
        cells = [
            clipdata.Cell(x, 20.0 + dy, 60.0, 500.0, 12.0, 1.0)
            for x, dy in ((10.0, 0.0), (10.0, 1.0), (22.0, -1.0), (22.5, 0.0))
        ]
        times = [0.0, 300.0, 600.0, 900.0]
        fit = clipdata.linear_track(cells, times)
        xs = [c.x_km for c in fit]
        steps = np.diff(xs)
        assert np.allclose(steps, steps[0])  # uniform
        assert steps[0] == pytest.approx(4.95, abs=0.05)  # the fitted speed, not the jumps
        assert (
            fit[0].max_dbz == 60.0 and fit[0].top_km == 12.0
        )  # the cell's other properties are kept
        u, _v = clipdata.storm_motion([(c.x_km, c.y_km) for c in fit], times)
        assert u == pytest.approx(steps[0] * 1000.0 / 300.0)
        assert clipdata.linear_track(cells[:1], times[:1]) == cells[:1]

    def ring_field(self, r_in=12.0, r_out=30.0, n=100, cell_m=2000.0, cx=0.0, cy=0.0):
        """Column-maximum reflectivity of a synthetic hurricane: an echo ring around an eye."""
        x0 = y0 = -n / 2 * cell_m
        jj, ii = np.mgrid[0:n, 0:n]
        xs = (x0 + ii * cell_m) / 1000.0
        ys = (y0 + jj * cell_m) / 1000.0
        r = np.hypot(xs - cx, ys - cy)
        comp = np.where((r >= r_in) & (r <= r_out), 45.0, np.nan)
        return comp, x0, y0, cell_m

    def test_find_eye_centre_and_radius_of_a_ring(self):
        comp, x0, y0, cm = self.ring_field(cx=4.0, cy=-6.0)
        eye = clipdata.find_eye(comp, x0, y0, cm, (30.0, 10.0))  # anchored on the wall, off centre
        assert eye is not None
        assert eye.x_km == pytest.approx(4.0, abs=2.5) and eye.y_km == pytest.approx(-6.0, abs=2.5)
        assert eye.radius_km == pytest.approx(12.0, abs=2.5)

    def test_open_water_is_not_an_eye(self):
        comp, x0, y0, cm = self.ring_field()
        # a patch of echo with no hole, far from the ring: the search near it finds nothing
        comp[:] = np.nan
        comp[40:60, 40:60] = 40.0
        assert clipdata.find_eye(comp, x0, y0, cm, (0.0, 0.0), search_km=60.0) is None
        assert clipdata.find_eye(np.full_like(comp, np.nan), x0, y0, cm, (0.0, 0.0)) is None

    def test_a_small_gap_is_not_an_eye(self):
        comp, x0, y0, cm = self.ring_field(r_in=2.0, r_out=20.0)
        assert clipdata.find_eye(comp, x0, y0, cm, (0.0, 0.0), min_radius_km=4.0) is None

    def test_box_around_points(self):
        assert clipdata.box_around([(0.0, 0.0), (10.0, -4.0)], 5.0) == (-5.0, -9.0, 15.0, 5.0)

    def test_decode(self):
        codes = np.array([[[0, 1, 255]]], dtype=np.uint8)
        out = clipdata.decode(codes, -20.0, 75.0)
        assert (
            np.isnan(out[0, 0, 0])
            and out[0, 0, 1] == pytest.approx(-20.0)
            and out[0, 0, 2] == pytest.approx(75.0)
        )


def test_out_folder_must_not_be_inside_the_case(tmp_path, capsys):
    case = tmp_path / "case"
    case.mkdir()
    with pytest.raises(SystemExit):
        clipdata.main(["--case", str(case), "--out", str(case / "bundle")])


class TestTileCache:
    """The disk-cached, polite tile proxy of the realistic clip (tools/storm_clip/tilecache.py)."""

    def test_url_and_path_mapping(self, tmp_path):
        tc = tilecache.TileCache(tmp_path)
        # the EOX WMTS order is {z}/{row}/{col} = {z}/{y}/{x}; Terrarium is {z}/{x}/{y}
        assert tc.url_for("eox", 11, 519, 798).endswith(
            "/s2cloudless-2021_3857/default/g/11/798/519.jpg"
        )
        assert tc.url_for("terrarium", 9, 130, 200) == (
            "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/9/130/200.png"
        )
        assert tc.path_for("eox", 11, 519, 798) == tmp_path / "eox" / "11" / "519" / "798.jpg"

    def test_parse_and_validation(self, tmp_path):
        tc = tilecache.TileCache(tmp_path)
        assert tc.parse("/tiles/eox/11/519/798") == ("eox", 11, 519, 798)
        assert tc.parse("/tiles/eox/11/519/798?x=1") == ("eox", 11, 519, 798)
        assert tc.parse("/bundle/manifest.json") is None
        assert tc.parse("/tiles/eox/11/519/abc") is None
        assert tc.valid("eox", 11, 519, 798)
        assert not tc.valid("evil", 1, 0, 0)  # unknown source: never forwarded
        assert not tc.valid("eox", 11, -1, 0)  # no path tricks through negative numbers
        assert not tc.valid("eox", 2, 4, 0)  # x outside the 2**z grid
        assert not tc.valid("eox", 20, 0, 0)  # above the highest zoom served
        assert tc.get("eox", 20, 0, 0) is None

    def test_a_tile_is_fetched_once_and_then_served_from_the_cache(self, tmp_path):
        calls: list[str] = []

        def fake(url: str) -> bytes:
            calls.append(url)
            return b"\x89PNG-fake"

        tc = tilecache.TileCache(tmp_path, fetch=fake)
        a = tc.get("terrarium", 9, 130, 200)
        b = tc.get("terrarium", 9, 130, 200)
        assert a == b == (b"\x89PNG-fake", "image/png")
        assert len(calls) == 1
        assert (tmp_path / "terrarium" / "9" / "130" / "200.png").is_file()
        assert tc.downloads == 1 and tc.hits == 1
        # a second cache object (a later render) finds it on disk without any request
        tc2 = tilecache.TileCache(tmp_path, fetch=lambda u: pytest.fail("network used"))
        assert tc2.get("terrarium", 9, 130, 200)[0] == b"\x89PNG-fake"

    def test_offline_mode_never_uses_the_network(self, tmp_path):
        tc = tilecache.TileCache(
            tmp_path, fetch=lambda u: pytest.fail("network used"), offline=True
        )
        assert tc.get("eox", 11, 519, 798) is None

    def test_failures_are_reported_by_host(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tilecache.time, "sleep", lambda s: None)

        def boom(url: str) -> bytes:
            raise OSError("blocked")

        tc = tilecache.TileCache(tmp_path, fetch=boom)
        assert tc.get("eox", 11, 519, 798) is None
        assert tc.failures == {"tiles.maps.eox.at": 1}
        assert "FAILED hosts: tiles.maps.eox.at" in tc.summary()

    def test_requests_say_who_asks(self):
        assert "xradar-desktop" in tilecache.USER_AGENT
        assert tilecache.MIN_INTERVAL_S > 0


class TestWinds:
    """The wind steps of the bundle (tools/storm_clip/clipwinds.py)."""

    def test_pack_layout_and_nan_rule(self):
        nz, ny, nx = 2, 3, 4
        u = np.arange(nz * ny * nx, dtype=np.float32).reshape(nz, ny, nx)
        v = -u
        u[0, 0, 0] = np.nan  # a cell with only one component is no wind
        x = np.arange(nx) * 2000.0 - 3000.0
        y = np.arange(ny) * 2000.0
        z = np.array([500.0, 1000.0])
        head, data = clipwinds.pack_wind({"u": u, "v": v}, x, y, z, (36.2, -89.8), 3, "analytic")
        assert (head["nx"], head["ny"], head["nz"]) == (nx, ny, nz)
        assert head["x0_km"] == -3.0 and head["dx_km"] == 2.0 and head["dz_km"] == 0.5
        assert head["origin"] == {"lat": 36.2, "lon": -89.8} and head["n_radars"] == 3
        assert len(data) == nz * ny * nx * 8
        flat = np.frombuffer(data, dtype="<f4")
        n = nz * ny * nx
        assert np.isnan(flat[0]) and np.isnan(flat[n])  # both blocks lose the cell
        assert flat[1] == 1.0 and flat[n + 1] == -1.0  # u block then v block
        assert head["valid_fraction"] == pytest.approx((n - 1) / n)

    def test_axes_must_match(self):
        u = np.zeros((1, 2, 2), dtype=np.float32)
        with pytest.raises(ValueError, match="disagree"):
            clipwinds.pack_wind(
                {"u": u, "v": u}, np.arange(3.0), np.arange(2.0), np.array([500.0]), (0, 0), 2, "m"
            )

    def test_fewer_than_two_radars_gives_no_winds(self):
        got = clipwinds.retrieve_step_winds(
            {"KAAA": Path("x")},
            (36.0, -90.0),
            (-10, -10, 10, 10),
            (5.0, 5.0),
            lambda p: None,
            log=lambda *a: None,
        )
        assert got is None


class TestObservedLightning:
    """Lightning files are read from disk only (tools/storm_clip/lightning_obs.py)."""

    def glm(self, tmp_path, base="2021-12-11T01:50:00Z"):
        import xarray as xr

        ds = xr.Dataset(
            {
                "flash_lat": ("number_of_flashes", np.array([36.1, 36.3, 40.0])),
                "flash_lon": ("number_of_flashes", np.array([-89.9, -89.7, -80.0])),
                "flash_time_offset_of_first_event": (
                    "number_of_flashes",
                    np.array([30.0, 400.0, 100.0]),
                ),
                "flash_energy": ("number_of_flashes", np.array([1e-14, 5e-14, 2e-14])),
                "product_time": ((), np.datetime64(base.replace("Z", ""))),
            }
        )
        path = tmp_path / "OR_GLM-L2-LCFA_G16_s20213450150000.nc"
        ds.to_netcdf(path)
        return path

    def test_glm_flashes_time_place_and_relative_strength(self, tmp_path):
        kind, fl = lightning_obs.read_flashes([self.glm(tmp_path)])
        assert kind == "glm" and len(fl) == 3
        assert fl[0].time == datetime(2021, 12, 11, 1, 50, 30, tzinfo=UTC)
        assert fl[1].time == datetime(2021, 12, 11, 1, 51, 40, tzinfo=UTC)  # sorted by time
        assert fl[2].time == datetime(2021, 12, 11, 1, 56, 40, tzinfo=UTC)
        assert (fl[0].lat, fl[0].lon) == (36.1, -89.9)
        energies = {round(f.lat, 1): f.energy for f in fl}
        assert energies[36.3] == pytest.approx(1.0) and energies[36.1] == pytest.approx(0.2)

    def test_bundle_keeps_the_window_and_the_box(self, tmp_path):
        _, fl = lightning_obs.read_flashes([self.glm(tmp_path)])
        t0 = datetime(2021, 12, 11, 1, 50, 0, tzinfo=UTC)
        t1 = datetime(2021, 12, 11, 2, 8, 0, tzinfo=UTC)
        # a flat 111 km per degree frame around (36, -90)
        to_xy = lambda lon, lat: ((lon + 90) * 90.0, (lat - 36) * 111.0)  # noqa: E731
        recs = lightning_obs.to_bundle(fl, t0, t1, to_xy, box_km=(-50.0, -50.0, 50.0, 50.0))
        assert len(recs) == 2  # the far flash (40 N) is outside the box
        assert recs[0]["t_s"] == pytest.approx(30.0) and recs[1]["t_s"] == pytest.approx(400.0)
        assert recs[0]["x"] == pytest.approx(9.0) and recs[0]["y"] == pytest.approx(11.1)
        late = lightning_obs.to_bundle(fl, t0, t0 + timedelta(seconds=60), to_xy)
        assert [r["t_s"] for r in late] == [pytest.approx(30.0)]

    def test_lma_flash_table_gives_the_initiation_point_and_height(self):
        import xarray as xr

        ds = xr.Dataset(
            {
                "flash_time_start": (
                    "number_of_flashes",
                    np.array(
                        ["2021-12-11T01:55:00", "2021-12-11T01:56:00"], dtype="datetime64[ns]"
                    ),
                ),
                "flash_init_latitude": ("number_of_flashes", np.array([36.2, 36.25])),
                "flash_init_longitude": ("number_of_flashes", np.array([-89.8, -89.75])),
                "flash_init_altitude": ("number_of_flashes", np.array([7500.0, np.nan])),
                "flash_event_count": ("number_of_flashes", np.array([20.0, 400.0])),
            }
        )
        fl = lightning_obs.flashes_from_lma_dataset(ds)
        assert fl[0].alt_km == pytest.approx(7.5) and fl[1].alt_km is None
        assert fl[1].energy > fl[0].energy
        assert fl[0].time == datetime(2021, 12, 11, 1, 55, 0, tzinfo=UTC)

    def test_nothing_is_downloaded_and_a_missing_file_says_so(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="nothing is downloaded"):
            lightning_obs.read_flashes([tmp_path / "OR_GLM-L2-LCFA_missing.nc"])
        with pytest.raises(ValueError, match="no lightning files"):
            lightning_obs.read_flashes([])

    def test_kinds_are_not_mixed(self, tmp_path):
        nc = self.glm(tmp_path)
        dat = tmp_path / "LYLOUT_211211_015000_0600.dat"
        dat.write_text("x")
        with pytest.raises(ValueError, match="not both"):
            lightning_obs.read_flashes([nc, dat])

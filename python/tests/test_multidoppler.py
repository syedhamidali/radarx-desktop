"""Tests for the multi-Doppler wind retrieval (engine/multidoppler.py and its server handlers).

There is no real two-radar dataset in the repo, so the retrieval is checked on SYNTHETIC volumes:
two radar sites ~70 km apart are "flown" through an imposed analytic wind field (uniform + shear,
a non-divergent vortex, a convergent anelastic flow with a vertical velocity) and the radial
velocities are forward-simulated onto each radar's polar sweeps. The retrieved u / v / w must
match the imposed wind in the well-observed (good beam-crossing) region. Everything that needs
``radarx`` is skipped when it is not installed.
"""

from __future__ import annotations

import importlib.util
import json
import math
import threading

import numpy as np
import pytest
import xarray as xr

import server
from engine import multidoppler as mdop
from engine.reader import RadarReader
from tests.test_server import _binary, _make_handler

HAS_RADARX = importlib.util.find_spec("radarx") is not None
needs_radarx = pytest.mark.skipif(not HAS_RADARX, reason="radarx is not installed")

RE_EFF = 4.0 / 3.0 * 6371e3
ELEVATIONS = [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.5, 8.0, 10.0, 12.0, 15.0, 19.0]
ORIGIN = (35.0, -97.0)
SEPARATION_KM = 70.0  # distance between the two radars

# ---------------------------------------------------------------------------
# Synthetic radar volumes
# ---------------------------------------------------------------------------


def _projections():
    import pyproj

    return (
        pyproj.Proj(proj="aeqd", lat_0=ORIGIN[0], lon_0=ORIGIN[1], datum="WGS84"),
        pyproj.Geod(ellps="WGS84"),
    )


def site_east_of_origin(km: float) -> tuple[float, float]:
    _, geod = _projections()
    lon, lat, _ = geod.fwd(ORIGIN[1], ORIGIN[0], 90.0, km * 1000.0)
    return float(lat), float(lon)


def make_wind_volume(
    lat: float,
    lon: float,
    wind,
    *,
    name: str,
    alt: float = 300.0,
    dbz: float | None = 30.0,
    fall_speed=None,
    n_az: int = 360,
    n_range: int = 220,
    gate_m: float = 500.0,
    velocity: bool = True,
) -> xr.DataTree:
    """Volume whose VRADH is what this radar measures in ``wind(x_km, y_km, z_km) -> (u, v, w)``.

    ``x, y`` are in the common grid (azimuthal equidistant about :data:`ORIGIN`); ``z`` is the
    height above sea level. ``fall_speed(z_km)`` (m/s, positive downward) is subtracted from w.
    """
    aeqd, geod = _projections()
    rx, ry = aeqd(lon, lat)
    az = np.arange(n_az) + 0.5
    rng = gate_m / 2 + gate_m * np.arange(n_range)
    sweeps = {}
    for i, el in enumerate(ELEVATIONS):
        shape = (n_az, n_range)
        s = rng[None, :] * np.cos(np.radians(el))
        h = rng[None, :] * np.sin(np.radians(el)) + rng[None, :] ** 2 / (2 * RE_EFF) + alt
        glon, glat, _ = geod.fwd(
            np.full(shape, lon),
            np.full(shape, lat),
            np.broadcast_to(az[:, None], shape),
            np.broadcast_to(s, shape),
        )
        gx, gy = aeqd(glon, glat)
        bearing = np.arctan2(gx - rx, gy - ry)
        el_local = np.radians(el) + s / RE_EFF
        u, v, w = wind(gx / 1e3, gy / 1e3, h / 1e3)
        if fall_speed is not None:
            w = w - fall_speed(h / 1e3)
        vr = (
            (u * np.sin(bearing) + v * np.cos(bearing)) * np.cos(el_local) + w * np.sin(el_local)
        )[...]
        data = {}
        if velocity:
            data["VRADH"] = (("azimuth", "range"), vr.astype(np.float32), {"units": "m/s"})
        if dbz is not None:
            data["DBZH"] = (
                ("azimuth", "range"),
                np.full(shape, dbz, dtype=np.float32),
                {"units": "dBZ"},
            )
        sweeps[f"/sweep_{i}"] = xr.Dataset(
            {
                **data,
                "sweep_mode": ((), "azimuth_surveillance"),
                "sweep_fixed_angle": ((), float(el)),
            },
            coords={
                "azimuth": ("azimuth", az),
                "range": ("range", rng.astype(np.float32)),
                "elevation": ("azimuth", np.full(n_az, el)),
                "time": (
                    "azimuth",
                    np.datetime64("2024-05-01T20:00:00")
                    + np.arange(n_az).astype("timedelta64[ms]") * 10,
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


# Imposed wind fields ---------------------------------------------------------------------


def shear_wind(x, y, z):
    """Uniform + vertical shear: u = 8 + 2 z[km], v = -5 + z[km]; no w."""
    zero = 0.0 * x
    return 8.0 + 2.0 * z + zero, -5.0 + z + zero, zero


VORTEX_Y_KM = 25.0  # the baseline itself has no crossing beams, so keep the vortex off it


def vortex_wind(x, y, z):
    """Non-divergent Gaussian vortex (L = 12 km, centred 25 km north of the baseline)."""
    length = 12.0
    amp = 15.0 * length
    yc = y - VORTEX_Y_KM
    e = np.exp(-(x**2 + yc**2) / (2 * length**2))
    zero = 0.0 * x
    return amp * yc / length**2 * e + 5.0 + zero, -amp * x / length**2 * e + zero, zero


A_CONV = 2.0e-4  # 1/s
SCALE_H_KM = 10.0


def convergent_wind(z0_km: float):
    """Anelastic convergent flow u = a x, v = a y with w = -2 a H (exp((z - z0)/H) - 1)."""

    def wind(x, y, z):
        a = A_CONV * 1e3  # (m/s) per km
        h = SCALE_H_KM
        w = -2.0 * A_CONV * (h * 1e3) * (np.exp((z - z0_km) / h) - 1.0)
        return a * x + 0.0 * z, a * y + 0.0 * z, w + 0.0 * x

    return wind


def rain_fall_speed(z_km):
    """Rain fall speed of 30 dBZ with radarx's density correction (m/s, positive down)."""
    return 2.65 * (10 ** (30.0 / 10.0)) ** 0.114 * np.exp(0.04 * z_km)


def truth_on(rec: mdop.WindRecord, wind):
    """Imposed (u, v, w) on the result grid, each ``(z, y, x)``."""
    zz, yy, xx = np.meshgrid(rec.z / 1e3, rec.y / 1e3, rec.x / 1e3, indexing="ij")
    return wind(xx, yy, zz)


def field(rec: mdop.WindRecord, name: str) -> np.ndarray:
    return rec.fields[mdop.FIELD_ORDER.index(name)]


def pair(wind, **kw):
    a = site_east_of_origin(-SEPARATION_KM / 2)
    b = site_east_of_origin(SEPARATION_KM / 2)
    return [
        ("fa", make_reader(make_wind_volume(*a, wind, name="WESTR", **kw))),
        ("fb", make_reader(make_wind_volume(*b, wind, name="EASTR", **kw))),
    ]


GRID = {"dx_m": 1000, "dz_m": 500, "z_max_m": 8000, "max_range_km": 90}


@pytest.fixture(autouse=True)
def _fresh_cache():
    mdop.WIND_RESULTS.clear()
    yield
    mdop.WIND_RESULTS.clear()


@pytest.fixture(scope="module")
def shear_readers():
    return pair(shear_wind, fall_speed=None)


@pytest.fixture(scope="module")
def vortex_readers():
    return pair(vortex_wind)


def good_region(rec: mdop.WindRecord, min_crossing: float = 40.0) -> np.ndarray:
    ok = np.isfinite(field(rec, "u")) & np.isfinite(field(rec, "v"))
    return ok & (field(rec, "crossing_angle") >= min_crossing)


# ---------------------------------------------------------------------------
# Recovery of imposed winds
# ---------------------------------------------------------------------------


@needs_radarx
class TestRecovery:
    def test_uniform_shear_is_recovered(self, shear_readers) -> None:
        rec = mdop.run_multidoppler(
            shear_readers, GRID, {"fall_speed_correction": False}, cache=None
        )
        ut, vt, _ = truth_on(rec, shear_wind)
        ok = good_region(rec)
        assert ok.sum() > 2000  # a substantial well-observed region
        assert np.sqrt(np.mean((field(rec, "u") - ut)[ok] ** 2)) < 0.3
        assert np.sqrt(np.mean((field(rec, "v") - vt)[ok] ** 2)) < 0.3
        w = field(rec, "w")[ok & np.isfinite(field(rec, "w"))]
        assert w.size > 500 and np.sqrt(np.mean(w**2)) < 0.3  # truth is w = 0
        assert np.nanmax(np.abs(field(rec, "divergence"))) < 0.5  # 1e-3/s
        assert rec.fields.dtype == np.float32
        assert rec.fields.shape == (len(mdop.FIELD_ORDER), *field(rec, "u").shape)

    def test_vortex_is_recovered(self, vortex_readers) -> None:
        rec = mdop.run_multidoppler(
            vortex_readers, GRID, {"fall_speed_correction": False}, cache=None
        )
        ut, vt, _ = truth_on(rec, vortex_wind)
        ok = good_region(rec)
        assert np.sqrt(np.mean((field(rec, "u") - ut)[ok] ** 2)) < 0.6
        assert np.sqrt(np.mean((field(rec, "v") - vt)[ok] ** 2)) < 0.6
        # vertical vorticity at the vortex centre: -2 A / L^2 = -2.5 (1e-3 1/s)
        k = rec.z.size // 2
        j = int(np.argmin(np.abs(rec.y / 1e3 - VORTEX_Y_KM)))
        i = int(np.argmin(np.abs(rec.x)))
        assert field(rec, "vorticity")[k, j, i] == pytest.approx(-2.5, abs=0.5)
        # peak speed of the imposed vortex (about 9 m/s on top of the 5 m/s flow)
        assert np.nanmax(field(rec, "speed")) == pytest.approx(
            np.nanmax(np.hypot(ut, vt)[ok]), abs=1.0
        )

    def test_vertical_velocity_of_a_convergent_flow(self) -> None:
        z0 = 1.8  # km: the lowest level both radars see everywhere (w = 0 is imposed there)
        wind = convergent_wind(z0)
        readers = pair(wind, fall_speed=None)
        rec = mdop.run_multidoppler(
            readers, {**GRID, "z_min_m": z0 * 1e3}, {"fall_speed_correction": False}, cache=None
        )
        assert rec.z[0] / 1e3 == pytest.approx(z0, abs=0.01)
        ut, _, wt = truth_on(rec, wind)
        ok = good_region(rec) & np.isfinite(field(rec, "w"))
        assert ok.sum() > 1000
        assert np.sqrt(np.mean((field(rec, "u") - ut)[ok] ** 2)) < 0.3
        assert np.sqrt(np.mean((field(rec, "w") - wt)[ok] ** 2)) < 0.4
        # sinking-then-rising pattern is the right sign and magnitude: w < 0 aloft (convergence)
        top = ok.copy()
        top[: rec.z.size // 2] = False
        assert np.mean(field(rec, "w")[top]) < -0.5
        # divergence of the imposed flow is 2a = 0.4e-3 1/s ... negative convergence here
        assert np.nanmedian(field(rec, "divergence")[ok]) == pytest.approx(
            2 * A_CONV * 1e3, abs=0.1
        )

    def test_fall_speed_correction_removes_the_fall_speed(self) -> None:
        readers = pair(shear_wind, fall_speed=rain_fall_speed)
        with_corr = mdop.run_multidoppler(
            readers, GRID, {"fall_speed_correction": True, "freezing_level": "rain"}, cache=None
        )
        without = mdop.run_multidoppler(readers, GRID, {"fall_speed_correction": False}, cache=None)
        ok = good_region(with_corr) & np.isfinite(field(with_corr, "w"))
        assert np.sqrt(np.mean(field(with_corr, "w")[ok] ** 2)) < 0.4  # truth is w = 0
        ut, vt, _ = truth_on(with_corr, shear_wind)
        err_with = np.hypot(field(with_corr, "u") - ut, field(with_corr, "v") - vt)[ok]
        err_without = np.hypot(field(without, "u") - ut, field(without, "v") - vt)[ok]
        assert np.sqrt(np.mean(err_with**2)) < 0.4
        # uncorrected, the ~6 m/s fall speed projected on the beams biases the horizontal wind
        assert np.sqrt(np.mean(err_without**2)) > 3 * np.sqrt(np.mean(err_with**2))

    def test_lowest_level_is_chosen_where_both_radars_have_data(self, shear_readers) -> None:
        rec = mdop.run_multidoppler(
            shear_readers, GRID, {"fall_speed_correction": False}, cache=None
        )
        # 0.5 degree beams at ~35 km range pass about 0.6-0.9 km above the sites (300 m ASL)
        assert 1000 <= rec.z[0] <= 2500
        assert rec.summary["z_min_auto"] is True
        assert np.isfinite(field(rec, "w")[0]).any()  # boundary level carries w = 0

    def test_variational_method(self, shear_readers) -> None:
        rec = mdop.run_multidoppler(
            shear_readers,
            GRID,
            {"method": "variational", "fall_speed_correction": False},
            cache=None,
        )
        ut, vt, _ = truth_on(rec, shear_wind)
        ok = good_region(rec)
        assert np.sqrt(np.mean((field(rec, "u") - ut)[ok] ** 2)) < 0.5
        assert np.sqrt(np.mean((field(rec, "v") - vt)[ok] ** 2)) < 0.5
        assert rec.summary["method"] == "variational"
        assert rec.summary["has_uncertainty"] is False

    def test_variational_with_uniform_background_and_uncertainty(self, shear_readers) -> None:
        # the posterior error costs a dozen extra solves: keep the grid small
        coarse = {"dx_m": 3000, "dz_m": 1000, "z_max_m": 6000, "max_range_km": 90}
        rec = mdop.run_multidoppler(
            shear_readers,
            coarse,
            {
                "method": "variational",
                "fall_speed_correction": False,
                "background": {"mode": "uniform", "u": 10.0, "v": -3.0},
                "uncertainty": True,
            },
            cache=None,
        )
        assert rec.summary["has_uncertainty"] is True
        assert rec.summary["background"] == {"mode": "uniform", "u": 10.0, "v": -3.0}
        unc = field(rec, "uncertainty")
        assert np.isfinite(unc).any() and np.nanmin(unc) > 0

    def test_analytic_uncertainty_and_crossing_mask(self, shear_readers) -> None:
        rec = mdop.run_multidoppler(
            shear_readers, GRID, {"fall_speed_correction": False}, cache=None
        )
        assert rec.summary["has_uncertainty"] is True
        cross = field(rec, "crossing_angle")
        assert np.nanmin(cross) >= 30.0 - 1e-3  # the default minimum is applied as a mask
        # error grows as the crossing angle shrinks
        unc = field(rec, "uncertainty")
        lo = np.isfinite(unc) & (cross < 45)
        hi = np.isfinite(unc) & (cross > 75)
        assert unc[lo].mean() > unc[hi].mean()
        stats = rec.summary["crossing_angle"]
        assert stats["min"] >= 30 - 1e-3 and stats["max"] <= 90 + 1e-3
        assert 0 < rec.summary["coverage_pct"] <= 100

    def test_stricter_crossing_angle_shrinks_the_area(self, shear_readers) -> None:
        loose = mdop.run_multidoppler(
            shear_readers,
            GRID,
            {"fall_speed_correction": False, "min_crossing_angle": 20},
            cache=None,
        )
        strict = mdop.run_multidoppler(
            shear_readers,
            {**GRID, "extent": {"x_min_km": -50, "x_max_km": 50, "y_min_km": -50, "y_max_km": 50}},
            {"fall_speed_correction": False, "min_crossing_angle": 60},
            cache=None,
        )
        assert strict.summary["coverage_pct"] < loose.summary["coverage_pct"] or (
            strict.summary["valid_cells"] < loose.summary["valid_cells"]
        )

    def test_vvp_background_profile(self, shear_readers) -> None:
        # the radars' VVP fits recover the horizontally uniform part of the imposed wind
        volumes = [mdop._prepare_volume(fid, rd, i) for i, (fid, rd) in enumerate(shear_readers)]
        z = np.arange(1500.0, 5001.0, 1000.0)
        u, v, _ = mdop._vvp_profile(volumes, z, mdop.GridSpec.from_dict(GRID))
        assert u == pytest.approx(8.0 + 2.0 * z / 1e3, abs=1.0)
        assert v == pytest.approx(-5.0 + z / 1e3, abs=1.0)

    def test_vvp_and_manual_backgrounds_run(self, shear_readers) -> None:
        for background, mode in (
            ({"mode": "vvp"}, "vvp"),
            (
                {
                    "mode": "manual",
                    "profile": [
                        {"height_m": 0, "u": 8, "v": -5},
                        {"height_m": 8000, "u": 24, "v": 3},
                    ],
                },
                "manual",
            ),
        ):
            rec = mdop.run_multidoppler(
                shear_readers,
                GRID,
                {"method": "variational", "fall_speed_correction": False, "background": background},
                cache=None,
            )
            assert rec.summary["background"]["mode"] == mode
            assert rec.summary["valid_cells"] > 1000

    def test_background_ignored_by_analytic_is_reported(self, shear_readers) -> None:
        rec = mdop.run_multidoppler(
            shear_readers,
            GRID,
            {
                "fall_speed_correction": False,
                "background": {"mode": "uniform", "u": 1, "v": 1},
            },
            cache=None,
        )
        assert any("only used by the variational" in w for w in rec.warnings)

    def test_fall_speed_with_unknown_freezing_level_warns(self, shear_readers) -> None:
        rec = mdop.run_multidoppler(
            shear_readers, GRID, {"fall_speed_correction": True}, cache=None
        )
        assert any("freezing level" in w for w in rec.warnings)

    def test_manual_z_min_and_obrien_boundary(self, shear_readers) -> None:
        rec = mdop.run_multidoppler(
            shear_readers,
            {**GRID, "z_min_m": 2000, "z_max_m": 6000},
            {"fall_speed_correction": False, "boundary": "obrien"},
            cache=None,
        )
        assert rec.z[0] == 2000 and rec.z[-1] == 6000
        assert rec.summary["boundary"] == "obrien" and rec.summary["z_min_auto"] is False

    def test_storm_motion_advects_to_a_common_time(self, shear_readers) -> None:
        rec = mdop.run_multidoppler(
            shear_readers,
            GRID,
            {"fall_speed_correction": False, "storm_motion": [10, 0]},
            cache=None,
        )
        assert rec.summary["valid_cells"] > 1000

    def test_progress_and_cancel(self, shear_readers) -> None:
        seen: list[int] = []
        mdop.run_multidoppler(
            shear_readers,
            GRID,
            {"fall_speed_correction": False},
            progress=lambda p, m: seen.append(p),
            cache=None,
        )
        assert seen == sorted(seen) and seen[0] < 20 and seen[-1] >= 90
        with pytest.raises(mdop.MultiDopplerCancelledError):
            mdop.run_multidoppler(shear_readers, GRID, None, cancel=lambda: True, cache=None)
        # cancelled between stages: the second check lets the volumes prepare first
        calls = {"n": 0}

        def cancel_later() -> bool:
            calls["n"] += 1
            return calls["n"] >= 2

        with pytest.raises(mdop.MultiDopplerCancelledError):
            mdop.run_multidoppler(shear_readers, GRID, None, cancel=cancel_later, cache=None)

    def test_record_header_export_and_cache(self, shear_readers, tmp_path) -> None:
        rec = mdop.run_multidoppler(shear_readers, GRID, {"fall_speed_correction": False})
        assert mdop.WIND_RESULTS.get(rec.wind_id) is rec
        header = json.loads(json.dumps(rec.header()))  # JSON-able
        shape = header["shape"]
        assert shape[0] == len(mdop.FIELD_ORDER) and shape[1:] == list(field(rec, "u").shape)
        assert [f["name"] for f in header["fields"]] == list(mdop.FIELD_ORDER)
        g = header["grid"]
        assert g["nz"] == rec.z.size and len(g["z"]) == g["nz"] and g["dx"] == 1000.0
        assert g["west"] < g["east"] and g["south"] < g["north"]
        assert [r["name"] for r in header["radars"]] == ["WESTR", "EASTR"]
        for key in (
            "coverage_pct",
            "crossing_angle",
            "background",
            "timing",
            "field_stats",
            "radarx",
        ):
            assert key in header
        assert header["timing"]["total_ms"] > 0
        path = tmp_path / "wind.nc"
        mdop.write_netcdf(rec, path)
        with xr.open_dataset(path) as ds:
            assert set(mdop.FIELD_ORDER) <= set(ds.data_vars)
            assert ds["u"].dims == ("z", "y", "x") and "lon" in ds.coords
            np.testing.assert_allclose(ds["u"].values, field(rec, "u"), equal_nan=True)
        mdop.WIND_RESULTS.clear()
        with pytest.raises(mdop.MultiDopplerError, match="Unknown or expired"):
            mdop.WIND_RESULTS.get(rec.wind_id)


# ---------------------------------------------------------------------------
# Validation and user-facing errors
# ---------------------------------------------------------------------------


class TestErrors:
    def test_fewer_than_two_radars(self) -> None:
        a = site_east_of_origin(0)
        reader = ("a", make_reader(make_wind_volume(*a, shear_wind, name="ONE", n_range=20)))
        with pytest.raises(mdop.MultiDopplerError, match="at least two radars"):
            mdop.run_multidoppler([reader], GRID, None)
        with pytest.raises(mdop.MultiDopplerError, match="at least two radars"):
            mdop.run_multidoppler([], GRID, None)

    @needs_radarx
    def test_missing_velocity(self) -> None:
        a, b = site_east_of_origin(-35), site_east_of_origin(35)
        readers = [
            ("a", make_reader(make_wind_volume(*a, shear_wind, name="GOOD", n_range=40))),
            (
                "b",
                make_reader(
                    make_wind_volume(*b, shear_wind, name="NOVEL", n_range=40, velocity=False)
                ),
            ),
        ]
        with pytest.raises(mdop.MultiDopplerError, match="NOVEL has no radial velocity"):
            mdop.run_multidoppler(readers, GRID, None)

    @needs_radarx
    def test_radars_at_the_same_site(self) -> None:
        a = site_east_of_origin(0)
        readers = [
            ("a", make_reader(make_wind_volume(*a, shear_wind, name="TWIN1", n_range=40))),
            ("b", make_reader(make_wind_volume(*a, shear_wind, name="TWIN2", n_range=40))),
        ]
        with pytest.raises(mdop.MultiDopplerError, match="same location"):
            mdop.run_multidoppler(readers, GRID, None)

    @needs_radarx
    def test_no_overlap(self) -> None:
        a, b = site_east_of_origin(-150), site_east_of_origin(150)
        readers = [
            ("a", make_reader(make_wind_volume(*a, shear_wind, name="FAR1", n_range=220))),
            ("b", make_reader(make_wind_volume(*b, shear_wind, name="FAR2", n_range=220))),
        ]
        with pytest.raises(mdop.MultiDopplerError, match="no overlapping coverage"):
            mdop.run_multidoppler(readers, GRID, None)

    @needs_radarx
    def test_crossing_angle_too_small(self, shear_readers) -> None:
        # a 90 degree minimum leaves (at most) isolated lattice points: nothing usable
        with pytest.raises(mdop.MultiDopplerError, match="cross"):
            mdop.run_multidoppler(
                shear_readers, {**GRID, "dx_m": 1100}, {"min_crossing_angle": 90}, cache=None
            )
        # an explicit extent beyond the baseline sees the radars nearly in line
        far = {"x_min_km": 60, "x_max_km": 85, "y_min_km": -3, "y_max_km": 3}
        with pytest.raises(mdop.MultiDopplerError, match="crossing angle"):
            mdop.run_multidoppler(shear_readers, {**GRID, "extent": far}, None, cache=None)

    @needs_radarx
    def test_grid_too_large_and_too_small(self, shear_readers) -> None:
        with pytest.raises(mdop.MultiDopplerError, match="million cells"):
            mdop.run_multidoppler(
                shear_readers,
                {"dx_m": 250, "dz_m": 100, "z_max_m": 20000, "max_range_km": 200},
                None,
                cache=None,
            )
        with pytest.raises(mdop.MultiDopplerError, match="height levels"):
            mdop.run_multidoppler(
                shear_readers, {**GRID, "z_min_m": 7900, "z_max_m": 8000}, None, cache=None
            )

    def test_missing_radarx(self, monkeypatch) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "radarx", None)
        monkeypatch.setitem(sys.modules, "radarx.retrieve", None)
        a, b = site_east_of_origin(-35), site_east_of_origin(35)
        readers = [
            ("a", make_reader(make_wind_volume(*a, shear_wind, name="X", n_range=10))),
            ("b", make_reader(make_wind_volume(*b, shear_wind, name="Y", n_range=10))),
        ]
        with pytest.raises(mdop.MultiDopplerError, match="pip install radarx"):
            mdop.run_multidoppler(readers, GRID, None)
        assert mdop.radarx_available() is False

    @pytest.mark.parametrize(
        ("grid", "needle"),
        [
            ({"dx_m": 10}, "dx_m"),
            ({"dz_m": 5000}, "dz_m"),
            ({"z_max_m": 10}, "z_max_m"),
            ({"z_min_m": 99999}, "z_min_m"),
            ({"max_range_km": 1}, "max_range_km"),
            ({"extent": {"x_min_km": 0}}, "extent"),
            ({"extent": {"x_min_km": 5, "x_max_km": 1, "y_min_km": 0, "y_max_km": 1}}, "maximum"),
            ({"origin": {"lat": 120, "lon": 0}}, "origin"),
            ({"origin": "centre"}, "origin"),
            ({"dx_m": True}, "dx_m"),
            ({"dx_m": "wide"}, "dx_m"),
            ({"dx_m": float("nan")}, "finite"),
        ],
    )
    def test_bad_grid(self, grid, needle) -> None:
        with pytest.raises(mdop.MultiDopplerError, match=needle):
            mdop.GridSpec.from_dict(grid)

    @pytest.mark.parametrize(
        ("options", "needle"),
        [
            ({"method": "magic"}, "method"),
            ({"boundary": "sideways"}, "boundary"),
            ({"min_crossing_angle": 120}, "min_crossing_angle"),
            ({"min_crossing_angle": -1}, "min_crossing_angle"),
            ({"obs_error": 0}, "obs_error"),
            ({"freezing_level": 99999}, "freezing_level"),
            ({"freezing_level": "snow"}, "freezing_level"),
            ({"background": "wind"}, "background"),
            ({"background": {"mode": "era5"}}, "background.mode"),
            ({"background": {"mode": "uniform", "u": 3}}, "background.v"),
            ({"background": {"mode": "manual", "profile": []}}, "profile"),
            (
                {
                    "background": {
                        "mode": "manual",
                        "profile": [{"height_m": 1, "u": 0, "v": 0}] * 2,
                    }
                },
                "duplicate",
            ),
            ({"storm_motion": "fast"}, "storm_motion"),
            ({"storm_motion": [1, float("inf")]}, "finite"),
        ],
    )
    def test_bad_options(self, options, needle) -> None:
        with pytest.raises(mdop.MultiDopplerError, match=needle):
            mdop.Options.from_dict(options)

    def test_defaults_are_sane(self) -> None:
        g = mdop.GridSpec.from_dict(None)
        o = mdop.Options.from_dict({})
        assert g.extent == "auto" and g.origin == "auto" and g.z_min_m is None
        assert o.method == "analytic" and o.min_crossing_angle == 30.0 and o.fall_speed_correction
        assert o.background == {"mode": "none"} and o.boundary == "upward"
        o2 = mdop.Options.from_dict(
            {
                "freezing_level": 3500,
                "storm_motion": {"u": 5, "v": -2},
                "background": {"mode": "vvp"},
            }
        )
        assert o2.freezing_level == 3500.0 and o2.storm_motion == (5.0, -2.0)

    def test_dual_mask_geometry(self) -> None:
        # radars 60 km apart on the x axis: beams cross at 90 deg on the bisector (60 km off),
        # nearly parallel far along the baseline and anti-parallel between the radars
        xy = np.array([[-30e3, 0.0], [30e3, 0.0]])
        reach = np.array([100e3, 100e3])
        x = np.array([0.0, 90e3])
        y = np.array([0.0, 30e3])
        seen, good = mdop._dual_mask(xy, reach, x, y, 30.0)
        # (0,0): between the radars -> beams collinear (0 deg); (0,30 km): 90 deg
        assert seen[0, 0] and not good[0, 0]
        assert good[1, 0]
        # (90 km, 0): the far radar is 120 km away (beyond the reach): only one radar sees it
        assert not seen[0, 1]
        assert mdop._crossing_deg(np.array([0.0]), np.array([math.pi / 2]))[0] == pytest.approx(90)
        assert mdop._crossing_deg(np.array([0.0]), np.array([math.pi]))[0] == pytest.approx(
            0, abs=1e-6
        )


# ---------------------------------------------------------------------------
# Server protocol
# ---------------------------------------------------------------------------


@pytest.fixture()
def _server_state():
    saved = (dict(server._file_readers), server._last_file_id, dict(server._generations))
    server._file_readers.clear()
    server._generations.clear()
    server._last_file_id = None
    server._multidoppler_runs.clear()
    yield
    server._file_readers.clear()
    server._file_readers.update(saved[0])
    server._generations.clear()
    server._generations.update(saved[2])
    server._last_file_id = saved[1]
    server._multidoppler_runs.clear()


@pytest.mark.usefixtures("_server_state")
class TestServerProtocol:
    @staticmethod
    def _open(readers) -> list[str]:
        return [server._register_reader(rd) for _, rd in readers]

    @staticmethod
    async def _run(handler, file_ids, **extra) -> dict:
        await handler.handle_message(
            json.dumps(
                {
                    "type": "run_multidoppler",
                    "request_id": "w1",
                    "file_ids": file_ids,
                    "grid": GRID,
                    "options": {"fall_speed_correction": False},
                    **extra,
                }
            )
        )
        return next(
            (
                m
                for m in handler._sent
                if m["type"] in ("multidoppler_result", "multidoppler_error")
            ),
            {},
        )

    @needs_radarx
    @pytest.mark.asyncio
    async def test_run_streams_float32_grids(self, shear_readers) -> None:
        ids = self._open(shear_readers)
        handler, _ = _make_handler()
        msg = await self._run(handler, ids)
        assert msg["type"] == "multidoppler_result" and msg["request_id"] == "w1"
        for key in (
            "wind_id",
            "fields",
            "shape",
            "grid",
            "radars",
            "coverage_pct",
            "crossing_angle",
            "background",
            "elapsed_ms",
            "byte_length",
            "chunks",
            "warnings",
        ):
            assert key in msg, key
        assert [r["file_id"] for r in msg["radars"]] == ids
        # header immediately followed by exactly `chunks` binary frames
        i = next(n for n, (k, f) in enumerate(handler._frames) if k == "json" and f is msg)
        assert [k for k, _ in handler._frames[i + 1 : i + 1 + msg["chunks"]]] == ["bin"] * msg[
            "chunks"
        ]
        raw = _binary(handler)
        assert len(raw) == msg["byte_length"] == int(np.prod(msg["shape"])) * 4
        data = np.frombuffer(raw, dtype=np.float32).reshape(msg["shape"])
        names = [f["name"] for f in msg["fields"]]
        u = data[names.index("u")]
        assert np.isfinite(u).sum() > 1000
        # progress closes at 100, last message
        progress = [m for m in handler._sent if m["type"] == "progress"]
        assert progress and progress[-1]["percent"] == 100 and handler._sent[-1] is progress[-1]

    @needs_radarx
    @pytest.mark.asyncio
    async def test_large_payload_is_chunked(self, shear_readers) -> None:
        ids = self._open(shear_readers)
        handler, _ = _make_handler()
        msg = await self._run(handler, ids)
        assert msg["byte_length"] > server.CHUNK_SIZE  # the default grid spans several frames
        assert msg["chunks"] == -(-msg["byte_length"] // server.CHUNK_SIZE) > 1
        assert len(_binary(handler)) == msg["byte_length"]
        assert all(len(f) <= server.CHUNK_SIZE for k, f in handler._frames if k == "bin")

    @needs_radarx
    @pytest.mark.asyncio
    async def test_export_netcdf(self, shear_readers, tmp_path) -> None:
        ids = self._open(shear_readers)
        handler, _ = _make_handler()
        msg = await self._run(handler, ids)
        handler._sent.clear()
        target = tmp_path / "wind.nc"
        await handler.handle_message(
            json.dumps(
                {"type": "export_multidoppler", "wind_id": msg["wind_id"], "path": str(target)}
            )
        )
        assert handler._sent[-1] == {"type": "file_saved", "path": str(target)}
        assert target.exists()
        # refuses to overwrite
        handler._sent.clear()
        await handler.handle_message(
            json.dumps(
                {"type": "export_multidoppler", "wind_id": msg["wind_id"], "path": str(target)}
            )
        )
        assert handler._sent[-1]["type"] == "multidoppler_error"
        assert "already exists" in handler._sent[-1]["message"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("payload", "needle"),
        [
            ({"file_ids": []}, "file_ids"),
            ({"file_ids": "abc"}, "file_ids"),
            ({"file_ids": ["nope", "nada"]}, "Unknown or closed"),
            ({"grid": "big"}, "'grid' must be an object"),
            ({"options": [1]}, "'options' must be an object"),
            ({"grid": {"dx_m": 10}}, "dx_m"),
            ({"options": {"method": "x"}}, "method"),
        ],
    )
    async def test_request_errors(self, shear_readers, payload, needle) -> None:
        ids = self._open(shear_readers)
        handler, _ = _make_handler()
        msg = {"file_ids": ids, **payload}
        await handler.handle_message(
            json.dumps({"type": "run_multidoppler", "request_id": "q", **msg})
        )
        errors = [m for m in handler._sent if m["type"] == "multidoppler_error"]
        assert len(errors) == 1 and errors[0]["request_id"] == "q"
        assert needle in errors[0]["message"]
        assert not [m for m in handler._sent if m["type"] == "error"]
        assert handler._sent[-1]["type"] == "progress" and handler._sent[-1]["percent"] == 100

    @pytest.mark.asyncio
    async def test_one_radar_is_a_clear_error(self, shear_readers) -> None:
        ids = self._open(shear_readers)[:1]
        handler, _ = _make_handler()
        msg = await self._run(handler, ids)
        assert msg["type"] == "multidoppler_error"
        assert "at least two radars" in msg["message"]

    @pytest.mark.asyncio
    async def test_missing_radarx_message(self, shear_readers, monkeypatch) -> None:
        def _raise():
            raise mdop.MultiDopplerError(
                "Multi-Doppler wind retrieval needs the Python package radarx: pip install radarx"
            )

        monkeypatch.setattr(mdop, "_radarx_mod", _raise)
        ids = self._open(shear_readers)
        handler, _ = _make_handler()
        msg = await self._run(handler, ids)
        assert msg["type"] == "multidoppler_error" and "pip install radarx" in msg["message"]

    @pytest.mark.asyncio
    async def test_cancel_before_start_and_unknown(self, shear_readers) -> None:
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps({"type": "multidoppler_cancel", "request_id": "zz"})
        )
        assert not [m for m in handler._sent if m["type"] == "error"]
        # a cancel that arrives first is remembered: the run stops at its first check
        ids = self._open(shear_readers)
        event = threading.Event()
        event.set()
        server._multidoppler_runs["w1"] = event
        await self._run(handler, ids)
        assert [m for m in handler._sent if m["type"] == "multidoppler_cancelled"] == [
            {"type": "multidoppler_cancelled", "request_id": "w1"}
        ]
        assert not [m for m in handler._sent if m["type"] == "multidoppler_result"]
        assert "w1" not in server._multidoppler_runs  # forgotten once the run ended

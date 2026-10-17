"""Tests for the cross-section / vertical-profile helpers in engine.processor."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from engine.processor import (
    _geod,
    _geodesic_samples,
    _get_sweep_data,
    _ground_range_azimuth,
    _sample_sweep,
    extract_cross_section,
    extract_vertical_profile,
)
from engine.volume import beam_height_and_ground_range, slant_range_and_height_from_ground

RADAR_LAT, RADAR_LON = 40.0, -100.0
RANGE = 250.0 * (1 + np.arange(600))  # to 150 km


def _tree(elevs, field, n_az: int = 360) -> xr.DataTree:
    az = np.arange(n_az) * (360.0 / n_az)
    sweeps = {}
    for i, e in enumerate(elevs):
        vals = field(az[:, None], RANGE[None, :], e).astype(np.float32)
        sweeps[f"/sweep_{i}"] = xr.Dataset(
            {"DBZH": (("azimuth", "range"), vals, {"units": "dBZ"})},
            coords={"azimuth": az, "range": RANGE},
            attrs={"sweep_fixed_angle": e},
        )
    sweeps["/"] = xr.Dataset(attrs={"latitude": RADAR_LAT, "longitude": RADAR_LON})
    return xr.DataTree.from_dict(sweeps)


def _slant_range_field(az, rng, e):
    return np.broadcast_to(rng / 1000.0, (az.shape[0], rng.shape[1]))


def _point_at(ground_m: float, azimuth_deg: float) -> tuple[float, float]:
    """(lat, lon) exactly ``ground_m`` metres from the radar along the geodesic."""
    lon, lat, _ = _geod().fwd(RADAR_LON, RADAR_LAT, azimuth_deg, ground_m, return_back_azimuth=True)
    return float(lat), float(lon)


def _point_north(ground_m: float) -> tuple[float, float]:
    return _point_at(ground_m, 0.0)


class TestSweepOrder:
    def test_sweep_index_is_numeric(self) -> None:
        elevs = [0.5 + i for i in range(12)]
        tree = _tree(elevs, _slant_range_field, n_az=8)
        for idx in (2, 9, 10, 11):
            _, elev, _, _ = _get_sweep_data(tree, idx, "DBZH")
            assert elev == pytest.approx(elevs[idx])

    def test_cross_section_sweeps_in_elevation_order(self) -> None:
        elevs = [0.5 + i for i in range(12)]
        tree = _tree(elevs, _slant_range_field, n_az=36)
        start = _point_north(20_000.0)
        end = _point_north(60_000.0)
        out = extract_cross_section(tree, start, end, "DBZH", n_points=5)
        assert out["n_sweeps"] == 12
        heights_mid = [row[2] for row in out["height_km"]]
        assert heights_mid == sorted(heights_mid)  # higher tilt -> higher beam, in index order


class TestSlantRange:
    """Lookup must use slant range (not ground distance) at the sample's ground range."""

    def test_vertical_profile_uses_slant_range(self) -> None:
        elevs = [0.5, 10.0, 19.5]
        tree = _tree(elevs, _slant_range_field)
        lat, lon = _point_north(60_000.0)
        out = extract_vertical_profile(tree, lat, lon, "DBZH")
        ground = 60_000.0  # exact by construction of _point_north (geodesic)
        for e, v, hk in zip(out["elevations_deg"], out["values"], out["heights_km"], strict=True):
            slant, h = slant_range_and_height_from_ground(ground, e)
            assert v == pytest.approx(float(slant) / 1000.0, abs=0.13)  # gate spacing 0.25 km
            assert hk == pytest.approx(float(h) / 1000.0, rel=1e-6)
        # at 19.5 deg slant range differs from ground range by ~6%, the old lookup was off by 3+ km
        assert out["values"][2] > 62.0
        assert out["units"] == "dBZ"

    def test_height_matches_beam_model(self) -> None:
        tree = _tree([0.5], _slant_range_field)
        lat, lon = _point_north(100_000.0)
        out = extract_vertical_profile(tree, lat, lon, "DBZH")
        h_ref, _ = beam_height_and_ground_range(100_000.0, 0.5)
        assert out["heights_km"][0] > 1.4
        assert out["heights_km"][0] == pytest.approx(float(h_ref) / 1000.0, abs=0.01)


class TestCrossSection:
    def test_matches_scalar_reference(self) -> None:
        """Vectorised sampling equals a straightforward per-point implementation."""
        rng = np.random.default_rng(3)
        az = rng.permutation(np.arange(0, 360, 1.0))  # unsorted rays
        vals = rng.uniform(0, 60, (az.size, RANGE.size)).astype(np.float32)
        az_pts = rng.uniform(0, 360, 50)
        r_pts = rng.uniform(RANGE[0] - 500, RANGE[-1] + 500, 50)
        got = _sample_sweep(vals, az, RANGE, az_pts, r_pts)
        for i in range(50):
            if r_pts[i] < RANGE[0] or r_pts[i] > RANGE[-1]:
                assert np.isnan(got[i])
                continue
            d = np.abs(az - az_pts[i])
            d = np.minimum(d, 360 - d)
            row = int(np.argmin(d))
            j = int(np.searchsorted(RANGE, r_pts[i]))
            j = min(max(j, 1), RANGE.size - 1)
            f = (r_pts[i] - RANGE[j - 1]) / (RANGE[j] - RANGE[j - 1])
            want = vals[row, j - 1] + f * (vals[row, j] - vals[row, j - 1])
            assert got[i] == pytest.approx(float(want), abs=1e-4)

    def test_nan_becomes_none(self) -> None:
        tree = _tree([0.5, 1.5], _slant_range_field)
        # a line that starts inside the radar's range and ends well beyond 150 km
        out = extract_cross_section(
            tree, _point_north(50_000.0), _point_north(220_000.0), "DBZH", n_points=10
        )
        flat = [v for row in out["values"] for v in row]
        assert None in flat
        assert not any(isinstance(v, float) and np.isnan(v) for v in flat)
        assert out["distance_km"][-1] == pytest.approx(170.0, rel=0.01)

    def test_sweep_loader_used(self) -> None:
        real = _tree([0.5, 1.5], _slant_range_field)
        placeholders = xr.DataTree.from_dict(
            {
                "/": xr.Dataset(attrs={"latitude": RADAR_LAT, "longitude": RADAR_LON}),
                "/sweep_0": real["sweep_0"].to_dataset(),
                "/sweep_1": xr.Dataset(attrs={"_sweep_variables": ["DBZH"]}),
            }
        )
        args = (placeholders, _point_north(10_000.0), _point_north(40_000.0), "DBZH")
        assert extract_cross_section(*args, n_points=4)["n_sweeps"] == 1
        out = extract_cross_section(
            *args, n_points=4, sweep_loader=lambda i: real[f"sweep_{i}"].to_dataset()
        )
        assert out["n_sweeps"] == 2

    def test_missing_variable_skips_sweeps(self) -> None:
        tree = _tree([0.5], _slant_range_field)
        out = extract_cross_section(tree, _point_north(1e4), _point_north(2e4), "NOPE", n_points=3)
        assert out["n_sweeps"] == 0
        assert out["values"] == []


class TestGeodesy:
    """lat/lon -> ground range/azimuth follows the WGS84 ellipsoid, not a flat-earth shortcut."""

    @pytest.mark.parametrize("azimuth", [0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0])
    def test_150km_baseline_matches_geod_reference(self, azimuth: float) -> None:
        lat, lon = _point_at(150_000.0, azimuth)
        ground, az = _ground_range_azimuth(np.array([lat]), np.array([lon]), RADAR_LAT, RADAR_LON)
        assert ground[0] == pytest.approx(150_000.0, rel=1e-3)  # spec: within 0.1 %
        assert ground[0] == pytest.approx(150_000.0, abs=1e-3)  # in practice: sub-millimetre
        d = abs((az[0] - azimuth + 180.0) % 360.0 - 180.0)
        assert d < 1e-6

    def test_reference_independent_of_pyproj_helper(self) -> None:
        """Cross-check against the textbook spherical haversine (<0.5 % at 150 km)."""
        lat, lon = _point_at(150_000.0, 60.0)
        ground, _ = _ground_range_azimuth(lat, lon, RADAR_LAT, RADAR_LON)
        p1, p2 = np.radians(RADAR_LAT), np.radians(lat)
        a = (
            np.sin((p2 - p1) / 2) ** 2
            + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lon - RADAR_LON) / 2) ** 2
        )
        haversine = 2 * 6371008.8 * np.arcsin(np.sqrt(a))
        assert float(ground) == pytest.approx(float(haversine), rel=5e-3)

    def test_old_flat_earth_shortcut_was_not_good_enough(self) -> None:
        """Documents why it was replaced: >0.4 % error at 150 km north (> the 0.1 % bar)."""
        lat, _ = _point_at(150_000.0, 0.0)
        old = (lat - RADAR_LAT) * 110540.0
        assert abs(old - 150_000.0) / 150_000.0 > 1e-3

    def test_scalar_and_array_inputs_agree(self) -> None:
        pts = [_point_at(d, a) for d, a in [(10e3, 10.0), (80e3, 200.0), (149e3, 300.0)]]
        lats = np.array([p[0] for p in pts])
        lons = np.array([p[1] for p in pts])
        g_arr, a_arr = _ground_range_azimuth(lats, lons, RADAR_LAT, RADAR_LON)
        for i in range(3):
            g, a = _ground_range_azimuth(lats[i], lons[i], RADAR_LAT, RADAR_LON)
            assert float(g) == pytest.approx(g_arr[i])
            assert float(a) == pytest.approx(a_arr[i])

    def test_radar_location_itself(self) -> None:
        ground, az = _ground_range_azimuth(RADAR_LAT, RADAR_LON, RADAR_LAT, RADAR_LON)
        assert float(ground) == pytest.approx(0.0, abs=1e-6)
        assert np.isfinite(az)

    def test_vectorised_and_fast(self) -> None:
        import time

        rng = np.random.default_rng(0)
        lats = RADAR_LAT + rng.uniform(-1.5, 1.5, 200_000)
        lons = RADAR_LON + rng.uniform(-1.5, 1.5, 200_000)
        t0 = time.perf_counter()
        ground, az = _ground_range_azimuth(lats, lons, RADAR_LAT, RADAR_LON)
        assert time.perf_counter() - t0 < 2.0
        assert ground.shape == az.shape == (200_000,)
        assert np.all((az >= 0) & (az < 360))

    def test_geodesic_samples_endpoints_and_spacing(self) -> None:
        start, end = _point_at(40_000.0, 270.0), _point_at(120_000.0, 90.0)
        lats, lons, along = _geodesic_samples(start, end, 7)
        assert (lats[0], lons[0]) == pytest.approx(start)
        assert (lats[-1], lons[-1]) == pytest.approx(end, abs=1e-9)
        _, _, total = _geod().inv(start[1], start[0], end[1], end[0], return_back_azimuth=True)
        assert along[-1] == pytest.approx(total)
        assert np.allclose(np.diff(along), total / 6)
        # every sample is on the line: consecutive geodesic distances add up
        _, _, seg = _geod().inv(lons[:-1], lats[:-1], lons[1:], lats[1:], return_back_azimuth=True)
        assert np.allclose(seg, total / 6, atol=1e-3)

    def test_geodesic_samples_degenerate(self) -> None:
        p = _point_at(30_000.0, 10.0)
        lats, lons, along = _geodesic_samples(p, p, 5)
        assert np.allclose(lats, p[0]) and np.allclose(lons, p[1]) and np.allclose(along, 0)
        lats, lons, along = _geodesic_samples(p, _point_at(50_000.0, 10.0), 1)
        assert lats.shape == (1,) and along[0] == 0.0

    def test_cross_section_distance_and_ranges_are_geodesic(self) -> None:
        tree = _tree([0.5], _slant_range_field)
        start, end = _point_at(20_000.0, 270.0), _point_at(120_000.0, 90.0)
        out = extract_cross_section(tree, start, end, "DBZH", n_points=11)
        assert out["distance_km"][0] == 0.0
        assert out["distance_km"][-1] == pytest.approx(140.0, rel=1e-3)
        # the line passes over the radar at 20 km: sample ground ranges follow |d - 20 km|
        d_m = np.array(out["distance_km"]) * 1000.0
        expected_ground = np.abs(d_m - 20_000.0)
        expected_slant, _ = slant_range_and_height_from_ground(expected_ground, 0.5)
        got = np.array([np.nan if v is None else v for v in out["values"][0]])
        ok = np.isfinite(got)
        assert ok.sum() >= 8
        assert np.allclose(got[ok], expected_slant[ok] / 1000.0, atol=0.13)

    def test_vertical_profile_east_of_radar(self) -> None:
        tree = _tree([0.5, 2.0], _slant_range_field)
        lat, lon = _point_at(140_000.0, 90.0)
        out = extract_vertical_profile(tree, lat, lon, "DBZH")
        for e, v in zip(out["elevations_deg"], out["values"], strict=True):
            slant, _ = slant_range_and_height_from_ground(140_000.0, e)
            assert v == pytest.approx(float(slant) / 1000.0, abs=0.13)

    def test_cross_section_n_points_one(self) -> None:
        tree = _tree([0.5], _slant_range_field)
        out = extract_cross_section(
            tree, _point_north(10_000.0), _point_north(40_000.0), "DBZH", n_points=1
        )
        assert out["distance_km"] == [0.0]

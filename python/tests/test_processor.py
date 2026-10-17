"""Tests for the cross-section / vertical-profile helpers in engine.processor."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from engine.processor import (
    _get_sweep_data,
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


def _point_north(ground_m: float) -> tuple[float, float]:
    return RADAR_LAT + ground_m / 110540.0, RADAR_LON


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
        ground = 60_000.0 * 110540.0 / 110540.0  # exact by construction of _point_north
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

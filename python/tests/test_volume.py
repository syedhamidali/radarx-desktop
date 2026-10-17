"""Tests for engine.volume (direct polar-space gridding, 4/3 earth beam model)."""

from __future__ import annotations

import time

import numpy as np
import pytest
import xarray as xr

from engine.volume import (
    EFFECTIVE_RADIUS_M,
    NODATA,
    beam_height_and_ground_range,
    extract_cross_section_3d,
    extract_volume,
    slant_range_and_height_from_ground,
)

ELEVS = [0.5, 1.5, 2.5, 3.5, 4.5, 6.0, 8.0, 10.0, 12.5, 15.0, 19.5]
N_AZ = 360
RANGE = 500.0 + 250.0 * np.arange(400)  # 0.5 .. 100 km


def _sweep_ds(
    elev: float,
    field,
    az: np.ndarray | None = None,
    rng: np.ndarray = RANGE,
) -> xr.Dataset:
    az = np.arange(N_AZ, dtype=np.float64) + 0.5 if az is None else az
    h, s = beam_height_and_ground_range(rng, elev)
    values = field(az[:, None], rng[None, :], h[None, :], s[None, :]).astype(np.float32)
    return xr.Dataset(
        {"DBZH": (("azimuth", "range"), values)},
        coords={
            "azimuth": az,
            "range": rng,
            "elevation": ("azimuth", np.full(az.size, elev, np.float32)),
        },
        attrs={"sweep_fixed_angle": elev},
    )


def _tree(field, elevs=ELEVS, az=None, rng=RANGE) -> xr.DataTree:
    return xr.DataTree.from_dict(
        {f"/sweep_{i}": _sweep_ds(e, field, az, rng) for i, e in enumerate(elevs)}
    )


def height_km(az, rng, h, s):
    return np.broadcast_to(h / 1000.0, np.broadcast_shapes(az.shape, rng.shape, h.shape))


BOX = {"x_min": -60000.0, "x_max": 60000.0, "y_min": -60000.0, "y_max": 60000.0}


class TestGeometry:
    def test_known_beam_height(self) -> None:
        # 100 km @ 0.5 deg: r sin(el) + r^2/(2 Re) = 872.7 + 588.6 m
        h, s = beam_height_and_ground_range(100_000.0, 0.5)
        assert float(h) == pytest.approx(1461.3, abs=3.0)
        assert float(s) == pytest.approx(99_981.3, abs=3.0)

    def test_inverse_roundtrip(self) -> None:
        r = np.linspace(1000, 200_000, 50)
        for el in (0.5, 5.0, 19.5, 45.0):
            h, s = beam_height_and_ground_range(r, el)
            r2, h2 = slant_range_and_height_from_ground(s, el)
            np.testing.assert_allclose(r2, r, rtol=1e-9)
            np.testing.assert_allclose(h2, h, atol=1e-3)

    def test_effective_radius(self) -> None:
        assert pytest.approx(8_494_666.7, abs=1) == EFFECTIVE_RADIUS_M


class TestExtractVolume:
    def test_height_field_is_reproduced(self) -> None:
        """A field equal to beam height must come back as the grid height (km)."""
        tree = _tree(height_km)
        out = extract_volume(tree, "DBZH", BOX, resolution=24, z_max=12000.0)
        grid = out["data"].reshape(out["nx"], out["ny"], out["nz"])
        assert out["data"].dtype == np.float32
        gz_km = np.linspace(0, 12.0, 24)
        valid = grid > NODATA + 1
        assert valid.mean() > 0.4
        expected = np.broadcast_to(gz_km[None, None, :], grid.shape)
        err = np.abs(grid[valid] - expected[valid])
        assert np.median(err) < 0.05
        assert err.max() < 0.8  # between widely spaced upper tilts the field is only linear in el
        assert out["vmin"] >= -0.01 and out["vmax"] <= 12.1

    def test_invalid_outside_beam_coverage(self) -> None:
        tree = _tree(height_km)
        out = extract_volume(tree, "DBZH", BOX, resolution=20, z_max=15000.0)
        grid = out["data"].reshape(20, 20, 20)
        # directly above the radar (cone of silence) nothing is sampled
        cx = 10
        assert (grid[cx - 1 : cx + 1, cx - 1 : cx + 1, -1] == NODATA).all()
        # nothing below the lowest beam far from the radar (z=0 at 60 km is below 0.5 deg beam
        # centre minus tolerance? -> valid), but 15 km up at 5 km range is above 19.5 deg
        assert grid[10, 11, -1] == NODATA

    def test_azimuth_wraparound_and_unsorted_rays(self) -> None:
        """Rays in acquisition order (starting mid-circle) must give the same grid."""

        def field(az, rng, h, s):
            return np.broadcast_to(10.0 + 5.0 * np.cos(np.deg2rad(az)), (az.shape[0], rng.shape[1]))

        az_sorted = np.arange(N_AZ, dtype=np.float64) + 0.5
        shift = 137
        az_rolled = np.roll(az_sorted, shift)
        ref_ds = _sweep_ds(0.5, field, az_sorted)
        rolled_ds = ref_ds.roll(azimuth=shift, roll_coords=True)
        ref = xr.DataTree.from_dict({"/sweep_0": ref_ds})
        rolled = xr.DataTree.from_dict({"/sweep_0": rolled_ds})
        np.testing.assert_array_equal(rolled_ds["azimuth"].values, az_rolled)
        a = extract_volume(ref, "DBZH", BOX, resolution=16, z_max=3000.0)
        b = extract_volume(rolled, "DBZH", BOX, resolution=16, z_max=3000.0)
        np.testing.assert_allclose(a["data"], b["data"], atol=1e-4)
        grid = a["data"].reshape(16, 16, 16)
        valid = grid > NODATA + 1
        assert valid.any()
        # the north seam (x ~ 0, y > 0) is covered: no NaN band at az 0/360
        north = grid[7:9, 9:, 0:2]
        assert (north > NODATA + 1).any()

    def test_single_sweep_only_near_its_elevation(self) -> None:
        tree = _tree(height_km, elevs=[2.0])
        out = extract_volume(tree, "DBZH", BOX, resolution=20, z_max=8000.0)
        grid = out["data"].reshape(20, 20, 20)
        valid = grid > NODATA + 1
        assert valid.any()
        assert not valid.all()
        # at the lowest level (z=0) only points near the radar (< ~0.5 deg of 2 deg) can hit
        assert not valid[:, :, 0].any() or valid[:, :, 0].sum() < valid[:, :, 8].sum()

    def test_nan_gates_become_nodata_not_zero(self) -> None:
        def field(az, rng, h, s):
            v = np.full((az.shape[0], rng.shape[1]), 30.0)
            v[:, rng[0] > 30_000] = np.nan  # no-op shape guard
            v[:, (rng[0] > 40_000) & (rng[0] < 60_000)] = np.nan
            return v

        tree = _tree(field, elevs=[0.5, 1.5, 2.5])
        out = extract_volume(tree, "DBZH", BOX, resolution=20, z_max=4000.0)
        grid = out["data"].reshape(20, 20, 20)
        assert np.isfinite(grid).all()
        assert set(np.unique(grid[grid > NODATA + 1]).round(3)) == {30.0}
        assert out["vmin"] == pytest.approx(30.0)
        assert out["vmax"] == pytest.approx(30.0)

    def test_sector_scan_does_not_fill_the_gap(self) -> None:
        az = np.arange(30.0, 60.0, 0.5)
        tree = _tree(lambda a, r, h, s: np.full((a.shape[0], r.shape[1]), 5.0), elevs=[1.0], az=az)
        out = extract_volume(tree, "DBZH", BOX, resolution=20, z_max=3000.0)
        grid = out["data"].reshape(20, 20, 20)
        xs = np.linspace(-60000, 60000, 20)
        gx, gy = np.meshgrid(xs, xs, indexing="ij")
        azg = np.degrees(np.arctan2(gx, gy)) % 360
        outside = (azg < 25) | (azg > 65)
        assert (grid[outside] == NODATA).all()
        assert (grid > NODATA + 1).any()

    def test_missing_variable_gives_empty_grid(self) -> None:
        out = extract_volume(_tree(height_km), "NOPE", BOX, resolution=6)
        assert out["data"].shape == (216,)
        assert (out["data"] == NODATA).all()
        assert (out["vmin"], out["vmax"]) == (0.0, 0.0)

    def test_no_sweeps_raises(self) -> None:
        tree = xr.DataTree.from_dict({"/other": xr.Dataset()})
        with pytest.raises(ValueError, match="No sweep groups"):
            extract_volume(tree, "DBZH", BOX, resolution=4)

    def test_sweeps_beyond_ten_are_used_in_numeric_order(self) -> None:
        elevs = [0.5 + i for i in range(13)]
        tree = _tree(
            lambda a, r, h, s: np.broadcast_to(h / 1000.0, (a.shape[0], r.shape[1])), elevs=elevs
        )
        out = extract_volume(tree, "DBZH", BOX, resolution=12, z_max=10000.0)
        grid = out["data"].reshape(12, 12, 12)
        # smooth monotone increase with height at a fixed (x, y) column
        col = grid[8, 8]
        col = col[col > NODATA + 1]
        assert np.all(np.diff(col) >= -1e-3)

    def test_sweep_loader_supplies_placeholder_sweeps(self) -> None:
        real = _tree(height_km, elevs=[0.5, 1.5, 2.5])
        placeholders = xr.DataTree.from_dict(
            {
                "/sweep_0": real["sweep_0"].to_dataset(),
                "/sweep_1": xr.Dataset(attrs={"_sweep_variables": ["DBZH"]}),
                "/sweep_2": xr.Dataset(attrs={"_sweep_variables": ["DBZH"]}),
            }
        )
        without = extract_volume(placeholders, "DBZH", BOX, resolution=12, z_max=5000.0)
        with_loader = extract_volume(
            placeholders,
            "DBZH",
            BOX,
            resolution=12,
            z_max=5000.0,
            sweep_loader=lambda i: real[f"sweep_{i}"].to_dataset(),
        )
        full = extract_volume(real, "DBZH", BOX, resolution=12, z_max=5000.0)
        np.testing.assert_allclose(with_loader["data"], full["data"])
        assert (without["data"] > NODATA + 1).sum() < (full["data"] > NODATA + 1).sum()

    def test_split_cut_duplicate_elevations_collapse(self) -> None:
        """NEXRAD repeats 0.5 deg (surface + Doppler cut); that must not break the hat weights."""
        tree = _tree(height_km, elevs=[0.5, 0.5, 1.5, 1.5, 2.5])
        out = extract_volume(tree, "DBZH", BOX, resolution=12, z_max=3000.0)
        assert (out["data"] > NODATA + 1).any()
        assert np.isfinite(out["data"]).all()

    def test_performance_res_60(self) -> None:
        tree = _tree(height_km)
        t0 = time.perf_counter()
        extract_volume(tree, "DBZH", BOX, resolution=60)
        assert time.perf_counter() - t0 < 5.0


class TestCrossSection3D:
    def test_height_field_along_line(self) -> None:
        tree = _tree(height_km)
        out = extract_cross_section_3d(
            tree,
            "DBZH",
            {"x": -50000, "y": 10000},
            {"x": 50000, "y": 20000},
            width=3000.0,
            n_horizontal=40,
            n_vertical=30,
            z_max=9000.0,
        )
        grid = out["data"].reshape(out["n_dist"], out["n_z"])
        assert out["data"].dtype == np.float32
        valid = grid > NODATA + 1
        assert valid.mean() > 0.4
        gz = np.linspace(0, 9.0, 30)
        err = np.abs(grid - gz[None, :])[valid]
        assert np.median(err) < 0.08
        assert out["dist_max"] == pytest.approx(np.hypot(100000, 10000))

    def test_too_short_line_raises(self) -> None:
        with pytest.raises(ValueError, match="too close"):
            extract_cross_section_3d(_tree(height_km), "DBZH", {"x": 0, "y": 0}, {"x": 0.5, "y": 0})

    def test_no_data_returns_nodata(self) -> None:
        out = extract_cross_section_3d(
            _tree(height_km),
            "NOPE",
            {"x": 0, "y": 0},
            {"x": 5000, "y": 0},
            n_horizontal=10,
            n_vertical=5,
        )
        assert (out["data"] == NODATA).all()
        assert out["data"].shape == (50,)

    def test_width_averages_across_the_corridor(self) -> None:
        # value = signed distance across the corridor in the x direction
        def field(az, rng, h, s):
            x = s * np.sin(np.deg2rad(az))
            return np.broadcast_to(x / 1000.0, (az.shape[0], rng.shape[1]))

        tree = _tree(field, elevs=[1.0, 2.0, 3.0, 4.0])
        narrow = extract_cross_section_3d(
            tree,
            "DBZH",
            {"x": 0, "y": 20000},
            {"x": 0, "y": 60000},
            width=10.0,
            n_horizontal=10,
            n_vertical=6,
            z_max=3000.0,
        )
        wide = extract_cross_section_3d(
            tree,
            "DBZH",
            {"x": 0, "y": 20000},
            {"x": 0, "y": 60000},
            width=8000.0,
            n_horizontal=10,
            n_vertical=6,
            z_max=3000.0,
        )
        n = narrow["data"][narrow["data"] > NODATA + 1]
        w = wide["data"][wide["data"] > NODATA + 1]
        assert n.size and w.size
        assert abs(float(n.mean())) < 0.3
        assert abs(float(w.mean())) < 0.3  # symmetric corridor averages to ~0

"""Regression tests for engine.dualpol and engine.statistics."""

from __future__ import annotations

import json

import numpy as np
import pytest
import xarray as xr

from engine import dualpol, statistics


def _tree(
    dbz: np.ndarray | None = None,
    extra: dict[str, np.ndarray] | None = None,
    n_az: int = 360,
    n_rng: int = 20,
) -> xr.DataTree:
    az = np.linspace(0, 359, n_az)
    rng = 250.0 * np.arange(n_rng) + 125.0
    if dbz is None:
        dbz = np.broadcast_to(az[:, None], (n_az, n_rng)).copy()  # value == azimuth
    data = {"DBZH": dbz, **(extra or {})}
    ds = xr.Dataset(
        {k: (("azimuth", "range"), v.astype(np.float32)) for k, v in data.items()},
        coords={"azimuth": az, "range": rng},
        attrs={"sweep_fixed_angle": 0.5},
    )
    return xr.DataTree.from_dict({"/": xr.Dataset(), "/sweep_0": ds})


class TestRegionStats:
    N_RNG = 20

    def test_plain_sector(self) -> None:
        res = dualpol.region_stats(_tree(), "DBZH", az_min=10, az_max=20, range_max=1e6)
        assert res["n_total"] == 11 * self.N_RNG
        assert res["stats"]["min"] == pytest.approx(10.0)
        assert res["stats"]["max"] == pytest.approx(20.0)

    @pytest.mark.parametrize(("lo", "hi"), [(350, 10), (-10, 10), (350, 370)])
    def test_sector_through_north(self, lo: float, hi: float) -> None:
        res = dualpol.region_stats(_tree(), "DBZH", az_min=lo, az_max=hi, range_max=1e6)
        assert res["n_total"] == 21 * self.N_RNG  # rays 350..359 and 0..10
        assert res["stats"]["min"] == pytest.approx(0.0)
        assert res["stats"]["max"] == pytest.approx(359.0)

    def test_full_circle(self) -> None:
        res = dualpol.region_stats(_tree(), "DBZH", az_min=0, az_max=360, range_max=1e6)
        assert res["n_total"] == 360 * self.N_RNG
        res = dualpol.region_stats(_tree(), "DBZH", az_min=-180, az_max=180, range_max=1e6)
        assert res["n_total"] == 360 * self.N_RNG

    def test_empty_selection(self) -> None:
        res = dualpol.region_stats(_tree(), "DBZH", range_min=9e5, range_max=1e6)
        assert res["n_valid"] == 0
        assert res["n_total"] == 0
        assert res["stats"] == {}

    def test_all_nan_region_counts_total_but_not_valid(self) -> None:
        dbz = np.full((360, self.N_RNG), np.nan)
        res = dualpol.region_stats(_tree(dbz), "DBZH", az_min=0, az_max=359, range_max=1e6)
        assert res["n_valid"] == 0
        assert res["n_total"] == 360 * self.N_RNG


class TestHistogramAndScatter:
    def test_constant_field_is_json_safe(self) -> None:
        tree = _tree(np.full((360, 20), 7.0))
        res = dualpol.histogram_data(tree, "DBZH", bins=10)
        json.dumps(res, allow_nan=False)  # no NaN/Infinity from a zero-width Gaussian fit
        assert res["stats"]["std"] == 0.0
        assert sum(res["counts"]) == 360 * 20

    def test_scatter_is_deterministic_and_bounded(self) -> None:
        rng = np.random.default_rng(0)
        tree = _tree(
            rng.normal(size=(360, 20)), extra={"ZDR": rng.normal(size=(360, 20))}, n_rng=20
        )
        a = dualpol.scatter_data(tree, "DBZH", "ZDR", subsample=500)
        b = dualpol.scatter_data(tree, "DBZH", "ZDR", subsample=500)
        assert a["n_returned"] == 500
        assert a["x"] == b["x"]
        assert a["n_valid"] == 360 * 20


class TestStatistics:
    def test_histogram_with_nans(self) -> None:
        dbz = np.random.default_rng(0).normal(size=(360, 20))
        dbz[:, :5] = np.nan
        res = statistics.get_statistics(_tree(dbz), "DBZH", 0, "histogram", {"n_bins": 10})
        assert res["stats"]["count_valid"] == 360 * 15
        assert sum(res["counts"]) == 360 * 15

    def test_cell_stats_ignores_non_reflectivity_selection(self) -> None:
        dbz = np.full((360, 20), 25.0)
        vel = np.full((360, 20), 90.0)
        tree = _tree(dbz, extra={"VRADH": vel})
        res = statistics.get_statistics(tree, "VRADH", 0, "cell_stats")
        assert res["max_reflectivity"] == pytest.approx(25.0)

    def test_cell_stats_honours_explicit_reflectivity_name(self) -> None:
        tree = _tree(np.full((360, 20), 25.0), extra={"DBZ_corr": np.full((360, 20), 33.0)})
        res = statistics.compute_cell_stats(tree, dbz_variable="DBZ_corr")
        assert res["max_reflectivity"] == pytest.approx(33.0)

    def test_qc_stats(self) -> None:
        dbz = np.full((10, 10), 5.0)
        dbz[:2] = np.nan
        res = statistics.get_statistics(
            _tree(dbz, n_az=10, n_rng=10), "DBZH", 0, "qc", {"noise_floor": 10.0}
        )
        assert res["missing_gates"] == 20
        assert res["below_noise_gates"] == 80

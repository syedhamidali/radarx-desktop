"""Regression tests for engine.qc and engine.retrievals."""

from __future__ import annotations

import json
import warnings

import numpy as np
import pytest
import xarray as xr
from numpy.lib.stride_tricks import sliding_window_view
from scipy import ndimage

from engine import qc, retrievals


def _field(values: np.ndarray, *, full_circle: bool = True) -> xr.DataArray:
    n_az, n_rng = values.shape
    az = np.linspace(0, 359, n_az) if full_circle else np.linspace(0, 90, n_az)
    return xr.DataArray(
        values,
        dims=["azimuth", "range"],
        coords={"azimuth": az, "range": 250.0 * np.arange(n_rng) + 125.0},
        name="X",
    )


# ---------------------------------------------------------------------------
# QC
# ---------------------------------------------------------------------------


class TestDespeckle:
    def test_matches_generic_filter_reference_in_interior(self) -> None:
        rng = np.random.default_rng(1)
        v = rng.normal(20, 5, (60, 80))
        out = qc.despeckle(_field(v), window_size=5, threshold=1.5).values

        med = ndimage.median_filter(v, size=5)
        std = ndimage.generic_filter(v, np.nanstd, size=5)
        expected = np.abs(v - med) / std > 1.5
        inner = (slice(3, -3), slice(3, -3))
        np.testing.assert_array_equal(np.isnan(out)[inner], expected[inner])

    def test_missing_gates_do_not_cause_false_positives(self) -> None:
        v = np.full((40, 40), 20.0)
        v[15:20, 10:30] = np.nan  # a gap in otherwise constant data
        out = qc.despeckle(_field(v)).values
        assert np.isnan(out).sum() == np.isnan(v).sum()

    def test_isolated_spike_is_removed(self) -> None:
        v = np.full((30, 30), 10.0)
        v[0, 5] = 60.0
        out = qc.despeckle(_field(v)).values
        assert np.isnan(out[0, 5])
        assert np.isnan(out).sum() == 1

    def test_even_window_is_made_odd(self) -> None:
        out = qc.despeckle(_field(np.full((20, 20), 5.0)), window_size=4)
        assert "window=5" in out.attrs["qc_despeckle"]


class TestAzimuthWrap:
    def test_full_circle_detection(self) -> None:
        assert qc._is_full_circle(_field(np.zeros((360, 4)), full_circle=True))
        assert not qc._is_full_circle(_field(np.zeros((90, 4)), full_circle=False))
        assert qc._filter_modes(_field(np.zeros((360, 4))))[0] == "wrap"
        assert qc._filter_modes(_field(np.zeros((90, 4)), full_circle=False))[0] != "wrap"

    def test_texture_sees_across_north(self) -> None:
        v = np.zeros((360, 6))
        v[-1] = 1.0  # last ray differs from its neighbours across 359 -> 0
        valid = np.ones_like(v, dtype=bool)
        _, std_wrap = qc._local_nan_stats(v, valid, 3, ("wrap", "reflect"))
        _, std_nowrap = qc._local_nan_stats(v, valid, 3, ("reflect", "reflect"))
        assert std_wrap[0].max() > 0.4
        assert std_nowrap[0].max() == pytest.approx(0.0, abs=1e-9)

    def test_median_filter_wraps(self) -> None:
        v = np.zeros((10, 5))
        v[-1] = 9.0  # ray 9 is the neighbour of ray 0 across north
        v[1] = 9.0
        wrapped = qc._median_filter(v, 3, ("wrap", "reflect"))
        plain = qc._median_filter(v, 3, ("reflect", "reflect"))
        assert wrapped[0, 2] == 9.0
        assert plain[0, 2] == 0.0


class TestRemoveClutter:
    def test_texture_matches_reference(self) -> None:
        rng = np.random.default_rng(2)
        v = rng.normal(0, 3, (40, 50))
        valid = np.ones_like(v, dtype=bool)
        _, std = qc._local_nan_stats(v, valid, 3, ("wrap", "reflect"))
        ref = ndimage.generic_filter(v, np.nanstd, size=3)
        np.testing.assert_allclose(std[1:-1, 1:-1], ref[1:-1, 1:-1], atol=1e-9)

    def test_gap_edges_are_not_flagged_as_clutter(self) -> None:
        v = np.full((40, 40), 30.0)
        v[10:30, 10:30] = np.nan
        out = qc.remove_clutter(_field(v), texture_threshold=0.3).values
        assert np.isnan(out).sum() == np.isnan(v).sum()


class TestVelocityDealiasing:
    NYQ = 25.0

    def _aliased(self) -> tuple[xr.DataArray, np.ndarray]:
        az = np.deg2rad(np.linspace(0, 359, 180))[:, None]
        r = np.arange(200)[None, :]
        truth = 40.0 * np.sin(az) * (1 - r / 200.0)  # exceeds +-Nyquist over a broad area
        aliased = (truth + self.NYQ) % (2 * self.NYQ) - self.NYQ
        return _field(aliased), truth

    def test_unfolds_broad_aliased_regions(self) -> None:
        data, truth = self._aliased()
        assert np.abs(truth).max() > self.NYQ
        out = qc.velocity_dealiasing(data, self.NYQ).values
        assert np.mean(np.abs(out - truth) < 1e-6) > 0.99
        assert np.abs(out).max() > self.NYQ  # must be allowed to exceed +-Nyquist

    def test_missing_gates_stay_missing_and_do_not_break_unfolding(self) -> None:
        data, truth = self._aliased()
        vals = data.values.copy()
        rng = np.random.default_rng(0)
        gaps = rng.random(vals.shape) < 0.15
        vals[gaps] = np.nan
        out = qc.velocity_dealiasing(_field(vals), self.NYQ).values
        np.testing.assert_array_equal(np.isnan(out), gaps)
        ok = ~gaps
        assert np.mean(np.abs(out[ok] - truth[ok]) < 1e-6) > 0.98

    def test_unaliased_smooth_field_is_unchanged(self) -> None:
        az = np.deg2rad(np.linspace(0, 359, 90))[:, None]
        v = 10.0 * np.sin(az) * np.ones((1, 60))
        out = qc.velocity_dealiasing(_field(v), self.NYQ).values
        np.testing.assert_allclose(out, v)

    def test_all_nan_and_invalid_nyquist(self) -> None:
        out = qc.velocity_dealiasing(_field(np.full((10, 10), np.nan)), 10.0)
        assert np.isnan(out.values).all()
        with pytest.raises(ValueError, match="nyquist_vel"):
            qc.velocity_dealiasing(_field(np.zeros((5, 5))), 0.0)


class TestSmoothAndRange:
    def test_gaussian_not_biased_by_gaps(self) -> None:
        v = np.full((40, 60), 5.0)
        v[:, 20:25] = np.nan
        out = qc.smooth_data(_field(v), "gaussian", 2.0).values
        np.testing.assert_allclose(out[~np.isnan(v)], 5.0, atol=1e-9)
        assert np.isnan(out[:, 20:25]).all()

    def test_median_not_biased_by_gaps(self) -> None:
        v = np.full((40, 60), 5.0)
        v[10:30, 20:25] = np.nan
        out = qc.smooth_data(_field(v), "median", 3).values
        np.testing.assert_allclose(out[~np.isnan(v)], 5.0)

    def test_median_even_kernel_is_odd(self) -> None:
        assert qc._odd(4) == 5
        assert qc._odd(2) == 3

    def test_range_correction_values_and_list_input(self) -> None:
        d = _field(np.zeros((4, 5)))
        r = [1000.0, 2000.0, 4000.0, 8000.0, 16000.0]
        out = qc.range_correction(d, r).values
        np.testing.assert_allclose(out[0], 20 * np.log10(np.array(r) / r[0]))


# ---------------------------------------------------------------------------
# Retrievals
# ---------------------------------------------------------------------------


class TestKdp:
    def test_constant_kdp_recovered_at_edges_and_around_gaps(self) -> None:
        true_kdp = 2.0  # deg/km
        r_km = (250.0 * np.arange(120) + 125.0) / 1000.0
        phi = 2.0 * true_kdp * r_km[None, :] * np.ones((6, 1))
        phi[:, 50:55] = np.nan
        out = retrievals.estimate_kdp(_field(phi), window_len=11).values
        valid = ~np.isnan(phi)
        # Gates a full window away from the ray ends / the gap are exact ...
        far = ndimage.binary_erosion(valid, structure=np.ones((1, 13)), border_value=0)
        np.testing.assert_allclose(out[far], true_kdp, rtol=1e-6)
        # ... and edge gates stay the right sign and order of magnitude; the old
        # zero-padded convolution produced large negative KDP at the ray ends.
        assert (out[valid] > 0.4 * true_kdp).all()
        assert (out[valid] < 1.1 * true_kdp).all()
        assert np.isnan(out[~valid]).all()

    def test_even_window_and_bad_spacing_are_handled(self) -> None:
        phi = _field(np.tile(np.arange(30.0), (3, 1)))
        a = retrievals.estimate_kdp(phi, window_len=4)
        b = retrievals.estimate_kdp(phi, window_len=5)
        np.testing.assert_allclose(a.values, b.values)
        # a degenerate range coordinate must fall back to the default spacing
        flat = phi.assign_coords(range=("range", np.zeros(30)))
        assert np.isfinite(retrievals.estimate_kdp(flat).values).all()


class TestRainRates:
    @staticmethod
    def _da(v: float | list[float]) -> xr.DataArray:
        return xr.DataArray(np.atleast_1d(np.asarray(v, dtype=float)), dims=["range"])

    def test_z_zdr_uses_linear_zdr_ratio(self) -> None:
        z = 10 ** (3.5 * 0.927) * 6.7e-3  # 35 dBZ, ZDR = 0 dB
        out0 = retrievals.rain_rate_z_zdr(self._da(35.0), self._da(0.0)).values[0]
        out1 = retrievals.rain_rate_z_zdr(self._da(35.0), self._da(1.0)).values[0]
        assert out0 == pytest.approx(z, rel=1e-4)
        assert out1 == pytest.approx(z * 10 ** (-0.343), rel=1e-4)

    def test_z_r_marshall_palmer(self) -> None:
        out = retrievals.rain_rate_z(self._da([23.0103, np.nan])).values  # Z = 200
        assert out[0] == pytest.approx(1.0, rel=1e-3)
        assert np.isnan(out[1])

    def test_z_r_rejects_nonpositive_coefficients(self) -> None:
        with pytest.raises(ValueError):
            retrievals.rain_rate_z(self._da(30.0), a=0.0)

    def test_kdp_rain_negative_nan_and_no_warnings(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            out = retrievals.rain_rate_kdp(self._da([-1.0, 0.0, 1.0, np.nan])).values
        assert out[0] == 0.0
        assert out[1] == 0.0
        assert out[2] == pytest.approx(44.0)
        assert np.isnan(out[3])


class TestAttenuation:
    def test_offset_removed_monotonic_and_gaps_keep_reflectivity(self) -> None:
        n = 60
        dbz = np.full((2, n), 30.0)
        phi = 20.0 + np.linspace(0, 40, n)[None, :] * np.ones((2, 1))  # 20 deg offset
        phi[:, 30:35] = np.nan  # missing PhiDP must not drop the reflectivity
        out = retrievals.correct_attenuation_phidp(_field(dbz), _field(phi), alpha=0.1).values
        assert np.isfinite(out).all()
        assert out[0, 0] == pytest.approx(30.0, abs=0.5)  # no offset-induced bias
        assert np.all(np.diff(out[0]) >= -1e-9)  # PIA never decreases with range
        assert out[0, 32] == pytest.approx(out[0, 29], abs=1e-6)  # gap inherits PIA

    def test_missing_reflectivity_stays_missing(self) -> None:
        dbz = np.full((1, 10), 30.0)
        dbz[0, 3] = np.nan
        out = retrievals.correct_attenuation_phidp(_field(dbz), _field(np.ones((1, 10)))).values
        assert np.isnan(out[0, 3])
        assert np.isfinite(np.delete(out[0], 3)).all()


class TestEchoClassification:
    def test_texture_matches_rolling_nanstd(self) -> None:
        rng = np.random.default_rng(3)
        zdr = rng.normal(0, 3, (20, 60))
        dbz = np.full_like(zdr, 20.0)
        out = retrievals.classify_echo(
            _field(dbz), zdr=_field(zdr), texture_window=5, zdr_texture_thresh=2.5
        ).values
        std = sliding_window_view(zdr, 5, axis=-1).std(axis=-1)  # interior gates only
        expected_clutter = std > 2.5
        np.testing.assert_array_equal(out[:, 2:-2] == 2, expected_clutter)

    def test_nan_input_gives_no_echo(self) -> None:
        dbz = np.full((2, 10), np.nan)
        out = retrievals.classify_echo(_field(dbz)).values
        assert (out == 0).all()


def test_retrieval_outputs_are_json_safe_after_nan_to_none() -> None:
    out = retrievals.rain_rate_z(xr.DataArray(np.array([10.0, 20.0]), dims=["range"]))
    json.dumps(out.values.tolist(), allow_nan=False)

"""Tests for engine.temporal."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from engine.reader import RadarReader
from engine.temporal import (
    _sample_point,
    compute_accumulation,
    compute_difference,
    compute_time_series,
    compute_trend,
)

AZ = np.arange(0.0, 360.0, 1.0)
RNG = 250.0 * (1 + np.arange(40))


def _reader(
    values: np.ndarray,
    *,
    time: str | None = None,
    az=AZ,
    rng=RNG,
    var: str = "DBZH",
    units: str | None = None,
) -> RadarReader:
    attrs = {"units": units} if units else {}
    ds = xr.Dataset(
        {var: (("azimuth", "range"), values.astype(np.float32), attrs)},
        coords={"azimuth": az, "range": rng},
        attrs={"sweep_fixed_angle": 0.5},
    )
    root_attrs = {"time_coverage_start": time} if time else {}
    tree = xr.DataTree.from_dict({"/": xr.Dataset(attrs=root_attrs), "/sweep_0": ds})
    reader = RadarReader()
    reader._dtree = tree
    reader._format = "synthetic"
    return reader


def _const(v: float) -> np.ndarray:
    return np.full((AZ.size, RNG.size), v)


class TestSamplePoint:
    def test_azimuth_wraps_around_north(self) -> None:
        values = np.zeros((AZ.size, RNG.size))
        values[0] = 7.0  # ray at 0 deg
        values[359] = 3.0  # ray at 359 deg
        da = _reader(values).get_sweep_data(0, "DBZH")
        # 359.9 is 0.1 deg from the 0 deg ray but 0.9 from the 359 deg ray
        assert _sample_point(da, 359.9, RNG[3]) == 7.0
        assert _sample_point(da, 359.4, RNG[3]) == 3.0  # nearer the 359 deg ray

    def test_outside_range_coverage_is_none(self) -> None:
        da = _reader(_const(5.0)).get_sweep_data(0, "DBZH")
        assert _sample_point(da, 10.0, RNG[5]) == 5.0
        assert _sample_point(da, 10.0, RNG[-1] + 50_000.0) is None
        assert _sample_point(da, 10.0, 0.0 - 50_000.0) is None

    def test_sector_scan_gap_is_none(self) -> None:
        az = np.arange(30.0, 60.0, 0.5)
        da = _reader(np.full((az.size, RNG.size), 2.0), az=az).get_sweep_data(0, "DBZH")
        assert _sample_point(da, 45.0, RNG[2]) == 2.0
        assert _sample_point(da, 200.0, RNG[2]) is None

    def test_nan_is_none(self) -> None:
        values = _const(1.0)
        values[10, 4] = np.nan
        da = _reader(values).get_sweep_data(0, "DBZH")
        assert _sample_point(da, 10.0, RNG[4]) is None


class TestTimeSeries:
    def test_sorted_chronologically(self) -> None:
        readers = {
            "late": _reader(_const(3.0), time="2024-05-01T12:10:00Z"),
            "early": _reader(_const(1.0), time="2024-05-01T12:00:00Z"),
            "mid": _reader(_const(2.0), time="2024-05-01T12:05:00Z"),
        }
        ts = compute_time_series(readers, "DBZH", 0, 10.0, float(RNG[3]))
        assert ts["file_ids"] == ["early", "mid", "late"]
        assert ts["values"] == [1.0, 2.0, 3.0]

    def test_trend_uses_chronological_order(self) -> None:
        readers = {
            "c": _reader(_const(30.0), time="2024-05-01T12:20:00Z"),
            "a": _reader(_const(10.0), time="2024-05-01T12:00:00Z"),
            "b": _reader(_const(20.0), time="2024-05-01T12:10:00Z"),
        }
        trend = compute_trend(readers, "DBZH", 0, 10.0, float(RNG[3]))
        assert trend["slope"] == pytest.approx(10.0)
        assert trend["r_squared"] == pytest.approx(1.0)

    def test_failing_reader_gives_none_entry(self) -> None:
        readers = {"ok": _reader(_const(4.0), time="2024-01-01T00:00:00Z"), "empty": RadarReader()}
        ts = compute_time_series(readers, "DBZH", 0, 10.0, float(RNG[3]))
        assert ts["values"][ts["file_ids"].index("empty")] is None


def _rr(values: np.ndarray, **kw) -> RadarReader:
    """A reader whose variable is a linear quantity (rain rate), summed and averaged as is."""
    return _reader(values, var="RR", units="mm h-1", **kw)


class TestAccumulation:
    def _readers(self):
        a = _const(1.0)
        b = _const(3.0)
        c = _const(5.0)
        a[0, 0] = np.nan
        b[0, 0] = np.nan
        c[0, 1] = np.nan
        return {"a": _rr(a), "b": _rr(b), "c": _rr(c)}, np.stack([a, b, c])

    @pytest.mark.parametrize("method", ["sum", "mean", "max"])
    def test_matches_numpy(self, method: str) -> None:
        readers, stack = self._readers()
        out = compute_accumulation(readers, "RR", 0, method)
        assert out["averaged_in"] == "native"
        got = np.array(
            [[np.nan if v is None else v for v in row] for row in out["values"]], dtype=float
        )
        with np.errstate(all="ignore"):
            want = {"sum": np.nansum, "mean": np.nanmean, "max": np.nanmax}[method](stack, axis=0)
        want = np.where(np.isfinite(stack).any(axis=0), want, np.nan if method != "sum" else 0.0)
        np.testing.assert_allclose(got, want, rtol=1e-6, equal_nan=True)
        assert out["n_files"] == 3
        assert (out["n_az"], out["n_range"]) == (AZ.size, RNG.size)

    def test_cell_without_any_echo(self) -> None:
        a, b = _const(1.0), _const(3.0)
        a[2, 2] = b[2, 2] = np.nan
        readers = {"a": _rr(a), "b": _rr(b)}
        assert compute_accumulation(readers, "RR", 0, "max")["values"][2][2] is None
        assert compute_accumulation(readers, "RR", 0, "mean")["values"][2][2] is None
        assert compute_accumulation(readers, "RR", 0, "sum")["values"][2][2] == 0.0

    def test_shape_mismatch_trims_to_common(self) -> None:
        small = _rr(np.full((100, 20), 2.0), az=AZ[:100], rng=RNG[:20])
        full = _rr(_const(4.0))
        out = compute_accumulation({"s": small, "f": full}, "RR", 0, "mean")
        assert (out["n_az"], out["n_range"]) == (100, 20)
        assert out["values"][0][0] == pytest.approx(3.0)

    @pytest.mark.parametrize("units", ["dBZ", None])  # by units, else by the name DBZH
    def test_reflectivity_mean_is_linear(self, units: str | None) -> None:
        """Known answer: 20 and 40 dBZ are Z = 100 and 10000 mm6 m-3; the mean Z is 5050,
        i.e. 37.03 dBZ (the dBZ average 30 would understate it by 7 dB)."""
        readers = {"a": _reader(_const(20.0), units=units), "b": _reader(_const(40.0), units=units)}
        out = compute_accumulation(readers, "DBZH", 0, "mean")
        assert out["values"][0][0] == pytest.approx(10 * np.log10(5050.0), abs=1e-4)
        assert out["values"][0][0] == pytest.approx(37.03, abs=0.01)
        assert out["averaged_in"] == "linear"

    def test_reflectivity_max_is_unchanged(self) -> None:
        readers = {"a": _reader(_const(20.0)), "b": _reader(_const(40.0))}
        assert compute_accumulation(readers, "DBZH", 0, "max")["values"][0][0] == 40.0

    def test_summing_decibels_is_refused(self) -> None:
        readers = {"a": _reader(_const(20.0), units="dBZ"), "b": _reader(_const(40.0))}
        with pytest.raises(ValueError, match="decibels"):
            compute_accumulation(readers, "DBZH", 0, "sum")
        zdr = {"a": _reader(_const(1.0), var="ZDR", units="dB")}
        with pytest.raises(ValueError, match="decibels"):
            compute_accumulation(zdr, "ZDR", 0, "sum")

    def test_no_valid_data_reports_reason(self) -> None:
        with pytest.raises(ValueError, match="No valid data"):
            compute_accumulation({"x": RadarReader()}, "DBZH", 0, "sum")

    def test_unknown_method(self) -> None:
        with pytest.raises(ValueError, match="Unknown accumulation method"):
            compute_accumulation({"x": _reader(_const(1.0))}, "DBZH", 0, "median")  # type: ignore[arg-type]


class TestDifference:
    def test_difference_and_byte_counts(self) -> None:
        r1, r2 = _reader(_const(2.0)), _reader(_const(5.0))
        out = compute_difference(r1, r2, "DBZH", 0)
        diff = np.frombuffer(out["diff_values"], dtype=np.float32)
        assert diff.size == AZ.size * RNG.size
        assert np.all(diff == 3.0)
        assert out["vmin"] == -3.0 and out["vmax"] == 3.0
        assert out["diff_bytes"] == len(out["diff_values"])
        assert out["azimuth_bytes"] == len(out["azimuth"])
        assert out["range_bytes"] == len(out["range"])

    def test_nan_cells_become_nodata_and_shape_trimmed(self) -> None:
        a = _const(1.0)
        a[0, 0] = np.nan
        r1 = _reader(a)
        r2 = _reader(np.full((100, 20), 4.0), az=AZ[:100], rng=RNG[:20])
        out = compute_difference(r1, r2, "DBZH", 0)
        assert (out["n_az"], out["n_range"]) == (100, 20)
        diff = np.frombuffer(out["diff_values"], dtype=np.float32).reshape(100, 20)
        assert diff[0, 0] == -9999.0
        assert diff[1, 1] == 3.0


class TestDifferenceNoEcho:
    """NEXRAD fills 'no echo' with -33 dBZ (a real number); it must not be subtracted as data."""

    def test_no_echo_fill_is_masked_not_subtracted(self) -> None:
        a, b = _const(-33.0), _const(20.0)
        a[5, 5] = b[5, 5] = 10.0  # one real pair of echoes
        b[6, 6] = -33.0  # echo in 1, none in 2
        a[6, 6] = 25.0
        out = compute_difference(_reader(a), _reader(b), "DBZH", 0)
        diff = np.frombuffer(out["diff_values"], dtype=np.float32).reshape(AZ.size, RNG.size)
        assert diff[5, 5] == 0.0  # both echoes: a real difference
        assert diff[0, 0] == -9999.0  # no echo vs 20 dBZ: masked (was +53 dB)
        assert diff[6, 6] == -9999.0  # echo vs no echo: masked
        assert out["vmax"] < 1.0  # the colour range is not blown up by the fill

    def test_other_variables_keep_all_values(self) -> None:
        r1 = _reader(_const(-33.0))
        r2 = _reader(_const(2.0))
        for r in (r1, r2):  # a velocity-like variable has no no-echo sentinel
            r._dtree["sweep_0"] = xr.DataTree(
                r._dtree["sweep_0"].to_dataset().rename({"DBZH": "VRADH"})
            )
        out = compute_difference(r1, r2, "VRADH", 0)
        diff = np.frombuffer(out["diff_values"], dtype=np.float32)
        assert np.all(diff == 35.0)


def _field(az: np.ndarray, rng: np.ndarray) -> np.ndarray:
    """A field that is smooth in azimuth and range, so that any pair of grids can be compared."""
    a = np.radians(az)[:, None]
    return 30.0 + 15.0 * np.sin(2.0 * a) * np.cos(a) + 10.0 * (rng[None, :] / 1e5)


def _diff_grid(out: dict) -> np.ndarray:
    d = np.frombuffer(out["diff_values"], dtype=np.float32).reshape(out["n_az"], out["n_range"])
    return np.where(d == -9999.0, np.nan, d)


class TestDifferenceOfDifferentGrids:
    """Ray ``i`` of one scan is not ray ``i`` of another: 0.5 and 1 degree, a missing ray."""

    RNG_G = 250.0 * (1 + np.arange(80))

    def _pair(self, az1: np.ndarray, az2: np.ndarray):
        r1 = _reader(_field(az1, self.RNG_G), az=az1, rng=self.RNG_G)
        r2 = _reader(_field(az2, self.RNG_G), az=az2, rng=self.RNG_G)
        return r1, r2

    def test_one_and_half_degree_rays_give_no_difference_of_the_same_field(self) -> None:
        coarse, fine = np.arange(360) + 0.5, np.arange(720) * 0.5 + 0.25
        for first, second in ((coarse, fine), (fine, coarse)):
            r1, r2 = self._pair(first, second)
            out = compute_difference(r1, r2, "DBZH", 0)
            assert out["n_az"] == first.size  # on the grid of the first scan, all around
            assert np.nanmax(np.abs(_diff_grid(out))) < 0.5  # was 17 dB: half a circle vs a circle

    def test_a_missing_ray_does_not_shift_the_rest(self) -> None:
        full = np.arange(720) * 0.5 + 0.25
        gap = np.delete(full, 20)
        r1, r2 = self._pair(full, gap)
        out = compute_difference(r1, r2, "DBZH", 0)
        diff = _diff_grid(out)
        far = diff[100:]  # rays after the gap: index i is still the same direction
        assert np.nanmax(np.abs(far)) < 0.1  # was 0.17 here, tens of dB at a storm edge
        assert np.nanmax(np.abs(diff[20])) < 0.5  # the neighbour (0.5 deg) stands in for it

    def test_the_grid_of_the_second_scan_may_start_in_another_place(self) -> None:
        a = np.arange(360) + 0.5
        b = (np.arange(360) + 0.5 + 0.4) % 360.0
        b = np.sort(b)  # the first ray is now at 0.1 degrees
        r1, r2 = self._pair(a, b)
        assert np.nanmax(np.abs(_diff_grid(compute_difference(r1, r2, "DBZH", 0)))) < 0.5

    def test_gates_of_different_length_are_matched_by_distance(self) -> None:
        az = np.arange(360) + 0.5
        fine, coarse = 250.0 * (1 + np.arange(80)), 500.0 * (1 + np.arange(40)) - 125.0
        r1 = _reader(_field(az, fine), az=az, rng=fine)
        r2 = _reader(_field(az, coarse), az=az, rng=coarse)
        diff = _diff_grid(compute_difference(r1, r2, "DBZH", 0))
        assert diff.shape == (360, 80) and np.nanmax(np.abs(diff)) < 0.5

    def test_identical_grids_are_not_resampled(self) -> None:
        a = _const(1.0)
        a[3, 3] = 7.0
        out = compute_difference(_reader(a), _reader(_const(4.0)), "DBZH", 0)
        diff = _diff_grid(out)
        assert diff[3, 3] == -3.0 and diff[0, 0] == 3.0  # exact values: no interpolation

    def test_accumulation_uses_the_grid_of_the_first_scan(self) -> None:
        coarse, fine = np.arange(360) + 0.5, np.arange(720) * 0.5 + 0.25
        readers = {
            "a": _reader(_field(coarse, self.RNG_G), az=coarse, rng=self.RNG_G),
            "b": _reader(_field(fine, self.RNG_G), az=fine, rng=self.RNG_G),
        }
        out = compute_accumulation(readers, "DBZH", 0, "mean")
        values = np.array(
            [[np.nan if v is None else v for v in row] for row in out["values"]], dtype=np.float64
        )
        assert values.shape == (360, 80)
        assert np.nanmax(np.abs(values - _field(coarse, self.RNG_G))) < 0.5

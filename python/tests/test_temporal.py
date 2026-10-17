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


def _reader(values: np.ndarray, *, time: str | None = None, az=AZ, rng=RNG) -> RadarReader:
    ds = xr.Dataset(
        {"DBZH": (("azimuth", "range"), values.astype(np.float32))},
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


class TestAccumulation:
    def _readers(self):
        a = _const(1.0)
        b = _const(3.0)
        c = _const(5.0)
        a[0, 0] = np.nan
        b[0, 0] = np.nan
        c[0, 1] = np.nan
        return {"a": _reader(a), "b": _reader(b), "c": _reader(c)}, np.stack([a, b, c])

    @pytest.mark.parametrize("method", ["sum", "mean", "max"])
    def test_matches_numpy(self, method: str) -> None:
        readers, stack = self._readers()
        out = compute_accumulation(readers, "DBZH", 0, method)
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
        readers = {"a": _reader(a), "b": _reader(b)}
        assert compute_accumulation(readers, "DBZH", 0, "max")["values"][2][2] is None
        assert compute_accumulation(readers, "DBZH", 0, "mean")["values"][2][2] is None
        assert compute_accumulation(readers, "DBZH", 0, "sum")["values"][2][2] == 0.0

    def test_shape_mismatch_trims_to_common(self) -> None:
        small = _reader(np.full((100, 20), 2.0), az=AZ[:100], rng=RNG[:20])
        full = _reader(_const(4.0))
        out = compute_accumulation({"s": small, "f": full}, "DBZH", 0, "mean")
        assert (out["n_az"], out["n_range"]) == (100, 20)
        assert out["values"][0][0] == pytest.approx(3.0)

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

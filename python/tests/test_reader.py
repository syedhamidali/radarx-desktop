"""Tests for engine.reader.RadarReader."""

from __future__ import annotations

import gzip
import threading
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from engine.reader import (
    _FORMAT_TABLE,
    RadarReader,
    _guess_formats_by_extension,
    _serializable,
    sorted_sweep_names,
)
from tests import radar_data
from tests.conftest import ARCO_LAT, ARCO_LON, ARCO_SCAN_KEY, make_synthetic_datatree

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_synthetic_datatree() -> xr.DataTree:
    """Build a minimal synthetic radar DataTree that mimics xradar output."""
    n_azimuth = 360
    n_range = 500

    azimuth = np.linspace(0, 359, n_azimuth)
    range_m = np.linspace(0, 150_000, n_range)

    sweeps: dict[str, xr.Dataset] = {}
    for i, elev in enumerate([0.5, 1.5, 2.4]):
        ds = xr.Dataset(
            {
                "DBZH": (
                    ["azimuth", "range"],
                    np.random.default_rng(i)
                    .uniform(-20, 75, (n_azimuth, n_range))
                    .astype(np.float32),
                ),
                "VRADH": (
                    ["azimuth", "range"],
                    np.random.default_rng(i + 100)
                    .uniform(-30, 30, (n_azimuth, n_range))
                    .astype(np.float32),
                ),
            },
            coords={
                "azimuth": azimuth,
                "range": range_m,
                "elevation": ("azimuth", np.full(n_azimuth, elev, dtype=np.float32)),
            },
            attrs={"sweep_fixed_angle": elev},
        )
        sweeps[f"sweep_{i}"] = ds

    root_ds = xr.Dataset(attrs={"instrument_name": "SYNTHETIC"})
    dtree = xr.DataTree.from_dict({"/": root_ds, **{f"/{k}": v for k, v in sweeps.items()}})
    return dtree


# ---------------------------------------------------------------------------
# Tests: instantiation
# ---------------------------------------------------------------------------


class TestRadarReaderInstantiation:
    def test_create_reader(self) -> None:
        reader = RadarReader()
        assert reader.datatree is None

    def test_get_schema_without_open_raises(self) -> None:
        reader = RadarReader()
        with pytest.raises(RuntimeError, match="No file is currently open"):
            reader.get_schema()

    def test_get_sweep_data_without_open_raises(self) -> None:
        reader = RadarReader()
        with pytest.raises(RuntimeError, match="No file is currently open"):
            reader.get_sweep_data(0, "DBZH")


# ---------------------------------------------------------------------------
# Tests: schema extraction from synthetic data
# ---------------------------------------------------------------------------


class TestSchemaExtraction:
    @pytest.fixture()
    def reader_with_data(self) -> RadarReader:
        reader = RadarReader()
        reader._dtree = _make_synthetic_datatree()
        reader._path = "/fake/path.nc"
        reader._format = "synthetic"
        return reader

    def test_schema_has_required_keys(self, reader_with_data: RadarReader) -> None:
        schema = reader_with_data.get_schema()
        assert "variables" in schema
        assert "dimensions" in schema
        assert "attributes" in schema
        assert "sweeps" in schema

    def test_schema_variables(self, reader_with_data: RadarReader) -> None:
        schema = reader_with_data.get_schema()
        assert "DBZH" in schema["variables"]
        assert "VRADH" in schema["variables"]

    def test_schema_dimensions(self, reader_with_data: RadarReader) -> None:
        schema = reader_with_data.get_schema()
        assert "azimuth" in schema["dimensions"]
        assert "range" in schema["dimensions"]
        assert schema["dimensions"]["azimuth"] == 360
        assert schema["dimensions"]["range"] == 500

    def test_schema_sweeps(self, reader_with_data: RadarReader) -> None:
        schema = reader_with_data.get_schema()
        assert len(schema["sweeps"]) == 3
        elevations = [s["elevation"] for s in schema["sweeps"]]
        assert elevations == pytest.approx([0.5, 1.5, 2.4], abs=0.01)

    def test_schema_attributes(self, reader_with_data: RadarReader) -> None:
        schema = reader_with_data.get_schema()
        assert schema["attributes"].get("instrument_name") == "SYNTHETIC"


# ---------------------------------------------------------------------------
# Tests: sweep data access
# ---------------------------------------------------------------------------


class TestSweepDataAccess:
    @pytest.fixture()
    def reader_with_data(self) -> RadarReader:
        reader = RadarReader()
        reader._dtree = _make_synthetic_datatree()
        reader._path = "/fake/path.nc"
        reader._format = "synthetic"
        return reader

    def test_get_sweep_data_returns_dataarray(self, reader_with_data: RadarReader) -> None:
        da = reader_with_data.get_sweep_data(0, "DBZH")
        assert isinstance(da, xr.DataArray)
        assert da.shape == (360, 500)

    def test_get_sweep_data_invalid_sweep(self, reader_with_data: RadarReader) -> None:
        with pytest.raises(IndexError, match="out of range"):
            reader_with_data.get_sweep_data(99, "DBZH")

    def test_get_sweep_data_invalid_variable(self, reader_with_data: RadarReader) -> None:
        with pytest.raises(KeyError, match="not in sweep"):
            reader_with_data.get_sweep_data(0, "NONEXISTENT")

    def test_different_sweeps_return_different_data(self, reader_with_data: RadarReader) -> None:
        da0 = reader_with_data.get_sweep_data(0, "DBZH")
        da1 = reader_with_data.get_sweep_data(1, "DBZH")
        # Different RNG seeds should produce different data
        assert not np.array_equal(da0.values, da1.values)


# ---------------------------------------------------------------------------
# Tests: format guessing
# ---------------------------------------------------------------------------


class TestFormatGuessing:
    def test_nexrad_gz(self) -> None:
        formats = _guess_formats_by_extension("KTLX20200101_000000_V06.gz")
        assert formats[0] == "nexrad_level2"

    def test_cfradial_nc(self) -> None:
        formats = _guess_formats_by_extension("radar_data.nc")
        assert formats[0] == "cfradial1"

    def test_odim_h5(self) -> None:
        formats = _guess_formats_by_extension("radar.h5")
        assert formats[0] == "odim"

    def test_no_extension_tries_nexrad_first(self) -> None:
        formats = _guess_formats_by_extension("KTLX20200101_000000_V06")
        assert formats[0] == "nexrad_level2"

    def test_unknown_extension_tries_all(self) -> None:
        formats = _guess_formats_by_extension("radar.xyz")
        # Should include all formats as fallbacks
        assert len(formats) >= 6

    def test_open_file_nonexistent(self) -> None:
        reader = RadarReader()
        with pytest.raises(FileNotFoundError):
            reader.open_file("/nonexistent/file.nc")


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------


def _reader_for(tree: xr.DataTree) -> RadarReader:
    reader = RadarReader()
    reader._dtree = tree
    reader._path = "/fake.nc"
    reader._format = "synthetic"
    return reader


ELEVS = [0.5 + i for i in range(14)]


class TestSweepOrdering:
    """sweep_10 must come after sweep_9 (a lexicographic sort scrambles 11+ sweep volumes)."""

    def test_sorted_sweep_names_numeric(self) -> None:
        tree = make_synthetic_datatree(n_azimuth=4, n_range=4, elevations=ELEVS)
        names = sorted_sweep_names(tree)
        assert names == [f"sweep_{i}" for i in range(14)]

    def test_schema_elevations_follow_sweep_index(self) -> None:
        tree = make_synthetic_datatree(n_azimuth=4, n_range=4, elevations=ELEVS)
        schema = _reader_for(tree).get_schema()
        assert [s["elevation"] for s in schema["sweeps"]] == pytest.approx(ELEVS)

    def test_get_sweep_data_index_matches_elevation(self) -> None:
        tree = make_synthetic_datatree(n_azimuth=4, n_range=4, elevations=ELEVS)
        reader = _reader_for(tree)
        for idx in (2, 9, 10, 13):
            ds = reader.get_sweep_dataset(idx)
            assert float(ds.attrs["sweep_fixed_angle"]) == pytest.approx(ELEVS[idx])

    def test_non_sweep_children_ignored(self) -> None:
        tree = make_synthetic_datatree(n_azimuth=4, n_range=4)
        tree["radar_parameters"] = xr.DataTree(xr.Dataset(attrs={"x": 1}))
        assert sorted_sweep_names(tree) == ["sweep_0", "sweep_1", "sweep_2"]


class TestFormatGuessingRegressions:
    def test_no_duplicate_formats(self) -> None:
        # Unknown extension used to list every format twice, doubling slow failed opens.
        for name in ("radar.xyz", "KTLX20200101_000000_V06", "radar.h5", "a.nc", "x.RAW"):
            formats = _guess_formats_by_extension(name)
            assert len(formats) == len(set(formats)), name

    def test_unknown_extension_covers_all_formats_once(self) -> None:
        formats = _guess_formats_by_extension("radar.xyz")
        assert len(formats) == len(_FORMAT_TABLE)
        assert set(formats) == {label for label, _ in _FORMAT_TABLE}


class TestSweepDataset:
    def test_returns_full_dataset(self) -> None:
        reader = _reader_for(make_synthetic_datatree(n_azimuth=8, n_range=5))
        ds = reader.get_sweep_dataset(1)
        assert isinstance(ds, xr.Dataset)
        assert {"DBZH", "VRADH"} <= set(ds.data_vars)
        assert {"azimuth", "range", "elevation"} <= set(ds.coords)
        assert ds.sizes["azimuth"] == 8

    def test_out_of_range_and_unopened(self) -> None:
        reader = _reader_for(make_synthetic_datatree(n_azimuth=4, n_range=4))
        with pytest.raises(IndexError):
            reader.get_sweep_dataset(3)
        with pytest.raises(IndexError):
            reader.get_sweep_dataset(-1)
        with pytest.raises(RuntimeError, match="No file is currently open"):
            RadarReader().get_sweep_dataset(0)

    def test_get_sweep_data_matches_dataset(self) -> None:
        reader = _reader_for(make_synthetic_datatree(n_azimuth=8, n_range=5))
        np.testing.assert_array_equal(
            reader.get_sweep_data(2, "DBZH").values, reader.get_sweep_dataset(2)["DBZH"].values
        )


class TestNodeCache:
    def test_nodes_cached_until_tree_replaced(self) -> None:
        reader = _reader_for(make_synthetic_datatree(n_azimuth=4, n_range=4))
        first = reader._sweep_nodes()
        assert reader._sweep_nodes() is first
        # The server swaps trees by plain attribute assignment; caches must follow.
        reader._dtree = make_synthetic_datatree(n_azimuth=4, n_range=4, elevations=[9.0])
        nodes = reader._sweep_nodes()
        assert nodes is not first
        assert len(nodes) == 1
        assert reader.get_schema()["sweeps"][0]["elevation"] == pytest.approx(9.0)

    def test_close_drops_state(self) -> None:
        reader = _reader_for(make_synthetic_datatree(n_azimuth=4, n_range=4))
        reader.get_schema()
        reader.close()
        assert reader.datatree is None
        with pytest.raises(RuntimeError):
            reader.get_schema()


def _write_generic_netcdf(path: Path) -> None:
    ds = xr.Dataset(
        {
            "Z": (("radial", "gate"), np.arange(12, dtype=np.float32).reshape(4, 3)),
            "radialAzim": ("radial", np.array([0.0, 90.0, 180.0, 270.0])),
        },
        attrs={"siteLat": 12.5, "siteLon": 77.5},
    )
    ds["gateSize"] = 250.0
    ds["firstGateRange"] = 500.0
    ds["elevationAngle"] = 1.5
    ds.to_netcdf(path)


class TestOpenFile:
    def test_generic_netcdf_fallback_and_failed_open_keeps_previous_file(
        self, tmp_path: Path
    ) -> None:
        good = tmp_path / "ok.nc"
        _write_generic_netcdf(good)
        reader = RadarReader()
        schema = reader.open_file(str(good))
        assert reader._format == "generic_netcdf"
        assert schema["variables"] == ["Z"]
        previous_tree = reader.datatree

        bad = tmp_path / "garbage.nc"
        bad.write_bytes(b"this is not a radar file" * 10)
        with pytest.raises(ValueError, match="Could not open") as excinfo:
            reader.open_file(str(bad))
        # error carries the per-format reasons instead of only the last one
        assert "cfradial1" in str(excinfo.value) or "nexrad" in str(excinfo.value)
        assert reader.datatree is previous_tree
        assert reader.get_schema()["variables"] == ["Z"]

    def test_gzip_file_is_decompressed_and_temp_removed(self, tmp_path: Path) -> None:
        plain = tmp_path / "ok.nc"
        _write_generic_netcdf(plain)
        gz = tmp_path / "ok.nc.gz"
        gz.write_bytes(gzip.compress(plain.read_bytes()))
        import tempfile

        before = set(Path(tempfile.gettempdir()).glob("tmp*_ok.nc"))
        reader = RadarReader()
        schema = reader.open_file(str(gz))
        assert schema["variables"] == ["Z"]
        # data must stay readable after the temp copy was unlinked
        assert reader.get_sweep_data(0, "Z").values.shape == (4, 3)
        assert set(Path(tempfile.gettempdir()).glob("tmp*_ok.nc")) == before

    def test_directory_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="directory"):
            RadarReader().open_file(str(tmp_path))

    def test_relative_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            RadarReader().open_file("relative/file.nc")


class TestSerializable:
    def test_nan_inside_arrays_and_lists_becomes_none(self) -> None:
        assert _serializable(np.array([1.0, np.nan, np.inf])) == [1.0, None, None]
        assert _serializable([1, float("nan")]) == [1, None]
        assert _serializable({"a": np.float32(2.5), "b": np.int16(3)}) == {"a": 2.5, "b": 3}
        assert _serializable(np.bool_(True)) is True
        assert _serializable(b"abc") == "abc"


class TestSiteCoordinates:
    def test_root_data_var_site_coords(self) -> None:
        tree = make_synthetic_datatree(n_azimuth=4, n_range=4)
        tree.dataset = xr.Dataset({"latitude": 10.5, "longitude": 20.25})
        attrs = _reader_for(tree).get_schema()["attributes"]
        assert attrs["latitude"] == pytest.approx(10.5)
        assert attrs["longitude"] == pytest.approx(20.25)


# ---------------------------------------------------------------------------
# ARCO lazy loading (in-memory icechunk store)
# ---------------------------------------------------------------------------


class TestArco:
    def test_open_orders_sweeps_numerically_and_exposes_site(self, fake_arco) -> None:
        reader = RadarReader()
        schema = reader.open_arco_scan("KLOT", ARCO_SCAN_KEY)
        assert [s["elevation"] for s in schema["sweeps"]] == pytest.approx(
            [0.5 + i for i in range(12)]
        )
        assert schema["variables"] == ["DBZH"]
        assert schema["attributes"]["latitude"] == pytest.approx(ARCO_LAT)
        assert schema["attributes"]["longitude"] == pytest.approx(ARCO_LON)
        assert schema["station"] == "KLOT"
        assert reader._format == "nexrad_arco"
        # site coordinates are also on the tree root so engine helpers can find them
        from engine.processor import _get_radar_location

        assert _get_radar_location(reader.datatree) == pytest.approx((ARCO_LAT, ARCO_LON))

    def test_only_sweep_zero_loaded_eagerly(self, fake_arco) -> None:
        reader = RadarReader()
        reader.open_arco_scan("KLOT", ARCO_SCAN_KEY)
        assert reader._arco_loaded_sweeps == {}
        placeholder = reader.datatree["sweep_5"].to_dataset()
        assert not placeholder.data_vars

    def test_lazy_sweep_returns_last_vcp_time(self, fake_arco) -> None:
        reader = RadarReader()
        reader.open_arco_scan("KLOT", ARCO_SCAN_KEY)
        da = reader.get_sweep_data(11, "DBZH")  # index 11 (not lexicographic position)
        assert "vcp_time" not in da.dims
        assert float(da.values[0, 0]) == pytest.approx(10.0 * 11 + 1)  # t = n_times-1
        ds = reader.get_sweep_dataset(10)
        assert {"azimuth", "range"} <= set(ds.coords)
        assert float(ds["DBZH"].values[0, 0]) == pytest.approx(101.0)
        with pytest.raises(KeyError, match="not in sweep"):
            reader.get_sweep_data(3, "NOPE")

    def test_concurrent_loads_open_each_sweep_once(self, fake_arco, monkeypatch) -> None:
        reader = RadarReader()
        reader.open_arco_scan("KLOT", ARCO_SCAN_KEY)

        calls: list[str] = []
        real_open = xr.open_dataset
        gate = threading.Barrier(8)

        def counting_open(*args, **kwargs):
            calls.append(kwargs.get("group", ""))
            return real_open(*args, **kwargs)

        monkeypatch.setattr(xr, "open_dataset", counting_open)
        results: list[float] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                gate.wait()
                results.append(float(reader.get_sweep_data(7, "DBZH").values[1, 1]))
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert results == [71.0] * 8
        assert len(calls) == 1

    def test_new_open_resets_arco_state(self, fake_arco) -> None:
        reader = RadarReader()
        reader.open_arco_scan("KLOT", ARCO_SCAN_KEY)
        reader.get_sweep_data(4, "DBZH")
        assert 4 in reader._arco_loaded_sweeps
        reader._commit(make_synthetic_datatree(n_azimuth=4, n_range=4), "/x.nc", "synthetic")
        assert reader._arco_loaded_sweeps == {}
        assert reader._arco_session is None
        assert reader.get_sweep_data(0, "DBZH").shape == (4, 4)


class TestMaskReservedCodes:
    """Integer-encoded moments: raw 0 (no echo) and raw 1 (range folded) are not measurements."""

    @staticmethod
    def _coded(codes: np.ndarray, scale: float, offset: float) -> xr.DataArray:
        values = offset + codes.astype(np.float64) * scale
        da = xr.DataArray(values, dims=("azimuth", "range"), attrs={"units": "dBZ"})
        da.encoding.update(
            {"dtype": np.dtype("uint8"), "scale_factor": scale, "add_offset": offset}
        )
        return da

    def test_codes_0_and_1_become_nan_real_values_survive(self) -> None:
        from engine.reader import mask_variable

        codes = np.array([[0, 1, 2, 3, 100, 255]])
        out = mask_variable(self._coded(codes, 0.5, -33.0))
        assert np.isnan(out.values[0, :2]).all()
        np.testing.assert_allclose(out.values[0, 2:], [-32.0, -31.5, 17.0, 94.5])
        assert out.attrs["units"] == "dBZ"

    def test_negative_scale_and_unencoded_arrays(self) -> None:
        from engine.reader import mask_variable

        out = mask_variable(self._coded(np.array([[0, 1, 2, 3]]), -0.5, 10.0))
        assert np.isnan(out.values[0, :2]).all() and np.isfinite(out.values[0, 2:]).all()
        plain = xr.DataArray(np.array([[-33.0, 1.0]]), dims=("azimuth", "range"))
        assert (
            mask_variable(plain) is plain
        )  # no encoding -> untouched (synthetic / pre-masked data)

    def test_dataset_masks_every_2d_variable(self) -> None:
        from engine.reader import mask_below_threshold

        ds = xr.Dataset({"DBZH": self._coded(np.array([[0, 5]]), 0.5, -33.0)})
        assert np.isnan(mask_below_threshold(ds)["DBZH"].values[0, 0])

    _KATX = radar_data.katx() or Path("/nonexistent/KATX20130717_195021_V06")

    @pytest.mark.skipif(not _KATX.exists(), reason="real NEXRAD sample not available")
    def test_real_nexrad_sweep_is_mostly_no_echo(self) -> None:
        reader = RadarReader()
        reader.open_file(str(self._KATX))
        v = reader.get_sweep_data(0, "DBZH").values
        assert np.isnan(v).mean() > 0.8  # ~93 % of gates are "below threshold"
        assert np.nanmin(v) >= -32.0  # no -33 fill left
        # the raw (unmasked) view is untouched
        raw = reader.get_sweep_dataset(0, mask=False)["DBZH"].values
        assert np.isfinite(raw).all() and raw.min() == -33.0

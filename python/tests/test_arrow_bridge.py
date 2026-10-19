"""Tests for engine.arrow_bridge — numpy-to-arrow conversion correctness."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

# pyarrow is an optional extra (pip install ".[arrow]"): the app does not use it at run time.
pa = pytest.importorskip("pyarrow")
ipc = pytest.importorskip("pyarrow.ipc")

from engine.arrow_bridge import (  # noqa: E402
    _to_flat_arrow,
    dataarray_to_arrow,
    dataset_to_arrow,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_ipc_bytes(raw: bytes) -> pa.RecordBatch:
    """Deserialise Arrow IPC stream bytes back to a RecordBatch."""
    reader = ipc.open_stream(raw)
    batches = list(reader)
    assert len(batches) == 1, f"Expected 1 batch, got {len(batches)}"
    return batches[0]


# ---------------------------------------------------------------------------
# _to_flat_arrow
# ---------------------------------------------------------------------------


class TestToFlatArrow:
    def test_1d_float(self) -> None:
        arr = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        result = _to_flat_arrow(arr, "test")
        assert result is not None
        assert len(result) == 3
        assert result.to_pylist() == pytest.approx([1.0, 2.0, 3.0])

    def test_2d_is_flattened(self) -> None:
        arr = np.array([[1, 2], [3, 4]], dtype=np.int64)
        result = _to_flat_arrow(arr, "test")
        assert result is not None
        assert len(result) == 4
        assert result.to_pylist() == [1, 2, 3, 4]

    def test_int_types(self) -> None:
        for dtype in [np.int8, np.int16, np.int32, np.int64]:
            arr = np.array([10, 20], dtype=dtype)
            result = _to_flat_arrow(arr, f"test_{dtype}")
            assert result is not None
            assert result.to_pylist() == [10, 20]

    def test_float_types(self) -> None:
        for dtype in [np.float32, np.float64]:
            arr = np.array([1.5, 2.5], dtype=dtype)
            result = _to_flat_arrow(arr, f"test_{dtype}")
            assert result is not None
            assert result.to_pylist() == pytest.approx([1.5, 2.5])

    def test_nan_preserved(self) -> None:
        arr = np.array([1.0, np.nan, 3.0], dtype=np.float64)
        result = _to_flat_arrow(arr, "test")
        assert result is not None
        values = result.to_pylist()
        assert values[0] == pytest.approx(1.0)
        assert values[2] == pytest.approx(3.0)
        # NaN check
        assert values[1] != values[1]  # NaN != NaN


# ---------------------------------------------------------------------------
# dataset_to_arrow
# ---------------------------------------------------------------------------


class TestDatasetToArrow:
    def test_roundtrip_values(self) -> None:
        ds = xr.Dataset(
            {"temperature": (["x"], np.array([10.0, 20.0, 30.0], dtype=np.float64))},
            coords={"x": [0, 1, 2]},
        )
        raw = dataset_to_arrow(ds)
        batch = _read_ipc_bytes(raw)

        assert "temperature" in batch.schema.names
        assert "__coord_x" in batch.schema.names
        np.testing.assert_array_almost_equal(
            batch.column("temperature").to_numpy(), [10.0, 20.0, 30.0]
        )

    def test_multiple_variables(self) -> None:
        ds = xr.Dataset(
            {
                "a": (["x"], np.array([1.0, 2.0])),
                "b": (["x"], np.array([3.0, 4.0])),
            }
        )
        raw = dataset_to_arrow(ds)
        batch = _read_ipc_bytes(raw)
        assert "a" in batch.schema.names
        assert "b" in batch.schema.names

    def test_empty_dataset(self) -> None:
        ds = xr.Dataset()
        raw = dataset_to_arrow(ds)
        batch = _read_ipc_bytes(raw)
        assert batch.num_columns == 0

    def test_2d_variable_is_flattened(self) -> None:
        data = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
        ds = xr.Dataset({"grid": (["y", "x"], data)})
        raw = dataset_to_arrow(ds)
        batch = _read_ipc_bytes(raw)
        # Flattened: 4 elements
        assert batch.column("grid").to_pylist() == pytest.approx([1.0, 2.0, 3.0, 4.0])


# ---------------------------------------------------------------------------
# dataarray_to_arrow
# ---------------------------------------------------------------------------


class TestDataArrayToArrow:
    def test_named_array(self) -> None:
        da = xr.DataArray(
            np.array([5.0, 10.0, 15.0]),
            dims=["x"],
            coords={"x": [0, 1, 2]},
            name="speed",
        )
        raw = dataarray_to_arrow(da)
        batch = _read_ipc_bytes(raw)
        assert "speed" in batch.schema.names
        np.testing.assert_array_almost_equal(batch.column("speed").to_numpy(), [5.0, 10.0, 15.0])

    def test_unnamed_array_uses_data(self) -> None:
        da = xr.DataArray(np.array([1.0, 2.0]), dims=["x"])
        raw = dataarray_to_arrow(da)
        batch = _read_ipc_bytes(raw)
        assert "data" in batch.schema.names

    def test_coords_included(self) -> None:
        da = xr.DataArray(
            np.array([1.0, 2.0]),
            dims=["x"],
            coords={"x": [10, 20]},
            name="vals",
        )
        raw = dataarray_to_arrow(da)
        batch = _read_ipc_bytes(raw)
        assert "__coord_x" in batch.schema.names

    def test_synthetic_radar_data(self, synthetic_dataarray: xr.DataArray) -> None:
        """Ensure a realistic radar DataArray survives the arrow roundtrip."""
        raw = dataarray_to_arrow(synthetic_dataarray)
        batch = _read_ipc_bytes(raw)

        # Data column should exist
        assert "DBZH" in batch.schema.names
        # Flattened length should equal azimuth * range
        expected_len = 360 * 500
        assert batch.column("DBZH").to_pylist()[:10]  # just check it's non-empty
        assert len(batch.column("DBZH")) == expected_len

    def test_float32_precision_preserved(self) -> None:
        """Float32 values should not be mangled during conversion."""
        original = np.array([0.123456789, -99.5, 1e-6], dtype=np.float32)
        da = xr.DataArray(original, dims=["x"], name="precise")
        raw = dataarray_to_arrow(da)
        batch = _read_ipc_bytes(raw)
        recovered = np.array(batch.column("precise").to_pylist(), dtype=np.float32)
        np.testing.assert_array_equal(original, recovered)


# ---------------------------------------------------------------------------
# Padding regressions
# ---------------------------------------------------------------------------


class TestPadding:
    def test_integer_coord_shorter_than_variable(self) -> None:
        """Padding used to go through np.nan and crashed for integer columns."""
        ds = xr.Dataset(
            {"grid": (["y", "x"], np.arange(6, dtype=np.float32).reshape(2, 3))},
            coords={"x": [0, 1, 2], "y": [10, 20]},
        )
        batch = _read_ipc_bytes(dataset_to_arrow(ds))
        assert batch.num_rows == 6
        assert batch.schema.field("__coord_x").type == pa.int64()  # type preserved
        assert batch.column("__coord_x").to_pylist() == [0, 1, 2, None, None, None]
        assert batch.column("__coord_y").to_pylist() == [10, 20, None, None, None, None]
        assert batch.column("grid").to_pylist() == pytest.approx([0, 1, 2, 3, 4, 5])

    def test_dataarray_padding_keeps_types(self) -> None:
        da = xr.DataArray(
            np.arange(6, dtype=np.float32).reshape(2, 3),
            dims=["y", "x"],
            coords={"x": [5, 6, 7]},
            name="v",
        )
        batch = _read_ipc_bytes(dataarray_to_arrow(da))
        assert batch.schema.field("v").type == pa.float32()
        assert batch.schema.field("__coord_x").type == pa.int64()
        assert batch.column("__coord_x").null_count == 3

    def test_equal_length_columns_untouched(self) -> None:
        ds = xr.Dataset({"a": (["x"], np.array([1, 2], dtype=np.int32))}, coords={"x": [3, 4]})
        batch = _read_ipc_bytes(dataset_to_arrow(ds))
        assert batch.column("a").null_count == 0
        assert batch.schema.field("a").type == pa.int32()

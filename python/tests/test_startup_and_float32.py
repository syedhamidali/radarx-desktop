"""Start-up imports stay lazy, and the opt-in float32 decoding keeps its promises (issue #97)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from engine import hdf5_guard, provenance, reader

PY_DIR = Path(__file__).resolve().parents[1]


def _encoded_moment(step: float = 0.5, offset: float = -33.0) -> xr.DataArray:
    """A Level-II-like moment: float64 values decoded from uint8 codes, 0 and 1 are reserved."""
    rng = np.random.default_rng(7)
    codes = rng.integers(0, 255, size=(36, 60))
    codes[0, :5] = [0, 1, 0, 1, 2]
    values = offset + codes * step
    da = xr.DataArray(values.astype("float64"), dims=("azimuth", "range"), attrs={"units": "dBZ"})
    da.encoding = {"scale_factor": step, "add_offset": offset, "dtype": "uint8"}
    return da


class TestImportsStayLazy:
    def test_importing_the_server_does_not_load_the_hdf5_libraries_or_pillow(self) -> None:
        code = (
            "import sys, server; "
            "print(','.join(m for m in ('netCDF4','h5netcdf','h5py','PIL','scipy','matplotlib',"
            "'xradar','radarx') if m in sys.modules))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=PY_DIR, capture_output=True, text=True, timeout=120
        )
        assert out.returncode == 0, out.stderr[-500:]
        loaded = set(out.stdout.strip().split(",")) - {""}
        assert not loaded, f"imported at start-up: {sorted(loaded)}"

    def test_the_guard_is_still_installed_without_those_imports(self) -> None:
        assert hdf5_guard.install() is True

    def test_openers_are_recognised_by_name(self) -> None:
        netcdf4 = pytest.importorskip("netCDF4")
        assert hdf5_guard._is_guarded_opener(netcdf4.Dataset)
        h5netcdf = pytest.importorskip("h5netcdf")
        assert hdf5_guard._is_guarded_opener(h5netcdf.File)
        assert not hdf5_guard._is_guarded_opener(open)
        assert not hdf5_guard._is_guarded_opener(xr.Dataset)

    def test_the_picture_code_still_works_with_pillow_imported_late(self) -> None:
        code = (
            "import numpy as np\n"
            "from engine import renderer\n"
            "img = renderer.Image.fromarray(np.zeros((4, 4, 4), np.uint8))\n"
            "print(img.size)\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=PY_DIR, capture_output=True, text=True, timeout=120
        )
        assert out.returncode == 0, out.stderr[-500:]
        assert out.stdout.strip() == "(4, 4)"


class TestFloat32Decoding:
    def test_default_stays_float64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XRADAR_FLOAT32", raising=False)
        assert not reader.decode_float32()
        assert reader.mask_variable_lazy(_encoded_moment()).values.dtype == np.float64
        assert reader.mask_variable(_encoded_moment()).values.dtype == np.float64

    @pytest.mark.parametrize("step", [0.5, 0.1, 0.25, 1.0 / 3.0])
    @pytest.mark.parametrize("lazy", [True, False])
    def test_same_gates_masked_and_values_within_float32_rounding(
        self, monkeypatch: pytest.MonkeyPatch, step: float, lazy: bool
    ) -> None:
        fn = reader.mask_variable_lazy if lazy else reader.mask_variable
        monkeypatch.delenv("XRADAR_FLOAT32", raising=False)
        ref = fn(_encoded_moment(step)).values
        monkeypatch.setenv("XRADAR_FLOAT32", "1")
        out = fn(_encoded_moment(step)).values
        assert out.dtype == np.float32
        assert np.array_equal(np.isnan(ref), np.isnan(out))  # the reserved codes stay no-echo
        assert np.isnan(ref).sum() >= 4
        ok = ~np.isnan(ref)
        # at most half a float32 step: 6e-8 of the value
        assert np.all(
            np.abs(ref[ok] - out[ok].astype(np.float64)) <= 6e-8 * np.abs(ref[ok]) + 1e-12
        )

    def test_lazy_array_reports_float32_before_it_is_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XRADAR_FLOAT32", "1")
        lazy = reader.mask_variable_lazy(_encoded_moment())
        assert lazy.dtype == np.float32  # without touching the data

    def test_a_moment_without_an_encoding_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XRADAR_FLOAT32", "1")
        plain = xr.DataArray(np.ones((3, 3)), dims=("azimuth", "range"))
        assert reader.mask_variable(plain).dtype == np.float64  # only encoded moments change

    def test_the_setting_is_written_to_the_record_only_when_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XRADAR_FLOAT32", raising=False)
        assert "numeric_precision" not in provenance.standard_global_attrs(title="t")
        assert "numeric_precision" not in provenance.json_provenance(action="a")
        monkeypatch.setenv("XRADAR_FLOAT32", "1")
        assert "float32" in provenance.standard_global_attrs(title="t")["numeric_precision"]
        assert "float32" in provenance.json_provenance(action="a")["numeric_precision"]

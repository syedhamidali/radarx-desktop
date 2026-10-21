"""The app reopens its own Zarr export (CfRadial2 layout) as it reopens its CfRadial2 NetCDF."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from engine.exporter import RadarExporter
from engine.reader import RadarReader, is_zarr_store, mask_variable

from .test_output_standards import make_cf_volume

pytest.importorskip("zarr")


def _text(values: np.ndarray) -> list:
    return [v.decode() if isinstance(v, bytes) else v for v in np.asarray(values).ravel().tolist()]


def _export(tree: xr.DataTree, fmt: str, path: Path, **options) -> str:
    return RadarExporter().export(tree, fmt, output_path=str(path), options=options or None)


@pytest.fixture(scope="module")
def volume() -> xr.DataTree:
    return make_cf_volume()


@pytest.fixture(scope="module")
def opened(volume, tmp_path_factory):
    """The volume exported as CfRadial2 NetCDF and as Zarr (v2 and v3), each reopened."""
    tmp = tmp_path_factory.mktemp("zarr")
    out = {}
    for key, fmt, name, opts in (
        ("nc", "cfradial2", "v.nc", {}),
        ("zarr2", "zarr", "v.zarr", {}),
        ("zarr3", "zarr", "v3.zarr", {"zarr_format": 3}),
    ):
        path = _export(volume, fmt, tmp / name, **opts)
        reader = RadarReader()
        schema = reader.open_file(str(Path(path).resolve()))
        out[key] = (reader, schema, path)
    return out


@pytest.mark.parametrize("key", ["zarr2", "zarr3"])
def test_zarr_export_reopens_like_the_cfradial2_export(opened, key):
    nc_reader, nc_schema, _ = opened["nc"]
    z_reader, z_schema, path = opened[key]
    assert is_zarr_store(path)
    assert z_reader._format == "zarr"
    assert len(z_schema["sweeps"]) == len(nc_schema["sweeps"]) == 3
    want_tree, got_tree = nc_reader.datatree, z_reader.datatree
    assert set(got_tree.children) == set(want_tree.children)
    want_root, got_root = want_tree["/"].to_dataset(), got_tree["/"].to_dataset()
    assert set(got_root.variables) == set(want_root.variables)
    want_attrs = {k for k in want_tree.attrs if k != "history"}  # history names the export
    assert want_attrs <= set(got_tree.attrs) and "export Zarr" in got_tree.attrs["history"]
    for name in want_attrs:
        assert str(got_tree.attrs[name]) == str(want_tree.attrs[name]), name
    for s in range(3):
        got = got_tree[f"sweep_{s}"].to_dataset()
        want = want_tree[f"sweep_{s}"].to_dataset()
        assert set(got.data_vars) == set(want.data_vars)
        assert got["DBZH"].dims == want["DBZH"].dims == ("azimuth", "range")
        for name in ("azimuth", "range", "elevation", "time"):
            np.testing.assert_array_equal(got[name].values, want[name].values)
        for name in want.data_vars:
            a, b = got[name], want[name]
            if a.dtype.kind in "fc":
                np.testing.assert_array_equal(a.values, b.values)
            else:  # text: str in Zarr, bytes (char arrays) in NetCDF
                assert _text(a.values) == _text(b.values), name
            for attr in ("units", "standard_name", "long_name"):
                assert a.attrs.get(attr) == b.attrs.get(attr), (name, attr)


def test_zarr_values_match_the_source(opened, volume):
    """Known answer: the reopened sweep is the source, rays in time order, no-echo code masked."""
    reader, _, _ = opened["zarr2"]
    got = reader.get_sweep_data(0, "DBZH").values
    src = volume["sweep_0"].to_dataset()
    order = np.argsort(src["time"].values, kind="stable")
    raw = src["DBZH"].values[order]
    np.testing.assert_allclose(got, mask_variable(src["DBZH"]).values[order], equal_nan=True)
    assert np.isnan(got[raw <= -33.0]).all()  # the "no echo" code is not data
    ok = np.isfinite(got)
    np.testing.assert_array_equal(got[ok], raw[ok])  # every kept value exactly as measured


def test_python_api_and_command_line_take_a_zarr_store(opened):
    from engine import api
    from engine.cli import expand_inputs

    path = opened["zarr2"][2]
    vol = api.open(path)
    assert vol.n_sweeps == 3
    assert expand_inputs([path]) == [path]  # one volume, not the files inside it


def test_plain_folder_is_still_refused(tmp_path):
    (tmp_path / "a.nc").write_bytes(b"x")
    with pytest.raises(ValueError, match="it is a folder"):
        RadarReader().open_file(str(tmp_path))


def test_foreign_zarr_store_says_what_to_do(tmp_path):
    store = tmp_path / "other.zarr"
    xr.Dataset({"x": ("y", np.arange(3.0))}).to_zarr(store, zarr_format=2)
    with pytest.raises(ValueError, match="CfRadial2 layout"):
        RadarReader().open_file(str(store))

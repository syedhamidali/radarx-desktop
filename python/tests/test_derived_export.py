"""Exporting derived (module-result) variables alongside the measured moments."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from engine.derived import DerivedError, DerivedStore, export_name, tree_with_derived
from engine.reader import sorted_sweep_names
from tests.conftest import make_synthetic_datatree

META = {
    "label": "Echo class",
    "units": "",
    "module": "qc",
    "field": "Echo class",
    "categories": [{"value": 1, "label": "Clean"}, {"value": 2, "label": "Low SNR"}],
}


@pytest.fixture
def tree_and_store():
    tree = make_synthetic_datatree(n_azimuth=20, n_range=30)
    store = DerivedStore(max_bytes=10_000_000)
    store.bind(tree)
    shape = (tree["sweep_0"].sizes["azimuth"], tree["sweep_0"].sizes["range"])
    # computed for sweep 0 only: the other sweeps must come out NaN, not missing
    store.put_variable("qc:echo_class", {0: np.ones(shape, dtype=np.float32)}, META)
    return tree, store


def test_export_name_is_netcdf_safe():
    assert export_name("radarx_clean:cleaned_zdr") == "radarx_clean__cleaned_zdr"


def test_derived_added_to_every_sweep(tree_and_store):
    tree, store = tree_and_store
    out = tree_with_derived(tree, store)
    sweeps = sorted_sweep_names(out)
    assert len(sweeps) > 1
    for i, name in enumerate(sweeps):
        ds = out[name].to_dataset()
        var = ds["qc__echo_class"]
        assert var.dims == ("azimuth", "range")
        assert "DBZH" in ds  # originals kept by default
        assert bool(np.isfinite(var.values).all()) == (i == 0)
    attrs = out["sweep_0"].to_dataset()["qc__echo_class"].attrs
    assert list(attrs["flag_values"]) == [1, 2]
    assert attrs["flag_meanings"] == "Clean Low_SNR"
    assert "qc" in attrs["comment"]


def test_original_tree_not_modified(tree_and_store):
    tree, store = tree_and_store
    tree_with_derived(tree, store, include_original=False)
    ds = tree["sweep_0"].to_dataset()
    assert "DBZH" in ds and "qc__echo_class" not in ds


def test_without_originals_only_derived_fields_remain(tree_and_store):
    tree, store = tree_and_store
    ds = tree_with_derived(tree, store, include_original=False)["sweep_0"].to_dataset()
    assert "DBZH" not in ds and "VRADH" not in ds
    assert "qc__echo_class" in ds


def test_unknown_name_and_empty_selection_rejected(tree_and_store):
    tree, store = tree_and_store
    with pytest.raises(DerivedError):
        tree_with_derived(tree, store, names=["nope:nothing"])
    with pytest.raises(DerivedError):
        tree_with_derived(tree, DerivedStore(max_bytes=1_000_000), include_original=False)


def test_zarr_roundtrip_keeps_derived(tree_and_store, tmp_path):
    pytest.importorskip("zarr")
    tree, store = tree_and_store
    out = tree_with_derived(tree, store)
    target = tmp_path / "vol.zarr"
    out.to_zarr(target)
    back = xr.open_datatree(target, engine="zarr")
    assert "qc__echo_class" in back["sweep_0"].to_dataset()
    assert "DBZH" in back["sweep_0"].to_dataset()

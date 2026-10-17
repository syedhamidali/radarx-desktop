"""A per-ray site position (with fill values) must not break NetCDF export."""

import numpy as np
import xarray as xr

from engine.exporter import _netcdf_safe_tree


def _tree(lat):
    n = lat.size
    root = xr.Dataset(
        {
            "latitude": ("time", lat, {"units": "degrees_north"}),
            "longitude": ("time", np.full(n, -88.0)),
            "altitude": ("time", np.array([58.0, -9999.0, 58.0, 58.0][:n])),
            "altitude_agl": ("time", np.full(n, np.nan)),
        },
        coords={"time": np.arange(n)},
    )
    return xr.DataTree.from_dict({"/": root})


def test_per_ray_position_collapses_to_scalar():
    lat = np.array([32.8, 32.9, np.nan, 32.8])
    ds = _netcdf_safe_tree(_tree(lat)).to_dataset()
    assert ds["latitude"].ndim == 0
    assert float(ds["latitude"]) == 32.8
    assert ds["latitude"].attrs["units"] == "degrees_north"
    assert float(ds["longitude"]) == -88.0
    assert float(ds["altitude"]) == 58.0  # the -9999 fill value is ignored
    assert "altitude_agl" not in ds  # no valid value at all -> dropped


def test_original_tree_untouched():
    tree = _tree(np.array([1.0, 2.0]))
    _netcdf_safe_tree(tree)
    assert tree.to_dataset()["latitude"].dims == ("time",)

"""Per-ray site position collapsed on open; modules see masked (no-echo = NaN) volumes."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from engine import plugins
from engine.reader import RadarReader, collapse_site_position
from tests.conftest import make_synthetic_datatree

_HOME = Path(os.path.expanduser("~/Downloads/Others/XRADAR_EXAMPLES"))
_COW = _HOME / "RADAR_CORRECTED/cfrad.20220322_220338.299_to_20220322_220506.977_COW1high_SUR.nc"
_KTLX = _HOME / "KTLX20130520_201643_V06.gz"


def test_collapse_keeps_most_common_valid_value():
    n = 6
    ds = xr.Dataset(
        {
            "latitude": ("time", np.array([32.8, np.nan, 32.8, 32.8, np.nan, 32.8])),
            "altitude": ("time", np.array([58.0, -9999.0, 58.0, 58.0, 58.0, 58.0])),
            "altitude_agl": ("time", np.full(n, np.nan)),
        },
        coords={"time": np.arange(n)},
    )
    out = collapse_site_position(ds)
    assert float(out["latitude"]) == 32.8 and out["latitude"].ndim == 0
    assert float(out["altitude"]) == 58.0
    assert "altitude_agl" not in out


@pytest.mark.skipif(not _COW.exists(), reason="COW sample not available")
def test_open_cfradial_with_per_ray_position_gives_scalars():
    reader = RadarReader()
    reader.open_file(str(_COW))
    root = reader.datatree.to_dataset()
    for name in ("latitude", "longitude", "altitude"):
        assert root[name].ndim == 0, name
        assert np.isfinite(float(root[name]))


def test_module_context_volume_is_masked_but_raw_is_kept():
    tree = make_synthetic_datatree(n_azimuth=20, n_range=30)
    ctx = plugins.ModuleContext(
        tree, 0, get_sweep_dataset=lambda i: tree[f"sweep_{i}"].to_dataset()
    )
    assert ctx.raw_datatree is tree
    assert ctx.datatree["sweep_0"].to_dataset()["DBZH"].shape == (20, 30)  # synthetic: unchanged


@pytest.mark.skipif(not _KTLX.exists(), reason="KTLX sample not available")
def test_module_context_volume_has_no_no_echo_values():
    reader = RadarReader()
    reader.open_file(str(_KTLX))
    ctx = plugins.ModuleContext(
        reader.datatree, 0, get_sweep_dataset=lambda i: reader.datatree[f"sweep_{i}"].to_dataset()
    )
    raw = np.asarray(ctx.raw_datatree["sweep_0"].to_dataset()["DBZH"].values)
    masked = np.asarray(ctx.datatree["sweep_0"].to_dataset()["DBZH"].values)
    assert np.nanmin(raw) < -30  # the decoded "no echo" code
    assert np.isnan(masked).sum() > np.isnan(raw).sum()
    assert not (masked < -30).any()

"""The speed-ups must not change a single value.

Each test computes a result the slow, straightforward way and the way the engine now does it, and
requires identical output. Covers: deferred scipy import, "mask only the moment you read",
and the faster CfRadial1 writer.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from engine import exporter, mosaic, plugins
from engine.reader import (
    RadarReader,
    lazy_mask_below_threshold,
    mask_below_threshold,
    mask_variable,
)
from engine.volume import extract_volume

PYTHON_DIR = Path(__file__).resolve().parent.parent

# The outer join of a split-cut volume leaves NaN in integer-encoded moments (same as xradar's own).
pytestmark = [
    pytest.mark.filterwarnings("ignore:saving variable"),
    pytest.mark.filterwarnings("ignore:invalid value encountered in cast"),
]

N_AZ = 90


def _encoded(rng, shape, scale, offset, dtype=np.uint8, units=""):
    """A moment as xradar decodes it: float64 = offset + code * scale, code 0/1 = no echo."""
    codes = rng.integers(0, 60, shape)
    codes[rng.random(shape) < 0.5] = 0
    codes[rng.random(shape) < 0.05] = 1
    var = xr.Variable(("azimuth", "range"), codes * scale + offset, {"units": units})
    var.encoding = {"scale_factor": scale, "add_offset": offset, "dtype": np.dtype(dtype)}
    return var


def make_tree(n_sweeps: int = 6, seed: int = 0) -> xr.DataTree:
    """Split-cut style volume: surveillance sweeps (DBZH, ZDR, 120 gates) alternate with Doppler
    sweeps (DBZH, VRADH, 80 gates); every ray has its own timestamp."""
    rng = np.random.default_rng(seed)
    nodes = {}
    t0 = np.datetime64("2024-05-01T20:00:00", "ns")
    for i in range(n_sweeps):
        doppler = i % 2 == 1
        n_rng = 80 if doppler else 120
        el = 0.5 + 0.9 * (i // 2) * 2 + (0.0 if not doppler else 0.05)
        shape = (N_AZ, n_rng)
        data = {"DBZH": _encoded(rng, shape, 0.5, -33.0, units="dBZ")}
        if doppler:
            data["VRADH"] = _encoded(rng, shape, 0.5, -64.5, units="m/s")
        else:
            data["ZDR"] = _encoded(rng, shape, 0.0625, -7.9, dtype=np.uint16, units="dB")
        nodes[f"/sweep_{i}"] = xr.Dataset(
            data
            | {
                "sweep_mode": ((), "azimuth_surveillance"),
                "sweep_number": ((), np.int64(i)),
                "sweep_fixed_angle": ((), float(el)),
                "prt_mode": ((), "not_set"),
                "follow_mode": ((), "not_set"),
            },
            coords={
                "azimuth": ("azimuth", (np.arange(N_AZ) + 0.5) * 360.0 / N_AZ),
                "range": ("range", 2000.0 + 500.0 * np.arange(n_rng)),
                "elevation": ("azimuth", np.full(N_AZ, el)),
                "time": (
                    "azimuth",
                    t0 + np.timedelta64(i * 40, "s") + np.arange(N_AZ) * np.timedelta64(8, "ms"),
                ),
            },
            attrs={"sweep_fixed_angle": float(el)},
        )
    root = xr.Dataset(
        coords={"latitude": 36.7, "longitude": -87.3, "altitude": 186.0},
        attrs={"instrument_name": "KTEST", "history": "synthetic", "mpda_vcp": True},
    )
    return xr.DataTree.from_dict({"/": root, **nodes})


def make_reader(tree: xr.DataTree) -> RadarReader:
    reader = RadarReader()
    reader._dtree = tree
    reader._format = "synthetic"
    return reader


def same(a: xr.Dataset | xr.DataArray, b: xr.Dataset | xr.DataArray) -> None:
    assert a.identical(b)


# ---------------------------------------------------------------------------------------------
# masking only the moment that is read
# ---------------------------------------------------------------------------------------------


def test_mask_only_matches_full_mask_for_the_chosen_variable():
    ds = make_tree().children["sweep_0"].to_dataset()
    full = mask_below_threshold(ds)
    part = mask_below_threshold(ds, only=["DBZH"])
    same(part["DBZH"], full["DBZH"])
    assert np.isnan(part["DBZH"].values).any()
    # the others are left exactly as they were
    same(part["ZDR"], ds["ZDR"])
    assert not np.isnan(part["ZDR"].values).any()
    assert mask_below_threshold(ds, only=[]).identical(ds)
    assert mask_below_threshold(ds, only=None).identical(full)


def test_mask_variable_unchanged_without_encoding():
    da = xr.DataArray(np.array([[1.0, np.nan], [-33.0, 5.0]]), dims=("azimuth", "range"))
    assert mask_variable(da) is da


def test_lazy_masking_is_identical_to_eager_masking():
    tree = make_tree()
    for name in ("sweep_0", "sweep_1"):
        ds = tree.children[name].to_dataset()
        eager, lazy = mask_below_threshold(ds), lazy_mask_below_threshold(ds)
        assert lazy.identical(eager)
        for var in eager.data_vars:
            assert np.array_equal(lazy[var].values, eager[var].values, equal_nan=True)
            assert lazy[var].dtype == eager[var].dtype
        part = lazy["DBZH"].isel(azimuth=slice(5, 25), range=[1, 4, 9])
        assert np.array_equal(
            part.values,
            eager["DBZH"].isel(azimuth=slice(5, 25), range=[1, 4, 9]).values,
            equal_nan=True,
        )
        assert float(lazy["DBZH"].mean()) == float(eager["DBZH"].mean())
        row = lazy["DBZH"].isel(azimuth=3)
        assert np.array_equal(row.values, eager["DBZH"].isel(azimuth=3).values, equal_nan=True)
        assert lazy.copy(deep=True).identical(eager)
        assert lazy.transpose("range", "azimuth").identical(eager.transpose("range", "azimuth"))
        assert (lazy["DBZH"] + 1).identical(eager["DBZH"] + 1)
    plain = xr.DataArray(np.ones((3, 4)), dims=("azimuth", "range"), name="a")
    assert lazy_mask_below_threshold(xr.Dataset({"a": plain}))["a"].identical(plain)


def test_module_context_tree_equals_the_eagerly_masked_tree():
    tree = make_tree()
    ctx = plugins.ModuleContext(
        tree, 0, get_sweep_dataset=lambda i: tree[f"sweep_{i}"].to_dataset()
    )
    lazy = ctx.datatree
    for name in ("sweep_0", "sweep_1", "sweep_2"):
        eager = mask_below_threshold(tree.children[name].to_dataset())
        assert lazy.children[name].to_dataset().identical(eager)
    # the reader's own tree is never modified
    assert not np.isnan(tree.children["sweep_0"].to_dataset()["ZDR"].values).any()


def test_reader_get_sweep_dataset_only():
    reader = make_reader(make_tree())
    for sweep in range(6):
        full = reader.get_sweep_dataset(sweep)
        for var in ("DBZH", "ZDR", "VRADH"):
            if var not in full:
                continue
            part = reader.get_sweep_dataset(sweep, only=[var])
            same(part[var], full[var])
            assert reader.get_sweep_data(sweep, var).identical(full[var])
        raw = reader.get_sweep_dataset(sweep, mask=False)
        same(raw["sweep_fixed_angle"], full["sweep_fixed_angle"])


def test_extract_volume_with_variable_loader_is_identical():
    tree = make_tree(8)
    reader = make_reader(tree)
    box = {"x_min": -30e3, "x_max": 30e3, "y_min": -30e3, "y_max": 30e3}
    for var in ("DBZH", "VRADH"):
        full = extract_volume(
            tree, var, box, resolution=24, sweep_loader=lambda i: reader.get_sweep_dataset(i)
        )
        only = extract_volume(
            tree,
            var,
            box,
            resolution=24,
            sweep_loader=lambda i, var=var: reader.get_sweep_dataset(i, only=[var]),
        )
        assert np.array_equal(full["data"], only["data"], equal_nan=True)
        assert (full["vmin"], full["vmax"]) == (only["vmin"], only["vmax"])
        assert np.isfinite(full["data"]).any()


def test_server_loader_masks_only_the_variable():
    import server

    reader = make_reader(make_tree())
    ds = server._loader_for(reader, "DBZH")(0)
    same(ds["DBZH"], reader.get_sweep_dataset(0)["DBZH"])
    same(ds["ZDR"], reader.get_sweep_dataset(0, mask=False)["ZDR"])  # not decoded or masked


@pytest.mark.skipif(not mosaic.radarx_available(), reason="radarx is not installed")
def test_mosaic_prepare_volume_masks_no_echo_and_keeps_only_the_variable():
    reader = make_reader(make_tree())
    vol = mosaic._prepare_volume("f", reader, "DBZH", 0, None)
    sweep = vol.tree["sweep_0"].to_dataset()
    ref = reader.get_sweep_dataset(0)["DBZH"].values
    assert list(sweep.data_vars) == ["DBZH", "sweep_fixed_angle"]
    assert np.array_equal(sweep["DBZH"].values, ref, equal_nan=True)
    assert np.isnan(sweep["DBZH"].values).any()


# ---------------------------------------------------------------------------------------------
# deferred imports
# ---------------------------------------------------------------------------------------------


def test_importing_the_server_does_not_import_scipy():
    code = (
        f"import sys; sys.path.insert(0, {str(PYTHON_DIR)!r}); import server\n"
        "from engine import plugins; plugins.discover_modules()\n"
        "assert 'scipy.ndimage' not in sys.modules, 'scipy.ndimage imported at start-up'\n"
        "from engine import qc\n"
        "import numpy as np, xarray as xr\n"
        "qc.despeckle(xr.DataArray(np.random.rand(20, 20), dims=('azimuth', 'range')))\n"
        "assert 'scipy.ndimage' in sys.modules\n"
    )
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr[-800:]


def test_lazy_module_behaves_like_the_module():
    from engine._lazy import LazyModule

    lazy = LazyModule("math")
    assert lazy.sqrt(16) == 4.0
    assert "sqrt" in dir(lazy)
    assert "math" in repr(lazy)


# ---------------------------------------------------------------------------------------------
# CfRadial1 writer
# ---------------------------------------------------------------------------------------------


def test_cfradial1_fast_mapper_is_identical_to_xradars():
    from xradar.io.export import cfradial1 as cf1

    tree = exporter._netcdf_safe_tree(make_tree(6))
    slow = cf1._variable_mapper(tree)
    fast = exporter._cfradial1_variables(cf1, tree)
    assert fast.identical(slow)
    assert fast.sizes["range"] == 120  # the union of the two gate counts


def test_cfradial1_file_is_identical_to_xradars(tmp_path):
    from xradar.io.export.cfradial1 import to_cfradial1

    tree = exporter._netcdf_safe_tree(make_tree(6))
    stock, ours = tmp_path / "stock.nc", tmp_path / "ours.nc"
    to_cfradial1(dtree=exporter._netcdf_safe_tree(make_tree(6)), filename=str(stock))
    exporter._to_cfradial1(tree, str(ours))
    with xr.open_dataset(stock) as a, xr.open_dataset(ours) as b:
        assert a.identical(b)
        for name in a.variables:
            assert a[name].dtype == b[name].dtype
            assert a[name].encoding.get("dtype") == b[name].encoding.get("dtype")


def test_cfradial1_export_through_the_exporter(tmp_path):
    reader = make_reader(make_tree(4))
    out = exporter.RadarExporter().export(
        reader.datatree, "netcdf", output_path=str(tmp_path / "x.nc"), variable="DBZH"
    )
    with xr.open_dataset(out) as ds:
        assert {"DBZH", "ZDR", "VRADH", "fixed_angle"} <= set(ds.variables)
        assert ds.attrs["Conventions"].startswith("CF-1.")  # the standards-clean writer


def test_cfradial1_falls_back_to_xradars_mapper(tmp_path, monkeypatch):
    from xradar.io.export import cfradial1 as cf1

    def boom(*_a, **_k):
        raise RuntimeError("fast path unavailable")

    monkeypatch.setattr(exporter, "_cfradial1_variables", boom)
    out = tmp_path / "fallback.nc"
    exporter._to_cfradial1(exporter._netcdf_safe_tree(make_tree(4)), str(out))
    assert out.exists()
    assert cf1._variable_mapper.__name__ == "_variable_mapper"  # restored after the export

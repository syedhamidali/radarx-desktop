"""Tests of the public Python API (``engine.api`` / ``import xrd``).

Synthetic volumes come from ``tests.conftest.make_synthetic_datatree`` (and, for the multi-radar
products, the radarx test volumes); the real KTLX and Mayfield volumes are optional and skipped
when the files are absent. Nothing here starts the WebSocket server.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from engine import api, derived, plugins
from tests.conftest import make_synthetic_datatree

KTLX = Path.home() / "Downloads/Others/XRADAR_EXAMPLES/KTLX20130520_201643_V06.gz"
MAYFIELD = Path("/Users/syed44/Downloads/SEVERE_CASES_TEST/mayfield")


def _mayfield(radar: str) -> Path:
    files = sorted((MAYFIELD / radar).glob("*_V06")) if MAYFIELD.is_dir() else []
    if not files:
        pytest.skip(f"Mayfield {radar} volume not available")
    return files[0]


def synthetic_volume(lat: float = 35.0, lon: float = -97.0, **kw) -> api.Volume:
    """A small in-memory volume with a radar position."""
    tree = make_synthetic_datatree(n_azimuth=90, n_range=120, **kw)
    root = tree.to_dataset().assign_coords(
        latitude=np.float64(lat), longitude=np.float64(lon), altitude=np.float64(300.0)
    )
    tree.dataset = root
    return api.Volume.from_tree(tree)


@pytest.fixture()
def vol() -> api.Volume:
    return synthetic_volume()


@pytest.fixture()
def nc_file(tmp_path: Path) -> Path:
    path = tmp_path / "synthetic.nc"
    make_synthetic_datatree(n_azimuth=90, n_range=120).to_netcdf(path)
    return path


# ---------------------------------------------------------------------------
# open / info / variables
# ---------------------------------------------------------------------------


def test_open_path_volume_and_tree(nc_file: Path) -> None:
    v = api.open(nc_file)
    assert isinstance(v, api.Volume)
    assert v.path == str(nc_file)
    assert v.format == "cfradial2"
    assert v.n_sweeps == 3
    assert api.open(v) is v  # a Volume passes through unchanged
    assert api.open(make_synthetic_datatree(n_azimuth=30, n_range=20)).n_sweeps == 3
    assert "3 sweeps" in repr(v)


def test_open_accepts_relative_paths_and_pathlike(nc_file: Path, monkeypatch) -> None:
    monkeypatch.chdir(nc_file.parent)
    assert api.open(nc_file.name).path == str(nc_file.resolve())
    assert api.open(Path(nc_file.name)).n_sweeps == 3


def test_open_errors_are_user_errors(tmp_path: Path) -> None:
    with pytest.raises(api.XrdArgumentError, match="File not found"):
        api.open(tmp_path / "nope.nc")
    with pytest.raises(api.XrdArgumentError, match="directory"):
        api.open(tmp_path)
    bad = tmp_path / "bad.nc"
    bad.write_bytes(b"not a radar file at all" * 10)
    with pytest.raises(api.XrdError):
        api.open(bad)
    assert issubclass(api.XrdArgumentError, ValueError)


def test_info_is_json_able(nc_file: Path) -> None:
    info = api.open(nc_file).info()
    json.dumps(info)
    assert info["format"] == "cfradial2"
    assert info["n_sweeps"] == 3
    assert info["variables"] == ["DBZH", "VRADH"]
    assert [s["elevation"] for s in info["sweeps"]] == pytest.approx([0.5, 1.5, 2.4], abs=1e-5)
    assert info["sweeps"][0]["n_rays"] == 90
    assert info["size_bytes"] == nc_file.stat().st_size


def test_variables_and_mapping_aliases(vol: api.Volume) -> None:
    assert vol.variables() == ["DBZH", "VRADH"]
    assert vol.variables(1) == ["DBZH", "VRADH"]
    for alias in ("DBZH", "reflectivity", "dbz"):
        assert vol.resolve(alias) == "DBZH"
    for alias in ("VRADH", "velocity", "radial_velocity"):
        assert vol.resolve(alias) == "VRADH"
    da = vol.field("velocity", 1)
    assert da.dims == ("azimuth", "range")
    assert da.shape == (90, 120)


def test_unknown_variable_and_sweep(vol: api.Volume) -> None:
    with pytest.raises(api.XrdArgumentError, match="ZDR"):
        vol.field("ZDR", 0)
    with pytest.raises(api.XrdArgumentError, match="out of range"):
        vol.field("DBZH", 9)
    with pytest.raises(api.XrdArgumentError, match="integer"):
        vol.dataset(True)  # type: ignore[arg-type]


def test_mapping_override_is_validated() -> None:
    v = synthetic_volume()
    v.mapping = {}
    renamed = api.Volume(v._reader, mapping={"velocity": "DBZH"})
    assert renamed.resolve("velocity") == "DBZH"
    with pytest.raises(Exception, match="Unknown role"):
        api.Volume(v._reader, mapping={"nonsense": "x"})


# ---------------------------------------------------------------------------
# modules
# ---------------------------------------------------------------------------


def test_modules_listing_and_description() -> None:
    mods = api.modules()
    ids = [m["id"] for m in mods]
    assert {"storm_detection", "qc_clean", "radarx_hid"} <= set(ids)
    text = repr(mods)
    assert "storm_detection" in text and "category" in text
    assert "<table>" in mods._repr_html_()
    qc = api.modules("qc")
    assert qc and {m["category"] for m in qc} == {"QC"}
    with pytest.raises(api.XrdArgumentError, match="Categories"):
        api.modules("no such category")
    info = api.module("storm_detection")
    assert "threshold_dbz" in repr(info)
    assert next(p["name"] for p in info["params"]) == "threshold_dbz"


def test_module_lookup_helpful_errors() -> None:
    with pytest.raises(api.XrdArgumentError, match="Did you mean storm_detection"):
        api.module("storm_detectio")
    assert api.module("hid")["id"] == "radarx_hid"  # radarx_ prefix may be omitted


def test_no_module_parameter_shadows_an_api_keyword() -> None:
    reserved = {"sweep", "all_sweeps", "params", "inputs", "out", "formats", "plot", "progress"}
    reserved |= {"timeout_s", "module"}
    for m in api.modules():
        names = {p["name"] for p in m["params"]}
        assert not names & reserved, (m["id"], names & reserved)


def test_check_params_converts_and_rejects() -> None:
    spec = api.find_module("storm_detection")
    got = api.check_params(spec, {"threshold_dbz": "42.5", "split_cells": "yes"})
    assert got == {"threshold_dbz": 42.5, "split_cells": True}
    with pytest.raises(api.XrdArgumentError, match="Did you mean 'threshold_dbz'"):
        api.check_params(spec, {"threshold_db": 1})
    with pytest.raises(api.XrdArgumentError, match="expected true/false"):
        api.check_params(spec, {"split_cells": "maybe"})
    with pytest.raises(api.XrdArgumentError, match=r"could not convert|must be"):
        api.check_params(spec, {"threshold_dbz": "abc"})
    with pytest.raises(api.XrdArgumentError, match="one of"):
        api.check_params(api.find_module("radarx_hid"), {"method": "x"})


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_run_single_sweep_result_object(vol: api.Volume) -> None:
    res = vol.run("storm_detection", 0, threshold_dbz=40)
    assert isinstance(res, api.ModuleResult)
    assert res.module == "storm_detection" and res.sweep == 0
    assert res.params["threshold_dbz"] == 40.0
    assert res.scalars["Threshold"] == 40.0
    assert len(res.table) == res.scalars["Cells"]
    assert "Storm cells" in next(iter(res.tables))
    assert res.overlays and res.overlays[0]["kind"] == "polygon"
    assert "ModuleResult" in repr(res)
    assert res.elapsed_ms > 0
    # params as a dict is the same as keywords
    again = vol.run("storm_detection", 0, params={"threshold_dbz": 40})
    assert again.scalars["Cells"] == res.scalars["Cells"]


def test_run_rejects_unknown_parameter_and_sweep(vol: api.Volume) -> None:
    with pytest.raises(api.XrdArgumentError, match="no parameter 'thresh'"):
        vol.run("storm_detection", 0, thresh=1)
    with pytest.raises(api.XrdArgumentError, match="out of range"):
        vol.run("storm_detection", 7)
    with pytest.raises(api.XrdArgumentError, match="Unknown module"):
        vol.run("nope")


def test_run_missing_input_field_is_module_error() -> None:
    tree = make_synthetic_datatree(n_azimuth=30, n_range=20)
    for name in tree.children:
        tree[name].dataset = tree[name].to_dataset().drop_vars("DBZH")
    with pytest.raises(api.XrdArgumentError, match=r"needs DBZH, but sweep 0 has no DBZH"):
        api.Volume.from_tree(tree).run("storm_detection", 0)


def test_run_field_result_and_plot(vol: api.Volume, tmp_path: Path) -> None:
    res = vol.run("qc_clean", 0, use_snr=False)
    assert res.fields
    da = res.field()
    assert da.dims == ("azimuth", "range") and da.shape == (90, 120)
    ds = res.to_dataset()
    assert len(ds.data_vars) == len(res.fields)
    fig = res.plot()
    out = tmp_path / "qc.png"
    fig.savefig(out)
    assert out.stat().st_size > 1000
    with pytest.raises(KeyError, match="No field"):
        res.field("not a field")


def test_run_all_sweeps_creates_one_derived_variable(vol: api.Volume) -> None:
    res = vol.run("qc_clean", all_sweeps=True, use_snr=False)
    assert res.sweep is None and res.derived  # one derived variable per output field
    name = res.derived[0]
    assert name.startswith("qc_clean:")
    assert [d["name"] for d in vol.derived] == res.derived
    assert vol.derived[0]["sweeps"] == [0, 1, 2]
    assert set(res.derived) <= set(vol.variables(1))
    assert vol.field(name, 2).shape == (90, 120)
    assert not res.skipped
    assert res.plot() is not None  # draws the first processed sweep
    ds = vol.to_dataset(0, include_derived=True)
    assert derived.export_name(name) in ds
    assert derived.export_name(name) not in vol.to_dataset(0)
    with pytest.raises(KeyError, match="all sweeps"):
        res.field()


def test_run_all_sweeps_rejects_volume_modules(vol: api.Volume) -> None:
    api.reload_modules()
    volume_module = next(m["id"] for m in api.modules() if m["scope"] == "volume")
    with pytest.raises(plugins.ModuleError, match=r"whole-volume|needs the Python package"):
        vol.run(volume_module, all_sweeps=True)


def test_run_can_save_results(vol: api.Volume, tmp_path: Path) -> None:
    res = vol.run("storm_detection", 0, out=tmp_path / "res.json", plot=tmp_path / "unused.png")
    payload = json.loads((tmp_path / "res.json").read_text())
    assert payload["module"] == "storm_detection" and payload["scalars"]["Cells"] >= 0
    assert not (tmp_path / "unused.png").exists()  # no gridded field to draw
    assert any("no gridded field" in n for n in res.save_notes)
    res2 = vol.run("qc_clean", 0, out=tmp_path / "folder", formats=["json", "nc", "png"])
    suffixes = {p.suffix for p in (tmp_path / "folder").iterdir()}
    assert suffixes == {".json", ".nc", ".png"}  # one png per field
    assert res2.fields
    vol.run("storm_detection", 0, out=tmp_path / "cells.csv")
    assert (tmp_path / "cells.csv").read_text().startswith("id,")


def test_volumes_do_not_share_derived_state() -> None:
    a, b = synthetic_volume(), synthetic_volume()
    a.run("qc_clean", all_sweeps=True, use_snr=False)
    assert a.derived and not b.derived
    a.close()
    assert not a.derived


def test_add_and_drop_derived(vol: api.Volume) -> None:
    arr = vol.field("DBZH", 0) + 5
    name = vol.add_derived("plus5", {0: arr}, units="dBZ", label="DBZH + 5")
    assert name == "user:plus5"
    assert vol.derived_meta(name)["units"] == "dBZ"
    assert float(vol.field(name, 0).mean()) == pytest.approx(float(arr.mean()), rel=1e-4)
    with pytest.raises(api.XrdArgumentError, match="shape"):
        vol.add_derived("bad", np.zeros((2, 2)))
    with pytest.raises(api.XrdArgumentError, match="not computed on sweep 1"):
        vol.field(name, 1)
    fig = vol.plot(name, 0)
    assert fig.axes
    assert vol.drop_derived(name) == [name]


# ---------------------------------------------------------------------------
# pipelines
# ---------------------------------------------------------------------------


def test_recipes_and_lookup() -> None:
    table = api.recipes()
    assert {"Reflectivity QC", "Dual-pol QC"} <= {r["name"] for r in table}
    assert "Reflectivity QC" in repr(table)
    assert api.recipe("reflectivity qc").name == "Reflectivity QC"
    assert api.recipe("dual-pol").target == "ZDR"
    assert api.recipe("doppler velocity").target == "VRADH"
    with pytest.raises(api.XrdArgumentError, match="Known recipes"):
        api.recipe("nonexistent recipe")
    with pytest.raises(api.XrdArgumentError, match="not found"):
        api.recipe("missing/recipe.json")
    assert "def process_sweep" in api.recipe_script("Reflectivity QC")
    assert any(s["id"] == "speckle" for s in api.pipeline_steps())


def test_recipe_from_dict_and_json_file(tmp_path: Path) -> None:
    raw = {
        "name": "mine",
        "target": "DBZH",
        "steps": [{"step_id": "speckle", "params": {"min_size": 5}}],
    }
    assert api.recipe(raw).steps[0]["params"]["min_size"] == 5
    path = tmp_path / "mine.json"
    path.write_text(json.dumps(raw))
    assert api.recipe(path).name == "mine"
    assert api.recipe(str(path)).name == "mine"
    path.write_text("{not json")
    with pytest.raises(api.XrdArgumentError, match="Cannot read"):
        api.recipe(path)
    with pytest.raises(api.XrdArgumentError, match="Invalid recipe"):
        api.recipe({"name": "x", "target": "DBZH", "steps": []})


def test_pipeline_creates_derived_variable(vol: api.Volume, tmp_path: Path) -> None:
    res = vol.pipeline("Reflectivity QC", target="DBZH", all_sweeps=True)
    assert isinstance(res, api.PipelineResult)
    assert res.name.startswith("pipeline:") and res.sweeps == [0, 1, 2]
    steps = res.steps
    assert {"title", "status", "removed", "valid_pct"} <= set(steps.columns)
    assert vol.derived[0]["name"] == res.name
    cleaned = vol.field(res.name, 0)
    assert int(cleaned.notnull().sum()) <= int(vol.field("DBZH", 0).notnull().sum())
    out = res.save(tmp_path / "qc", ["json", "png"], sweep=1)
    assert sorted(p.suffix for p in out) == [".json", ".png"]
    assert "sweep1" in out[1].name
    fig = res.plot(1)
    assert len(fig.axes) >= 2
    assert res.to_dataset().data_vars
    assert "PipelineResult" in repr(res)


def test_pipeline_single_sweep_and_step_overrides(vol: api.Volume) -> None:
    res = vol.pipeline(
        "Reflectivity QC",
        sweep=1,
        params={"speckle.min_size": 3, "gap_fill": False},
    )
    assert res.sweeps == [1]
    speckle = next(s for s in res.recipe["steps"] if s["step_id"] == "speckle")
    assert speckle["params"]["min_size"] == 3
    assert next(s for s in res.recipe["steps"] if s["step_id"] == "gap_fill")["enabled"] is False
    with pytest.raises(api.XrdArgumentError, match="has no step 'nope'"):
        vol.pipeline("Reflectivity QC", params={"nope.x": 1})
    with pytest.raises(api.XrdArgumentError, match="no parameter 'min_sze'"):
        vol.pipeline("Reflectivity QC", params={"speckle.min_sze": 1})
    with pytest.raises(api.XrdArgumentError, match="not both"):
        vol.pipeline("Reflectivity QC", sweep=0, all_sweeps=True)


def test_pipeline_feeds_a_module_through_inputs(vol: api.Volume) -> None:
    res = vol.pipeline("Reflectivity QC", all_sweeps=True)
    out = vol.run("storm_detection", 0, inputs={"reflectivity": res.name})
    assert out.scalars["Cells"] >= 0
    with pytest.raises(api.XrdArgumentError, match="does not exist"):
        vol.run("storm_detection", 0, inputs={"reflectivity": "pipeline:nothing"})


def test_pipeline_on_missing_target_is_module_error(vol: api.Volume) -> None:
    with pytest.raises(plugins.ModuleError, match="ZDR"):
        vol.pipeline("Dual-pol QC", all_sweeps=True)


# ---------------------------------------------------------------------------
# plot / export
# ---------------------------------------------------------------------------


def test_plot_file_variable_uses_publication_renderer(vol: api.Volume, tmp_path: Path) -> None:
    fig = vol.plot("reflectivity", 0)
    assert fig.axes and "DBZH" in fig.axes[0].get_title()
    dark = vol.plot("DBZH", 1, theme="dark", vmin=0, vmax=50, title="my title")
    assert dark.axes[0].get_title() == "my title"
    out = tmp_path / "sub" / "ppi.png"
    vol.plot(out=out)
    assert out.stat().st_size > 1000
    with pytest.raises(api.XrdArgumentError, match="theme"):
        vol.plot(theme="blue")
    with pytest.raises(api.XrdArgumentError, match=r"\.png, \.svg or \.pdf"):
        vol.plot(out=tmp_path / "ppi.txt")
    import matplotlib.pyplot as plt

    _, ax = plt.subplots()
    assert vol.plot("DBZH", 0, ax=ax) is ax.figure


def test_plot_masks_no_echo_codes() -> None:
    """The publication renderer must not paint reserved no-echo codes as data."""
    tree = make_synthetic_datatree(n_azimuth=40, n_range=30)
    for name in tree.children:
        ds = tree[name].to_dataset()
        tree[name].dataset = ds.assign(DBZH=ds["DBZH"].where(ds["DBZH"] > 70, -33.0))
    fig = api.Volume.from_tree(tree).plot("DBZH", 0)
    mesh = fig.axes[0].collections[0]
    assert mesh is not None


def test_export_formats(vol: api.Volume, tmp_path: Path) -> None:
    csv = vol.export("csv", tmp_path / "a" / "dbz.csv", variable="reflectivity", sweep=1)
    assert csv.read_text().splitlines()[0] == "azimuth_deg,range_m,DBZH"
    png = vol.export(out=tmp_path / "x.png", variable="DBZH")
    assert png.stat().st_size > 1000
    zarr = vol.export(out=tmp_path / "v.zarr")
    assert zarr.is_dir()
    assert vol.export("zarr", tmp_path / "v2.zarr").is_dir()
    with pytest.raises(api.XrdArgumentError, match="Cannot tell"):
        vol.export(out=tmp_path / "unknown.bin")
    with pytest.raises(api.XrdArgumentError, match="Unknown export format"):
        vol.export("docx", tmp_path / "x.docx")
    with pytest.raises(api.XrdArgumentError, match="no derived variables yet"):
        vol.export("zarr", tmp_path / "z.zarr", include_derived=True)
    with pytest.raises(api.XrdArgumentError, match="No derived variable"):
        vol.export("zarr", tmp_path / "z.zarr", include_derived=["user:x"])
    with pytest.raises(api.XrdError, match="lacks metadata"):
        vol.export("netcdf", tmp_path / "z.nc")  # synthetic volumes cannot be CfRadial1
    with pytest.raises(api.XrdArgumentError, match="Nothing to export"):
        vol.export("zarr", tmp_path / "z.zarr", include_original=False)


def test_export_first_argument_may_be_a_path(vol: api.Volume, tmp_path: Path) -> None:
    out = vol.export(str(tmp_path / "pathfirst.csv"), variable="DBZH")
    assert out.name == "pathfirst.csv"


def test_export_zarr_with_derived(vol: api.Volume, tmp_path: Path) -> None:
    res = vol.pipeline("Reflectivity QC", all_sweeps=True)
    out = vol.export("zarr", tmp_path / "d.zarr", include_derived=[res.name])
    tree = xr.open_datatree(out, engine="zarr")
    assert derived.export_name(res.name) in tree["sweep_0"].to_dataset()
    csv = vol.export("csv", tmp_path / "d.csv", variable=res.name, sweep=0)
    assert derived.export_name(res.name) in csv.read_text().splitlines()[0]


def test_export_default_names_in_current_directory(
    vol: api.Volume, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    out = vol.export("csv", variable="DBZH", sweep=1)
    assert out.parent == tmp_path.resolve() or out.parent == tmp_path
    assert "sweep1" in out.name and out.suffix == ".csv"


# ---------------------------------------------------------------------------
# cells and tracking
# ---------------------------------------------------------------------------


def test_cells_result(vol: api.Volume, tmp_path: Path) -> None:
    cells = vol.cells(0, threshold_dbz=30, min_size_km2=1, backend="numpy")
    assert isinstance(cells, api.CellsResult) and len(cells) > 0
    df = cells.to_dataframe()
    assert len(df) == len(cells) and {"id", "area_km2", "max_dbz"} <= set(df.columns)
    assert cells.backend == "numpy"
    out = cells.save([tmp_path / "c.csv", tmp_path / "c.json", tmp_path / "c.png"])
    assert [p.suffix for p in out] == [".csv", ".json", ".png"]
    assert json.loads((tmp_path / "c.json").read_text())["cells"]
    with pytest.raises(api.XrdArgumentError, match="Cannot write cells"):
        cells.save(tmp_path / "c.xlsx")
    with pytest.raises(api.XrdArgumentError, match="backend"):
        vol.cells(0, backend="magic")


def test_track_two_volumes(tmp_path: Path) -> None:
    a, b = synthetic_volume(), synthetic_volume()
    res = api.track(
        [a, b],
        threshold_dbz=30,
        min_size_km2=1,
        backend="numpy",
        dt_seconds=300,
        out=tmp_path / "cells.csv",
        plot=tmp_path / "tracks.png",
    )
    assert isinstance(res, api.TrackingResult) and len(res.scans) == 2
    assert res.times_s == [0.0, 300.0]
    assert len(res) == len(res.to_dataframe())
    assert set(res.cells_dataframe()["scan"]) == {0, 1}
    assert (tmp_path / "cells.csv").stat().st_size > 0
    assert (tmp_path / "tracks.png").stat().st_size > 1000
    paths = res.save(tmp_path / "t.json", tracks=tmp_path / "tracks.csv")
    assert {p.name for p in paths} == {"t.json", "tracks.csv"}


def test_track_opens_and_closes_paths(nc_file: Path) -> None:
    res = api.track(
        [nc_file, nc_file], threshold_dbz=30, min_size_km2=1, backend="numpy", dt_seconds=60
    )
    assert len(res.scans) == 2


# ---------------------------------------------------------------------------
# multi-radar products
# ---------------------------------------------------------------------------


@pytest.fixture()
def two_radars() -> tuple[api.Volume, api.Volume]:
    pytest.importorskip("radarx")
    from tests.test_radarx_modules import SITE, storm_volume

    trees = []
    for dlon in (0.0, 0.8):
        tree = storm_volume()
        root = tree.to_dataset().assign_coords(
            latitude=np.float64(SITE["latitude"]),
            longitude=np.float64(SITE["longitude"] + dlon),
        )
        tree.dataset = root
        trees.append(api.Volume.from_tree(tree))
    return trees[0], trees[1]


def test_mosaic_products_and_files(two_radars, tmp_path: Path) -> None:
    v1, v2 = two_radars
    mos = api.mosaic(
        [v1, v2],
        product="composite",
        variable="DBZH",
        resolution_m=2000,
        out=[tmp_path / "m.png", tmp_path / "m.nc"],
        plot=tmp_path / "map.png",
    )
    assert isinstance(mos, api.Mosaic) and len(mos.radars) == 2
    assert mos.image.ndim == 2 and np.isfinite(mos.image).any()
    assert {p.name for p in tmp_path.iterdir()} == {"m.png", "m.pgw", "m.prj", "m.nc", "map.png"}
    assert len(mos.header()["radars"]) == 2
    ds = mos.to_dataset()
    assert "DBZH_composite" in ds
    assert xr.open_dataset(tmp_path / "m.nc")["DBZH_composite"].shape == ds["DBZH_composite"].shape
    assert "composite" in repr(mos)
    assert (tmp_path / "m.pgw").read_text().count("\n") == 6
    assert mos.to_dict()["product"] == "composite"
    # variable aliases and other products
    cappi = api.mosaic([v1], product="cappi", variable="reflectivity", height_m=1500)
    assert cappi.height_m == 1500
    top = api.mosaic([v1, v2], product="echo_top", variable="DBZH", resolution_m=2000, merge=False)
    assert top.product == "echo_top"
    assert api.mosaic([v1, v2], resolution_m=2000).record is not None
    with pytest.raises(api.XrdArgumentError, match="Cannot write a mosaic"):
        mos.save(tmp_path / "m.tif")


def test_mosaic_option_validation(two_radars) -> None:
    v1, _ = two_radars
    with pytest.raises(api.XrdArgumentError, match=r"unknown option 'resolutoin'|Did you mean"):
        api.mosaic([v1], resolutoin=1)
    with pytest.raises(api.XrdArgumentError, match="at least 1"):
        api.mosaic([])
    with pytest.raises(Exception, match="product must be"):
        api.mosaic([v1], product="bogus")
    # engine options pass through to the right place
    mos = api.mosaic([v1], resolution_m=2000, threshold_dbz=10, range_scale_km=30)
    assert mos.record.merge["range_scale_km"] == 30


def test_winds_argument_validation(vol: api.Volume) -> None:
    with pytest.raises(api.XrdArgumentError, match="at least 2"):
        api.winds([vol])
    with pytest.raises(api.XrdArgumentError, match="unknown option 'min_crossing_angel'"):
        api.winds([vol, vol], min_crossing_angel=20)
    with pytest.raises(api.XrdArgumentError, match="extent"):
        api.winds([vol, vol], extent="wide")
    with pytest.raises(api.XrdArgumentError, match="origin"):
        api.winds([vol, vol], origin="somewhere")


def test_winds_dealias_option_follows_the_engine(vol: api.Volume, monkeypatch) -> None:
    """A dealias mode the engine cannot take is reported; the default 'auto' is dropped."""
    import dataclasses

    from engine import multidoppler as md

    seen = {}

    def fake_run(readers, grid, options, **kw):
        seen["options"] = options
        raise md.MultiDopplerError("stop here")

    monkeypatch.setattr(md, "run_multidoppler", fake_run)
    has = "dealias" in {f.name for f in dataclasses.fields(md.Options)}
    with pytest.raises(md.MultiDopplerError):
        api.winds([vol, vol], method="variational", background="vvp", dealias="auto")
    assert seen["options"]["method"] == "variational"
    assert seen["options"]["background"] == {"mode": "vvp"}
    assert ("dealias" in seen["options"]) == has
    if not has:
        with pytest.raises(api.XrdArgumentError, match="no dealias option"):
            api.winds([vol, vol], dealias="always")


def test_winds_grid_arguments_are_translated(vol: api.Volume, monkeypatch) -> None:
    from engine import multidoppler as md

    seen = {}

    def fake_run(readers, grid, options, **kw):
        seen.update(grid=grid, kw=kw)
        raise md.MultiDopplerError("stop here")

    monkeypatch.setattr(md, "run_multidoppler", fake_run)
    with pytest.raises(md.MultiDopplerError):
        api.winds([vol, vol], extent=80, origin=(36.5, -88.0), dz_m=250, zmin_m=500, z_max_m=8000)
    grid = seen["grid"]
    assert grid["extent"] == {
        "x_min_km": -80.0,
        "x_max_km": 80.0,
        "y_min_km": -80.0,
        "y_max_km": 80.0,
    }
    assert grid["origin"] == {"lat": 36.5, "lon": -88.0}
    assert (grid["dz_m"], grid["z_min_m"], grid["z_max_m"]) == (250, 500, 8000)
    assert seen["kw"]["cache"] is None  # no global cache


# ---------------------------------------------------------------------------
# real data (optional)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not KTLX.exists(), reason="KTLX example volume not available")
def test_real_ktlx_end_to_end(tmp_path: Path) -> None:
    v = api.open(KTLX)
    info = v.info()
    assert info["radar"] == "KTLX" and info["n_sweeps"] == 17
    assert info["sweeps"][1]["nyquist_estimated_ms"] > 20  # velocity sweep: estimated
    with pytest.raises(
        api.XrdArgumentError, match=r"Sweeps that have it: 0, 2.*-s 0"
    ):  # names the fix
        v.field("ZDR", 1)
    pytest.importorskip("radarx")
    res = v.run("radarx_hid", 0)
    assert res.fields and res.scalars["Classified gates"] > 1000
    assert res.plot() is not None
    allres = v.run("radarx_hid", all_sweeps=True)
    assert len(allres.derived) == 1 and allres.skipped  # Doppler cuts have no ZDR
    pipe = v.pipeline("Doppler velocity", target="VRADH", all_sweeps=True)
    assert pipe.sweeps and pipe.steps["removed"].sum() > 0
    out = v.export("netcdf", tmp_path / "ktlx.nc", include_derived=True)
    assert "radarx_hid__hydrometeor_class" in api.open(out).dataset(0)
    assert v.cells(0, threshold_dbz=40).to_dataframe()["max_dbz"].max() > 60


@pytest.mark.skipif(not MAYFIELD.is_dir(), reason="Mayfield volumes not available")
def test_real_mayfield_mosaic_and_winds(tmp_path: Path) -> None:
    pytest.importorskip("radarx")
    files = [_mayfield("KHPX"), _mayfield("KPAH")]
    mos = api.mosaic(files, product="composite", out=tmp_path / "m.nc")
    assert {r["name"] for r in mos.radars} == {"KHPX", "KPAH"}
    w = api.winds(files, method="analytic", extent=60, dz_m=1000, z_min_m=500, z_max_m=5000)
    assert isinstance(w, api.Winds) and set(w.fields) >= {"u", "v", "w", "speed"}
    w.save([tmp_path / "w.nc", tmp_path / "w.png"], level_km=2.5)
    assert (tmp_path / "w.nc").exists() and (tmp_path / "w.png").exists()
    assert "u" in w.to_dataset()
    assert os.path.getsize(tmp_path / "w.png") > 1000


# ---------------------------------------------------------------------------
# packaging
# ---------------------------------------------------------------------------


def test_xrd_alias_package_reexports_the_api() -> None:
    import xrd

    assert xrd.open is api.open
    assert xrd.mosaic is api.mosaic
    assert xrd.Volume is api.Volume
    assert xrd.api is api


# ---------------------------------------------------------------------------
# sensible defaults, helpful errors, one-liners
# ---------------------------------------------------------------------------


def split_cut_volume() -> api.Volume:
    """Velocity only on sweeps 1 and 2, like the Doppler cuts of a NEXRAD volume."""
    tree = make_synthetic_datatree(n_azimuth=90, n_range=120)
    node = tree["sweep_0"]
    node.dataset = node.to_dataset().drop_vars("VRADH")
    return api.Volume.from_tree(tree)


def test_default_sweep_is_where_the_data_is() -> None:
    v = split_cut_volume()
    assert v.sweeps_with(["DBZH"]) == [0, 1, 2]
    assert v.sweeps_with(["velocity"]) == [1, 2]
    assert v.default_sweep(["velocity"]) == 1
    assert v.default_sweep(["DBZH", "VRADH"]) == 1
    assert v.default_sweep([]) == 0 and v.default_sweep(["nonexistent"]) == 0
    assert v.cells(variable="velocity", threshold_dbz=-100, min_size_km2=0.1).sweep == 1
    fig = v.plot("velocity")  # sweep chosen for you
    assert "Sweep 1" in fig.axes[0].get_title()


def test_missing_field_error_names_the_fix() -> None:
    v = split_cut_volume()
    with pytest.raises(api.XrdArgumentError) as info:
        v.field("velocity", 0)
    text = str(info.value)
    assert "Sweeps that have it: 1, 2" in text and "sweep=1" in text and "-s 1" in text
    tree = make_synthetic_datatree(n_azimuth=30, n_range=20)
    for name in tree.children:
        tree[name].dataset = tree[name].to_dataset().drop_vars("DBZH")
    with pytest.raises(api.XrdArgumentError, match="No single sweep of this volume has all"):
        api.Volume.from_tree(tree).run("storm_detection")


def test_run_without_a_sweep_picks_the_lowest_suitable_one(monkeypatch) -> None:
    v = split_cut_volume()
    spec = api.find_module("qc_clean")
    monkeypatch.setattr(spec, "requires", ["VRADH"])  # a module that needs velocity
    res = v.run("qc_clean", use_snr=False)
    assert res.sweep == 1
    with pytest.raises(api.XrdArgumentError, match=r"needs VRADH, but sweep 0 has no VRADH.*-s 1"):
        v.run("qc_clean", 0, use_snr=False)


def test_error_classes_are_distinct_and_compatible(tmp_path: Path) -> None:
    assert issubclass(api.XrdInputError, api.XrdArgumentError)
    assert issubclass(api.XrdDependencyError, api.XrdError)
    assert not issubclass(api.XrdDependencyError, api.XrdArgumentError)
    with pytest.raises(api.XrdInputError):
        api.open(tmp_path / "nothing.nc")
    with pytest.raises(api.XrdInputError, match="directory"):
        api.open(tmp_path)
    assert api.XrdDependencyError in {type(e) for e in [api.XrdDependencyError("x")]}


def test_module_missing_package_is_a_dependency_error(vol: api.Volume, monkeypatch) -> None:
    spec = api.find_module("storm_detection")
    monkeypatch.setattr(type(spec), "missing_packages", lambda self: ["radarx"])
    with pytest.raises(api.XrdDependencyError, match="pip install -U radarx"):
        vol.run("storm_detection")


def test_one_liners_open_the_file_for_you(nc_file: Path, vol: api.Volume, tmp_path: Path) -> None:
    assert api.info(nc_file)["n_sweeps"] == 3
    fig = api.plot(nc_file, "DBZH", out=tmp_path / "p.png")
    assert fig.axes and (tmp_path / "p.png").exists()
    res = api.run("storm_detection", nc_file, threshold_dbz=30)
    assert res.scalars["Threshold"] == 30.0
    cells = api.cells(nc_file, threshold_dbz=30, min_size_km2=1, backend="numpy")
    assert len(cells) >= 0
    pipe = api.pipeline("Reflectivity QC", vol, sweep=1)  # a Volume works as well as a path
    assert pipe.sweeps == [1]
    out = api.export(vol, out=tmp_path / "e.zarr")
    assert out.is_dir()
    csv = api.export(nc_file, "csv", tmp_path / "e.csv", variable="DBZH")
    assert csv.exists()


def test_help_of_the_package_is_a_guide() -> None:
    import pydoc

    text = pydoc.render_doc(api, renderer=pydoc.plaintext)
    for word in ("Look", "Clean", "Analyse", "Combine", "Save", "sweep=None", "xrd mosaic"):
        assert word in text
    for name in ("open", "info", "plot", "run", "pipeline", "mosaic", "winds", "export", "cells"):
        assert name in api.__all__

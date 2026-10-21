"""Autopilot rough edges found while writing the bulk-data tutorial (docs/AUTOPILOT.md).

* a case folder made of symlinks expands like a real one, without looping;
* the automatic memory budget says where it came from (60 % of RAM or 90 % of what is available);
* a cancelled run keeps (and lists) what was written before the cancel;
* a gridded product (mosaic NetCDF) is refused by the radar opener with a clear reason.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from engine.autopilot import govern
from engine.autopilot import plan as plan_mod
from engine.autopilot import run as run_mod
from engine.autopilot.inspect import expand_inputs

GB = 1e9


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"radar")
    return p


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlinked_case_folders_are_followed_once(tmp_path):
    archive = tmp_path / "archive"
    _touch(archive / "KHPX" / "KHPX20211211_015357_V06")
    _touch(archive / "KNQA" / "KNQA20211211_015018_V06")
    case = tmp_path / "case"
    case.mkdir()
    (case / "KHPX").symlink_to(archive / "KHPX", target_is_directory=True)
    (case / "KNQA").symlink_to(archive / "KNQA", target_is_directory=True)
    (case / "again").symlink_to(archive / "KHPX", target_is_directory=True)  # same folder twice
    (archive / "KHPX" / "loop").symlink_to(case, target_is_directory=True)  # a link loop
    _touch(case / "KPAH20211211_015335_V06")

    names = sorted(p.name for p in expand_inputs([str(case)]))
    assert names == [
        "KHPX20211211_015357_V06",
        "KNQA20211211_015018_V06",
        "KPAH20211211_015335_V06",
    ]


def test_expand_inputs_still_skips_side_files_and_keeps_zarr_whole(tmp_path):
    _touch(tmp_path / "a" / "KHPX_V06")
    _touch(tmp_path / "a" / "notes.txt")
    _touch(tmp_path / "a" / ".DS_Store")
    _touch(tmp_path / "vol.zarr" / ".zgroup")
    got = sorted(p.name for p in expand_inputs([str(tmp_path)]))
    assert got == ["KHPX_V06", "vol.zarr"]


def test_budget_is_the_smaller_of_ram_share_and_available_share():
    mach = {"ram_bytes": 17.18 * GB, "available_bytes": 3.0 * GB}
    why = govern.budget_explained(None, mach)
    assert why["source"] == "available"
    assert why["bytes"] == int(3.0 * GB * 0.9)
    assert why["ram_share_bytes"] == int(17.18 * GB * 0.6)
    roomy = govern.budget_explained(None, {"ram_bytes": 16 * GB, "available_bytes": 15 * GB})
    assert roomy["source"] == "ram" and roomy["bytes"] == int(16 * GB * 0.6)
    no_psutil = govern.budget_explained(None, {"ram_bytes": 16 * GB, "available_bytes": None})
    assert no_psutil["source"] == "ram"
    given = govern.budget_explained(4, mach)
    assert given["source"] == "given" and given["bytes"] == 4 * 1024**3
    tiny = govern.budget_explained(None, {"ram_bytes": 16 * GB, "available_bytes": 0.1 * GB})
    assert tiny["source"] == "minimum" and tiny["bytes"] == govern.MIN_BUDGET
    assert govern.budget_bytes(None, mach) == why["bytes"]


def test_budget_line_shows_the_numbers():
    g = {
        "machine": {"ram_bytes": 17.18 * GB, "available_bytes": 3.0 * GB},
        "budget_bytes": 2.7 * GB,
        "budget_source": "available",
    }
    assert plan_mod.budget_line(g).startswith("RAM 17.2 GB, available 3.0 GB -> budget 2.7 GB (")


def _plan_with(steps):
    return {
        "situation": {"label": "test", "evidence": []},
        "steps": steps,
        "refusals": [],
        "rules": [],
        "governor": {"reductions": []},
    }


def _plot_step(sid, inputs):
    return {
        "id": sid,
        "kind": "plot",
        "title": f"Plot {sid}",
        "why": "test",
        "provenance": "OBSERVED",
        "rxd_command": "rxd plot",
        "inputs": inputs,
        "enabled": True,
        "reductions": [],
        "estimate": {},
    }


def test_cancel_keeps_and_lists_what_was_written(tmp_path, monkeypatch):
    from engine import api

    calls = []

    def fake_plot(p, out):
        Path(out).write_bytes(b"png")
        calls.append(p)

    monkeypatch.setattr(api, "plot", fake_plot)
    inv = {"n_files": 3, "files": [{"path": f"/d/f{i}"} for i in range(3)], "radars": {}}
    pl = _plan_with([_plot_step("a", ["/d/f0", "/d/f1", "/d/f2"]), _plot_step("b", ["/d/f0"])])
    rec = run_mod.run_plan(
        inv, pl, out_dir=tmp_path, cancel=lambda: len(calls) >= 2
    )  # cancel after two volumes of step a
    a, b = rec["steps"]
    assert a["status"] == "cancelled"
    assert [Path(f).name for f in a["files"]] == ["rhi_RADAR_f0.png", "rhi_RADAR_f1.png"]
    assert all(Path(f).is_file() for f in a["files"])
    assert b["status"] == "not run (cancelled)"
    on_disk = json.loads((tmp_path / "run_record.json").read_text())
    assert on_disk["steps"][0]["status"] == "cancelled"
    assert "rhi_RADAR_f1.png" in (tmp_path / "SUMMARY.md").read_text()

    # Run again resumes: a finished step is skipped, the cancelled one runs whole
    calls.clear()
    pl2 = _plan_with([_plot_step("b", ["/d/f0"])])
    run_mod.run_plan(inv, pl2, out_dir=tmp_path)
    again = run_mod.run_plan(inv, _plan_with(pl["steps"]), out_dir=tmp_path)
    assert [s["status"] for s in again["steps"]] == ["done", "skipped (done earlier)"]


def test_mosaic_steps_get_distinct_names(tmp_path, monkeypatch):
    """KAAA has one volume near both KBBB times: two mosaics, two files (none overwritten)."""
    from engine import api

    def fake_mosaic(paths, out, plot, progress=None):
        Path(out).write_bytes(b"nc")
        Path(plot).write_bytes(b"png")

    monkeypatch.setattr(api, "mosaic", fake_mosaic)
    files = [
        {"path": "/d/a0", "radar": "KAAA", "time": "2021-12-11T01:53:57"},
        {"path": "/d/b0", "radar": "KBBB", "time": "2021-12-11T01:50:44"},
        {"path": "/d/b1", "radar": "KBBB", "time": "2021-12-11T01:55:22"},
    ]
    inv = {"n_files": 3, "files": files, "radars": {}}
    step = _plot_step("mosaic", ["/d/a0", "/d/b0", "/d/b1"]) | {"kind": "mosaic"}
    rec = run_mod.run_plan(inv, _plan_with([step]), out_dir=tmp_path)
    names = [Path(f).name for f in rec["steps"][0]["files"]]
    assert len(names) == len(set(names)) == 4
    assert "mosaic_20211211T015044.nc" in names and "mosaic_20211211T015522.nc" in names


def test_gallery_marks_only_radar_volumes_for_the_viewer(monkeypatch, tmp_path):
    from engine.autopilot import ws

    record = {
        "steps": [
            {"id": "qc", "files": [str(tmp_path / "KHPX_qc.nc")]},
            {"id": "mosaic", "files": [str(tmp_path / "mosaic_0156.nc")]},
        ]
    }
    monkeypatch.setattr(ws, "run_plan", lambda *a, **k: record)
    pl = {"steps": [{"id": "qc", "kind": "qc"}, {"id": "mosaic", "kind": "mosaic"}]}
    out = ws.run(
        {"inventory": {}, "plan": pl, "out_dir": str(tmp_path)}, lambda *a: None, lambda: False
    )
    viewer = {g["name"]: g["viewer"] for g in out["gallery"]}
    assert viewer == {"KHPX_qc.nc": True, "mosaic_0156.nc": False}


def test_a_mosaic_grid_is_not_opened_as_a_radar(tmp_path):
    from engine.reader import RadarReader

    y, x = np.arange(20.0), np.arange(30.0)
    ds = xr.Dataset(
        {
            "DBZH_composite": (("y", "x"), np.random.default_rng(0).random((20, 30)) * 50),
            "lat": (("y", "x"), np.zeros((20, 30))),
            "lon": (("y", "x"), np.zeros((20, 30))),
        },
        coords={"y": y, "x": x},
        attrs={"title": "Multi-radar mosaic: composite of DBZH", "Conventions": "CF-1.10"},
    )
    path = tmp_path / "mosaic_0156.nc"
    ds.to_netcdf(path)
    with pytest.raises(ValueError, match="gridded product on a map grid"):
        RadarReader().open_file(str(path))


def _write_in_thread(path: Path, guarded: bool) -> None:
    import threading

    from engine import hdf5_guard

    def work() -> None:
        ds = xr.Dataset({"a": ("x", np.arange(3.0))})
        if guarded:
            with hdf5_guard.new_file(path):
                ds.to_netcdf(path, engine="netcdf4", format="NETCDF4")
        else:
            ds.to_netcdf(path, engine="netcdf4", format="NETCDF4")

    t = threading.Thread(target=work)
    t.start()
    t.join()


def test_new_netcdf_from_a_worker_thread_prints_no_hdf5_error(tmp_path, capfd):
    """netCDF-C probes the target before creating it; a missing file made HDF5 print a stack."""
    pytest.importorskip("netCDF4")
    xr.Dataset({"a": ("x", np.arange(3.0))}).to_netcdf(tmp_path / "init.nc", engine="netcdf4")
    capfd.readouterr()
    _write_in_thread(tmp_path / "new.nc", guarded=True)
    assert "H5F__is_hdf5" not in capfd.readouterr().err
    with xr.open_dataset(tmp_path / "new.nc") as ds:
        assert ds["a"].values.tolist() == [0.0, 1.0, 2.0]


def test_new_file_removes_the_placeholder_when_the_write_fails(tmp_path):
    from engine import hdf5_guard

    target = tmp_path / "out.nc"
    with pytest.raises(RuntimeError), hdf5_guard.new_file(target):
        raise RuntimeError("writer failed")
    assert not target.exists()

"""Exports into a bad place or that fail half way leave nothing behind and keep old files."""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
from pathlib import Path

import pandas as pd
import pytest
import xarray as xr

import server
from engine.exporter import RadarExporter, _output_target
from tests.conftest import make_synthetic_datatree
from tests.test_server import _make_handler, _reader_with_tree

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


@pytest.fixture()
def tree() -> xr.DataTree:
    return make_synthetic_datatree(n_azimuth=24, n_range=30)


def _listing(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


class TestOutputTarget:
    def test_success_moves_the_finished_file_into_place(self, tmp_path: Path) -> None:
        dest = tmp_path / "sub" / "out.csv"
        with _output_target(str(dest), suffix=".csv", prefix="x_") as target:
            assert str(target) != str(dest)  # written beside it, not in it
            assert Path(target).parent == dest.parent
            assert str(target).endswith("out.csv")  # name-based format guesses still work
            Path(target).write_text("done")
        assert dest.read_text() == "done"
        assert _listing(dest.parent) == ["out.csv"]
        assert target.final == str(dest)

    def test_failure_keeps_the_old_file_and_leaves_no_temp(self, tmp_path: Path) -> None:
        dest = tmp_path / "out.csv"
        dest.write_text("previous good export")
        with pytest.raises(OSError), _output_target(str(dest), suffix=".csv", prefix="x_") as t:
            Path(t).write_text("half")
            raise OSError(errno.ENOSPC, "No space left on device")
        assert dest.read_text() == "previous good export"
        assert _listing(tmp_path) == ["out.csv"]

    def test_failure_without_an_old_file_leaves_nothing(self, tmp_path: Path) -> None:
        dest = tmp_path / "out.png"
        with (
            pytest.raises(RuntimeError),
            _output_target(str(dest), suffix=".png", prefix="x_") as t,
        ):
            Path(t).write_bytes(b"partial")
            raise RuntimeError("boom")
        assert _listing(tmp_path) == []

    def test_cancellation_is_cleaned_up_like_any_failure(self, tmp_path: Path) -> None:
        dest = tmp_path / "out.nc"
        with (
            pytest.raises(KeyboardInterrupt),
            _output_target(str(dest), suffix=".nc", prefix="x") as t,
        ):
            Path(t).write_bytes(b"partial")
            raise KeyboardInterrupt
        assert _listing(tmp_path) == []

    def test_directory_target_replaces_an_existing_folder_atomically(self, tmp_path: Path) -> None:
        dest = tmp_path / "out.zarr"
        dest.mkdir()
        (dest / "old").write_text("old")
        with _output_target(str(dest), suffix=".zarr", prefix="x", directory=True) as t:
            Path(t).mkdir()
            (Path(t) / "new").write_text("new")
        assert _listing(dest) == ["new"]
        assert _listing(tmp_path) == ["out.zarr"]

    def test_failed_directory_export_restores_nothing_lost(self, tmp_path: Path) -> None:
        dest = tmp_path / "out.zarr"
        dest.mkdir()
        (dest / "old").write_text("old")
        with (
            pytest.raises(RuntimeError),
            _output_target(str(dest), suffix=".zarr", prefix="x", directory=True) as t,
        ):
            Path(t).mkdir()
            raise RuntimeError("boom")
        assert _listing(dest) == ["old"]
        assert _listing(tmp_path) == ["out.zarr"]

    def test_default_location_is_removed_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XRADAR_EXPORT_DIR", str(tmp_path / "exports"))
        with pytest.raises(RuntimeError), _output_target(None, suffix=".png", prefix="x_") as t:
            assert Path(t).exists()
            raise RuntimeError("boom")
        assert _listing(tmp_path / "exports") == []


class TestExporterFailures:
    @pytest.mark.parametrize("fmt", ["png", "svg", "csv", "pdf"])
    def test_disk_full_while_writing(
        self, tree: xr.DataTree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str
    ) -> None:
        dest = tmp_path / f"out.{fmt}"
        dest.write_text("previous good export")

        def disk_full(self, target, *a, **k):
            Path(target).write_bytes(b"partial")
            raise OSError(errno.ENOSPC, "No space left on device", str(target))

        import matplotlib.figure

        monkeypatch.setattr(matplotlib.figure.Figure, "savefig", disk_full)
        monkeypatch.setattr(pd.DataFrame, "to_csv", lambda self, target, **k: disk_full(0, target))
        with pytest.raises(OSError) as exc:
            RadarExporter().export(
                tree, fmt=fmt, output_path=str(dest), variable="DBZH", sweep=0, dpi=72
            )
        assert exc.value.errno == errno.ENOSPC
        assert dest.read_text() == "previous good export"
        assert _listing(tmp_path) == [dest.name]

    def test_netcdf_failure_leaves_nothing(
        self, tree: xr.DataTree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import xradar.io.export.cfradial1 as cf1

        def broken(*, dtree, filename):
            Path(filename).write_bytes(b"partial")
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(cf1, "to_cfradial1", broken)
        with pytest.raises(OSError):
            RadarExporter().export(
                tree, fmt="netcdf", output_path=str(tmp_path / "x.nc"), variable="DBZH", sweep=0
            )
        assert _listing(tmp_path) == []

    def test_batch_export_keeps_going_and_leaves_no_temp(
        self, tree: xr.DataTree, tmp_path: Path
    ) -> None:
        saved = RadarExporter().batch_export(
            tree,
            variables=["DBZH", "NOPE"],
            sweeps=[0],
            fmt="csv",
            output_dir=str(tmp_path / "b"),
        )
        assert [Path(p).name for p in saved] == ["DBZH_sweep00.csv"]
        assert _listing(tmp_path / "b") == ["DBZH_sweep00.csv"]

    def test_second_export_to_the_same_name_replaces_it(
        self, tree: xr.DataTree, tmp_path: Path
    ) -> None:
        dest = tmp_path / "again.csv"
        for _ in range(2):
            RadarExporter().export(tree, fmt="csv", output_path=str(dest), variable="DBZH", sweep=0)
        assert _listing(tmp_path) == ["again.csv"]
        assert dest.stat().st_size > 100


class TestDegenerateFigures:
    def test_rays_without_an_angle_are_left_out(self, tmp_path: Path) -> None:
        tree = make_synthetic_datatree(n_azimuth=24, n_range=30)
        ds = tree["sweep_0"].to_dataset()
        az = ds["azimuth"].values.copy()
        az[2], az[3] = float("nan"), float("inf")
        tree["sweep_0"] = xr.DataTree(ds.assign_coords(azimuth=("azimuth", az)))
        out = RadarExporter().export(
            tree, fmt="png", output_path=str(tmp_path / "x.png"), variable="DBZH", sweep=0, dpi=72
        )
        assert Path(out).stat().st_size > 1000

    def test_sweep_without_any_data_is_a_clear_error(self, tmp_path: Path) -> None:
        tree = make_synthetic_datatree(n_azimuth=24, n_range=30)
        ds = tree["sweep_0"].to_dataset().isel(azimuth=slice(0, 0))
        tree["sweep_0"] = xr.DataTree(ds)
        with pytest.raises(ValueError, match="no DBZH data"):
            RadarExporter().export(
                tree,
                fmt="png",
                output_path=str(tmp_path / "x.png"),
                variable="DBZH",
                sweep=0,
                dpi=72,
            )
        assert _listing(tmp_path) == []


class TestAnimationLimits:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("field", "value", "needle"),
        [
            ("dpi", 1, "between 72 and 600"),
            ("dpi", True, "between 72 and 600"),
            ("frame_duration_ms", 10**30, "between 50 and 60000"),
            ("frame_duration_ms", 70_000, "between 50 and 60000"),
        ],
    )
    async def test_out_of_range_values_are_refused_up_front(
        self, field: str, value, needle: str
    ) -> None:
        server._register_reader(_reader_with_tree())
        try:
            handler, _ = _make_handler()
            await handler.handle_message(
                json.dumps(
                    {"type": "export_animation", "variable": "DBZH", "sweeps": [0], field: value}
                )
            )
        finally:
            server._file_readers.clear()
        errs = [m["message"] for m in handler._sent if m["type"] == "error"]
        assert len(errs) == 1 and needle in errs[0]


class TestExportMessages:
    @pytest.mark.asyncio
    async def test_unwritable_folder_says_permission_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if sys.platform == "win32" or os.geteuid() == 0:
            pytest.skip("POSIX permissions are not enforced here")
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            server._register_reader(_reader_with_tree())
            handler, _ = _make_handler()
            await handler.handle_message(
                json.dumps(
                    {
                        "type": "export",
                        "format": "csv",
                        "variable": "DBZH",
                        "sweep": 0,
                        "path": str(locked / "sub" / "out.csv"),
                    }
                )
            )
        finally:
            locked.chmod(stat.S_IRWXU)
            server._file_readers.clear()
        errs = [m["message"] for m in handler._sent if m["type"] == "error"]
        assert errs and errs[0].startswith("I/O error in export:")
        assert "permission denied" in errs[0] and "allowed to write" in errs[0]
        assert not any(m["type"] == "export_complete" for m in handler._sent)
        assert _listing(locked) == []

    @pytest.mark.asyncio
    async def test_blank_destination_means_the_default_folder_not_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cwd, exports = tmp_path / "cwd", tmp_path / "exports"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("XRADAR_EXPORT_DIR", str(exports))
        server._register_reader(_reader_with_tree())
        try:
            handler, _ = _make_handler()
            for path_key, extra in (
                ("path", {}),
                ("output_dir", {"variables": ["DBZH"], "sweeps": [0]}),
            ):
                kind = "export" if path_key == "path" else "batch_export"
                await handler.handle_message(
                    json.dumps(
                        {
                            "type": kind,
                            "format": "csv",
                            "variable": "DBZH",
                            "sweep": 0,
                            path_key: "  ",
                            **extra,
                        }
                    )
                )
        finally:
            server._file_readers.clear()
        assert not any(m["type"] == "error" for m in handler._sent), handler._sent
        assert _listing(cwd) == []
        assert any(exports.rglob("*.csv"))

    @pytest.mark.asyncio
    async def test_disk_full_message_through_the_server(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def disk_full(self, target, **k):
            Path(target).write_text("partial")
            raise OSError(errno.ENOSPC, "No space left on device", str(target))

        monkeypatch.setattr(pd.DataFrame, "to_csv", disk_full)
        server._register_reader(_reader_with_tree())
        try:
            handler, _ = _make_handler()
            dest = tmp_path / "out.csv"
            await handler.handle_message(
                json.dumps(
                    {
                        "type": "export",
                        "format": "csv",
                        "variable": "DBZH",
                        "sweep": 0,
                        "path": str(dest),
                    }
                )
            )
        finally:
            server._file_readers.clear()
        errs = [m["message"] for m in handler._sent if m["type"] == "error"]
        assert errs and "disk is full" in errs[0] and "Free some space" in errs[0]
        assert _listing(tmp_path) == []

"""The engine must not die in netCDF-C / HDF5 (docs/STABILITY.md, "Native crash in NetCDF files").

The crash is a use-after-free inside the libraries, so these tests run the real code paths in
*child processes*: a segfault fails one test instead of killing pytest.
"""

from __future__ import annotations

import concurrent.futures as cf
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import xarray as xr

from engine import hdf5_guard
from tests.test_rhi import make_rhi_sweep, rhi_tree
from tests.web_helpers import make_netcdf

PYTHON_DIR = Path(__file__).resolve().parents[1]
CHILD = Path(__file__).with_name("native_child.py")

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


@pytest.fixture(scope="module")
def files(tmp_path_factory: pytest.TempPathFactory) -> list[str]:
    """Files with variable-length string variables (what xarray writes for ``str``)."""
    folder = tmp_path_factory.mktemp("nc")
    rhi_a = folder / "rhi_a.nc"
    rhi_b = folder / "rhi_b.nc"
    rhi_tree(make_rhi_sweep(n_rays=60, n_range=300, azimuth=135, echo_top_km=12)).to_netcdf(rhi_a)
    rhi_tree(make_rhi_sweep(n_rays=40, n_range=200, azimuth=45, echo_top_km=9)).to_netcdf(rhi_b)
    volume = make_netcdf(folder / "volume.nc", n_azimuth=30, n_range=60)
    return [str(rhi_a), str(rhi_b), str(volume)]


def _run_child(mode: str, paths: list[str], extra_env: dict[str, str] | None = None):
    env = {**os.environ, **(extra_env or {})}
    return subprocess.run(
        [sys.executable, "-X", "faulthandler", str(CHILD), mode, *paths],
        capture_output=True,
        text=True,
        timeout=240,
        cwd=PYTHON_DIR,
        env=env,
    )


def _assert_survived(result: subprocess.CompletedProcess[str], what: str) -> None:
    assert result.returncode == 0, (
        f"{what}: exit code {result.returncode} (negative = killed by that signal)\n"
        f"{result.stderr[-1500:]}"
    )
    assert "CHILD-OK" in result.stdout, f"{what}: {result.stdout[-300:]} {result.stderr[-800:]}"


class TestNoNativeCrash:
    def test_the_reported_repro_30_times(self, files: list[str]) -> None:
        """Open a small RHI NetCDF through the handler, let the prefetch run, read a sweep."""
        with cf.ThreadPoolExecutor(6) as pool:
            results = list(pool.map(lambda _: _run_child("handler", files[:1]), range(30)))
        crashed = [i for i, r in enumerate(results) if r.returncode != 0]
        assert not crashed, (
            f"{len(crashed)}/30 runs died; first: exit {results[crashed[0]].returncode}\n"
            f"{results[crashed[0]].stderr[-1500:]}"
        )

    def test_overlapping_opens_of_the_same_files(self, files: list[str]) -> None:
        with cf.ThreadPoolExecutor(4) as pool:
            jobs = [pool.submit(_run_child, "serial", files) for _ in range(4)]
            for job in jobs:
                _assert_survived(job.result(), "serial overlapping opens")

    def test_several_threads_on_several_files(self, files: list[str]) -> None:
        with cf.ThreadPoolExecutor(3) as pool:
            jobs = [pool.submit(_run_child, "threads", files) for _ in range(3)]
            for job in jobs:
                _assert_survived(job.result(), "6 threads x 3 files")

    def test_handler_flow_over_all_files(self, files: list[str]) -> None:
        _assert_survived(_run_child("handler", files), "handler flow on 3 files")


class TestGuardMechanics:
    def test_read_only_managers_of_one_file_share_a_handle(self, files: list[str]) -> None:
        assert hdf5_guard.install()
        from xarray.backends.file_manager import FILE_CACHE

        first = xr.open_dataset(files[2], group="sweep_0")
        second = xr.open_dataset(files[2], group="sweep_1")
        try:
            _ = first["DBZH"].values, second["DBZH"].values
            handles = {id(v) for k, v in list(FILE_CACHE._cache.items()) if files[2] in str(k)}
            assert len(handles) == 1  # one file, one handle (two used to be opened)
        finally:
            first.close()
            second.close()

    def test_xarrays_locks_are_ours_and_reentrant(self) -> None:
        hdf5_guard.install()
        from xarray.backends import locks

        assert locks.NETCDFC_LOCK.lock is hdf5_guard.LOCK
        assert locks.HDF5_LOCK.lock is hdf5_guard.LOCK
        # re-entrant: a read may open a file, a garbage collection may close one
        with (
            locks.NETCDFC_LOCK,
            locks.HDF5_LOCK,
            hdf5_guard.native_section(),
            hdf5_guard.native_section(),
        ):
            pass

    def test_install_is_idempotent_and_can_be_switched_off(self, monkeypatch) -> None:
        assert hdf5_guard.install() and hdf5_guard.install()
        monkeypatch.setenv("XRADAR_HDF5_GUARD", "0")
        assert not hdf5_guard.enabled()

    def test_guarded_decorator_takes_the_lock(self) -> None:
        seen = []

        @hdf5_guard.guarded
        def probe() -> bool:
            # RLock has no public "locked"; a non-blocking acquire from another thread fails
            import threading

            out = []
            t = threading.Thread(target=lambda: out.append(hdf5_guard.LOCK.acquire(blocking=False)))
            t.start()
            t.join()
            seen.extend(out)
            return True

        assert probe() and seen == [False]


class TestCrashLog:
    def test_a_native_crash_leaves_a_stack_in_the_crash_log(self, tmp_path: Path) -> None:
        code = textwrap.dedent(
            """
            import sys, faulthandler
            sys.path.insert(0, ".")
            import server
            assert server.enable_crash_log() is not None
            faulthandler._sigsegv()
            """
        )
        env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=PYTHON_DIR,
            env=env,
        )
        assert result.returncode != 0
        log = (tmp_path / ".xradar-desktop" / "engine-crash.log").read_text()
        assert "Fatal Python error: Segmentation fault" in log
        assert "engine " in log and "pid" in log  # which engine process this was
        assert "enable_crash_log" in log or "<string>" in log  # the Python stack

    def test_web_mode_writes_nothing_into_home(self, tmp_path: Path) -> None:
        code = (
            "import sys; sys.path.insert(0, '.'); import server; print(server.enable_crash_log())"
        )
        env = {
            **os.environ,
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "XRADAR_WEB": "1",
        }
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=PYTHON_DIR,
            env=env,
        )
        assert result.returncode == 0, result.stderr[-500:]
        assert result.stdout.strip().endswith("None")
        assert not (tmp_path / ".xradar-desktop").exists()

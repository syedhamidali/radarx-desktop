"""Real cancel for modules and exports, the memory guard, the cloud time limit and the watchdog.

Each test reproduces an item from the "What remains" list of docs/STABILITY.md (issue #98).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest

import server
from engine import plugins, watchdog
from engine.jobs import JobCancelled, JobRegistry, cancel_on_progress, module_memory_limit_bytes
from engine.plugins import Result, module
from tests.test_server import _make_handler, _reader_with_tree

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


@pytest.fixture(autouse=True)
def _isolated_state():
    saved = (dict(server._file_readers), server._last_file_id, dict(server._generations))
    server._file_readers.clear()
    server._generations.clear()
    server._last_file_id = None
    yield
    server._file_readers.clear()
    server._file_readers.update(saved[0])
    server._last_file_id = saved[1]
    server._generations.clear()
    server._generations.update(saved[2])


async def _send(handler, msg: dict) -> None:
    await handler.handle_message(json.dumps(msg))


async def _until(predicate, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        await asyncio.sleep(0.01)


class TestJobRegistry:
    def test_cancel_reaches_only_the_kind_and_file_asked_for(self) -> None:
        jobs = JobRegistry()
        a = jobs.start("module", "f1")
        b = jobs.start("module", "f2")
        c = jobs.start("export", "f1")
        assert jobs.cancel("module", "f1") == 1
        assert a.is_set() and not b.is_set() and not c.is_set()
        assert jobs.cancel("module") == 1  # b; a was already asked
        assert b.is_set()
        jobs.finish("module", a)
        assert jobs.running_count("module") == 1

    def test_progress_raises_once_cancelled(self) -> None:
        seen: list[int] = []
        flag = threading.Event()
        progress = cancel_on_progress(lambda pct, msg: seen.append(pct), flag)
        progress(10, "a")
        flag.set()
        with pytest.raises(JobCancelled):
            progress(20, "b")
        assert seen == [10]


class TestCancelModule:
    @pytest.fixture()
    def stubborn(self):
        """A module that never checks the cancel flag (the worst case) and one that does."""
        before = dict(plugins.MODULES)
        release = threading.Event()
        started = threading.Event()
        stopped_early = threading.Event()

        @module(id="stab_stubborn", title="Stubborn", category="Test")
        def stab_stubborn(ctx):
            started.set()
            release.wait(30)
            return Result()

        @module(id="stab_polite", title="Polite", category="Test")
        def stab_polite(ctx):
            started.set()
            for i in range(600):
                ctx.progress(i // 6, "working")  # raises ModuleCancelled once cancelled
                time.sleep(0.02)
            return Result()

        @module(id="stab_polite_flag", title="Polite flag", category="Test")
        def stab_polite_flag(ctx):
            started.set()
            while not ctx.cancelled:
                time.sleep(0.01)
            stopped_early.set()
            return Result()

        yield release, started, stopped_early
        release.set()
        plugins.MODULES.clear()
        plugins.MODULES.update(before)

    @pytest.mark.asyncio
    async def test_cancel_ends_a_module_that_ignores_the_flag(self, stubborn) -> None:
        release, started, _ = stubborn
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        run = asyncio.create_task(_send(handler, {"type": "run_module", "module": "stab_stubborn"}))
        await _until(started.is_set)
        t0 = time.monotonic()
        await _send(handler, {"type": "cancel_module"})
        await asyncio.wait_for(run, 10)
        assert time.monotonic() - t0 < 5  # not the 300 s timeout
        ack = next(m for m in handler._sent if m["type"] == "cancel_requested")
        assert ack == {"type": "cancel_requested", "kind": "module", "count": 1}
        err = next(m for m in handler._sent if m["type"] == "module_error")
        assert err["cancelled"] is True and err["message"] == "Cancelled"
        release.set()

    @pytest.mark.asyncio
    async def test_cancel_stops_a_module_that_reports_progress(self, stubborn) -> None:
        _, started, _ = stubborn
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        run = asyncio.create_task(_send(handler, {"type": "run_module", "module": "stab_polite"}))
        await _until(started.is_set)
        await _send(handler, {"type": "cancel_module"})
        await asyncio.wait_for(run, 10)
        assert any(m["type"] == "module_error" and m.get("cancelled") for m in handler._sent)
        assert not any(m["type"] == "module_result" for m in handler._sent)

    @pytest.mark.asyncio
    async def test_the_module_thread_really_stops_computing(self, stubborn) -> None:
        _, started, stopped_early = stubborn
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        run = asyncio.create_task(
            _send(handler, {"type": "run_module", "module": "stab_polite_flag"})
        )
        await _until(started.is_set)
        await _send(handler, {"type": "cancel_module"})
        await asyncio.wait_for(run, 10)
        assert stopped_early.wait(5)  # the module saw ctx.cancelled and returned

    @pytest.mark.asyncio
    async def test_cancel_with_nothing_running_is_harmless(self) -> None:
        handler, _ = _make_handler()
        await _send(handler, {"type": "cancel_module"})
        await _send(handler, {"type": "cancel_export", "file_id": "nope"})
        counts = [m["count"] for m in handler._sent if m["type"] == "cancel_requested"]
        assert counts == [0, 0]

    @pytest.mark.asyncio
    async def test_cancel_of_another_file_leaves_the_run_alone(self, stubborn) -> None:
        release, started, _ = stubborn
        fid = server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        run = asyncio.create_task(
            _send(handler, {"type": "run_module", "module": "stab_stubborn", "file_id": fid})
        )
        await _until(started.is_set)
        await _send(handler, {"type": "cancel_module", "file_id": "some-other-file"})
        await asyncio.sleep(0.3)
        assert not run.done()
        release.set()
        await asyncio.wait_for(run, 10)
        assert any(m["type"] == "module_result" for m in handler._sent)


class TestModuleMemoryGuard:
    @pytest.fixture()
    def hungry(self):
        before = dict(plugins.MODULES)
        started = threading.Event()
        hoard: list[np.ndarray] = []

        @module(id="stab_hungry", title="Hungry", category="Test")
        def stab_hungry(ctx):
            started.set()
            for _ in range(400):  # up to ~800 MB if nothing stops it; the guard fires at +60 MB
                ctx.check_cancelled()
                hoard.append(np.ones(5_000_000, dtype=np.float32) * 1.5)  # touched, 20 MB
                time.sleep(0.05)
            return Result()

        yield started, hoard
        hoard.clear()
        plugins.MODULES.clear()
        plugins.MODULES.update(before)

    @pytest.mark.asyncio
    async def test_runaway_module_is_stopped_with_a_plain_message(
        self, hungry, monkeypatch
    ) -> None:
        from engine.governor import current_rss

        base = current_rss()
        assert base
        monkeypatch.setenv("XRADAR_MODULE_MAX_MB", str(int(base / 1048576) + 60))
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        t0 = time.monotonic()
        await asyncio.wait_for(_send(handler, {"type": "run_module", "module": "stab_hungry"}), 60)
        err = next(m for m in handler._sent if m["type"] == "module_error")
        assert "too much memory" in err["message"]
        assert "XRADAR_MODULE_MAX_MB" in err["message"]
        assert time.monotonic() - t0 < 30
        # the cooperative module stopped allocating after the guard fired: wait for the length to
        # stop changing rather than a fixed sleep (check_cancelled() is only re-read at the top of
        # the next loop iteration, so one more append can land shortly after the error is sent,
        # more so on a loaded CI runner)
        _, hoard = hungry
        size = len(hoard)
        for _ in range(50):  # up to ~5 s
            await asyncio.sleep(0.1)
            if len(hoard) == size:
                break
            size = len(hoard)
        assert len(hoard) == size < 400

    def test_limit_settings(self, monkeypatch) -> None:
        monkeypatch.setenv("XRADAR_MODULE_MAX_MB", "512")
        assert module_memory_limit_bytes() == 512 * 1048576
        monkeypatch.setenv("XRADAR_MODULE_MAX_MB", "0")
        assert module_memory_limit_bytes() is None
        monkeypatch.setenv("XRADAR_MODULE_MAX_MB", "garbage")
        default = module_memory_limit_bytes()
        monkeypatch.delenv("XRADAR_MODULE_MAX_MB")
        assert default == module_memory_limit_bytes()  # an unparsable value keeps the default


class TestCancelExport:
    @pytest.fixture()
    def slow_export(self, monkeypatch):
        """An export that reports progress every 20 ms for ~6 s, like a long figure or batch."""
        started = threading.Event()

        def export(self, tree, *, progress, output_path=None, **kw):
            started.set()
            for i in range(300):
                progress(min(i // 3, 99), "working")
                time.sleep(0.02)
            Path(output_path).write_text("done")
            return output_path

        monkeypatch.setattr(server.RadarExporter, "export", export)
        return started

    @pytest.mark.asyncio
    async def test_cancel_stops_an_export_and_frees_the_lock(self, slow_export, tmp_path) -> None:
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        out = tmp_path / "out.csv"
        msg = {"type": "export", "format": "csv", "variable": "DBZH", "path": str(out)}
        run = asyncio.create_task(_send(handler, msg))
        await _until(slow_export.is_set)
        t0 = time.monotonic()
        await _send(handler, {"type": "cancel_export"})
        await asyncio.wait_for(run, 10)
        assert time.monotonic() - t0 < 3
        assert any(m["type"] == "export_cancelled" for m in handler._sent)
        assert not any(m["type"] in ("export_complete", "error") for m in handler._sent)
        assert not out.exists()
        # the worker really ended: the next export is not queued behind a running thread
        assert not server._mutation_lock().locked()

    @pytest.mark.asyncio
    async def test_real_exporter_removes_the_half_written_file_and_keeps_the_old_one(
        self, tmp_path
    ) -> None:
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        out = tmp_path / "keep.png"
        out.write_text("previous good export")

        flag: list[threading.Event] = []
        real_progress = server.ConnectionHandler._progress_callback

        def spy(self, loop):
            inner = real_progress(self, loop)

            def progress(percent, message):
                if percent >= 30 and not flag:  # "Rendering figure": ask for the cancel here
                    flag.append(self._jobs._jobs["export"][0][1])
                    flag[0].set()
                inner(percent, message)

            return progress

        server.ConnectionHandler._progress_callback = spy
        try:
            msg = {"type": "export", "format": "png", "variable": "DBZH", "path": str(out)}
            await asyncio.wait_for(_send(handler, msg), 60)
        finally:
            server.ConnectionHandler._progress_callback = real_progress
        assert any(m["type"] == "export_cancelled" for m in handler._sent)
        assert out.read_text() == "previous good export"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.png"]  # no temp file left

    @pytest.mark.asyncio
    async def test_batch_stops_between_files_and_keeps_the_finished_ones(self, tmp_path) -> None:
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        real_export = server.RadarExporter.export
        done: list[str] = []

        def export(self, tree, **kw):
            path = real_export(self, tree, **kw)
            done.append(path)
            if len(done) == 2:  # the user presses Cancel while the second file was written
                asyncio.run_coroutine_threadsafe(handler._handle_cancel_export({}), loop).result(10)
            return path

        loop = asyncio.get_running_loop()
        server.RadarExporter.export = export
        try:
            await asyncio.wait_for(
                _send(
                    handler,
                    {
                        "type": "batch_export",
                        "variables": ["DBZH"],
                        "sweeps": [0, 1, 2, 3, 4, 5],
                        "format": "csv",
                        "output_dir": str(tmp_path),
                    },
                ),
                60,
            )
        finally:
            server.RadarExporter.export = real_export
        assert any(m["type"] == "export_cancelled" for m in handler._sent)
        assert not any(m["type"] == "batch_export_complete" for m in handler._sent)
        assert len(done) == 2  # stopped, not "failed on the remaining 4 items"
        assert sorted(p.name for p in tmp_path.iterdir()) == sorted(Path(p).name for p in done)


class TestCloudTimeout:
    @pytest.mark.asyncio
    async def test_a_stalled_download_ends_with_a_clear_message(self, monkeypatch) -> None:
        release = threading.Event()
        monkeypatch.setenv("XRADAR_CLOUD_TIMEOUT_S", "0.3")
        monkeypatch.setattr(server.RadarReader, "open_s3_file", lambda self, p: release.wait(20))
        handler, _ = _make_handler()
        t0 = time.monotonic()
        await _send(handler, {"type": "open_nexrad_l2", "path": "unidata-nexrad-level2/x/KTLX"})
        release.set()
        assert time.monotonic() - t0 < 5
        errors = [m["message"] for m in handler._sent if m["type"] == "error"]
        assert len(errors) == 1
        assert "timed out after 0.3 s" in errors[0] and "internet connection" in errors[0]
        assert not any(m["type"] == "file_opened" for m in handler._sent)

    @pytest.mark.asyncio
    async def test_a_stalled_listing_ends_too(self, monkeypatch) -> None:
        release = threading.Event()
        monkeypatch.setenv("XRADAR_CLOUD_TIMEOUT_S", "0.3")
        monkeypatch.setattr(server, "list_nexrad_l2_files", lambda s, d: release.wait(20))
        handler, _ = _make_handler()
        await _send(
            handler, {"type": "list_nexrad_l2_files", "station": "KTLX", "date": "2020-01-01"}
        )
        release.set()
        assert any("timed out after 0.3 s" in m.get("message", "") for m in handler._sent)

    @pytest.mark.asyncio
    async def test_realtime_open_has_the_limit_as_well(self, monkeypatch) -> None:
        release = threading.Event()
        monkeypatch.setenv("XRADAR_CLOUD_TIMEOUT_S", "0.3")
        monkeypatch.setattr(server, "get_latest_nexrad_l2", lambda s: release.wait(20))
        handler, _ = _make_handler()
        await _send(handler, {"type": "open_nexrad_realtime", "station": "KTLX"})
        release.set()
        assert any("timed out" in m.get("message", "") for m in handler._sent)

    def test_setting(self, monkeypatch) -> None:
        monkeypatch.delenv("XRADAR_CLOUD_TIMEOUT_S", raising=False)
        assert server.cloud_timeout_s() == server.DEFAULT_CLOUD_TIMEOUT_S
        monkeypatch.setenv("XRADAR_CLOUD_TIMEOUT_S", "45")
        assert server.cloud_timeout_s() == 45
        monkeypatch.setenv("XRADAR_CLOUD_TIMEOUT_S", "0")
        assert server.cloud_timeout_s() is None
        monkeypatch.setenv("XRADAR_CLOUD_TIMEOUT_S", "nan")
        assert server.cloud_timeout_s() == server.DEFAULT_CLOUD_TIMEOUT_S


class TestWatchdog:
    @pytest.mark.asyncio
    async def test_a_blocked_event_loop_is_detected(self) -> None:
        fired = threading.Event()
        seen: list[float] = []

        def on_stall(seconds: float) -> None:
            seen.append(seconds)
            fired.set()

        guard = watchdog.LoopWatchdog(0.3, on_stall, tick_s=0.05, check_s=0.05).start()
        try:
            await asyncio.sleep(0.2)
            assert not fired.is_set()  # a healthy loop keeps beating
            time.sleep(0.8)  # the bug: something blocks the loop itself
            assert fired.wait(2)
            assert guard.fired and seen[0] > 0.3
        finally:
            guard.stop()

    @pytest.mark.asyncio
    async def test_a_healthy_busy_loop_is_left_alone(self) -> None:
        fired = threading.Event()
        guard = watchdog.LoopWatchdog(0.5, lambda s: fired.set(), tick_s=0.05, check_s=0.05).start()
        try:
            for _ in range(15):
                await asyncio.sleep(0.05)
                await asyncio.get_running_loop().run_in_executor(None, time.sleep, 0.05)
            assert not fired.is_set()
        finally:
            guard.stop()

    def test_the_limit_setting(self, monkeypatch) -> None:
        monkeypatch.delenv("XRADAR_WATCHDOG_S", raising=False)
        assert watchdog.stall_limit_s() == watchdog.DEFAULT_STALL_S
        monkeypatch.setenv("XRADAR_WATCHDOG_S", "0")
        assert watchdog.stall_limit_s() is None
        monkeypatch.setenv("XRADAR_WATCHDOG_S", "12")
        assert watchdog.stall_limit_s() == 12
        monkeypatch.setenv("XRADAR_WATCHDOG_S", "x")
        assert watchdog.stall_limit_s() == watchdog.DEFAULT_STALL_S

    def test_default_is_far_above_the_measured_worst_stall(self) -> None:
        # docs/STABILITY.md: the slowest healthy stall measured was ~2 s (NetCDF export).
        assert watchdog.DEFAULT_STALL_S >= 20 * 2

    @pytest.mark.asyncio
    async def test_the_real_exit_path_runs_in_a_child_process(self, tmp_path) -> None:
        """The engine really ends with exit code 75 and leaves the stacks in the crash log."""
        import subprocess
        import sys

        log = tmp_path / "crash.log"
        code = (
            "import asyncio, time, sys\n"
            "from engine import watchdog\n"
            f"f = open({str(log)!r}, 'a')\n"
            "async def main():\n"
            "    watchdog.LoopWatchdog(0.5, lambda s: watchdog.exit_stalled(s, f),"
            " tick_s=0.05, check_s=0.05).start()\n"
            "    await asyncio.sleep(0.2)\n"
            "    time.sleep(30)  # blocks the loop\n"
            "asyncio.run(main())\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            timeout=60,
        )
        assert proc.returncode == watchdog.EXIT_CODE_STALLED
        assert "time.sleep" in log.read_text() or "main" in log.read_text()

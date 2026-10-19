"""Web mode under stress: sessions that end while work is running, concurrent visitors."""

from __future__ import annotations

import asyncio
import csv
import threading
from pathlib import Path

import pytest

import server
from engine import web
from tests.web_helpers import WebClient, make_netcdf


@pytest.fixture()
def web_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key, value in {
        "XRADAR_WEB": "1",
        "XRADAR_WEB_TMP": str(tmp_path / "webtmp"),
        "XRADAR_MAX_UPLOAD_MB": "5",
        "XRADAR_MAX_SESSION_MB": "20",
        "XRADAR_MAX_SESSIONS": "4",
        "XRADAR_RATE_BURST": "1000",
        "XRADAR_RATE_PER_S": "1000",
    }.items():
        monkeypatch.setenv(key, value)
    web.reload_settings()
    yield tmp_path
    for ctx in list(web.manager().sessions.values()):
        server._end_web_session(ctx)
    monkeypatch.undo()
    web.reload_settings()


def _session_folders(root: Path) -> list[str]:
    folder = root / "webtmp"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


class TestSessionEndsWhileWorking:
    async def test_late_export_does_not_resurrect_the_session_folder(
        self, web_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker thread cannot be stopped: one still exporting when the visitor leaves must
        not leave a folder (with a partial file) behind on the server."""
        client = WebClient()
        fid = await client.upload_and_open(make_netcdf(web_env / "scan.nc"))
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def slow_export(self, tree, *, fmt, output_path, **kw):
            started.set()
            release.wait(20)
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)  # as every exporter does
            Path(output_path).write_bytes(b"late partial data")
            finished.set()
            return output_path

        monkeypatch.setattr(server.RadarExporter, "export", slow_export)
        task = asyncio.create_task(
            client.call({"type": "export", "format": "csv", "file_id": fid, "variable": "DBZH"})
        )
        for _ in range(200):  # wait until the worker thread is inside the export
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()

        task.cancel()  # the connection dropped: its tasks are cancelled ...
        await asyncio.gather(task, return_exceptions=True)
        client.close()  # ... and the session is torn down while the thread still runs
        assert _session_folders(web_env) == []

        release.set()
        assert await asyncio.to_thread(finished.wait, 20)
        for _ in range(200):  # the done-callback removes the folder right after the job ends
            if not _session_folders(web_env):
                break
            await asyncio.sleep(0.01)
        assert _session_folders(web_env) == []

    async def test_closing_with_no_running_jobs_is_immediate(self, web_env: Path) -> None:
        client = WebClient()
        await client.upload_and_open(make_netcdf(web_env / "scan.nc"))
        assert len(_session_folders(web_env)) == 1
        client.close()
        assert _session_folders(web_env) == []
        client.close()  # idempotent


class TestConcurrentVisitors:
    async def test_parallel_sessions_keep_files_exports_and_errors_apart(
        self, web_env: Path
    ) -> None:
        """Two visitors work at the same time with the same file name and the same ids."""
        a, b = WebClient(), WebClient()
        try:
            (web_env / "a").mkdir()
            (web_env / "b").mkdir()
            fa, fb = await asyncio.gather(
                a.upload_and_open(make_netcdf(web_env / "a" / "scan.nc", n_azimuth=12, n_range=20)),
                b.upload_and_open(make_netcdf(web_env / "b" / "scan.nc", n_azimuth=30, n_range=50)),
            )
            exports = await asyncio.gather(
                a.call({"type": "export", "format": "csv", "file_id": fa, "variable": "DBZH"}),
                b.call({"type": "export", "format": "csv", "file_id": fb, "variable": "DBZH"}),
                # A's file id is not valid in B's session, and the failure stays in B
                b.call({"type": "get_sweep_data", "variable": "DBZH", "sweep": 0, "file_id": fa}),
            )
            for client, replies in ((a, exports[0]), (b, exports[1])):
                done = next(m for m in replies if m["type"] == "export_complete")
                path = client.ctx.exports_dir / done["path"]
                with path.open() as fh:
                    rows = sum(1 for _ in csv.reader(fh)) - 1
                client._rows = rows
            assert a._rows != b._rows  # each exported its own volume (12x20 vs 30x50 gates)
            assert any(m["type"] == "error" for m in exports[2])
            assert not any(m["type"] == "sweep_data" for m in exports[2])
            assert not any(m["type"] == "error" for m in exports[0])  # A never saw B's failure
            assert a.ctx.dir != b.ctx.dir
        finally:
            a.close()
            b.close()
        assert _session_folders(web_env) == []

    async def test_many_sessions_open_and_close_leave_nothing(self, web_env: Path) -> None:
        path = make_netcdf(web_env / "scan.nc")
        for _ in range(10):
            client = WebClient()
            fid = await client.upload_and_open(path)
            replies = await client.call(
                {"type": "get_sweep_data", "variable": "DBZH", "sweep": 0, "file_id": fid}
            )
            assert any(m["type"] == "sweep_data" for m in replies)
            client.close()
        assert _session_folders(web_env) == []
        assert not web.manager().sessions
        assert not web.manager().downloads

    async def test_server_paths_never_reach_the_visitor_in_errors(self, web_env: Path) -> None:
        client = WebClient()
        try:
            junk = web_env / "junk.nc"
            junk.write_bytes(b"CDF\x01" + b"\x00" * 200)
            done = await client.upload(junk.read_bytes(), "junk.nc")
            replies = await client.call({"type": "open_file", "path": done["path"]})
            errs = [m["message"] for m in replies if m["type"] == "error"]
            assert errs and errs[0].startswith("open_file failed:")
            home = str(Path.home())
            for text in errs:
                assert str(client.ctx.dir) not in text
                assert str(web_env) not in text
                assert home not in text
                assert "Traceback" not in text
        finally:
            client.close()
